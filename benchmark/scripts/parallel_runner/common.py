from pathlib import Path
import json,os,sys,time,hashlib,math
PACKAGE=Path(__file__).resolve().parents[1]
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def rows(p):
 with Path(p).open(encoding='utf-8') as stream:return [json.loads(x) for x in stream if x.strip()]
def write(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+f'.{os.getpid()}.{time.time_ns()}.tmp')
 with tmp.open('w',encoding='utf-8') as f:json.dump(v,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
 tmp.replace(p)
 if os.name=='posix':
  fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(fd)
  finally:os.close(fd)
def path(v,base=PACKAGE):
 p=Path(os.path.expandvars(os.path.expanduser(v)));return p.resolve() if p.is_absolute() else (base/p).resolve()
def load_config(p):
 p=Path(p).resolve();c=read(p)
 for key in ['output','prepared_root','env_file','ledger_dir']:
  c[key]=str(path(c[key],p.parent))
 if c.get('tokenizer_cache'):c['tokenizer_cache']=str(path(c['tokenizer_cache'],p.parent))
 if c.get('runtime_file'):c['runtime_file']=str(path(c['runtime_file'],p.parent))
 for m in c['methods']:
  executable=os.path.expandvars(os.path.expanduser(m['config'].get('python',sys.executable)))
  # Resolving the final symlink bypasses venv/pyvenv.cfg and loses method dependencies.
  m['config']['python']=os.path.abspath(os.path.join(str(p.parent),executable))
  for k in ['source_root','adapter_file','fastembed_cache','memma_root']:
   if k in m['config']:m['config'][k]=str(path(m['config'][k],p.parent))
 validate_config(c);return c
def pool_size(limit, tasks):
 """Null admits every task; positive user limits have no built-in ceiling."""
 return max(1, tasks if limit is None else min(limit, tasks))
def validate_config(c):
 if c.get('refind_context_policy') not in {None,'deduplicate_observations_v1'}:raise ValueError('Unknown ReFind context policy')
 if not c.get('methods') or len({m['name'] for m in c['methods']})!=len(c['methods']):raise ValueError('Select distinct methods')
 if any(m['adapter'] not in {'activememoryindex','mem0','invmem','refind'} for m in c['methods']):raise ValueError('Use one of the four pinned native adapters')
 if not c.get('seeds') or len(set(c['seeds']))!=len(c['seeds']) or any(type(s) is not int for s in c['seeds']):raise ValueError('Distinct integer seeds required')
 for key in ['request_timeout_seconds','report_seconds']:
  if type(c[key]) is not int or c[key]<=0:raise ValueError('Invalid '+key)
 for key in ['memory_workers','reader_history_workers','query_workers','per_method_history_workers','total_history_workers']:
  if c.get(key) is not None and (type(c[key]) is not int or c[key]<=0):raise ValueError('Use positive integer or null: '+key)
 for key in ['request_max_attempts','task_max_attempts']:
  if c[key] is not None and (type(c[key]) is not int or c[key]<=0):raise ValueError('Use positive attempts or null: '+key)
 for kind in ['llm','embedding']:
  if any(c['providers'][kind].get(k) is not None and (type(c['providers'][kind][k]) is not int or c['providers'][kind][k]<=0) for k in ['inflight','rpm','tpm']):raise ValueError('Use positive provider limit or null')
 if c['budget_cny'] is not None and (not math.isfinite(c['budget_cny']) or c['budget_cny']<=0):raise ValueError('Invalid budget')
 for v in c['rates_cny_per_million'].values():
  if not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValueError('Positive actual provider rates required')
def framework(run):
 root=Path(run)/'framework';sys.path.insert(0,str(root/'src'));return root
def setup_env(c,run,scope):
 os.environ.update(AML_SHARED_LEDGER=str(Path(c['ledger_dir'])/'ledger.sqlite3'),AML_RUN_SCOPE=scope,
  AML_RETRY_MAX_ATTEMPTS=str(c['request_max_attempts'] or 0),AML_RETRY_BASE_SECONDS=str(c.get('retry_base_seconds',2)),
  AML_RETRY_MAX_SECONDS=str(c.get('retry_max_seconds',60)),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',TOKENIZERS_PARALLELISM='false')
 if c.get('tokenizer_cache'):os.environ['TIKTOKEN_CACHE_DIR']=str(path(c['tokenizer_cache']))
def taskdir(run,j):return Path(run)/'tasks'/('seed-'+str(j['seed']))/j['method']/j['id']
def qdir(folder,ident):return Path(folder)/'queries'/hashlib.sha256(ident.encode()).hexdigest()[:24]
def check(c,run):
 from .shared_control import SharedControl,ControlStopped
 if (Path(run)/'STOP.json').exists():raise ControlStopped('User stop')
 SharedControl(Path(c['ledger_dir'])/'ledger.sqlite3').check()
def safe_error(e):return {'type':type(e).__name__,'status':getattr(e,'benchmark_http_status',getattr(e,'status_code',None))}
def attach_provider_status(client,error):
 calls=getattr(getattr(client,'budget',None),'rows',[])
 if calls:
  try:error.benchmark_http_status=calls[-1].get('http_status')
  except (AttributeError,TypeError):pass
def allowed_retry(attempt,limit):return limit is None or attempt<limit
