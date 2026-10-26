"""One process/history; caches successful Search without opening targets."""
from .common import *
def main(jobpath):
 job=read(jobpath);framework(job['run']);setup_env(job['launch_config'],job['run'],job['scope'])
 from memory_benchmark.runtime import worker
 from .model_client import Gateway
 worker.Gateway=Gateway
 from memory_benchmark.schemas import Evidence
 from memory_benchmark.pipeline.context import normalize
 create=worker.create
 def instrumented(*args,**kwargs):
  adapter=create(*args,**kwargs)
  if job['method']=='refind' and not job['offline'] and job['launch_config'].get('refind_context_policy')=='deduplicate_observations_v1':
   from .planner_context import PlannerContext
   context=PlannerContext()
   adapter.retriever._format_observation=context.format_observation
   complete=adapter.planner.complete
   def bounded_complete(messages):
    fitted=context.fit(messages)
    write(Path(job['output'])/'planner_context_audit.json',context.events)
    return complete(fitted)
   adapter.planner.complete=bounded_complete
  search=adapter.search
  def cached(query,top_k):
   p=Path(job['cache'])/(hashlib.sha256(query.qa_id.encode()).hexdigest()+'.json')
   if p.exists():
    v=read(p)
    if v['id']!=query.qa_id:raise ValueError('Search cache ID mismatch')
    return [Evidence.from_wire(e) for e in v['evidence']]
   evidence=normalize(search(query,top_k),top_k)
   write(p,{'id':query.qa_id,'evidence':[e.to_wire() for e in evidence]});return evidence
  adapter.search=cached;return adapter
 worker.create=instrumented
 result=worker.execute(job);print(json.dumps({'status':result['status'],'method':job['method']}),flush=True)
 return 0 if result['status']=='complete' else 1
if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
