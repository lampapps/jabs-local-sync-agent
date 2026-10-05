#!/usr/bin/env python3
"""JABS Local Sync Agent — CLI entry point for running a single sync job.

Usage:
    python backup.py --job <name-or-path> [--dry-run]
    python backup.py --check [--job <name-or-path>]        # quick verify, no sync
    python backup.py --full-check [--job <name-or-path>]   # deep verify, no sync

    python backup.py --job example                  # config/jobs/example.yaml
    python backup.py --job config/jobs/example.yaml
    python backup.py --job example --dry-run         # rsync --dry-run, still reports to JABS

Each job config may list multiple `paths` (source/destination pairs); each
pair is synced and reported to the dashboard independently, so a failure in
one pair never stops the others in the same job.

A cheap quick verify (rsync --dry-run comparing size/mtime only — no file
content is read) runs automatically after every pair's sync unless disabled
with `verify_after_sync: false` in global.yaml or the job config. `--full-check`
reads and compares actual file content and is slow, so it's manual-only — see
jabs-agent.sh's `check`/`check-deep` commands.
"""

import argparse
import os
import socket
import sys
import time
import uuid

import yaml
from dotenv import load_dotenv

import rsync_client
from rsync_client import RsyncError
from emailer import process_email_event
from locking import acquire_lock, release_lock
from logger import setup_logger
from monitoring_client import (
    send_sync_start, send_sync_stage, send_sync_complete, send_check_result,
    send_sync_progress,
)
from settings import CONFIG_DIR, ENV_PATH, GLOBAL_CONFIG_PATH, JOBS_DIR, LOCK_DIR

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars

cli_logger = setup_logger("backup")


def load_yaml_config(path):
    """Load a YAML config file. Raises OSError/yaml.YAMLError on failure — caller handles."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_job_path(job):
    """Resolve --job value: a path to a .yaml file, or a bare job name under config/jobs/."""
    if job.endswith(".yaml") and os.path.exists(job):
        return job
    candidate = os.path.join(JOBS_DIR, f"{job}.yaml")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(f"Job config not found: {job} (looked for {candidate})")


def get_all_job_paths():
    """Return every config/jobs/*.yaml path, for --check/--full-check over all jobs."""
    if not os.path.isdir(JOBS_DIR):
        return []
    return sorted(
        os.path.join(JOBS_DIR, f) for f in os.listdir(JOBS_DIR) if f.endswith(".yaml")
    )


def build_config(job_path):
    """Load and merge global.yaml + job yaml into one effective config dict."""
    job_config = load_yaml_config(job_path)
    try:
        global_config = load_yaml_config(GLOBAL_CONFIG_PATH)
    except (OSError, yaml.YAMLError) as e:
        cli_logger.warning(f"Could not load global config: {e}")
        global_config = {}

    config = dict(job_config)

    exclude = list(job_config.get("exclude") or [])
    if global_config.get("use_common_exclude", True):
        common_exclude_path = os.path.join(CONFIG_DIR, "common_exclude.yaml")
        if os.path.exists(common_exclude_path):
            exclude = list(load_yaml_config(common_exclude_path) or []) + exclude
    config["exclude"] = exclude

    for key, value in global_config.items():
        if key == "email":
            continue
        if key not in config or config[key] is None:
            config[key] = value

    return config


def _sanitize(name):
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def _rsync_options_from_config(config):
    return dict(
        delete=config.get("delete", True),
        preserve_owner=config.get("preserve_owner", False),
        preserve_group=config.get("preserve_group", False),
        preserve_permissions=config.get("preserve_permissions", True),
        numeric_ids=config.get("numeric_ids", True),
        whole_file=config.get("whole_file", True),
        hard_links=config.get("hard_links", False),
        timeout=config.get("io_timeout", 300),
        extra_args=config.get("extra_args"),
    )


def _path_label(job_name, path_config):
    source = path_config.get("source") or ""
    name = path_config.get("name") or os.path.basename(source.rstrip("/")) or "path"
    return f"{job_name}:{name}"


def sync_one_path(job_name, path_config, job_exclude, config, dry_run=False, job_run_id=None):
    """Run one source/destination pair from a job's `paths` list. Returns True
    on success (including a partial-transfer warning), False on failure."""
    source = path_config.get("source")
    destination = path_config.get("destination")
    label = _path_label(job_name, path_config)
    logger = setup_logger(label)

    if not source or not destination:
        logger.error("Path entry missing 'source' or 'destination'")
        return False

    exclude = job_exclude + list(path_config.get("exclude") or [])

    run_id = str(uuid.uuid4())
    target_id = label
    # NOTE: target_label must stay stable across every run of this pair (it's
    # the human-friendly display name for the job target) — do not embed a
    # per-run timestamp here, or the dashboard's grouping/deep-link filtering
    # will treat each run as belonging to a different target.
    target_label = label
    start_time = time.time()

    send_sync_start(job_name, target_id, target_label, source=source,
                     destination=destination, run_id=run_id, job_run_id=job_run_id, dry_run=dry_run)
    logger.info(f"###### Starting sync: {label} ({socket.gethostname()}) ######")
    logger.info(f"  Source : {source}")
    logger.info(f"  Dest   : {destination}")

    if not rsync_client.check_path_accessible(source):
        msg = f"Source directory not accessible: {source}"
        logger.error(msg)
        duration = time.time() - start_time
        send_sync_complete(job_name, target_id, target_label, duration,
                            run_id=run_id, success=False, error_message=msg, dry_run=dry_run)
        process_email_event(
            "error", f"JABS local sync FAILED: {label}",
            f"{'[DRY RUN] ' if dry_run else ''}Sync of {label} failed: {msg}",
        )
        return False

    if not dry_run:
        os.makedirs(destination, exist_ok=True)
    elif not rsync_client.check_path_accessible(destination):
        logger.warning(f"Destination does not exist yet (dry-run, not creating it): {destination}")

    min_free_bytes = config.get("min_free_bytes", 0)
    if min_free_bytes and not dry_run:
        free = rsync_client.free_space_bytes(destination)
        if free is not None and free < min_free_bytes:
            msg = (
                f"Destination has only {free / (1024 ** 3):.1f} GiB free "
                f"(minimum {min_free_bytes / (1024 ** 3):.1f} GiB): {destination}"
            )
            logger.error(msg)
            duration = time.time() - start_time
            send_sync_complete(job_name, target_id, target_label, duration,
                                run_id=run_id, success=False, error_message=msg, dry_run=dry_run)
            process_email_event(
                "error", f"JABS local sync FAILED: {label}",
                f"{'[DRY RUN] ' if dry_run else ''}Sync of {label} failed: {msg}",
            )
            return False

    send_sync_stage(job_name, target_id, target_label, "Running sync",
                     run_id=run_id, dry_run=dry_run)

    io_timeout = config.get("io_timeout", 300)
    max_duration = config.get("max_duration") or None
    base_args = rsync_client.build_base_args(**_rsync_options_from_config(config))

    # Best-effort live progress: throttled independently for the dashboard
    # POST (wall-clock, so long jobs don't flood the API) and the local log
    # (10%-decile crossings, so long jobs don't flood the log file either).
    progress_state = {"last_post": 0.0, "last_decile": -1}

    def on_progress(parsed):
        pct = parsed["percent"]
        bps = parsed["bytes_per_second"]
        decile = pct // 10
        if decile > progress_state["last_decile"]:
            progress_state["last_decile"] = decile
            logger.info(f"Progress: {pct}% ({bps / (1024 ** 2):.1f} MB/s)")
        now = time.time()
        if now - progress_state["last_post"] >= 5:
            progress_state["last_post"] = now
            send_sync_progress(job_name, target_id, run_id=run_id,
                                percent_complete=pct, bytes_per_second=bps)

    try:
        result = rsync_client.sync(
            source, destination, exclude_patterns=exclude, dry_run=dry_run,
            base_args=base_args, io_timeout=io_timeout, max_duration=max_duration,
            on_progress=on_progress,
        )
    except RsyncError as e:
        duration = time.time() - start_time
        logger.error(f"Sync failed: {e}")
        send_sync_complete(job_name, target_id, target_label, duration,
                            run_id=run_id, success=False, error_message=str(e), dry_run=dry_run)
        process_email_event(
            "error", f"JABS local sync FAILED: {label}",
            f"{'[DRY RUN] ' if dry_run else ''}Sync of {label} failed after {duration:.1f}s: {e}",
        )
        return False

    duration = time.time() - start_time
    if result["partial"]:
        logger.warning(f"Sync completed with warnings (partial transfer, exit {result['returncode']})")
    else:
        logger.info(
            f"Sync complete: files={result['files_transferred']} "
            f"bytes={result['bytes_transferred']} duration={duration:.1f}s"
        )

    send_sync_complete(
        job_name, target_id, target_label, duration, run_id=run_id,
        files_backed_up=result["files_transferred"], bytes_backed_up=result["bytes_transferred"],
        success=True, dry_run=dry_run,
    )
    process_email_event(
        "backup_complete", f"JABS local sync complete: {label}",
        f"{'[DRY RUN] ' if dry_run else ''}Sync of {label} completed in {duration:.1f}s "
        f"({result['files_transferred']} files transferred).",
    )

    if config.get("verify_after_sync", True) and not dry_run:
        try:
            logger.info("Running quick verify (size/mtime only)")
            send_sync_stage(job_name, target_id, target_label,
                             "Verifying (quick)", run_id=run_id, dry_run=dry_run)
            mismatches = rsync_client.count_diffs(
                source, destination, exclude_patterns=exclude, checksum=False,
                delete=config.get("delete", True), io_timeout=io_timeout, max_duration=max_duration,
            )
            if mismatches == 0:
                logger.info("Quick verify passed")
                send_check_result(job_name, target_id, target_label, run_id, success=True)
            else:
                logger.warning(f"Quick verify found {mismatches} mismatched item(s)")
                send_check_result(job_name, target_id, target_label, run_id,
                                   success=False, mismatch_count=mismatches)
                process_email_event(
                    "error", f"JABS local sync quick verify found mismatches: {label}",
                    f"Quick verify after sync of {label} found {mismatches} mismatched item(s).\n"
                    f"Source: {source}\nDestination: {destination}",
                )
        except RsyncError as e:
            # A verify failure doesn't invalidate the sync that already completed.
            logger.error(f"Quick verify FAILED to run: {e}")
            send_check_result(job_name, target_id, target_label, run_id,
                               success=False, error_message=str(e))

    return True


def run_job(job_path, dry_run=False):
    """Run every path pair configured in one job. Returns True only if every
    pair succeeded (a pair-level failure is logged/reported but does not stop
    the remaining pairs in the job)."""
    try:
        config = build_config(job_path)
    except (OSError, yaml.YAMLError) as e:
        cli_logger.error(f"Failed to load job config {job_path}: {e}")
        return False
    job_name = config.get("job_name", os.path.splitext(os.path.basename(job_path))[0])
    logger = setup_logger(job_name)

    paths = config.get("paths") or []
    if not paths:
        logger.error("No paths configured for this job (set 'paths' in the job config)")
        return False

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, f"{_sanitize(job_name)}.lock")
    try:
        lock_file = acquire_lock(lock_path)
    except RuntimeError as e:
        logger.error(f"Could not start job: {e}")
        return False

    job_exclude = list(config.get("exclude") or [])
    job_run_id = str(uuid.uuid4())

    try:
        all_ok = True
        for path_config in paths:
            ok = sync_one_path(job_name, path_config, job_exclude, config,
                                dry_run=dry_run, job_run_id=job_run_id)
            all_ok = all_ok and ok
        return all_ok
    finally:
        release_lock(lock_file)


def run_check(job_path=None, deep=False):
    """Run a quick (or deep) verify pass against one job or every configured
    job's paths, with no sync performed. Returns True only if every checked
    pair reports zero mismatches."""
    job_paths = [job_path] if job_path else get_all_job_paths()
    if not job_paths:
        print(f"No job configs found in {JOBS_DIR}")
        return False

    overall_ok = True
    for jp in job_paths:
        try:
            config = build_config(jp)
        except (OSError, yaml.YAMLError) as e:
            print(f"ERROR: Could not load {jp}: {e}")
            overall_ok = False
            continue

        job_name = config.get("job_name", os.path.splitext(os.path.basename(jp))[0])
        job_exclude = list(config.get("exclude") or [])
        for path_config in (config.get("paths") or []):
            source = path_config.get("source")
            destination = path_config.get("destination")
            label = _path_label(job_name, path_config)
            exclude = job_exclude + list(path_config.get("exclude") or [])

            if not source or not destination:
                print(f"SKIP  {label}  (missing source/destination)")
                overall_ok = False
                continue
            if not rsync_client.check_path_accessible(source) or not rsync_client.check_path_accessible(destination):
                print(f"SKIP  {label}  (source or destination not accessible)")
                overall_ok = False
                continue

            kind = "deep (reads file content, slow)" if deep else "quick (size/mtime only)"
            print(f"Checking {label} [{kind}] ...")
            try:
                mismatches = rsync_client.count_diffs(
                    source, destination, exclude_patterns=exclude, checksum=deep,
                    delete=config.get("delete", True), io_timeout=config.get("io_timeout", 300),
                    max_duration=config.get("max_duration") or None,
                )
            except RsyncError as e:
                print(f"  FAILED to run check: {e}")
                overall_ok = False
                continue

            if mismatches == 0:
                print("  OK — 0 mismatched items")
            else:
                print(f"  MISMATCH — {mismatches} item(s) differ")
                overall_ok = False

    return overall_ok


def main():
    parser = argparse.ArgumentParser(description="JABS Local Sync Agent CLI")
    parser.add_argument("--job", help="Job name or path to job config YAML")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run through to rsync")
    parser.add_argument("--check", action="store_true",
                         help="Run a quick verify (size/mtime only), no sync; all jobs unless --job is given")
    parser.add_argument("--full-check", action="store_true",
                         help="Run a deep verify that reads file content (slow), no sync; "
                              "all jobs unless --job is given")
    args = parser.parse_args()

    if args.check or args.full_check:
        job_path = None
        if args.job:
            try:
                job_path = resolve_job_path(args.job)
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
                sys.exit(2)
        ok = run_check(job_path=job_path, deep=args.full_check)
        sys.exit(0 if ok else 1)

    if not args.job:
        parser.error("--job is required (or use --check/--full-check to verify without syncing)")

    try:
        job_path = resolve_job_path(args.job)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

    success = run_job(job_path, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
