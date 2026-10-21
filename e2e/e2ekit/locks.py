"""fcntl slot locks: project, build, simulator pool.

flock locks die with the process that holds them, so a crashed engine can
never wedge a slot; the lock files themselves are only names.
"""

import fcntl
import os
import time

from . import config, store


class Lock(object):
    def __init__(self, path):
        self.path = path
        self.fd = None

    def try_acquire(self, note=""):
        if self.fd is not None:
            return True
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            # `e2e clean` may have unlinked the file between our open and
            # flock; a lock on an orphaned inode guards nothing, so retry.
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except OSError:
                same = False
            if same:
                break
            os.close(fd)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d %s\n" % (os.getpid(), note)).encode())
        self.fd = fd
        return True

    def acquire(self, note="", should_stop=None, poll=0.25):
        while not self.try_acquire(note):
            if should_stop and should_stop():
                return False
            time.sleep(poll)
        return True

    def release(self):
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

    def held(self):
        return self.fd is not None


def project_lock(project):
    return Lock(os.path.join(store.engine_dir(), "project-%s.lock" % project))


def build_lock():
    return Lock(os.path.join(store.engine_dir(), "build.lock"))


def sim_lock(k):
    return Lock(os.path.join(store.engine_dir(), "sim-%d.lock" % k))


def acquire_sims(want, note=""):
    """Grab up to `want` free machine-wide sim slots, never more than the
    project cap. Returns [(slot_number, Lock)], possibly empty."""
    want = max(0, min(want, config.LIMITS["sims"][1]))
    got = []
    for k in range(1, config.MACHINE_SIMS + 1):
        if len(got) >= want:
            break
        lk = sim_lock(k)
        if lk.try_acquire(note):
            got.append((k, lk))
    return got


def is_busy(path):
    """True when someone holds the lock at path (probe without keeping it)."""
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def slot_usage():
    eng = store.engine_dir()
    return {
        "build_busy": is_busy(os.path.join(eng, "build.lock")),
        "sims_busy": [k for k in range(1, config.MACHINE_SIMS + 1)
                      if is_busy(os.path.join(eng, "sim-%d.lock" % k))],
        "sims_cap": config.MACHINE_SIMS,
    }


def clean_stale():
    """Unlink lock files nobody holds. Returns names removed."""
    eng = store.engine_dir()
    removed = []
    for name in sorted(os.listdir(eng)):
        if not name.endswith(".lock"):
            continue
        lk = Lock(os.path.join(eng, name))
        if lk.try_acquire("clean"):
            try:
                os.unlink(lk.path)
                removed.append(name)
            finally:
                lk.release()
    return removed
