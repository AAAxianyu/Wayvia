#!/usr/bin/python3.12
"""Fresh baseline, plan, apply and immediate confirm for one explicitly selected role."""
import argparse
import json
import subprocess
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('role',choices=['exit','ingress']);p.add_argument('--exit-interface');p.add_argument('--apply',action='store_true');args=p.parse_args()
control=Path('/var/lib/wayvia/control')
def cli(*argv,accepted=(0,)):
    r=subprocess.run(['runuser','-u','wayvia','--','/opt/wayvia/.venv/bin/wayvia','--system',*argv],capture_output=True,text=True,timeout=180)
    if r.returncode not in accepted:
        raise SystemExit(r.stdout or r.stderr or 'Wayvia operation failed')
    return json.loads(r.stdout)
plan=control/(args.role+'-plan.json')
argv=['plan',str(control/'config.yaml'),'--role',args.role,'--baseline','auto','--probes',str(control/'probes.json'),'--output-file',str(plan)]
if args.role=='exit':
    if not args.exit_interface:raise SystemExit('Actual exit interface is required')
    argv+=['--exit-interface',args.exit_interface]
result=cli(*argv);print(json.dumps(result),flush=True)
if args.apply:
    result=cli('apply',str(plan),accepted=(0,6));print(json.dumps(result),flush=True)
    if result.get('state')!='AWAITING_CONFIRM':raise SystemExit('Apply did not reach AWAITING_CONFIRM')
    confirmed=cli('confirm',result['id']);print(json.dumps(confirmed),flush=True)
    if confirmed.get('state')!='ACTIVE':raise SystemExit('Confirm did not reach ACTIVE')
