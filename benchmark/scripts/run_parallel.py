#!/usr/bin/env python3
"""Linux/Python3.11 portable launcher. No API call during prepare/check/status."""
import argparse,os,sys,subprocess,json,time
from pathlib import Path
from parallel_runner.common import *

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','check','start','resume','status','stop','_run'])
 p.add_argument('--config',default=str(PACKAGE.parent/'configs/parallel/four-methods.json'));p.add_argument('--run',type=Path);p.add_argument('--foreground',action='store_true');p.add_argument('--clear-user-stop',action='store_true')
 args=p.parse_args();request=None
 if args.run:run=args.run.resolve()
 else:request=load_config(args.config);run=Path(request['output'])
 if args.command=='prepare':
  from parallel_runner.prepare import initialize
  c=initialize(request or load_config(args.config));print(json.dumps(read(run/'READY.json'),ensure_ascii=False));return
 if not (run/'CONFIG.json').exists():raise ValueError('Run prepare first')
 c=read(run/'CONFIG.json');framework(run);setup_env(c,run,'launcher')
 if args.command=='status':
  if (run/'PROGRESS.json').exists():
   v=read(run/'PROGRESS.json');print(json.dumps({k:v[k] for k in ['updated_unix','complete','scored','expected','eta_minutes_scoring_only','provider_limits','provider_slots','ledger_costs','stop_reason','aggregate']},ensure_ascii=False,indent=2))
  else:print(json.dumps(read(run/'READY.json'),ensure_ascii=False))
  return
 if args.command=='stop':
  from parallel_runner.shared_control import SharedControl
  write(run/'STOP.json',{'time':time.time(),'reason':'user_stop'});SharedControl(Path(c['ledger_dir'])/'ledger.sqlite3').stop('user_stop');print('Stop requested; in-flight successes drain and persist.');return
 from parallel_runner.prepare import verify
 c=verify(run)
 if args.command=='check':print('PASS: frozen code, settings, env and prepared inputs verified; no API request.');return
 if sys.platform!='linux':raise ValueError('Execute on Linux or WSL2; Windows can edit/configure the package')
 if args.command=='_run':
  from parallel_runner.supervisor import run_supervisor
  v=run_supervisor(run,c);print(json.dumps({'complete':v['complete'],'scored':v['scored'],'expected':v['expected']}));return
 import fcntl
 lock=(run/'supervisor.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if args.command=='resume' and args.clear_user_stop:
  from parallel_runner.prepare import clear_user_stop
  clear_user_stop(run,c)
 from parallel_runner.shared_control import SharedControl
 SharedControl(Path(c['ledger_dir'])/'ledger.sqlite3').check()
 lock.close()
 cmd=[sys.executable,str(run/'launcher/launch.py'),'_run','--run',str(run)]
 if args.foreground:return subprocess.run(cmd).returncode
 with (run/'supervisor.log').open('ab') as log:child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 print(json.dumps({'pid':child.pid,'run':str(run),'log':str(run/'supervisor.log'),'state':'starting; inspect status/log for initialization failures'},ensure_ascii=False))

if __name__=='__main__':
 try:raise SystemExit(main())
 except (ValueError,FileNotFoundError,BlockingIOError) as e:
  print(type(e).__name__+': '+str(e),file=sys.stderr);raise SystemExit(2)
