"""Tracks running job threads and cancellation events."""
import threading

_lock:   threading.Lock         = threading.Lock()
_threads: dict                  = {}
_events:  dict                  = {}


def register(job_id: str, thread: threading.Thread) -> None:
    with _lock:
        _threads[job_id] = thread
        _events[job_id]  = threading.Event()


def unregister(job_id: str) -> None:
    with _lock:
        _threads.pop(job_id, None)
        _events.pop(job_id, None)


def request_cancel(job_id: str) -> bool:
    """Signal cancellation. Returns True if the job is registered and was signalled."""
    with _lock:
        ev = _events.get(job_id)
        if ev:
            ev.set()
            return True
        return False


def is_cancel_requested(job_id: str) -> bool:
    with _lock:
        ev = _events.get(job_id)
        return ev.is_set() if ev else False


def thread_alive(job_id: str) -> bool:
    with _lock:
        t = _threads.get(job_id)
        return t.is_alive() if t else False
