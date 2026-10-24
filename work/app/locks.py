"""Serialize mutations of the same document, without blocking unrelated uploads."""
import threading
from functools import wraps
from weakref import WeakValueDictionary
catalog=threading.RLock()
_guard=threading.Lock()
_locks=WeakValueDictionary()

def document(doc_id):
    with _guard:
        lock=_locks.get(doc_id)
        if lock is None:
            lock=threading.RLock()
            _locks[doc_id]=lock
        return lock

def processing(fn):
    @wraps(fn)
    def wrapped(conn, doc_id, *args, **kwargs):
        with document(doc_id):
            return fn(conn,doc_id,*args,**kwargs)
    return wrapped
