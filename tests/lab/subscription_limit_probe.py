"""Exercise nginx in a disposable network namespace with synthetic subscriptions."""

import http.client
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TOKEN = "A" * 64
PROFILE = b"p" * 9_207_741
release = threading.Event()
held = threading.Condition()
held_count = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        global held_count
        parts = urlsplit(self.path)
        if parts.path not in (f"/subscriptions/{TOKEN}/mihomo", f"/subscriptions/{TOKEN}/info"):
            status, body = 401, b"unauthorized"
        elif parts.path.endswith("/info"):
            status, body = 200, b'{"title":"synthetic"}'
        else:
            status, body = 200, PROFILE
        if parts.query == "hold=1":
            with held:
                held_count += 1
                held.notify_all()
            if not release.wait(15):
                raise TimeoutError("test backend hold expired")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.end_headers()
        self.wfile.write(body)


def request(path, source, method="GET", forwarded=None):
    context = ssl.create_default_context(cafile="/tmp/lab-cert.pem")
    connection = http.client.HTTPSConnection(
        "127.0.0.1", 9443, context=context, source_address=(source, 0), timeout=30
    )
    try:
        headers = {"Accept-Encoding": "identity"}
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    valid = f"/subscriptions/{TOKEN}/mihomo"
    info = f"/subscriptions/{TOKEN}/info"
    for _ in range(2):
        status, body, headers = request(valid, "127.0.0.2")
        assert status == 200 and body == PROFILE
        assert "no-store" in headers.get("Cache-Control", "")
        assert request(info, "127.0.0.2")[0] == 200
    assert request("/api/v1/devices", "127.0.0.2")[0] == 404
    assert request("/wayvia-probe", "127.0.0.2")[0] == 200
    assert request(valid, "127.0.0.2", method="POST")[0] == 403

    invalid = "/subscriptions/" + "B" * 64 + "/mihomo"
    replies = [request(invalid, "127.0.0.3", forwarded=f"192.0.2.{i}") for i in range(1, 8)]
    statuses = [response[0] for response in replies]
    assert statuses == [401] * 6 + [429], statuses
    assert ("B" * 64).encode() not in replies[-1][1]
    time.sleep(13)
    assert request(invalid, "127.0.0.3")[0] == 401

    results = []
    workers = [
        threading.Thread(
            target=lambda source=s: results.append(request(valid + "?hold=1", source)[0])
        )
        for s in ("127.0.0.4", "127.0.0.5")
    ]
    for worker in workers:
        worker.start()
    with held:
        assert held.wait_for(lambda: held_count == 2, timeout=10)
    blocked, body, _headers = request(info, "127.0.0.6")
    assert blocked == 429 and TOKEN.encode() not in body
    release.set()
    for worker in workers:
        worker.join(20)
        assert not worker.is_alive()
    assert sorted(results) == [200, 200]
    assert request(info, "127.0.0.6")[0] == 200
    server.shutdown()
    server.server_close()
    print(
        json.dumps(
            {
                "normal_two_devices_and_info": "passed",
                "large_uncompressed_profile_bytes": len(PROFILE),
                "invalid_token_rate_and_recovery": "passed",
                "two_concurrent_global_limit_and_recovery": "passed",
                "admin_and_method_protection": "passed",
                "network": "isolated_container_loopback_only",
            }
        )
    )


if __name__ == "__main__":
    main()
