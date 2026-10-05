"""Thin subprocess wrapper around the rsync CLI, optimized for local/NFS-mounted
paths (both source and destination are ordinary filesystem paths on this host
— no remote-shell `user@host:` syntax, no bandwidth limiting, no compression).

All functions shell out to the `rsync` binary (see settings.RSYNC_BIN). Any
non-zero exit that isn't a recognized "partial transfer" code raises
RsyncError with rsync's stderr attached; callers decide how to handle it —
this module never logs or swallows failures itself.
"""

import os
import re
import subprocess
import time

from settings import RSYNC_BIN

# rsync exit codes treated as a partial (not fatal) transfer — some files were
# skipped due to errors (23) or vanished mid-run (24). Matches nas_sync_agent's
# handling of these same codes.
PARTIAL_TRANSFER_CODES = (23, 24)

_STATS_FILES_RE = re.compile(r"Number of regular files transferred:\s*([\d,]+)")
# With the single --human-readable flag this module always passes, rsync's
# "Total transferred file size" switches from comma-grouped digits to a
# decimal + unit-letter form (e.g. "10.49M bytes") once the total is large
# enough — confirmed empirically these unit letters are 1000-based (decimal/
# SI), not 1024-based like --info=progress2's rate suffixes.
_STATS_BYTES_RE = re.compile(r"Total transferred file size:\s*([\d,]+\.?\d*)\s*([KMGT])?\s*bytes")
_STATS_SIZE_MULTIPLIERS = {None: 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}

# rsync --info=progress2's single running overall-progress line, e.g.:
#   "      1,234,567  43%   12.34MB/s    0:00:10 (xfr#5, to-chk=120/200)"
# or, with --human-readable, the leading byte count itself becomes a decimal
# + unit (e.g. "2.00M") instead of a comma-grouped integer — only percent and
# rate are parsed, so the leading byte-count token is matched loosely.
_PROGRESS_RE = re.compile(
    r"^\s*\S+\s+(\d+)%\s+([\d.]+)([KMGT]?B)/s"
)
_RATE_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

# rsync --itemize-changes lines start with an 11-char change code (e.g.
# ">f.st....." or "*deleting") followed by a space and the path. A tree with
# no differences prints nothing.
_ITEMIZE_LINE_RE = re.compile(r"^([><ch.*][fdLDS*].{9}|\*deleting) ")


class RsyncError(RuntimeError):
    """Raised when rsync exits with a code other than 0 or a partial-transfer code."""

    def __init__(self, message, returncode=None):
        super().__init__(message)
        self.returncode = returncode


def _run(args, timeout=None):
    """Run `rsync <args>` and return the CompletedProcess. Raises RsyncError
    only for genuine failures (FileNotFoundError, or exit codes outside
    0/23/24) — partial-transfer codes are returned to the caller to handle."""
    cmd = [RSYNC_BIN] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as e:
        raise RsyncError(f"rsync binary not found: {RSYNC_BIN}") from e
    except subprocess.TimeoutExpired as e:
        raise RsyncError(f"rsync timed out after {timeout}s") from e

    if result.returncode != 0 and result.returncode not in PARTIAL_TRANSFER_CODES:
        raise RsyncError(
            f"rsync failed (exit {result.returncode}): {result.stderr.strip()}",
            returncode=result.returncode,
        )
    return result


def _parse_progress_line(line):
    """Parse one --info=progress2 overall-progress line. Returns
    {"percent": int, "bytes_per_second": float} or None if the line doesn't match."""
    m = _PROGRESS_RE.match(line)
    if not m:
        return None
    percent = int(m.group(1))
    rate_value = float(m.group(2))
    rate_unit = m.group(3)
    bytes_per_second = rate_value * _RATE_MULTIPLIERS.get(rate_unit, 1)
    return {"percent": percent, "bytes_per_second": bytes_per_second}


def _run_with_progress(args, timeout=None, on_progress=None):
    """Like _run(), but streams stdout line-by-line so on_progress(dict) can be
    called for each --info=progress2 line as rsync emits it, instead of only
    seeing output after the whole transfer completes."""
    cmd = [RSYNC_BIN] + list(args)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as e:
        raise RsyncError(f"rsync binary not found: {RSYNC_BIN}") from e

    stdout_lines = []
    timed_out = False
    for line in proc.stdout:
        stdout_lines.append(line)
        parsed = _parse_progress_line(line)
        if parsed and on_progress:
            on_progress(parsed)
        if timeout and (time.monotonic() - start) > timeout:
            proc.kill()
            timed_out = True
            break

    stderr = proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
    returncode = proc.wait()

    if timed_out:
        raise RsyncError(f"rsync timed out after {timeout}s")
    if returncode != 0 and returncode not in PARTIAL_TRANSFER_CODES:
        raise RsyncError(
            f"rsync failed (exit {returncode}): {stderr.strip()}",
            returncode=returncode,
        )
    return subprocess.CompletedProcess(cmd, returncode, stdout="".join(stdout_lines), stderr=stderr)


def build_exclude_args(exclude_patterns):
    """Return a list of --exclude=PATTERN flags for each pattern."""
    return [f"--exclude={pattern}" for pattern in (exclude_patterns or [])]


def build_base_args(delete=True, preserve_owner=False, preserve_group=False,
                     preserve_permissions=True, numeric_ids=True, whole_file=True,
                     hard_links=False, timeout=300, extra_args=None):
    """
    Build the shared rsync flag set used for an actual sync run.

    Defaults are chosen for local/NFS-mounted paths on a trusted LAN:
      - no --owner/--group (avoids uid/gid mismatches between NFS clients —
        the same reasoning as nas_sync_agent's --no-owner/--no-group)
      - --numeric-ids (skips NFS name lookups for any ids that are kept)
      - --whole-file (skips rsync's delta/checksum algorithm; with fast local
        or NFS I/O and no bandwidth constraint, a straight copy is cheaper
        than computing rolling checksums)
      - no compression, no bandwidth limiting (not needed on a LAN)
    """
    args = ["--recursive", "--links", "--times", "--devices", "--specials"]
    if preserve_permissions:
        args.append("--perms")
    if preserve_owner:
        args.append("--owner")
    if preserve_group:
        args.append("--group")
    if numeric_ids:
        args.append("--numeric-ids")
    if hard_links:
        args.append("--hard-links")
    args.append("--whole-file" if whole_file else "--no-whole-file")
    if delete:
        args += ["--delete", "--delete-excluded"]
    args += [
        "--partial",
        "--partial-dir=.rsync-partial",
        "--sparse",
        "--force",
        "--human-readable",
    ]
    if timeout:
        args.append(f"--timeout={timeout}")
    args += list(extra_args or [])
    return args


def _parse_stats(stdout):
    """Pull file/byte counts out of rsync's --stats block. Returns (files, bytes)."""
    files_match = _STATS_FILES_RE.search(stdout)
    bytes_match = _STATS_BYTES_RE.search(stdout)
    files = int(files_match.group(1).replace(",", "")) if files_match else 0
    if bytes_match:
        value = float(bytes_match.group(1).replace(",", ""))
        unit = bytes_match.group(2)
        total_bytes = int(round(value * _STATS_SIZE_MULTIPLIERS[unit]))
    else:
        total_bytes = 0
    return files, total_bytes


def sync(source, destination, exclude_patterns=None, dry_run=False,
         base_args=None, io_timeout=300, max_duration=None, on_progress=None):
    """
    Run one rsync mirror pass from source into destination.

    If on_progress is given, it's called with {"percent": int,
    "bytes_per_second": float} for each --info=progress2 line rsync emits
    while the transfer is running (best-effort; throttling/reporting to the
    dashboard is the caller's responsibility, not this module's).

    Returns a dict: {"partial": bool, "files_transferred": int,
    "bytes_transferred": int, "returncode": int}.
    Raises RsyncError on a genuine (non-partial) failure.
    """
    args = list(base_args) if base_args is not None else build_base_args(timeout=io_timeout)
    args += build_exclude_args(exclude_patterns)
    args.append("--stats")
    if on_progress is not None:
        args.append("--info=progress2")
    if dry_run:
        args.append("--dry-run")
    args.append(source.rstrip("/") + "/")
    args.append(destination.rstrip("/") + "/")

    # io_timeout (rsync's own --timeout) already aborts stalled/idle transfers;
    # it must NOT also bound the outer subprocess, or a large-but-actively-
    # progressing transfer gets killed early. max_duration is an independent,
    # opt-in hard wall-clock cap for the whole run (None = unbounded).
    if on_progress is not None:
        result = _run_with_progress(args, timeout=max_duration, on_progress=on_progress)
    else:
        result = _run(args, timeout=max_duration)
    files_transferred, bytes_transferred = _parse_stats(result.stdout)
    return {
        "partial": result.returncode in PARTIAL_TRANSFER_CODES,
        "files_transferred": files_transferred,
        "bytes_transferred": bytes_transferred,
        "returncode": result.returncode,
    }


def count_diffs(source, destination, exclude_patterns=None, checksum=False,
                 delete=True, io_timeout=300, max_duration=None):
    """
    Low-cost (checksum=False) or thorough (checksum=True) verification: runs
    an rsync --dry-run pass and counts how many items rsync would still
    change. checksum=False compares size/mtime only (no file content read —
    about as cheap as the sync's own scan); checksum=True reads and compares
    actual file content on both sides (slow).

    Returns the mismatch count (0 means source and destination fully agree).
    """
    args = ["--recursive", "--links", "--times", "--dry-run", "--itemize-changes"]
    if io_timeout:
        args.append(f"--timeout={io_timeout}")
    if delete:
        args += ["--delete", "--delete-excluded"]
    if checksum:
        args.append("--checksum")
    args += build_exclude_args(exclude_patterns)
    args.append(source.rstrip("/") + "/")
    args.append(destination.rstrip("/") + "/")

    result = _run(args, timeout=max_duration)
    return sum(1 for line in result.stdout.splitlines() if _ITEMIZE_LINE_RE.match(line))


def check_path_accessible(path):
    """Return True if path exists and is a readable/listable directory."""
    if not os.path.isdir(path):
        return False
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def free_space_bytes(path):
    """Return free space (bytes) on the filesystem containing path, or None."""
    try:
        return os.statvfs(path).f_bavail * os.statvfs(path).f_frsize
    except OSError:
        return None
