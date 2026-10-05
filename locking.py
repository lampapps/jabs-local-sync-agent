"""Process-lock helpers to prevent overlapping cron runs of the same job.

local_sync_agent has no local DB (the destination filesystem itself is the
source of truth), so this only needs a plain portalocker-based lock file per
job, matching snapshot_agent's locking.py.
"""

import json
import os
import socket
import sys
import time

import portalocker


def acquire_lock(lock_path):
    """
    Acquire an exclusive lock on the given file path using portalocker.

    Returns an open file handle with the lock held.
    Raises RuntimeError if the lock cannot be acquired.
    """
    lock_dir = os.path.dirname(lock_path)
    if lock_dir and not os.path.exists(lock_dir):
        try:
            os.makedirs(lock_dir, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Could not create lock directory: {lock_dir}") from e

    # Clear a stale lock file (>2 hours old, or unreadable) before trying to lock.
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as existing:
                lock_info = json.load(existing)
            if time.time() - lock_info.get("created_at", 0) > 7200:
                os.remove(lock_path)
        except (OSError, json.JSONDecodeError, ValueError):
            try:
                os.remove(lock_path)
            except OSError:
                pass

    try:
        lock_file = open(lock_path, "a+", encoding="utf-8")
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)

        lock_file.seek(0)
        lock_file.truncate()
        lock_info = {
            "pid": os.getpid(),
            "created_at": time.time(),
            "hostname": socket.gethostname(),
            "command": " ".join(["python"] + [os.path.basename(a) for a in sys.argv]),
        }
        json.dump(lock_info, lock_file)
        lock_file.flush()
        return lock_file

    except portalocker.exceptions.LockException as e:
        msg = "Lock exists but details couldn't be read"
        try:
            with open(lock_path, "r", encoding="utf-8") as existing_lock:
                lock_info = json.load(existing_lock)
            pid = lock_info.get("pid", "unknown")
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lock_info.get("created_at", 0)))
            msg = f"Lock held by PID {pid} since {created}"
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        raise RuntimeError(f"Could not acquire lock: {lock_path}. {msg}") from e
    except OSError as e:
        raise RuntimeError(f"Error acquiring lock: {lock_path}") from e


def release_lock(lock_file):
    """Release the lock, close the file, and remove the lock file."""
    lock_path = lock_file.name
    try:
        portalocker.unlock(lock_file)
    except (portalocker.exceptions.LockException, OSError):
        pass
    finally:
        lock_file.close()
    try:
        os.remove(lock_path)
    except OSError:
        pass
