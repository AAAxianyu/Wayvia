set -eu
systemctl start wayvia-monitor.service
python3.12 - <<'REMOTE'
import subprocess,json,pathlib,platform

def run(args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=20)
 return {'exit':r.returncode,'stdout':r.stdout.strip(),'stderr':r.stderr.strip()}
route=run(['ip','-j','route','show','default'])
before=json.loads(pathlib.Path('/root/wayvia-deploy-20261020/default-route-before-activation.json').read_text())
status=run(['runuser','-u','wayvia','--','/opt/wayvia/.venv/bin/wayvia','--system','status','--json'])
assert status['exit']==0
record={'hostname':platform.node(),'status':json.loads(status['stdout']),'default_route_unchanged':json.loads(route['stdout'])==before,'units':run(['systemctl','list-units','wayvia-*','--all','--no-pager','--plain']),'renew_timer_enabled':run(['systemctl','is-enabled','wayvia-certificate-renew.timer']),'renew_timer_active':run(['systemctl','is-active','wayvia-certificate-renew.timer']),'host_listeners':run(['ss','-lnt']),'ipv4_forward':run(['sysctl','-n','net.ipv4.ip_forward'])}
assert record['default_route_unchanged']
assert all(x['state']=='ACTIVE' and not x.get('blocked_reason') and all(y['status']=='healthy' for y in x['evidence']) for x in record['status']['agent'] if x.get('current'))
connections=run(['ss','-ntp'])['stdout'].splitlines()
record['wayvia_host_connections']=[s for s in connections if any(t in s for t in ['openvpn','sing-box','v2ray'])]
for name in ['wayvia','wayvia_cloud_access','wayvia_ingress','wayvia_exit']:
 r=run(['nft','list','table','inet',name])
 if r['exit']==0:record['nft_'+name]=r
if pathlib.Path('/run/netns/wayvia-egress').exists():
 record['business_routes']=run(['ip','-n','wayvia-egress','route'])
 record['business_connections']=run(['ip','netns','exec','wayvia-egress','ss','-ntp'])
 record['business_guards']=run(['ip','netns','exec','wayvia-egress','nft','list','ruleset'])
 record['existing_apps']=run(['systemctl','is-active','nginx','docker'])
 record['nginx_existing_hashes']=run(['sha256sum','--check','/root/wayvia-deploy-20261020/nginx-pre-wayvia.sha256'])
 assert record['existing_apps']['exit']==0 and record['nginx_existing_hashes']['exit']==0
 record['public_admin_status']=run(['curl','--noproxy','*','-sS','-o','/dev/null','-w','%{http_code}','--max-time','10','--resolve','<ALI_PUBLIC_IP>:9443:127.0.0.1','https://<ALI_PUBLIC_IP>:9443/api/v1/devices'])
 assert record['public_admin_status']['stdout']=='404'
print(json.dumps(record,ensure_ascii=False,indent=2))
REMOTE
