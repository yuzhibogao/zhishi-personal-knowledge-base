"""Bounded in-memory search jobs; progress reflects completed stages, not token guesses."""
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from . import db, hybrid, model_search

_lock = threading.Lock()
_jobs = {}
_slots = threading.BoundedSemaphore(4)
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='kb-search')
TTL = 600


def _prune():
    now = time.time()
    for key in list(_jobs):
        job = _jobs[key]
        if job['state'] in ('done', 'failed') and now-job['updated'] > TTL:
            del _jobs[key]
    finished = sorted((j for j in _jobs.values() if j['state'] in ('done','failed')), key=lambda j:j['updated'])
    for job in finished[:max(0,len(_jobs)-20)]:
        del _jobs[job['job_id']]


def snapshot(ident):
    with _lock:
        _prune()
        job = _jobs.get(ident)
        return copy.deepcopy(job) if job else None


def submit(path, query, limit, filters, method, broad):
    if not _slots.acquire(blocking=False):
        raise ValueError('检索任务较多，请稍后再试')
    ident = uuid.uuid4().hex
    with _lock:
        _prune()
        _jobs[ident] = dict(job_id=ident, state='queued', percent=0, stage='排队等待',
            total=0, processed=0, matched=0, excluded=0, failed=0, fallback=False,
            started=time.time(), updated=time.time(), trace=[], result=None)

    def report(percent, stage, **counts):
        with _lock:
            job=_jobs[ident]
            if stage != job['stage']:
                job['trace'].append({'stage':stage,'percent':percent,'elapsed':round(time.time()-job['started'],1)})
                job['trace']=job['trace'][-12:]
            job.update(counts, state='running', percent=max(job['percent'],min(99,percent)),
                       stage=stage, updated=time.time())

    def work():
        try:
            report(3,'准备检索')
            with db.session(path) as conn:
                if method=='model':
                    result=model_search.search(conn,query,limit,filters,broad,progress=report)
                else:
                    options={k:filters.get(k) for k in ('source_type','time_from','time_to','category_path','tag')}
                    results=hybrid.hybrid_search(conn,query,limit,broad=broad,progress=report,**options)
                    result={'results':results,'search_method':'local','message':'本地全文检索完成。'}
            with _lock:
                job=_jobs[ident]
                fallback=result.get('search_method')=='local_fallback'
                stage='已完成（回退本地）' if fallback else '检索完成'
                job.update(state='done',percent=100,stage=stage,result=result,returned=len(result['results']),
                           fallback=fallback,updated=time.time())
                job['trace'].append({'stage':stage,'percent':100,'elapsed':round(time.time()-job['started'],1)})
        except Exception:
            with _lock:
                _jobs[ident].update(state='failed',stage='检索失败，请重试',failed=1,updated=time.time())
        finally:
            _slots.release()

    try:
        _workers.submit(work)
    except Exception:
        with _lock:_jobs.pop(ident,None)
        _slots.release()
        raise
    return snapshot(ident)
