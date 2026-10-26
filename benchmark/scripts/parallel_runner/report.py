from .common import *
import statistics,sqlite3
def capability_overall(summary):
 scores=[summary.get('capabilities',{}).get(k,{}).get('score') for k in ['A','B','C','D','E','G','H']]
 return statistics.mean(scores) if summary.get('complete') and all(x is not None for x in scores) else None
def report(run,c,jobs,state=None):
 framework(run)
 from memory_benchmark.pipeline.aggregate import import_native,summarize
 result={};scored=expected=0;histories=0
 for seed in c['seeds']:
  manifest=read(Path(run)/'prepared'/f'seed-{seed}/manifest.json');total=sum(len(b['items']) for b in manifest['benchmarks'].values());seed_data={}
  for method in c['methods']:
   selected=[j for j in jobs if j['seed']==seed and j['method']==method['name']];records=[];counts={'histories_expected':len(selected),'memory_complete':0,'histories_complete':0,'answered':0,'scored':0,'expected':total}
   for j in selected:
    d=taskdir(run,j);counts['memory_complete']+=(d/'MEMORY_COMPLETE.json').exists();counts['histories_complete']+=(d/'COMPLETE.json').exists()
    for q in rows(Path(run)/'prepared'/f'seed-{seed}'/j['case']['queries']):
     p=qdir(d,q['qa_id']);counts['answered']+=(p/'answer.json').exists()
     if (p/'score.json').exists():
      records.extend(import_native(manifest,[read(p/'score.json')],q['benchmark'],method['name'],Path(run).name));counts['scored']+=1
   s=summarize(manifest,records)
   # a3f9610 computes this in run_five_seed_experiments.round_summary,
   # not pipeline.aggregate.summarize. Preserve that exact seven-family rule.
   s['local_overall_macro_capability']=capability_overall(s)
   if c.get('offline'):
    s['local_overall_macro_capability']=None;s['local_overall_macro_benchmark']=None
   write(Path(run)/'summaries'/f'seed-{seed}'/(method['name']+'.json'),s)
   seed_data[method['name']]={**counts,'capability_overall':s['local_overall_macro_capability'],'dataset_overall':s['local_overall_macro_benchmark'],'complete':counts['scored']==total and counts['histories_complete']==len(selected)}
   scored+=counts['scored'];expected+=total;histories+=counts['histories_complete']
  result[str(seed)]=seed_data
 aggregate={}
 for method in c['methods']:
  values=[result[str(s)][method['name']] for s in c['seeds']];metrics={}
  for k in ['capability_overall','dataset_overall']:
   v=[x[k] for x in values];ok=all(x['complete'] for x in values) and all(x is not None for x in v)
   metrics[k]={'mean':statistics.mean(v) if ok else None,'sample_std':statistics.stdev(v) if ok and len(v)>1 else None,'seed_values':v}
  aggregate[method['name']]={'complete':all(x['complete'] for x in values),**metrics}
 now=time.time();samples_path=Path(run)/'PROGRESS_SAMPLES.json';samples=read(samples_path) if samples_path.exists() else []
 samples=[x for x in samples if now-x['time']<1800];samples.append({'time':now,'scored':scored});write(samples_path,samples)
 rate=(scored-samples[0]['scored'])/max(1,now-samples[0]['time']) if now-samples[0]['time']>30 else None
 ledger=Path(c['ledger_dir'])/'ledger.sqlite3'
 with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as db:
  costs=[{'state':s,'calls':n,'estimated_cny':v/1e9} for s,n,v in db.execute('SELECT state,COUNT(*),SUM(charged) FROM calls GROUP BY state')]
  slots=db.execute("SELECT provider,state,COUNT(*) FROM tickets WHERE state IN ('active','waiting') GROUP BY provider,state").fetchall()
  cfg=json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0]);stop=db.execute("SELECT value FROM settings WHERE key='stop'").fetchone()
 value={'updated_unix':now,'complete':scored==expected and histories==len(jobs),'scored':scored,'expected':expected,'history_tasks_complete':histories,'history_tasks_expected':len(jobs),
  'by_seed':result,'aggregate':aggregate,'primary_metric':'capability_overall; seven families equally weighted per seed, seeds equally weighted','offline_fixture':c.get('offline',False),
  'recent_scores_per_minute':rate*60 if rate else None,'eta_minutes_scoring_only':(expected-scored)/rate/60 if rate and rate>0 else None,
  'eta_limitation':'Scoring-only trend; remaining Add, upstream errors, long contexts and tail work may extend it.',
  'ledger_costs':costs,'provider_slots':slots,'provider_limits':cfg['providers'],'stop_reason':stop[0] if stop else None,'scheduler_state':state or {}}
 from .tables import export_tables
 export_tables(run,c,aggregate)
 write(Path(run)/'PROGRESS.json',value);return value
