"""Logging utilities for JABS Local Sync Agent."""

import logging
import os
import glob
from settings import LOG_DIR, MAX_LOG_LINES, ENV_MODE

_logger = logging.getLogger(__name__)


class JobNameFormatter(logging.Formatter):
    """Custom formatter to include the job name in every log message."""
    def format(self, record):
        original_msg = record.msg
        if hasattr(record, "job_name"):
            record.msg = f"{record.job_name} - {original_msg}"
        try:
            return super().format(record)
        finally:
            record.msg = original_msg


def setup_logger(job_name, log_file="sync.log"):
    """
    Set up a logger with the job name included in every message.
    :param job_name: Name of the job to include in log messages.
    :param log_file: Name or path to the log file.
    :return: A logger instance.
    """
    if not os.path.isabs(log_file):
        log_file = os.path.join(LOG_DIR, log_file)

    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(job_name)
    if ENV_MODE == "development":
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if not logger.handlers:
        file_handler = logging.FileHandler(log_file)
        stream_handler = logging.StreamHandler()

        formatter = JobNameFormatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logging.LoggerAdapter(logger, {"job_name": job_name})


def trim_log_file(log_path, max_lines):
    """Trim the log file to the last max_lines lines."""
    try:
        if not os.path.exists(log_path):
            return
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            lines_to_keep = lines[-max_lines:]
            with open(log_path, 'w', encoding='utf-8') as f:
                f.writelines(lines_to_keep)
    except OSError as e:
        _logger.error("Error trimming log file %s: %s", log_path, e)


def trim_all_logs():
    """Trim all log files in the log directory to MAX_LOG_LINES."""
    for log_file in glob.glob(os.path.join(LOG_DIR, "*.log")):
        trim_log_file(log_file, MAX_LOG_LINES)
