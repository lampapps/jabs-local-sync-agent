"""Email notification utility for the JABS local sync agent.

Sends immediate email notifications for sync events (errors, completions)
based on the `email.notify_on` configuration in config/global.yaml. Unlike the
dashboard (which sends a scheduled daily digest), the agent always sends
immediately — there is no queueing/digest logic here.
"""

import os
import smtplib
import socket
from email.mime.text import MIMEText

from dotenv import load_dotenv

from logger import setup_logger
from settings import EMAIL_CONFIG, ENV_PATH, ENV_MODE

email_logger = setup_logger("email", log_file="email.log")

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars


def _get_smtp_credentials():
    """Fetch SMTP username and password from environment variables."""
    username = os.environ.get("JABS_SMTP_USERNAME")
    password = os.environ.get("JABS_SMTP_PASSWORD")
    return username, password


def _send_email(subject, body, html=False):
    """Send an email with the given subject and body."""
    if not EMAIL_CONFIG:
        email_logger.debug("No email config found; skipping send.")
        return False

    username, password = _get_smtp_credentials()
    if ENV_MODE == "development":
        subject = f"[DEV] {subject}"
    if not username:
        email_logger.error("No SMTP username found in environment variable JABS_SMTP_USERNAME.")
        return False
    if not password:
        email_logger.error("No SMTP password found in environment variable JABS_SMTP_PASSWORD.")
        return False

    to_addrs = EMAIL_CONFIG.get("to_addrs") or []
    if not to_addrs:
        email_logger.error("No recipient addresses configured (email.to_addrs).")
        return False

    msg_type = "html" if html else "plain"
    msg = MIMEText(body, msg_type)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = ", ".join(to_addrs)

    try:
        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"], timeout=10)
        try:
            if EMAIL_CONFIG.get("use_tls"):
                server.starttls()
            server.login(username, password)
            server.sendmail(username, to_addrs, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # pylint: disable=broad-except
                pass
        email_logger.info(f"Email sent: '{subject}' to {to_addrs}")
        return True
    except (smtplib.SMTPException, OSError, socket.timeout) as e:
        email_logger.error(f"Failed to send email '{subject}': {e}")
        return False


def process_email_event(event_type, subject, body, html=False):
    """
    Send an immediate email notification if the event_type is enabled in config.

    event_type is expected to be one of "error" or "backup_complete".
    """
    notify_on = EMAIL_CONFIG.get("notify_on", {}) if EMAIL_CONFIG else {}
    event_cfg = notify_on.get(event_type, {})
    if isinstance(event_cfg, dict):
        enabled = event_cfg.get("enabled", False)
    else:
        enabled = bool(event_cfg)

    if not enabled:
        email_logger.debug(f"Notification for event '{event_type}' is disabled in config.")
        return False

    return _send_email(subject, body, html)
