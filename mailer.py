"""
Email delivery module for Snapchat Bot execution reports.
Sends email notifications with status, AI quote, and attaches verbose log file,
then safely cleans up.
"""

import email.encoders
import logging
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("snap-bot")


def send_run_email(
    email_cfg: dict,
    status_summary: str,
    quote: str,
    error_details: str = None,
    log_file_path: Path = None,
    screenshot_path: Path = None,
) -> bool:
    """
    Send an execution report email with status, quote, attached log file,
    and attached pre-send screenshot.
    """
    if not email_cfg.get("enabled", False):
        logger.info("Email notifications are disabled in config.json.")
        return False

    smtp_host = email_cfg.get("smtp_host", "").strip()
    smtp_port = int(email_cfg.get("smtp_port", 587))
    smtp_user = email_cfg.get("smtp_user", "").strip()
    smtp_pass = email_cfg.get("smtp_password", "").strip()
    recipient = email_cfg.get("recipient_email", "").strip()

    # Fallback to standard environment variables if config fields are empty
    if not smtp_user:
        smtp_user = os.getenv("EMAIL_USER") or os.getenv("SMTP_USER") or os.getenv("SNAP_SMTP_USER", "").strip()
    if not smtp_pass:
        smtp_pass = os.getenv("EMAIL_PASSWORD") or os.getenv("SMTP_PASSWORD") or os.getenv("SNAP_SMTP_PASSWORD", "").strip()
    if not recipient:
        recipient = os.getenv("RECIPIENT_EMAIL") or os.getenv("SNAP_RECIPIENT_EMAIL", "").strip()

    if not (smtp_host and smtp_user and smtp_pass and recipient):
        logger.warning(
            "Email notification skipped: Incomplete SMTP configuration. "
            "Please check 'email_notifications' in config.json."
        )
        return False

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = recipient
    is_success = "SUCCESS" in status_summary.upper()
    if is_success:
        if "DRY-RUN" in status_summary.upper():
            subject_status = "SUCCESS (Dry-Run)"
            status_emoji = "ℹ️"
        else:
            subject_status = "SUCCESS"
            status_emoji = "✅"
    else:
        subject_status = "FAILED"
        status_emoji = "❌"

    # Clean short subject line (detailed error is inside body and attached log)
    msg["Subject"] = f"{status_emoji} Snapchat Bot Report: {subject_status}"

    body_lines = [
        "<h2>Snapchat Daily Streak Automation Report</h2>",
        f"<p><strong>Status:</strong> <span style='color: {'green' if is_success else 'red'};'>{status_summary}</span></p>",
        f"<p><strong>AI Quote Generated:</strong> <em>{quote if quote else 'None / Generation Failed'}</em></p>",
    ]

    if screenshot_path and Path(screenshot_path).is_file():
        body_lines.append(
            "<p>📸 <strong>Snap Caption Screenshot:</strong> Attached (showing snap with visible caption)</p>"
        )

    if error_details:
        body_lines.append(
            f"<div style='background: #fee; border-left: 4px solid #c00; padding: 10px; margin: 10px 0;'>"
            f"<strong>Error Details:</strong><pre>{error_details}</pre></div>"
        )

    body_lines.append(
        "<p>The complete verbose execution log and snap caption screenshot are attached to this email.</p>"
        "<hr><small>Snapchat Automation Bot • Running on Linux</small>"
    )

    msg.attach(MIMEText("\n".join(body_lines), "html"))

    # Attach log file if it exists
    if log_file_path and log_file_path.is_file():
        try:
            with open(log_file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            email.encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={log_file_path.name}",
            )
            msg.attach(part)
            logger.info(f"Attached verbose log file: {log_file_path.name}")
        except Exception as e:
            logger.error(f"Failed to attach log file: {e}")

    # Attach pre-send screenshot if it exists
    if screenshot_path and Path(screenshot_path).is_file():
        try:
            with open(screenshot_path, "rb") as f:
                img_part = MIMEBase("image", "png")
                img_part.set_payload(f.read())
            email.encoders.encode_base64(img_part)
            img_part.add_header(
                "Content-Disposition",
                f"attachment; filename={Path(screenshot_path).name}",
            )
            msg.attach(img_part)
            logger.info(f"Attached pre-send screenshot: {Path(screenshot_path).name}")
        except Exception as e:
            logger.error(f"Failed to attach screenshot: {e}")

    try:
        logger.info(f"Connecting to SMTP server {smtp_host}:{smtp_port}...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        logger.info(f"Execution report email successfully delivered to {recipient}!")
        return True
    except Exception as e:
        logger.error(f"Failed to send execution report email: {e}")
        return False
