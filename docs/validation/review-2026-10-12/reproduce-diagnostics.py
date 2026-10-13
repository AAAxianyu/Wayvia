"""Read-only audit reproduction; one temporary loopback TCP listener, no TLS/VPN."""
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Probe, LAYERS
from wayvia.agent.probes import verify
from wayvia.agent.validation import validate_probes
from wayvia.services.diagnostics import dataplane_diagnostic

root = Path(__file__).resolve().parents[3]
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    probes = [Probe.model_validate(row) for row in json.loads((root/'examples/ingress-probes.json').read_text())]
    probes[0] = probes[0].model_copy(update={'port': listener.getsockname()[1]})
    validate_probes(SimpleNamespace(role='ingress', probes=probes))
    evidence = verify(LinuxExecutor(enforce_environment=False), [probes[0]])
result = {
    'access_probe': {'server': 'plain local TCP listener, no TLS or Trojan handshake',
                     'probe_validation': 'accepted', 'result': evidence},
    'stale_dataplane': dataplane_diagnostic([{
        'id': 'audit-stale', 'role': 'ingress', 'state': 'ACTIVE',
        'updated_at': '2020-01-01T00:00:00Z',
        'evidence': [{'layer': layer, 'status': 'healthy'} for layer in sorted(LAYERS)],
    }]),
}
print(json.dumps(result, indent=2))
