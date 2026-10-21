#!/usr/bin/python3.12
"""Install the validated ACME IP certificate under Wayvia's role lock.
Never starts an inactive data-plane service or clears an agent safety block.
"""
import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path

os.umask(0o077)
cfg = json.loads(Path('/etc/wayvia-acme/deploy.json').read_text())
lineage = Path(cfg['lineage'])
if os.environ.get('RENEWED_LINEAGE', str(lineage)) != str(lineage):
    raise SystemExit('Unexpected certificate lineage')

def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

command(['openssl', 'verify', '-purpose', 'sslserver', '-verify_ip', cfg['ip'],
         '-untrusted', str(lineage / 'chain.pem'), str(lineage / 'cert.pem')])
command(['openssl', 'x509', '-checkend', '86400', '-noout', '-in', str(lineage / 'cert.pem')])
public_cert = command(['openssl', 'x509', '-in', str(lineage / 'cert.pem'), '-pubkey', '-noout'])
public_key = command(['openssl', 'pkey', '-in', str(lineage / 'privkey.pem'), '-pubout'])
if public_cert != public_key:
    raise SystemExit('Certificate and key mismatch')
role = cfg['role']
if role not in {'ingress', 'exit'}:
    raise SystemExit('Unexpected role')
root = Path('/var/lib/wayvia/agent')
with (root / (role + '.lock')).open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    runtime_path = root / (role + '-runtime.json')
    runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
    txid = runtime.get('transaction_id')
    if txid:
        tx = json.loads((root / (txid + '.json')).read_text())
        if tx['state'] not in {'ACTIVE', 'DEGRADED'}:
            raise SystemExit('Certificate deployment waits for the network transaction')
    changed = False
    for source, suffix in [('fullchain.pem', 'crt'), ('privkey.pem', 'key')]:
        target = Path('/etc/wayvia/secrets') / (cfg['reference'] + '.' + suffix)
        if target.is_symlink():
            raise SystemExit('Secret symlinks are forbidden')
        data = (lineage / source).read_bytes()
        if target.exists() and target.read_bytes() == data:
            continue
        fd, temporary = tempfile.mkstemp(prefix='.renew-', dir=target.parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        changed = True
    # The marker makes a failed reload retryable even when the certificate bytes match.
    marker = Path('/var/lib/wayvia-acme/reload-pending')
    if changed:
        marker.write_text('validated certificate installed\n')
    if marker.exists():
        if not runtime.get('blocked_reason'):
            unit = 'wayvia-ingress.service' if role == 'ingress' else 'wayvia-exit.service'
            if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
                command(['systemctl', 'try-restart', unit])
        if cfg.get('nginx') and subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx']).returncode == 0:
            command(['nginx', '-t'])
            command(['systemctl', 'reload', 'nginx'])
        marker.unlink()
    print(json.dumps({'certificate_installed': True, 'changed': changed,
                      'role': role, 'blocked_state_preserved': bool(runtime.get('blocked_reason'))}))
