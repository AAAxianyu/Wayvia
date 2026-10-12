#!/usr/bin/env python3
"""Controlled HTTP/UDP/DNS target; records observed source addresses."""

import http.server
import json
import socket
import struct
import sys
import threading
from pathlib import Path

identity, address, output = sys.argv[1:]
lock = threading.Lock()


def record(protocol, peer):
    with lock:
        with Path(output).open("a") as stream:
            stream.write(
                json.dumps({"target": identity, "protocol": protocol, "peer": peer}) + "\n"
            )


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        record("tcp", self.client_address[0])
        body = json.dumps({"target": identity, "peer": self.client_address[0]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def udp():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((address, 9999))
    while True:
        packet, peer = server.recvfrom(65535)
        record("udp", peer[0])
        server.sendto(identity.encode() + b":" + packet, peer)


def dns():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((address, 53))
    while True:
        packet, peer = server.recvfrom(4096)
        record("dns", peer[0])
        end = 12
        labels = []
        while packet[end]:
            length = packet[end]
            labels.append(packet[end + 1 : end + 1 + length].decode())
            end += length + 1
        end += 5
        question = packet[12:end]
        response = packet[:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00" + question
        response += (
            b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 30, 4)
            + socket.inet_aton(
                "10.73.0.2"
                if identity == "management" and ".".join(labels) == "carrier.lab"
                else address
            )
        )
        server.sendto(response, peer)


threading.Thread(target=udp, daemon=True).start()
threading.Thread(target=dns, daemon=True).start()
if identity == "domestic":

    class IPv6HTTPServer(http.server.HTTPServer):
        address_family = socket.AF_INET6

    server6 = IPv6HTTPServer(("fd72:1::1", 8080), Handler)
    threading.Thread(target=server6.serve_forever, daemon=True).start()
http.server.HTTPServer((address, 8080), Handler).serve_forever()
