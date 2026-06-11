import os
import logging
import threading

_CM_DIR_CANDIDATES = ['/run/napprox', '/var/run/napprox', '/tmp/napprox-cm']
_cm_dir = None
_cm_dir_lock = threading.Lock()


def _ensure_cm_dir():
    global _cm_dir
    if _cm_dir:
        return _cm_dir
    with _cm_dir_lock:
        if _cm_dir:
            return _cm_dir
        for cand in _CM_DIR_CANDIDATES:
            try:
                os.makedirs(cand, mode=0o700, exist_ok=True)
                probe = os.path.join(cand, '.write-test')
                with open(probe, 'w') as f:
                    f.write('1')
                os.unlink(probe)
                _cm_dir = cand
                return _cm_dir
            except Exception as e:
                logging.debug(f"[ssh_pool] cannot use {cand}: {e}")
        return None


def controlmaster_args(host, user, persist_seconds=300):
    d = _ensure_cm_dir()
    if not d:
        return []
    socket_path = os.path.join(d, 'cm-%r@%h:%p')
    return [
        '-o', 'ControlMaster=auto',
        '-o', f'ControlPath={socket_path}',
        '-o', f'ControlPersist={persist_seconds}',
    ]
