from .common import *
import collections,concurrent.futures as cf,multiprocessing as mp,sqlite3,signal,subprocess
from .tasks import memory_task,reader_task
from .report import report

def ordered_jobs(jobs):
 """Warm each seed/method, then long histories first to reduce late tails."""
 groups=collections.defaultdict(list)
 for j in jobs:groups[(j['seed'],j['method'])].append(j)
 for key,g in groups.items():
  g.sort(key=lambda j:(j['weight'],j['id']));groups[key]=collections.deque(g[:1]+list(reversed(g[1:])))
 out=[]
 while any(groups.values()):
  for key in sorted(groups):
   if groups[key]:out.append(groups[key].popleft())
 return out

def take_next_job(queue, memory_active, reader_active, config):
 """Respect the method cap across both pools, without head-of-line blocking."""
 cap=config.get('per_method_history_workers')
 active=collections.Counter(j['method'] for j in list(memory_active.values())+list(reader_active.values()))
 for j in queue:
  if cap is None or active[j['method']]<cap:
   queue.remove(j)
   return j
 return None

def governor(c,run):
 if not c.get('adaptive_concurrency',False):return
 dbpath=Path(c['ledger_dir'])/'ledger.sqlite3';now=time.time();p=Path(run)/'GOVERNOR_LAST.json';last=read(p) if p.exists() else {'time':0}
 with sqlite3.connect(dbpath,timeout=30) as db:
  vals=[x[0] for x in db.execute("SELECT status FROM calls WHERE kind='llm' AND started>? AND status IS NOT NULL",(now-300,))]
  config=json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0]);cap=config['providers']['llm']['inflight']
  if cap is None or c['providers']['llm'].get('inflight') is None:return
  if len(vals)<30:return
  fraction=sum(s in {429,500,502,503,504} for s in vals)/len(vals);new=cap
  if fraction>.15 and cap>8 and now-last['time']>300:new=max(8,int(cap*.75))
  elif fraction<.02 and cap<c['providers']['llm']['inflight'] and now-last['time']>900:new=min(c['providers']['llm']['inflight'],cap+8)
  if new==cap:return
  db.execute('BEGIN IMMEDIATE');config=json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0]);config['providers']['llm']['inflight']=new;db.execute("UPDATE settings SET value=? WHERE key='config'",(json.dumps(config,sort_keys=True),))
 event={'time':now,'old':cap,'new':new,'last5min_error_fraction':fraction};write(p,event)
 with (Path(run)/'GOVERNOR.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')

def run_supervisor(run,c):
 import fcntl
 run=Path(run);lock=(run/'supervisor.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 framework(run);setup_env(c,run,'supervisor')
 from .shared_control import SharedControl,ControlStopped
 control=SharedControl(Path(c['ledger_dir'])/'ledger.sqlite3');check(c,run)
 def stop(*_):write(run/'STOP.json',{'time':time.time(),'reason':'user_stop'});control.stop('user_stop')
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 jobs=ordered_jobs(read(run/'JOBS.json'));states=read(run/'STATE.json') if (run/'STATE.json').exists() else {}
 write(run/'CONTROLLER.json',{'pid':os.getpid(),'started':time.time(),'memory_workers':c['memory_workers'],'reader_history_workers':c['reader_history_workers'],'query_workers':c['query_workers']})
 pending_memory=collections.deque();pending_reader=collections.deque();retryq=[]
 for j in jobs:
  out=taskdir(run,j)
  if (out/'COMPLETE.json').exists():continue
  stage='reader' if (out/'MEMORY_COMPLETE.json').exists() else 'memory';key=stage+':'+j['id']
  if states.get(key,{}).get('status')=='failed':continue
  (pending_reader if stage=='reader' else pending_memory).append(j)
 context=mp.get_context('spawn');last_report=last_govern=0;paused=False
 total_limit=pool_size(c.get('total_history_workers'),len(jobs))
 memory_limit=pool_size(c.get('memory_workers'),total_limit);reader_limit=pool_size(c.get('reader_history_workers'),total_limit)
 with cf.ProcessPoolExecutor(memory_limit,mp_context=context,max_tasks_per_child=1) as memories,cf.ProcessPoolExecutor(reader_limit,mp_context=context,max_tasks_per_child=1) as readers:
  mf={};rf={}
  try:
   while pending_memory or pending_reader or mf or rf or retryq:
    check(c,run);now=time.time()
    for item in list(retryq):
     if item[0]<=now:
      _,kind,j=item;(pending_memory if kind=='memory' else pending_reader).append(j);retryq.remove(item)
    for queue,active,pool,limit,fn,kind in [(pending_reader,rf,readers,reader_limit,reader_task,'reader'),(pending_memory,mf,memories,memory_limit,memory_task,'memory')]:
     while queue and len(active)<limit and len(mf)+len(rf)<total_limit:
      j=take_next_job(queue,mf,rf,c)
      if j is None:break
      key=kind+':'+j['id'];prior=states.get(key,{})
      states[key]={'status':'running','attempts':prior.get('attempts',0)+1,'started':time.time(),'seed':j['seed'],'method':j['method'],'episode':j['case']['episode_id']}
      active[pool.submit(fn,str(run),c,j)]=j
    write(run/'STATE.json',states)
    done,_=cf.wait(list(mf)+list(rf),timeout=1,return_when=cf.FIRST_COMPLETED)
    for f in done:
     if f in mf:j=mf.pop(f);kind='memory'
     else:j=rf.pop(f);kind='reader'
     key=kind+':'+j['id']
     try:value=f.result()
     except Exception as e:value={'status':'retry',**safe_error(e)}
     states[key].update(value,ended=time.time())
     if value['status']=='complete':
      if kind=='memory':pending_reader.append(j)
     elif value['status']!='failed' and allowed_retry(states[key]['attempts'],c['task_max_attempts']):
      states[key]['status']='retry_pending';retryq.append((time.time()+c.get('task_retry_seconds',5),kind,j))
     else:states[key]['status']='failed'
     write(run/'STATE.json',states)
    if now-last_report>=c['report_seconds']:report(run,c,jobs,states);last_report=now
    if now-last_govern>=60:governor(c,run);last_govern=now
  except ControlStopped:paused=True
  finally:
   # Context managers drain all in-flight workers; no cancellation of HTTP200
   # products. Each reader future persists before this process observes it.
   if paused:write(run/'PAUSED.json',{'time':time.time(),'inflight_draining':True})
 value=report(run,c,jobs,states)
 if value['complete']:write(run/'COMPLETE.json',{'time':time.time(),'scored':value['scored'],'expected':value['expected'],'offline_fixture':c.get('offline',False)})
 elif not paused:write(run/'NEEDS_REVIEW.json',{'time':time.time(),'failed':[k for k,v in states.items() if v.get('status')=='failed']})
 return value
