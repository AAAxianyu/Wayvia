import json,subprocess,datetime,re,sys
start='2026-10-21T08:24:00+00:00';end='2026-10-21T09:13:00+00:00'
patterns={'oom':r'out of memory|oom-kill|killed process','link_down':r'link.*down|lost carrier','link_up':r'gained carrier|link.*up','dhcp_or_address_change':r'dhcp.*(lost|renew|acquir|expir)|address.*(added|remov)|route.*(added|remov)','clock_step':r'time.*(jump|step)|clock.*(jump|step)','virtio_error':r'virtio.*(error|fail|reset|timeout)','watchdog_or_stall':r'watchdog|soft lockup|hard lockup|rcu.*stall|blocked for more than','conntrack_full':r'conntrack.*full','nic_timeout':r'netdev.*watchdog|transmit queue.*timed out'}
rows=[];counts={};exits={}
for category,args in [('kernel',['-k']),('network',['-u','systemd-networkd.service','-u','NetworkManager.service']),('clock',['-u','systemd-timesyncd.service'])]:
 p=subprocess.run(['journalctl','--since',start,'--until',end,'--output=json','--no-pager',*args],capture_output=True,text=True,timeout=15)
 exits[category]=p.returncode;count=0
 for line in p.stdout.splitlines():
  try:r=json.loads(line)
  except json.JSONDecodeError:continue
  count+=1;message=r.get('MESSAGE','')
  if not isinstance(message,str):continue
  matched=[name for name,pattern in patterns.items() if re.search(pattern,message,re.I)]
  if matched:rows.append({'at':datetime.datetime.fromtimestamp(int(r['__REALTIME_TIMESTAMP'])/1e6,datetime.timezone.utc).isoformat(),'source':category,'categories':matched})
 counts[category]=count
print(json.dumps({'host':sys.argv[1],'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'window_utc':[start,end],'journal_exit':exits,'read_record_counts':counts,'classified_events':rows,'limitation':'Only allowlisted categories; absence does not establish upstream health.'}))
