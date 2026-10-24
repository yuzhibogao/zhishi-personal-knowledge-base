"""Explicit batch classification; recheck each item so manual moves are respected."""
import threading
from . import db, locks, runtime

_lock=threading.Lock()
_state={'running':False,'done':0,'total':0,'classified':0,'unclassified':0,'failed':0,'skipped':0}

def state():
    with _lock:return dict(_state)

def prepare(path):
    with _lock:
        if _state['running']:raise ValueError('一键分类正在进行，请等待完成')
        with db.session(path) as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM documents WHERE category_id IS NULL OR category_path='未分类'")]
            mode=runtime.mode(conn)
        _state.update(running=bool(ids),done=0,total=len(ids),classified=0,unclassified=0,failed=0,skipped=0)
        return ids,mode

def run(path,ids,mode):
    from concurrent.futures import ThreadPoolExecutor
    from . import pipeline,manage
    import os
    # At most two simultaneous documents; provider limits can opt back into one.
    try:workers=max(1,min(2,int(os.environ.get('CLASSIFICATION_WORKERS','2'))))
    except ValueError:workers=2
    def process_one(doc_id):
        outcome='skipped'
        try:
            with locks.document(doc_id), db.session(path) as conn:
                row=conn.execute('SELECT category_id,category_path FROM documents WHERE id=?',(doc_id,)).fetchone()
                if row and (row['category_id'] is None or row['category_path']=='未分类'):
                    with locks.catalog:
                        conn.execute('BEGIN IMMEDIATE')
                        conn.execute("DELETE FROM corrections WHERE doc_id=? AND kind='category'",(doc_id,))
                        conn.execute('UPDATE documents SET category_id=NULL,category_path=NULL WHERE id=?',(doc_id,))
                        if row['category_id']:manage.rebuild_centroid(conn,row['category_id'],manage._model())
                        conn.execute('INSERT OR REPLACE INTO document_processing(doc_id,mode) VALUES(?,?)',(doc_id,mode))
                        conn.commit()
                    status=pipeline.process_document(conn,doc_id)
                    result=conn.execute('SELECT category_path FROM documents WHERE id=?',(doc_id,)).fetchone()
                    outcome='failed' if status!='stored' else 'unclassified' if not result or result[0]=='未分类' else 'classified'
        except Exception:
            outcome='failed'
        with _lock:
            _state['done']+=1;_state[outcome]+=1
    try:
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='kb-classify') as executor:
            # executor.map is consumed to surface worker errors; database connections are per worker.
            for _ in executor.map(process_one,ids):pass
    finally:
        with _lock:_state['running']=False
