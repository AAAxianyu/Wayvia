import json,re,socket,subprocess,sys
from pathlib import Path
from wayvia.adapters.linux import LinuxExecutor
from wayvia.adapters.base import AdapterError
from wayvia.agent.models import Action,Plan
from datetime import datetime,UTC,timedelta

def run():
    executor=LinuxExecutor(config_dir='/tmp/wayvia-cfg',state_dir='/tmp/wayvia-state',unit_dir='/tmp/wayvia-units')
    actions=[Action(kind='namespace',resource='wayvia-egress'),Action(kind='guard',resource='wayvia_guard',namespace='wayvia-egress'),Action(kind='veth',resource='wayvia-host'),Action(kind='guard',resource='wayvia_guard')]
    default_before=subprocess.check_output(['ip','-j','route','show','default'],text=True)
    steps=[]
    try:
        for action in actions:
            before=executor.capture(action);steps.append((action,before));executor.apply(action)
        guard=actions[-1]
        rules_before=executor.inspect_action(guard)['rules']
        baseline_before=executor.baseline([guard])
        try:
            socket.create_connection(('10.89.0.1',1080),timeout=1).close()
            raise AssertionError('guard accepted an unauthorized connection')
        except (TimeoutError,OSError):
            pass
        rules_after=executor.inspect_action(guard)['rules']
        baseline_after=executor.baseline([guard])
        normalize=lambda text:re.sub(r'counter packets \d+ bytes \d+','counter',text)
        assert rules_before!=rules_after
        assert normalize(rules_before)==normalize(rules_after)
        assert baseline_before!=baseline_after
        now=datetime.now(UTC)
        plan=Plan(role='ingress',revision='counter-audit',baseline=baseline_before,actions=(guard,),probes=(),created_at=now,expires_at=now+timedelta(minutes=5)).seal()
        try:
            plan.check(baseline_after)
            raise AssertionError('stale baseline unexpectedly accepted')
        except AdapterError as error:
            assert error.code=='BASELINE_CHANGED'
        result={'status':'confirmed','scope':'disposable Linux actual kernel with unmodified production LinuxExecutor and Plan.check','unauthorized_connection_blocked':True,'rules_structurally_identical':True,'baseline_changed_by_real_counter_increment':True,'rejection':'BASELINE_CHANGED','baseline_before':baseline_before,'baseline_after':baseline_after,'rules_before':rules_before,'rules_after':rules_after}
    finally:
        for action,before in reversed(steps):executor.rollback(action,before)
    assert subprocess.check_output(['ip','-j','route','show','default'],text=True)==default_before
    assert not subprocess.check_output(['ip','netns','list'],text=True).strip()
    result['cleanup']={'owned_resources_removed':True,'container_default_route_unchanged':True}
    return result
try: result=run()
except Exception as error:result={'status':'failed','error':str(error)}
Path('/evidence/kernel-baseline-result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
sys.exit(0 if result['status']=='confirmed' else 1)
