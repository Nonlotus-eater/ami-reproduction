"""Kernel-held request ownership shared across Linux PID namespaces.

Lock files deliberately remain after close: unlinking can split contenders
across different inodes and turn a live request into an apparently dead one.
"""
import fcntl
import hashlib
import os
from pathlib import Path

POLICY = 'request_file_lock_v1'


def lease_path(ledger, request_id):
    ledger = Path(ledger)
    key = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
    return ledger.with_name(ledger.name + '.leases') / (key + '.lock')


class RequestLease:
    def __init__(self, ledger, request_id):
        self.path = lease_path(ledger, request_id)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def request_alive(ledger, request_id):
    """True if locked, False if released, None if ownership cannot be proved."""
    try:
        fd = os.open(lease_path(ledger, request_id), os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
