"""
Email reports via Resend (resend.com).

Why Resend:
    - Free tier: 3,000 emails/month, 100/day. Generous for a team tool.
    - Modern, simple JSON API. No SMTP fiddling.
    - Free 'onboarding@resend.dev' sender for testing without domain setup.

Setup (quick path — works in 5 minutes):
    1. Sign up at https://resend.com (use Google or GitHub login)
    2. Dashboard → API Keys → Create API Key → copy
    3. Set env var on Render: RESEND_API_KEY=re_xxxxxxxxxx
    4. Use 'onboarding@resend.dev' as the sender — works immediately.

Setup (proper path — for production):
    1. Same as above, plus:
    2. Resend dashboard → Domains → Add domain → add the 3 DNS records
       Resend gives you to your domain registrar.
    3. Once verified, set EMAIL_FROM=reports@yourdomain.com on Render.

Behavior when no API key:
    Emails are written as JSON lines to outbox.jsonl in the working directory
    so nothing is lost. When the key is set later, those queued emails can be
    flushed manually (or just let new audits send fresh ones).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("audit-wizard.email")

RESEND_API_KEY = (os.environ.get("RESEND_API_KEY") or "").strip()
EMAIL_FROM = (os.environ.get("EMAIL_FROM") or "Audit Wizard <onboarding@resend.dev>").strip()
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")

OUTBOX_PATH = Path(os.environ.get("EMAIL_OUTBOX", "outbox.jsonl"))


def _is_configured() -> bool:
    return bool(RESEND_API_KEY)


def queue_for_later(to_email: str, subject: str, html: str, reason: str = "no_api_key"):
    """Append the email to a JSON-lines outbox file when we can't send."""
    try:
        OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTBOX_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "queued_at": time.time(),
                "to": to_email,
                "subject": subject,
                "html_length": len(html),
                "reason": reason,
            }) + "\n")
        log.info(f"queued email to {to_email} (reason={reason})")
    except Exception:
        log.exception("failed to write to outbox")


def send_email(to_email: str, subject: str, html_body: str) -> tuple[bool, str]:
    """
    Send an email via Resend's API. Returns (success, message).

    If RESEND_API_KEY is not set, writes to outbox and returns (False, "queued").
    If the API call fails, returns (False, error_string) — caller decides what to do.
    """
    if not to_email or "@" not in to_email:
        return False, "invalid email address"

    if not _is_configured():
        queue_for_later(to_email, subject, html_body, reason="no_api_key")
        return False, "queued (no RESEND_API_KEY set)"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201, 202):
            email_id = resp.json().get("id", "?")
            log.info(f"sent email to {to_email} (id={email_id})")
            return True, f"sent (id={email_id})"
        else:
            err = f"Resend API {resp.status_code}: {resp.text[:200]}"
            log.error(err)
            queue_for_later(to_email, subject, html_body, reason=f"api_error_{resp.status_code}")
            return False, err
    except Exception as e:
        log.exception("Resend API call failed")
        queue_for_later(to_email, subject, html_body, reason=f"exception_{type(e).__name__}")
        return False, str(e)


# ============================================================
#  Audit-specific notification email
# ============================================================

def send_audit_complete(
    to_email: str, site_url: str, job_id: str, summary: dict,
) -> tuple[bool, str]:
    """
    Send the 'your audit is ready' notification with a link back to the
    results page. The HTML report itself is NOT attached — too large for
    most emails and we'd need to spool it. Link is safer.
    """
    base = PUBLIC_BASE_URL or "https://your-app.onrender.com"
    results_url = f"{base}/job/{job_id}/results"

    subject = f"Audit ready: {site_url}"
    html_body = f"""\
<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;color:#1a202c;
max-width:560px;margin:24px auto;padding:0 20px">
  <h2 style="color:#2d3748">Your website audit is ready</h2>
  <p>The audit for <strong>{site_url}</strong> has finished.</p>

  <table cellpadding="10" cellspacing="0" style="border-collapse:collapse;
  margin:14px 0;font-size:14px">
    <tr><td style="border:1px solid #e2e8f0;background:#f7fafc">Pages scanned</td>
        <td style="border:1px solid #e2e8f0"><b>{summary.get('page_count', 0)}</b></td></tr>
    <tr><td style="border:1px solid #e2e8f0;background:#f7fafc">Average score</td>
        <td style="border:1px solid #e2e8f0"><b>{summary.get('avg_score', 0)} / 100</b></td></tr>
    <tr><td style="border:1px solid #e2e8f0;background:#f7fafc">Critical issues</td>
        <td style="border:1px solid #e2e8f0;color:#c53030">
          <b>{summary.get('critical_total', 0)}</b></td></tr>
    <tr><td style="border:1px solid #e2e8f0;background:#f7fafc">Warnings</td>
        <td style="border:1px solid #e2e8f0;color:#c05621">
          <b>{summary.get('warning_total', 0)}</b></td></tr>
  </table>

  <p>
    <a href="{results_url}" style="display:inline-block;padding:11px 22px;
    background:#3182ce;color:#fff;border-radius:6px;text-decoration:none;
    font-weight:600">View Full Report</a>
  </p>

  <p style="color:#718096;font-size:13px;margin-top:24px">
    The report stays online for 1 hour after generation. Download the
    HTML / Excel / CSV from the results page if you need to keep it.
  </p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#a0aec0;font-size:12px">
    Sent by Audit Wizard. You're receiving this because you entered this
    email address when starting the audit.
  </p>
</body></html>"""
    return send_email(to_email, subject, html_body)
