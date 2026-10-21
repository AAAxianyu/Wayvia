import json,os,subprocess,time,ipaddress,hashlib
from pathlib import Path
assert Path('/.dockerenv').exists() and os.environ.get('WAYVIA_LAB_DISPOSABLE')=='1'
def cmd(args,timeout=30,check=True):
 r=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
 if check and r.returncode:raise RuntimeError(json.dumps({'cmd':args[:3],'exit':r.returncode,'stderr':r.stderr[-1000:]}))
 return r
probe='https://'+os.environ['WAYVIA_ENTRY_IP']+':9443/wayvia-probe'
def get(url):return cmd(['curl','--noproxy','*','-fsS','--connect-timeout','8','--max-time','20',url]).stdout.strip()
results={'scope':'disposable Linux TUN client against actual deployed servers; not Android UI','core_version':cmd(['sing-box','version']).stdout.splitlines()[0]}
results['original_profile_sha256']=hashlib.sha256(Path('/handoff/wayvia-android.json').read_bytes()).hexdigest()
results['config_check']=cmd(['sing-box','check','-c','/handoff/wayvia-android.json']).returncode
baseline=get(probe);ipaddress.ip_address(baseline)
results['baseline_direct_ip']=baseline
process=None
try:
 with open('/tmp/core.log','w') as log:
  process=subprocess.Popen(['sing-box','run','-c','/handoff/wayvia-android.json'],stdout=log,stderr=subprocess.STDOUT)
  deadline=time.monotonic()+15
  while time.monotonic()<deadline:
   if process.poll() is not None:raise RuntimeError('core stopped: '+Path('/tmp/core.log').read_text()[-1200:])
   r=cmd(['ip','-j','route','get','1.1.1.1'],check=False)
   routes=json.loads(r.stdout or '[]')
   if routes and routes[0].get('dev')=='wayvia-tun' and routes[0].get('prefsrc')=='172.28.0.1':break
   time.sleep(.1)
  else:raise RuntimeError('TUN readiness timeout')
  results['tun_route']=routes
  own_probe=get(probe)
  results['own_probe_ip']=own_probe;assert own_probe==os.environ['WAYVIA_EXIT_IP']
  results['own_probe_classification']='default_proxy: ingress IP is outside APNIC CN CIDRs; only entry port 8443 is bootstrap direct'
  with open('/tmp/domestic-capture.log','w') as caplog:
   cap=subprocess.Popen(['timeout','12','tcpdump','-nn','-l','-i','eth0','tcp and dst port 443'],stdout=caplog,stderr=subprocess.STDOUT)
   time.sleep(.2)
   domestic=cmd(['curl','--noproxy','*','-fsS','--connect-timeout','5','--max-time','9','-o','/dev/null','-w','%{http_code} %{remote_ip}','https://www.baidu.com'])
   results['domestic_https']=domestic.stdout
   cap.wait(timeout=15)
  results['domestic_physical_capture']=Path('/tmp/domestic-capture.log').read_text()
  domestic_peer=domestic.stdout.split()[1]
  assert domestic.stdout.startswith('200 ') and (domestic_peer+'.443') in results['domestic_physical_capture']
  trace=get('https://1.1.1.1/cdn-cgi/trace')
  trace=dict(line.split('=',1) for line in trace.splitlines() if '=' in line)
  results['foreign_https_ip']=trace.get('ip');results['foreign_tls']=trace.get('tls')
  assert trace.get('ip')==os.environ['WAYVIA_EXIT_IP']
  with open('/tmp/dns-capture.log','w') as caplog:
   cap=subprocess.Popen(['timeout','8','tcpdump','-nn','-l','-i','eth0','udp and dst host 1.1.1.1 and dst port 53'],stdout=caplog,stderr=subprocess.STDOUT)
   time.sleep(.2)
   dns=cmd(['dig','@1.1.1.1','example.net','A','+time=5','+tries=1'])
   results['foreign_dns_A']=dns.stdout
   assert 'status: NOERROR' in dns.stdout and 'ANSWER: 0' not in dns.stdout
   aaaa=cmd(['dig','@1.1.1.1','example.net','AAAA','+time=5','+tries=1'])
   results['foreign_dns_AAAA']=aaaa.stdout
   assert 'status: NOERROR' in aaaa.stdout and 'ANSWER: 0' in aaaa.stdout
   cap.wait(timeout=10)
  captext=Path('/tmp/dns-capture.log').read_text()
  results['physical_foreign_dns_capture']=captext
  assert '0 packets captured' in captext
  bad=cmd(['curl','--noproxy','*','-g','-fsS','--connect-timeout','3','--max-time','5','https://[2606:4700:4700::1111]/cdn-cgi/trace'],check=False)
  results['protected_ipv6_request_exit']=bad.returncode;assert bad.returncode!=0
  results['profile_unchanged']=hashlib.sha256(Path('/handoff/wayvia-android.json').read_bytes()).hexdigest()==results['original_profile_sha256']
  assert results['profile_unchanged']
finally:
 if process is not None:
  process.terminate()
  try:process.wait(timeout=5)
  except subprocess.TimeoutExpired:process.kill();process.wait()
 results['core_log']=Path('/tmp/core.log').read_text() if Path('/tmp/core.log').exists() else ''
 print(json.dumps(results,ensure_ascii=False,indent=2),flush=True)
