#!/usr/bin/env python3
"""Small standards-based SOCKS5 client for isolated TCP/UDP path assertions."""

import ipaddress
import json
import socket
import struct
import sys
import time

CONNECTIONS = []


def native_connection(host, port):
    """Keep a failed native socket's source before create_connection would close it."""
    family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    stream = socket.socket(family, socket.SOCK_STREAM)
    stream.settimeout(4)
    observation = {"peer": [host, port], "started_at": time.time()}
    try:
        stream.connect((host, port))
    except Exception:
        observation.update(local=stream.getsockname(), finished_at=time.time())
        CONNECTIONS.append(observation)
        stream.close()
        raise
    observation.update(local=stream.getsockname(), finished_at=time.time())
    CONNECTIONS.append(observation)
    return stream


def exact(stream, count):
    result = b""
    while len(result) < count:
        part = stream.recv(count - len(result))
        if not part:
            raise RuntimeError("SOCKS connection closed")
        result += part
    return result


def address(host, port):
    try:
        ip = ipaddress.ip_address(host)
        prefix = b"\x01" if ip.version == 4 else b"\x04"
        return prefix + ip.packed + struct.pack("!H", port)
    except ValueError:
        return b"\x03" + bytes([len(host)]) + host.encode() + struct.pack("!H", port)


def read_address(stream, kind):
    if kind == 1:
        host = socket.inet_ntoa(exact(stream, 4))
    elif kind == 4:
        host = socket.inet_ntop(socket.AF_INET6, exact(stream, 16))
    elif kind == 3:
        host = exact(stream, exact(stream, 1)[0]).decode()
    else:
        raise RuntimeError("unexpected SOCKS address type")
    return host, struct.unpack("!H", exact(stream, 2))[0]


def connect(command, host, port):
    stream = socket.create_connection(("127.0.0.1", 1080), timeout=4)
    stream.sendall(b"\x05\x01\x00")
    if exact(stream, 2) != b"\x05\x00":
        raise RuntimeError("SOCKS authentication rejected")
    stream.sendall(b"\x05" + bytes([command]) + b"\x00" + address(host, port))
    head = exact(stream, 4)
    if head[1]:
        raise RuntimeError(f"SOCKS reply {head[1]}")
    bound = read_address(stream, head[3])
    return stream, bound


def probe(mode, host):
    if mode in ("tcp", "native-tcp"):
        stream, _ = (
            connect(1, host, 8080) if mode == "tcp" else (native_connection(host, 8080), None)
        )
        with stream:
            stream.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            result = b""
            while True:
                part = stream.recv(4096)
                if not part:
                    break
                result += part
        if b"\r\n\r\n" not in result:
            raise RuntimeError("protected connection closed without an HTTP response")
        payload = json.loads(result.split(b"\r\n\r\n", 1)[1])
        if mode == "native-tcp":
            payload["connection"] = CONNECTIONS[-1]
        return payload
    if mode == "native-udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            datagram.settimeout(4)
            datagram.sendto(b"wayvia-native-udp", (host, 9999))
            result, _ = datagram.recvfrom(4096)
            return {"payload": result.decode()}
    if mode in ("udp", "dns"):
        stream, relay = connect(3, "0.0.0.0", 0)
        with stream, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            datagram.settimeout(4)
            if relay[0] in ("0.0.0.0", "::"):
                relay = ("127.0.0.1", relay[1])
            if mode == "dns":
                name = (
                    b"\x08domestic\x03lab\x00" if host == "10.71.0.1" else b"\x07foreign\x03lab\x00"
                )
                data = (
                    b"\x42\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x01\x00\x01"
                )
                port = 53
            else:
                data = b"wayvia-udp"
                port = 9999
            datagram.sendto(b"\x00\x00\x00" + address(host, port) + data, relay)
            result, _ = datagram.recvfrom(4096)
            if mode == "dns":
                return {"answer": socket.inet_ntoa(result[-4:])}
            return {"payload": result[-(len(b"foreign:wayvia-udp")) :].decode()}
    raise RuntimeError("unsupported probe")


if __name__ == "__main__":
    try:
        print(json.dumps(probe(*sys.argv[1:])))
    except Exception as error:
        print(json.dumps({"error": str(error), "connections": CONNECTIONS}))
        sys.exit(1)
