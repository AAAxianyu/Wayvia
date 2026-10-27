"""Independent native API 4 stream. It never runs inside the serial root agent."""

import ipaddress
import re

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.empty_pb2 import Empty

from wayvia.adapters.singbox.usage import API_PORT
from wayvia.observability.models import LogError, domain, event


def messages():
    schema = descriptor_pb2.FileDescriptorProto(name="wayvia_connections.proto", syntax="proto3")
    definitions = {
        "Version": [("version", 1, 9), ("apiVersion", 2, 5)],
        "StartedAt": [("startedAt", 1, 3)],
        "Request": [("interval", 1, 3)],
        "Connection": [
            ("id", 1, 9),
            ("inbound", 2, 9),
            ("network", 5, 9),
            ("source", 6, 9),
            ("destination", 7, 9),
            ("domain", 8, 9),
            ("protocol", 9, 9),
            ("user", 10, 9),
            ("createdAt", 12, 3),
            ("closedAt", 13, 3),
            ("uplinkTotal", 16, 3),
            ("downlinkTotal", 17, 3),
        ],
        "Event": [
            ("type", 1, 5),
            ("id", 2, 9),
            ("connection", 3, 11, "Connection"),
            ("uplinkDelta", 4, 3),
            ("downlinkDelta", 5, 3),
            ("closedAt", 6, 3),
        ],
        "Events": [("events", 1, 11, "Event", 3), ("reset", 2, 8)],
    }
    for name, fields in definitions.items():
        msg = schema.message_type.add(name=name)
        for field in fields:
            item = msg.field.add(name=field[0], number=field[1], type=field[2], label=1)
            if len(field) > 3:
                item.type_name = field[3]
            if len(field) > 4:
                item.label = field[4]
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return {
        name: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
        for name in definitions
    }


MESSAGES = messages()


def endpoint(value):
    """No URI parsing or inference from IP to host. IPv6 must be bracketed."""
    match = re.fullmatch(r"(?:\[([^\]]+)\]|([^:]+)):(\d{1,5})", value)
    if not match or not 1 <= int(match[3]) <= 65535:
        return {}
    address = match[1] or match[2]
    try:
        return {"ip": str(ipaddress.ip_address(address)), "port": int(match[3])}
    except ValueError:
        try:
            return {"host": domain(address), "port": int(match[3])}
        except (ValueError, UnicodeError):
            return {}


class Projector:
    def __init__(self, host_id, epoch, devices):
        self.host_id, self.epoch, self.devices = host_id, str(epoch), set(devices)
        self.cache = {}
        self.sequence = 0

    def make(self, kind, **fields):
        self.sequence += 1
        return event(
            self.host_id,
            "ingress",
            "connections",
            self.epoch,
            kind,
            sequence=self.sequence,
            **fields,
        )

    def project(self, batch):
        if len(batch.events) > 10000:
            self.cache.clear()
            return [self.make("gap", code="buffer_full")]
        rows = []
        if batch.reset:
            self.cache.clear()
            rows.append(self.make("gap", code="reconnected"))
        for update in batch.events:
            conn = update.connection
            ident = conn.id or update.id
            try:
                if conn.id:
                    if conn.user not in self.devices or conn.inbound != "entry":
                        continue
                    source, target = endpoint(conn.source), endpoint(conn.destination)
                    host = None
                    try:
                        host = domain(conn.domain) if conn.domain else target.get("host")
                    except (ValueError, UnicodeError):
                        pass
                    fields = {
                        "connection_id": ident,
                        "core_epoch": self.epoch,
                        "device_id": conn.user,
                        "identity_source": "credential_only",
                        "source_ip": source.get("ip"),
                        "source_port": source.get("port"),
                        "target_ip": target.get("ip"),
                        "target_host": host,
                        "target_port": target.get("port"),
                        "domain_source": "core" if host else "unknown",
                        "transport": conn.network if conn.network in {"tcp", "udp"} else "unknown",
                        "application_protocol": conn.protocol
                        if conn.protocol in {"http", "tls", "quic", "dns"}
                        else "unknown",
                        "created_at": conn.createdAt / 1000 if conn.createdAt else None,
                        "closed_at": conn.closedAt / 1000 if conn.closedAt else None,
                        "upload_bytes": conn.uplinkTotal,
                        "download_bytes": conn.downlinkTotal,
                        "lifecycle": "closed" if conn.closedAt else "active",
                    }
                    if len(self.cache) >= 10000 and ident not in self.cache:
                        rows.append(self.make("gap", code="buffer_full"))
                        continue
                    self.cache[ident] = fields
                elif ident in self.cache:
                    fields = self.cache[ident].copy()
                    if fields["lifecycle"] == "closed":
                        continue
                    if min(update.uplinkDelta, update.downlinkDelta) < 0:
                        rows.append(self.make("gap", code="counter_reset"))
                        continue
                    fields["upload_bytes"] += update.uplinkDelta
                    fields["download_bytes"] += update.downlinkDelta
                    if update.type == 2:
                        fields.update(lifecycle="closed", closed_at=update.closedAt / 1000 or None)
                    self.cache[ident] = fields
                else:
                    rows.append(self.make("gap", code="missing_start"))
                    continue
                rows.append(self.make("connection", **fields))
                if fields["lifecycle"] == "closed":
                    self.cache.pop(ident, None)
            except (ValueError, TypeError):
                rows.append(self.make("gap", code="parse_failed"))
        return rows


def stream(secret, host_id, devices, emit, enabled, *, seconds=3600):
    """Call in its own process. No routing writes, core mutations or log RPCs."""
    metadata = (("authorization", "Bearer " + secret),)
    with grpc.insecure_channel(
        f"127.0.0.1:{API_PORT}",
        options=(
            ("grpc.max_receive_message_length", 4194304),
            ("grpc.enable_http_proxy", 0),
        ),
    ) as channel:

        def unary(method, kind):
            return channel.unary_unary(
                "/daemon.StartedService/" + method,
                request_serializer=Empty.SerializeToString,
                response_deserializer=MESSAGES[kind].FromString,
            )(Empty(), metadata=metadata, timeout=3)

        try:
            version = unary("GetVersion", "Version")
            if version.version != "1.14.3" or version.apiVersion != 4:
                raise LogError("LOG_CORE_VERSION", status=503)
            epoch = unary("GetStartedAt", "StartedAt").startedAt
            if epoch <= 0:
                raise LogError("LOG_CORE_EPOCH", status=503)
            projector = Projector(host_id, epoch, devices() if callable(devices) else devices)
            updates = channel.unary_stream(
                "/daemon.StartedService/SubscribeConnections",
                request_serializer=MESSAGES["Request"].SerializeToString,
                response_deserializer=MESSAGES["Events"].FromString,
            )(MESSAGES["Request"](interval=5_000_000_000), metadata=metadata, timeout=seconds)
            # A watchdog can cancel a blocked stream when policy is disabled.
            import threading

            stopped = threading.Event()

            def watch():
                while not stopped.wait(5):
                    try:
                        if not enabled() or unary("GetStartedAt", "StartedAt").startedAt != epoch:
                            updates.cancel()
                            return
                        if callable(devices):
                            projector.devices = set(devices())
                        emit(projector.make("heartbeat"))
                    except Exception:
                        updates.cancel()
                        return

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                first = True
                for batch in updates:
                    if first and not batch.reset:
                        raise LogError("LOG_CORE_SNAPSHOT", status=503)
                    first = False
                    if not enabled():
                        break
                    for row in projector.project(batch):
                        emit(row)
            finally:
                updates.cancel()
                stopped.set()
                watcher.join(timeout=4)
        except grpc.RpcError:
            raise LogError("LOG_CORE_UNAVAILABLE", status=503) from None
