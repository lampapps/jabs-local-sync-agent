"""Agent-specific settings and configuration constants."""

import os
from datetime import timedelta

import yaml
from dotenv import load_dotenv

VERSION = "0.1.1"

# Type of agent, reported to the dashboard so it can distinguish agent kinds
# on the Agents page, and used to key its per-agent-type retention policy.
AGENT_TYPE = "Local Sync"

# --- Environment Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars

# Environment mode (development/production)
ENV_MODE = os.environ.get("ENV_MODE", "production")

# API key this agent uses to authenticate to the dashboard (sent as the
# X-API-Key header on every request). Generated per-agent when the agent is
# registered on the dashboard's Agents page.
AGENT_KEY = os.environ.get("JABS_AGENT_KEY")

# Path to the rsync binary; override if it's not on PATH.
RSYNC_BIN = os.environ.get("RSYNC_BIN") or "rsync"

# --- Application Configuration ---
LOCK_DIR = os.path.join(BASE_DIR, "locks")
CLI_SCRIPT = os.path.join(BASE_DIR, "backup.py")

# --- Config Configuration ---
CONFIG_DIR = os.path.join(BASE_DIR, "config")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
GLOBAL_CONFIG_PATH = os.path.join(CONFIG_DIR, "global.yaml")

# --- Data / Logging Configuration ---
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MAX_LOG_LINES = 10000

# --- Scheduler Configuration ---
SCHEDULE_TOLERANCE = timedelta(seconds=15)  # buffer for cron job execution
SCHEDULER_STATUS_FILE = os.path.join(LOG_DIR, "scheduler.status")

# --- Global config / email (loaded once; empty dict if global.yaml not set up yet) ---
try:
    with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        GLOBAL_CONFIG = yaml.safe_load(f) or {}
except (FileNotFoundError, yaml.YAMLError):
    GLOBAL_CONFIG = {}

EMAIL_CONFIG = GLOBAL_CONFIG.get("email", {})
