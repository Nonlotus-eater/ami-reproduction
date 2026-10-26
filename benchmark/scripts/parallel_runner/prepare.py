from .common import *
import shutil,sqlite3,subprocess
SKIP={'.git','.benchmark-env','.venv','venv','__pycache__','data','datasets','dataset','models','node_modules','runs','results','artifacts'}
def copy_source(src,dst):
 def ignore(folder,names):return [n for n in names if n in SKIP or (n.startswith('.env') and not n.endswith(('.example','.template')))]
 shutil.copytree(src,dst,ignore=ignore)
def fingerprints(root):
 return {str(p.relative_to(root)):sha(p) for p in Path(root).rglob('*') if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith(('.pyc','.log'))}
def copy_framework(source,destination):
 destination.mkdir()
 for name in ['src','sources']:
  shutil.copytree(source/name,destination/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 (destination/'configs').mkdir()
 shutil.copy2(source/'configs/runtime.json',destination/'configs/runtime.json')
 shutil.copy2(source/'pyproject.toml',destination/'pyproject.toml')

def initialize(c):
 run=Path(c['output']);run.mkdir(parents=True,exist_ok=True)
 if (run/'CONFIG.json').exists():raise ValueError('Existing run: use resume; never overwrite frozen inputs')
 if any(run.iterdir()):raise ValueError('Output must be an empty directory')
 write(run/'REQUEST_CONFIG.json',c)
 copy_framework(PACKAGE.parent,run/'framework')
 (run/'launcher').mkdir()
 shutil.copy2(PACKAGE/'run_parallel.py',run/'launcher/launch.py');shutil.copytree(PACKAGE/'parallel_runner',run/'launcher/parallel_runner',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 frozen=json.loads(json.dumps(c));native_proofs={}
 for method in frozen['methods']:
  if c.get('offline'):continue
  source=Path(method['config']['source_root']);dest=run/'native'/method['name']
  copy_source(source,dest);method['config']['source_root']=str(dest)
  if 'adapter_file' in method['config']:
   original=Path(method['config']['adapter_file']);adapter=run/'native-adapters'/(method['name']+'.py');adapter.parent.mkdir(exist_ok=True);shutil.copy2(original,adapter);method['config']['adapter_file']=str(adapter)
  native_proofs[method['name']]={'original':str(source),'frozen':str(dest),'sha256':fingerprints(dest)}
 framework(run)
 from memory_benchmark.runtime.native_freeze import environment_identity
 environments={} if c.get('offline') else {m['name']:environment_identity(m['config']['python']) for m in frozen['methods']}
 from memory_benchmark.pipeline.prepare import code_identity,verify_prepared
 from memory_benchmark.runtime.artifacts import contract_hash
 from memory_benchmark.runtime.profile import read_profile,validate_profile,public_identity
 from .shared_control import SharedControl
 rt=read(c.get('runtime_file',run/'framework/configs/runtime.json'));rt.update(env_file=c['env_file'],seed=c['seeds'][0],request_timeout_seconds=c['request_timeout_seconds'],worker_timeout_seconds=c.get('worker_timeout_seconds',86400),
  rates_cny_per_million=c['rates_cny_per_million'],max_retries=0,persist_answer_responses=True,
  memory_checkpoint_policy=None if c.get('offline') else 'add_chunks_sqlite_snapshot_v1')
 from memory_benchmark.pipeline.runner import validate_runtime
 validate_runtime(rt)
 if c.get('offline'):provider=None;env_proof=None
 else:
  profile=validate_profile(read_profile(c['env_file']),rt['model']);provider=public_identity(profile,rt);env_proof=sha(c['env_file'])
 jobs=[];proofs=[]
 for seed in c['seeds']:
  src=Path(c['prepared_root'])/f'seed-{seed}';old=read(src/'manifest.json')
  for rel,expected in old['inputs'].items():
   if sha(src/rel)!=expected:raise ValueError('Prepared input changed: '+rel)
  dst=run/'prepared'/f'seed-{seed}';shutil.copytree(src,dst)
  write(dst/'ORIGINAL_MANIFEST.json',old)
  # Fully frozen prepared input is sufficient; original raw dataset paths are
  # retained as provenance, not silently dereferenced on the teammate machine.
  manifest=json.loads(json.dumps(old));manifest['runtime']={**rt,'seed':seed};manifest['framework']=code_identity(root=run/'framework')
  for name,b in manifest['benchmarks'].items():b['source_sha256']={}
  manifest['handoff_import']={'original_manifest_sha256':sha(src/'manifest.json'),'all_prepared_input_bytes_verified':True,
   'raw_source_paths_not_required':'Original manifest retained; input content hashes unchanged. Original source files are not independently reverified on this machine.'}
  write(dst/'manifest.json',manifest);meta=read(dst/'prepared.json');write(dst/'prepared.json',{**meta,'contract_sha256':contract_hash(manifest)});verify_prepared(dst)
  cases=rows(dst/'cases.jsonl')
  role_set=set()
  for case in cases:
   h=read(dst/case['history']);weight=sum(len(m['text']) for s in h['sessions'] for m in s['messages'])
   role_set.update(m['role'] for s in h['sessions'] for m in s['messages'])
   for method in frozen['methods']:
    key=f"{seed}|{method['name']}|{case['benchmark']}|{case['key']}";ident=hashlib.sha256(key.encode()).hexdigest()[:24]
    jobs.append({'id':ident,'seed':seed,'method':method['name'],'case':case,'weight':weight})
  if not c.get('offline'):
   from memory_benchmark.methods.compatibility import validate_ami_roles
   for method in frozen['methods']:
    if method['adapter']=='activememoryindex':validate_ami_roles(method['config'],role_set)
  proofs.append({'seed':seed,'histories':len(cases),'questions':sum(len(b['items']) for b in manifest['benchmarks'].values()),'original_manifest_sha256':sha(src/'manifest.json')})
 ledger_dir=Path(c['ledger_dir']);ledger_dir.mkdir(parents=True,exist_ok=True)
 ledger=ledger_dir/'ledger.sqlite3'
 if ledger.exists():raise ValueError('Choose a new ledger_dir; refusing to reuse another run ledger')
 SharedControl.create(ledger,cap_cny=c['budget_cny'],providers=c['providers'],rates=c['rates_cny_per_million'],identity={'run':str(run),'provider':provider,'config_sha256':digest(c)})
 write(run/'CONFIG.json',frozen);write(run/'JOBS.json',jobs);write(run/'IDENTITY.json',{'provider':provider,'env_sha256':env_proof,'native':native_proofs,'environments':environments,'inputs':proofs,
  'configuration_sha256':digest(frozen),'jobs_sha256':digest(jobs),
  'framework_sha256':fingerprints(run/'framework'),'launcher_sha256':fingerprints(run/'launcher'),'scheduler_origin':'a3f9610 parallel handoff plus documented repository integration','API_calls':0})
 write(run/'READY.json',{'time':time.time(),'history_tasks':len(jobs),'questions_per_method':sum(p['questions'] for p in proofs),'offline_fixture':c.get('offline',False)})
 return frozen

def verify(run):
 run=Path(run);c=read(run/'CONFIG.json');identity=read(run/'IDENTITY.json')
 if digest(c)!=identity['configuration_sha256'] or digest(read(run/'JOBS.json'))!=identity['jobs_sha256']:raise ValueError('Frozen config or job allocation changed')
 for key,folder in [('framework_sha256','framework'),('launcher_sha256','launcher')]:
  if fingerprints(run/folder)!=identity[key]:raise ValueError('Frozen '+folder+' changed')
 if not c.get('offline') and sha(c['env_file'])!=identity['env_sha256']:raise ValueError('Provider env changed; use a new run or explicit audited migration')
 for v in identity['native'].values():
  if fingerprints(Path(v['frozen']))!=v['sha256']:raise ValueError('Frozen native source/config changed')
 framework(run)
 from memory_benchmark.runtime.native_freeze import environment_identity
 for method in c['methods']:
  if method['name'] in identity.get('environments',{}) and environment_identity(method['config']['python'])!=identity['environments'][method['name']]:raise ValueError('Native Python environment changed')
 from memory_benchmark.pipeline.prepare import verify_prepared
 for seed in c['seeds']:verify_prepared(run/'prepared'/f'seed-{seed}')
 from .shared_control import SharedControl
 SharedControl(Path(c['ledger_dir'])/'ledger.sqlite3')
 return c

def clear_user_stop(run,c):
 from .shared_control import SharedControl
 # Supervisor lock is checked by launch.py before this function.
 dbpath=Path(c['ledger_dir'])/'ledger.sqlite3'
 control=SharedControl(dbpath)
 with sqlite3.connect(dbpath,timeout=30) as db:
  stamp=str(time.time_ns());backup=sqlite3.connect(Path(c['ledger_dir'])/('before-resume-'+stamp+'.sqlite3'));db.backup(backup);backup.close()
  db.execute('BEGIN IMMEDIATE');stop=db.execute("SELECT value FROM settings WHERE key='stop'").fetchone()
  if stop and stop[0]!='user_stop':raise ValueError('Not a user stop: '+stop[0]+'; budget/provider stops need review')
  live=[call_id for call_id, in db.execute("SELECT call_id FROM tickets WHERE state IN ('active','waiting')") if control.request_alive(call_id) is not False]
  if live:raise ValueError('Provider requests still live or ownership unknown; wait for drain')
  db.row_factory=sqlite3.Row
  control.reap_abandoned(db)
  db.execute("DELETE FROM settings WHERE key='stop'")
 stopfile=Path(run)/'STOP.json'
 if stopfile.exists():stopfile.rename(stopfile.with_name('STOP.resumed-'+stamp+'.json'))
