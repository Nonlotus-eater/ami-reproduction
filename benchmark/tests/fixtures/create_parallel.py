"""Generate tiny synthetic prepared inputs; never used for method scores."""
from pathlib import Path
import sys,json,tempfile
R=Path(__file__).resolve().parents[2];sys.path.insert(0,str(R/'scripts'));sys.path.insert(0,str(R/'src'))
from parallel_runner.common import write,sha,read
from memory_benchmark.runtime.artifacts import contract_hash
from memory_benchmark.pipeline.prepare import code_identity
dest=Path(sys.argv[1]).resolve();dest.mkdir(parents=True,exist_ok=True)
for seed in [17,18,19,20,21]:
 p=dest/'inputs'/f'seed-{seed}';p.mkdir(parents=True,exist_ok=True);cases=[];inputs={};items={}
 for k in range(2):
  key=f'h{k}';episode=f'fixture-{k}'
  h={'benchmark':'locomo_refined','episode_id':episode,'speakers':['A','B'],'sessions':[{'session_id':'s0','timestamp':'2024-01-01T00:00:00+00:00','raw_timestamp':'2024-01-01','messages':[{'source_id':'m0','role':'user','speaker':'A','text':'My pet is Milo.','timestamp':'2024-01-01T00:00:00+00:00'}]}]}
  qs=[{'benchmark':'locomo_refined','episode_id':episode,'qa_id':key+f'q{i}','text':'Which pet was mentioned?'} for i in range(3)]
  ts=[{'qa_id':q['qa_id'],'gold':'Milo','capability_leaves':['A1']} for q in qs]
  write(p/(key+'.history.json'),h)
  for suffix,values in [('queries',qs),('targets',ts)]:
   fp=p/(key+'.'+suffix+'.jsonl');fp.write_text('\n'.join(json.dumps(x) for x in values)+'\n')
  case={'benchmark':'locomo_refined','key':key,'episode_id':episode,'history':key+'.history.json','queries':key+'.queries.jsonl','targets':key+'.targets.jsonl'};cases.append(case)
  for field in ['history','queries','targets']:inputs[case[field]]=sha(p/case[field])
  items.update({q['qa_id']:{'source_unit_id':episode,'cluster_id':episode,'capability_leaves':['A1']} for q in qs})
 (p/'cases.jsonl').write_text('\n'.join(json.dumps(x) for x in cases)+'\n');inputs['cases.jsonl']=sha(p/'cases.jsonl')
 manifest={'suite_id':'synthetic-no-network','scope':'pilot','aggregation':'macro_benchmark','purpose':'No-network scheduler fixture; not method performance','benchmarks':{'locomo_refined':{'primary_metric':'judged_accuracy','items':items,'source_sha256':{}}},'runtime':{},'inputs':inputs,'framework':code_identity()}
 write(p/'manifest.json',manifest);write(p/'prepared.json',{'status':'prepared','contract_sha256':contract_hash(manifest)})
c=read(R/'configs/parallel/four-methods.json');c.update(output=str(dest/'run'),prepared_root=str(dest/'inputs'),env_file=str(dest/'unused.env'),ledger_dir=str(dest/'ledger'),seeds=[17,18,19,20,21],offline=True,total_history_workers=20,per_method_history_workers=5,memory_workers=20,reader_history_workers=20,query_workers=1,report_seconds=1,adaptive_concurrency=False)
c['runtime_file']=str(R/'configs/runtime.json')
c['methods'][0]['config']['python']=sys.executable;c['methods'][0]['config']['source_root']=str(R/'baselines/ami-reproduction')
write(dest/'settings.json',c);print(dest/'settings.json')
