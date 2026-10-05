#!/usr/bin/env python3
"""Cron-driven scheduler for local_sync_agent. Run this from cron (e.g. every
15 minutes); it checks each config/jobs/*.yaml's schedules[] and runs any job
that's due, in-process (no per-check subprocess spawn — backup.py itself is
only invoked as a subprocess when a job is actually due)."""

import glob
import importlib.util
import os
import time
from datetime import datetime

import yaml
from croniter import croniter
from dotenv import load_dotenv

from logger import setup_logger, trim_all_logs
from monitoring_client import send_scheduler_check, send_job_schedule
from settings import (
    CLI_SCRIPT, ENV_PATH, JOBS_DIR, LOG_DIR, SCHEDULE_TOLERANCE, SCHEDULER_STATUS_FILE,
)

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars

os.makedirs(LOG_DIR, exist_ok=True)
logger = setup_logger("scheduler", log_file="scheduler.log")


def load_yaml_config(path):
    """Load a YAML configuration file and return its contents, or None on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {path}")
        return None
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {path}: {e}")
        return None
    except OSError as e:
        logger.error(f"Error loading config {path}: {e}")
        return None


def get_job_configs():
    """Return a list of job config file paths."""
    return glob.glob(os.path.join(JOBS_DIR, "*.yaml"))


def should_trigger(cron_expr, now):
    """Return (matched, prev_run_time) — True if cron_expr's previous run time
    is within SCHEDULE_TOLERANCE of now."""
    try:
        cron = croniter(cron_expr, now)
        prev_run_time = cron.get_prev(datetime)
        next_run_time = cron.get_next(datetime)

        time_since_prev = now - prev_run_time
        time_until_next = next_run_time - now

        return (time_since_prev < SCHEDULE_TOLERANCE) and (time_since_prev < time_until_next), prev_run_time
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid cron expression '{cron_expr}': {e}")
        return False, None


def update_status_file():
    """Update the scheduler status file with the current timestamp."""
    try:
        with open(SCHEDULER_STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.error(f"Failed to update status file {SCHEDULER_STATUS_FILE}: {e}")


def call_run_job(job_path):
    """Call backup.py's run_job() directly (via importlib) to avoid a subprocess
    spawn per due job, matching snapshot_agent/file_backup_agent's convention."""
    try:
        spec = importlib.util.spec_from_file_location("backup", CLI_SCRIPT)
        backup_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup_module)
        return backup_module.run_job(job_path)
    except (ImportError, AttributeError, OSError) as e:
        logger.error(f"Error running job {job_path}: {e}", exc_info=True)
        return False


def main():
    logger.debug("--- Scheduler check started ---")
    now = datetime.now()

    config_files = get_job_configs()
    if not config_files:
        logger.info(f"No job configs found in {JOBS_DIR}")
        send_scheduler_check(0)
        update_status_file()
        return

    triggered = []
    for config_path in config_files:
        job_name_from_file = os.path.splitext(os.path.basename(config_path))[0]
        config = load_yaml_config(config_path)
        if not config:
            logger.warning(f"Config file is empty or invalid: {config_path}")
            continue

        job_name = config.get("job_name", job_name_from_file)
        schedules = config.get("schedules", [])
        if not schedules:
            logger.debug(f"No schedules defined in {config_path}")
            continue

        enabled_crons = [s.get("cron") for s in schedules if s.get("enabled", False) and s.get("cron")]
        if enabled_crons:
            send_job_schedule(job_name, ",".join(enabled_crons))

        for schedule in schedules:
            if not schedule.get("enabled", False):
                continue
            cron_expr = schedule.get("cron")
            if not cron_expr:
                continue

            matched, prev_run_time = should_trigger(cron_expr, now)
            if matched:
                logger.info(f"Job '{job_name}' is due (matched {cron_expr} at {prev_run_time})")
                success = call_run_job(config_path)
                triggered.append((job_name, success))

    trim_all_logs()
    send_scheduler_check(len(triggered))
    update_status_file()
    logger.debug(f"--- Scheduler check finished: {len(triggered)} job(s) triggered ---")


if __name__ == "__main__":
    main()
