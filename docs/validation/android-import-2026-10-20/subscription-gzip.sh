set -eu
umask 077
python3.12 - <<'REMOTE'
from pathlib import Path
import os,subprocess,json
p=Path('/etc/nginx/conf.d/wayvia-https.conf');original=p.read_text();backup=Path('/root/wayvia-deploy-20261020/wayvia-https.pre-gzip.conf')
if not backup.exists():backup.write_text(original);backup.chmod(0o600)
if 'gzip_types application/yaml application/json;' not in original:
 anchor='        proxy_buffering off;'
 assert original.count(anchor)==1
 updated=original.replace(anchor,'        gzip on;\n        gzip_types application/yaml application/json;\n        gzip_min_length 1024;\n        gzip_comp_level 5;\n        gzip_vary on;\n        gzip_proxied any;\n'+anchor)
 tmp=p.with_suffix('.conf.tmp');tmp.write_text(updated);tmp.chmod(0o644);os.replace(tmp,p)
 check=subprocess.run(['nginx','-t'],capture_output=True,text=True)
 if check.returncode:
  p.write_text(original);p.chmod(0o644);raise RuntimeError(check.stderr)
 subprocess.run(['systemctl','reload','nginx'],check=True)
subprocess.run(['sha256sum','--check','/root/wayvia-deploy-20261020/nginx-pre-wayvia.sha256'],check=True)
print(json.dumps({'subscription_gzip_enabled':True,'scope':'Wayvia subscriptions location only','nginx_reload':'success','existing_vhosts':'hashes_unchanged'}))
REMOTE
