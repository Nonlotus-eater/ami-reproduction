from .common import *
import concurrent.futures as cf,subprocess,threading,sqlite3
from .sandbox import worker_command

def memory_task(run,c,j):
 framework(run);setup_env(c,run,'memory:'+j['id']);check(c,run)
 out=taskdir(run,j);out.mkdir(parents=True,exist_ok=True)
 if (out/'MEMORY_COMPLETE.json').exists():return {'status':'complete'}
 prepared=Path(run)/'prepared'/('seed-'+str(j['seed']));m=read(prepared/'manifest.json')
 attempt=1+max([int(p.name.rsplit('_',1)[1]) for p in out.glob('memory_attempt_*') if p.is_dir()] or [0]);dest=out/f'memory_attempt_{attempt:04d}'
 method=next(x for x in c['methods'] if x['name']==j['method']);rt=m['runtime']
 job={'run':str(run),'launch_config':c,'cache':str(out/'retrieval_cache'),'method':method['adapter'],'method_config':method['config'],
  'runtime':rt,'offline':c.get('offline',False),'provider_identity':read(Path(run)/'IDENTITY.json')['provider'],
  'scope':'parallel:'+j['id'],'history':str(prepared/j['case']['history']),'queries':str(prepared/j['case']['queries']),
  'input_hashes':{k:m['inputs'][j['case'][k]] for k in ['history','queries']},'output':str(dest)}
 jp=out/f'job-{attempt:04d}.json';write(jp,job)
 # No targets/answer settings appear in the memory worker job. a3f9610
 # does not enforce filesystem namespaces; see the latest-version review.
 env={**os.environ,'PYTHONPATH':os.pathsep.join([str(PACKAGE),str(Path(run)/'framework/src')]),'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
 executable=sys.executable if c.get('offline') else method['config']['python']
 with (out/f'memory-{attempt:04d}.log').open('wb') as log:
  command,process_env,private=worker_command(job,jp,executable,run,c)
  try:p=subprocess.Popen(command,env=process_env,stdout=log,stderr=subprocess.STDOUT)
  except BaseException:
   if private:private.unlink(missing_ok=True)
   raise
  write(out/'NATIVE_PROCESS.json',{'pid':p.pid,'attempt':attempt,'started':time.time()})
  stop_started=None;deadline=time.monotonic()+rt['worker_timeout_seconds']
  while p.poll() is None:
   if time.monotonic()>deadline and stop_started is None:stop_started=time.monotonic()
   if stop_started is not None and time.monotonic()-stop_started>c.get('stop_drain_seconds',240)+60:p.kill()
   elif stop_started is not None and time.monotonic()-stop_started>c.get('stop_drain_seconds',240):p.terminate()
   try:check(c,run)
   except Exception:
    stop_started=stop_started or time.monotonic()
    if time.monotonic()-stop_started>c.get('stop_drain_seconds',240):p.terminate()
    if time.monotonic()-stop_started>c.get('stop_drain_seconds',240)+60:p.kill()
   time.sleep(.5)
  code=p.wait()
  if private:private.unlink(missing_ok=True)
 report=read(dest/'report.json') if (dest/'report.json').exists() else {'error_type':'WorkerProcessExit'}
 if code or report.get('status')!='complete':
  status=None;failure_class=None
  if (dest/'usage.json').exists():
   calls=read(dest/'usage.json').get('calls',[]);status=calls[-1].get('http_status') if calls else None;failure_class=calls[-1].get('failure_class') if calls else None
  permanent=failure_class=='context_length_exceeded' or status in {400,401,403,404,413,422} or report.get('error_type') in {'ValueError','ImportError','ModuleNotFoundError','FileNotFoundError'}
  return {'status':'failed' if permanent else 'retry','error_type':report.get('error_type'),'http_status':status,'attempt':attempt}
 expected={q['qa_id'] for q in rows(prepared/j['case']['queries'])}
 actual={read(p)['id'] for p in (out/'retrieval_cache').glob('*.json')}
 if actual!=expected:return {'status':'failed','error_type':'RetrievalSetMismatch'}
 write(out/'MEMORY_COMPLETE.json',{'attempt':attempt,'time':time.time(),'queries':len(expected),'checkpoint':str(out/'memory_resume/checkpoint.json')})
 return {'status':'complete'}

def reader_task(run,c,j):
 framework(run);setup_env(c,run,'reader:'+j['id'])
 from memory_benchmark.schemas import History,Query,Target
 from memory_benchmark.pipeline.context import GPTTokenizer,FixtureTokenizer
 from memory_benchmark.pipeline.reader import answer
 from memory_benchmark.pipeline.evaluate import evaluate
 from .model_client import Gateway,FixtureClient
 from .shared_control import ControlStopped
 out=taskdir(run,j);prepared=Path(run)/'prepared'/('seed-'+str(j['seed']));rt=read(prepared/'manifest.json')['runtime']
 history=History.from_wire(read(prepared/j['case']['history']));queries=[Query(**q) for q in rows(prepared/j['case']['queries'])]
 errors=[];cancelled=threading.Event()
 def client_for(p):return FixtureClient() if c.get('offline') else Gateway(rt,p)
 def one_answer(q):
  p=qdir(out,q.qa_id);p.mkdir(parents=True,exist_ok=True)
  if (p/'answer.json').exists():return
  check(c,run);cl=client_for(p/f'usage-answer-{time.time_ns()}.json')
  try:
   ev=read(out/'retrieval_cache'/(hashlib.sha256(q.qa_id.encode()).hexdigest()+'.json'))['evidence']
   record=answer(q,ev,history.speakers,cl,FixtureTokenizer() if c.get('offline') else GPTTokenizer(rt['model']),rt)
   write(p/'answer.json',record)
  except Exception as e:attach_provider_status(cl,e);raise
  finally:cl.close()
 def collect(fn,items,stage):
  with cf.ThreadPoolExecutor(pool_size(c.get('query_workers'),len(items))) as pool:
   fs={pool.submit(fn,q):q.qa_id for q in items}
   # A single failure never interrupts collection of other successful futures.
   for f in cf.as_completed(fs):
    try:f.result()
    except Exception as e:errors.append({'id':fs[f],'stage':stage,**safe_error(e)})
  write(out/'READER_ERRORS.json',errors)
 collect(one_answer,queries,'answer')
 if any(not (qdir(out,q.qa_id)/'answer.json').exists() for q in queries):
  return {'status':'failed' if any(e.get('status') in {400,401,403,404,413,422} for e in errors) else 'retry','stage':'answer','errors':errors}
 # All Answer futures have drained and durable outputs exist BEFORE targets open.
 targets={t['qa_id']:Target(**t) for t in rows(prepared/j['case']['targets'])}
 def one_judge(q):
  p=qdir(out,q.qa_id)
  if (p/'score.json').exists():return
  check(c,run);cl=client_for(p/f'usage-judge-{time.time_ns()}.json')
  try:
   if c.get('offline'):score={'id':q.qa_id,'is_correct':False,'label':'WRONG','offline_fixture':True}
   else:score=evaluate(q,targets[q.qa_id],read(p/'answer.json'),cl,rt)
   write(p/'score.json',score)
  except Exception as e:attach_provider_status(cl,e);raise
  finally:cl.close()
 collect(one_judge,queries,'judge')
 if any(not (qdir(out,q.qa_id)/'score.json').exists() for q in queries):return {'status':'failed' if any(e.get('status') in {400,401,403,404,413,422} for e in errors) else 'retry','stage':'judge','errors':errors}
 write(out/'COMPLETE.json',{'time':time.time(),'questions':len(queries),'offline_fixture':c.get('offline',False)})
 return {'status':'complete'}
