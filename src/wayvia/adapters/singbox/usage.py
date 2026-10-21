"""Read-only, bounded native API adapter for the locked sing-box 1.14.3.

Wire field numbers come from daemon/started_service.proto at tag v1.14.3.
Only attribution/counters are projected out; destinations and addresses are never returned.
"""

import re
import time

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.empty_pb2 import Empty

from wayvia.adapters.base import AdapterError

API_PORT = 19090
API_TAG = "wayvia-usage-api"


def api_config(secret):
    return {
        "type": "api",
        "tag": API_TAG,
        "listen": "127.0.0.1",
        "listen_port": API_PORT,
        "secret": secret,
    }


def _messages():
    # A minimal protobuf projection; protobuf itself handles unknown wire fields.
    schema = descriptor_pb2.FileDescriptorProto(name="wayvia_usage.proto", syntax="proto3")
    fields = {
        "Version": [("version", 1, 9), ("apiVersion", 2, 5)],
        "StartedAt": [("startedAt", 1, 3)],
        "Request": [("interval", 1, 3)],
        "Connection": [
            ("id", 1, 9),
            ("inbound", 2, 9),
            ("user", 10, 9),
            ("createdAt", 12, 3),
            ("closedAt", 13, 3),
            ("uplinkTotal", 16, 3),
            ("downlinkTotal", 17, 3),
        ],
        "Event": [("connection", 3, 11, "Connection")],
        "Events": [("events", 1, 11, "Event", 3), ("reset", 2, 8)],
    }
    for name, definitions in fields.items():
        message = schema.message_type.add(name=name)
        for field in definitions:
            item = message.field.add(name=field[0], number=field[1], type=field[2], label=1)
            if len(field) > 3:
                item.type_name = field[3]
            if len(field) > 4:
                item.label = field[4]
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return {
        name: message_factory.GetMessageClass(pool.FindMessageTypeByName(name)) for name in fields
    }


MESSAGES = _messages()


def snapshot(secret):
    """No caller-selectable URL, method, token, or mutation is accepted by the agent."""
    metadata = (("authorization", "Bearer " + secret),)
    deadline = time.monotonic() + 3
    with grpc.insecure_channel(
        f"127.0.0.1:{API_PORT}",
        options=(
            ("grpc.max_receive_message_length", 4 * 1024 * 1024),
            ("grpc.enable_http_proxy", 0),
        ),
    ) as channel:

        def unary(method, message):
            return channel.unary_unary(
                "/daemon.StartedService/" + method,
                request_serializer=Empty.SerializeToString,
                response_deserializer=MESSAGES[message].FromString,
            )(Empty(), metadata=metadata, timeout=max(0.01, deadline - time.monotonic()))

        try:
            version = unary("GetVersion", "Version")
            if version.version != "1.14.3" or version.apiVersion != 4:
                raise AdapterError(
                    "USAGE_CORE_VERSION", "Usage adapter requires sing-box 1.14.3 API 4"
                )
            before = unary("GetStartedAt", "StartedAt").startedAt
            stream = channel.unary_stream(
                "/daemon.StartedService/SubscribeConnections",
                request_serializer=MESSAGES["Request"].SerializeToString,
                response_deserializer=MESSAGES["Events"].FromString,
            )(
                MESSAGES["Request"](interval=5_000_000_000),
                metadata=metadata,
                timeout=max(0.01, deadline - time.monotonic()),
            )
            try:
                initial = next(stream)
            finally:
                stream.cancel()
            after = unary("GetStartedAt", "StartedAt").startedAt
        except (grpc.RpcError, StopIteration):
            raise AdapterError(
                "USAGE_UNAVAILABLE", "Ingress usage observation unavailable"
            ) from None
    if before <= 0 or before != after or not initial.reset or len(initial.events) > 10000:
        raise AdapterError("USAGE_SNAPSHOT_INVALID", "Ingress usage snapshot is inconsistent")
    connections = {}
    closed_times = []
    for event in initial.events:
        conn = event.connection
        if conn.closedAt:
            closed_times.append(conn.closedAt / 1000)
        if not re.fullmatch(r"[a-f0-9]{32}", conn.user):
            continue  # Internal probe and unowned accounts are not device usage.
        if min(conn.uplinkTotal, conn.downlinkTotal, conn.createdAt, conn.closedAt) < 0:
            raise AdapterError("USAGE_SNAPSHOT_INVALID", "Ingress usage counters are invalid")
        # A connection can close between the core's active and closed snapshots.
        connections[conn.id] = {
            "id": conn.id,
            "device_id": conn.user,
            "created_at": conn.createdAt / 1000,
            "closed_at": conn.closedAt / 1000 if conn.closedAt else None,
            "upload": conn.uplinkTotal,
            "download": conn.downlinkTotal,
        }
    return {
        "generation": str(before),
        "observed_at": time.time(),
        "connections": list(connections.values()),
        "closed_buffer_full": len(closed_times) >= 1000,
        "oldest_closed_at": min(closed_times) if closed_times else None,
    }
