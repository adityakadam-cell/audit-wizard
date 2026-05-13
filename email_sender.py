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
    recommendations: Optional[list[dict]] = None,
    health: Optional[dict] = None,
) -> tuple[bool, str]:
    """
    Send the audit-ready email. Includes:
      - Overall health card (grade, avg score, issue counts)
      - Top 5 design-based recommendations (the actually-useful content)
      - Link to the full interactive report

    recommendations: list of dicts from audit_engine.top_recommendations(),
                     or None to fall back to a minimal email.
    health:          dict from audit_engine.overall_health_summary(),
                     or None to use the older 'summary' shape.
    """
    base = PUBLIC_BASE_URL or "https://your-app.onrender.com"
    results_url = f"{base}/job/{job_id}/results"

    # Build the recommendations block — this is the differentiator
    recs_html = ""
    if recommendations:
        theme_colors = {
            'seo':     ('#2b6cb0', '#ebf4ff'),
            'design':  ('#6b46c1', '#faf5ff'),
            'content': ('#2f855a', '#f0fff4'),
            'trust':   ('#c05621', '#fffaf0'),
            'tech':    ('#c53030', '#fff5f5'),
        }
        sev_label = {'critical': 'CRITICAL', 'warning': 'WARNING', 'info': 'IMPROVEMENT'}
        sev_color = {'critical': '#c53030', 'warning': '#c05621', 'info': '#2b6cb0'}

        rec_cards = []
        for i, r in enumerate(recommendations[:5], 1):
            tc_fg, tc_bg = theme_colors.get(r.get('theme', 'seo'), ('#4a5568', '#f7fafc'))
            sev = r.get('severity', 'info')
            actions = ''.join(
                f'<li style="margin-bottom:4px">{_e(a)}</li>'
                for a in r.get('action_steps', [])[:3]
            )
            rec_cards.append(f"""
<div style="border:1px solid #e2e8f0;border-radius:7px;padding:14px 16px;
            margin-bottom:10px;background:#fafbfc">
  <div style="font-size:11px;margin-bottom:6px;display:flex;gap:8px;flex-wrap:wrap">
    <span style="color:#a0aec0;font-weight:700">#{i}</span>
    <span style="color:{tc_fg};background:{tc_bg};padding:2px 7px;border-radius:3px;
                 font-weight:600;letter-spacing:.3px">{_e(r.get('theme_label', ''))}</span>
    <span style="color:{sev_color[sev]};font-weight:700;letter-spacing:.3px">
      {sev_label[sev]}
    </span>
    <span style="color:#718096;margin-left:auto">
      {r.get('affected_pages', 0)} of {r.get('total_pages', 0)} pages
    </span>
  </div>
  <h3 style="font-size:14px;font-weight:600;color:#2d3748;margin:4px 0 6px">
    {_e(r.get('headline', ''))}
  </h3>
  <p style="font-size:12.5px;color:#4a5568;line-height:1.55;margin:0 0 8px">
    {_e(r.get('why', ''))}
  </p>
  <div style="font-size:12px;color:#2d3748">
    <strong style="color:#3182ce">Action steps:</strong>
    <ol style="margin:6px 0 0 22px;line-height:1.55">{actions}</ol>
  </div>
</div>""")

        recs_html = f"""
<h2 style="font-size:17px;color:#2d3748;margin:24px 0 4px">Top Recommendations</h2>
<p style="font-size:13px;color:#718096;margin:0 0 14px">
  Design-based suggestions ordered by impact across your site. Start at the top.
</p>
{''.join(rec_cards)}"""

    # Health card
    h = health or {}
    grade = h.get('grade', 'N/A')
    grade_color = {'A': '#22543d', 'B': '#2f855a', 'C': '#c05621',
                   'D': '#c53030', 'F': '#742a2a'}.get(grade, '#4a5568')
    grade_bg = {'A': '#c6f6d5', 'B': '#d4f1de', 'C': '#feebc8',
                'D': '#fed7d7', 'F': '#fed7d7'}.get(grade, '#edf2f7')

    page_count = h.get('page_count', summary.get('page_count', 0))
    avg_score = h.get('avg_score', summary.get('avg_score', 0))
    critical = h.get('critical_issues', summary.get('critical_total', 0))
    warnings = h.get('warning_issues', summary.get('warning_total', 0))

    subject = f"Audit ready: {site_url} — Grade {grade} ({critical} critical, {warnings} warnings)"
    html_body = f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
color:#1a202c;max-width:640px;margin:0 auto;padding:24px 20px;background:#f7fafc">
  <div style="background:#fff;border-radius:9px;padding:24px;
              box-shadow:0 1px 3px rgba(0,0,0,.06)">

    <h2 style="color:#2d3748;margin:0 0 4px;font-size:22px">
      Your website audit is ready
    </h2>
    <p style="color:#4a5568;margin:0 0 18px;font-size:14px">
      Full audit complete for <strong>{_e(site_url)}</strong>.
    </p>

    <!-- Health summary card -->
    <div style="display:flex;align-items:center;gap:16px;padding:16px;
                background:#f7fafc;border-radius:8px;margin-bottom:18px">
      <div style="width:72px;height:72px;border-radius:50%;background:{grade_bg};
                  color:{grade_color};display:flex;align-items:center;
                  justify-content:center;font-size:34px;font-weight:700;
                  flex-shrink:0">{grade}</div>
      <div>
        <div style="font-size:13px;color:#718096;margin-bottom:2px">Overall site health</div>
        <div style="font-size:22px;font-weight:700;color:#2d3748">
          {avg_score} <span style="color:#a0aec0;font-size:14px">/ 100</span>
        </div>
        <div style="font-size:12px;color:#718096;margin-top:4px">
          {page_count} pages audited &nbsp;·&nbsp;
          <span style="color:#c53030;font-weight:600">{critical} critical</span> &nbsp;·&nbsp;
          <span style="color:#c05621;font-weight:600">{warnings} warnings</span>
        </div>
      </div>
    </div>

    <!-- Top recommendations -->
    {recs_html}

    <!-- CTA to full report -->
    <p style="margin:24px 0">
      <a href="{results_url}" style="display:inline-block;padding:12px 26px;
      background:#3182ce;color:#fff;border-radius:6px;text-decoration:none;
      font-weight:600;font-size:14px">View Full Report →</a>
    </p>
    <p style="color:#718096;font-size:12px;margin-top:6px">
      Full report includes the 27-point checklist, per-page issue breakdown,
      and downloadable HTML / Excel / CSV. Available for 1 hour after generation.
    </p>

    <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 16px">
    <p style="color:#a0aec0;font-size:11.5px;margin:0">
      Sent by Audit Wizard. You're receiving this because you entered this
      email address when starting the audit.
    </p>
  </div>
</body></html>"""
    return send_email(to_email, subject, html_body)


def _e(s) -> str:
    """HTML-escape helper, also used inline above."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
