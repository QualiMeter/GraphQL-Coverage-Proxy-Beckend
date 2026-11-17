"""Opt-in 45,001-field/20-run benchmark; use a disposable local database.

AUDIT_DATABASE_URL=postgresql+asyncpg://... python tests/benchmark_comparison.py
Optional SOL_SOURCE selects a separate checkout for before/after measurement.
The benchmark creates a service/snapshot/runs and leaves them for inspection.
"""
import asyncio, json, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, os.environ.get('SOL_SOURCE', str(Path(__file__).resolve().parents[1])))
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import get_session, get_session_factory
from app.main import app
from app.services import create_service, import_sdl, create_run, finish_run
from app.schema_cache import schema_from_sdl

async def main():
    engine=create_async_engine(os.environ['AUDIT_DATABASE_URL'])
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    sdl='type Query { item: T0 }\n'+'\n'.join('type T'+str(i)+' { '+
        ' '.join('f'+str(j)+': Int' for j in range(90))+' }' for i in range(500))
    async with sessions() as s:
        service=await create_service(s,'wide-'+uuid.uuid4().hex,'Wide',is_public=True)
        snapshot=await import_sdl(s,service.id,sdl)
        runs=[]
        for n in range(20):
            run=await create_run(s,snapshot.id,'Wide '+str(n),{})
            await finish_run(s,run.id)
            runs.append(run.id)
    async def dep():
        async with sessions() as s: yield s
    app.dependency_overrides[get_session]=dep
    app.dependency_overrides[get_session_factory]=lambda:sessions
    payload={'runIds':[str(i) for i in runs],'baselineRunId':str(runs[0]),
             'fields':{'limit':1},'types':{'limit':1}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://graphql.rebyte.fun') as c:
        for temperature in ['cold','warm']:
            if temperature=='cold': schema_from_sdl.cache_clear()
            gaps=[]; health_times=[]; health_due_times=[]; stop=False
            async def heartbeat():
                before=time.perf_counter()
                while not stop:
                    due=before+.005
                    await asyncio.sleep(.005)
                    current=time.perf_counter(); gaps.append(current-before); before=current
                    t=time.perf_counter(); assert (await c.get('/health')).status_code==200
                    health_times.append(time.perf_counter()-t)
                    health_due_times.append(time.perf_counter()-due)
            watcher=asyncio.create_task(heartbeat())
            await asyncio.sleep(.02)
            t=time.perf_counter(); response=await c.post('/api/runs/compare',json=payload); elapsed=time.perf_counter()-t
            await asyncio.sleep(.02); stop=True; await watcher
            assert response.status_code==200,response.text
            data=response.json()
            assert data['runs'][0]['totalFields']==45001 and data['fields']['total']==45001
            assert len(data['fields']['items'])==len(data['types']['items'])==1
            assert data['types']['total']==501
            assert all(r['totalFields']==45001 and r['coveredFields']==0
                       and r['coveragePercent']==0 and r['totalRequests']==0 for r in data['runs'])
            assert data['fieldCategories']['neverCovered']==45001
            assert data['fieldCategories']['stillUncovered']==45001
            print(json.dumps({'cache':temperature,'schema_bytes':len(sdl),'fields':45001,'runs':20,
                 'compare_seconds':elapsed,'max_event_loop_gap_seconds':max(gaps),
                 'max_health_handler_seconds':max(health_times),
                 'max_health_since_scheduled_seconds':max(health_due_times),'concurrent_tasks':2,'http_status':response.status_code}),flush=True)
    app.dependency_overrides.clear()
    await engine.dispose()
asyncio.run(main())
