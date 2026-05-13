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

# Always-BCC list: comma-separated emails that get a copy of EVERY audit email.
# Set in Render dashboard as ALWAYS_BCC=admin@yourcompany.com or
# ALWAYS_BCC=admin@a.com,manager@b.com for multiple addresses.
# Leave unset to disable. The BCC list is sent via Resend's bcc field, so
# the primary recipient doesn't see the admin addresses.
ALWAYS_BCC = [
    e.strip() for e in (os.environ.get("ALWAYS_BCC") or "").split(",")
    if e.strip() and "@" in e.strip()
]

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


def send_email(
    to_email: str, subject: str, html_body: str,
    attachments: Optional[list[dict]] = None,
) -> tuple[bool, str]:
    """
    Send an email via Resend's API. Returns (success, message).

    attachments: list of dicts with keys:
        - filename: str (e.g. "audit-report.xlsx")
        - content:  bytes (raw file content — we base64-encode for Resend)
        - content_type: str (MIME type, e.g. "application/vnd.openxmlformats-...")

    Resend supports up to 40 MB total per email (combined body + attachments).
    We don't enforce that here — caller is responsible for keeping it under.

    If RESEND_API_KEY is not set, writes to outbox and returns (False, "queued").
    If the API call fails, returns (False, error_string) — caller decides what to do.
    """
    if not to_email or "@" not in to_email:
        return False, "invalid email address"

    if not _is_configured():
        queue_for_later(to_email, subject, html_body, reason="no_api_key")
        return False, "queued (no RESEND_API_KEY set)"

    # Build Resend payload — attachments use base64 + filename
    payload: dict = {
        "from": EMAIL_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    # Always-BCC list (from env var) — admin addresses that get a copy of
    # every audit email. Hidden from the primary recipient.
    if ALWAYS_BCC:
        payload["bcc"] = ALWAYS_BCC
    if attachments:
        import base64
        payload["attachments"] = [
            {
                "filename": a["filename"],
                "content": base64.b64encode(a["content"]).decode("ascii"),
                # content_type is optional but recommended — helps email clients
                # display the right icon and pick the right "Open with" app.
                **({"content_type": a["content_type"]} if a.get("content_type") else {}),
            }
            for a in attachments
        ]

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,   # bumped from 15s — attachments take longer to upload
        )
        if resp.status_code in (200, 201, 202):
            email_id = resp.json().get("id", "?")
            log.info(
                f"sent email to {to_email} (id={email_id}, "
                f"body={len(html_body)}b, attachments={len(attachments or [])}, "
                f"bcc={len(ALWAYS_BCC)})"
            )
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
    results: Optional[list[dict]] = None,
    checklist_summary: Optional[list[dict]] = None,
    report_html_bytes: Optional[bytes] = None,
    report_xlsx_bytes: Optional[bytes] = None,
    report_csv_bytes: Optional[bytes] = None,
) -> tuple[bool, str]:
    """
    Send the audit-ready email with:
      - Health card (grade, avg score, issue counts)
      - Top 5 design-based recommendations with action steps
      - 27-Point Checklist Summary (NEW — every checkpoint with pass/fail share)
      - Per-Page Detail (NEW — every URL with every issue, fully flat)
      - Attachments: HTML report, Excel, CSV (NEW)
      - Link to the live interactive results page

    Inline body has a self-limiting safety net: if total HTML would exceed
    ~95 KB (Gmail clips at ~102 KB), we trim per-page detail and add a
    "see attached files" notice. The attachments are always sent in full.
    """
    base = PUBLIC_BASE_URL or "https://your-app.onrender.com"
    results_url = f"{base}/job/{job_id}/results"

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

    # ---- 27-Point Checklist Summary (NEW inline block) ----
    checklist_html = ""
    if checklist_summary:
        groups = ['On-page SEO', 'Images', 'Content', 'Product Tables',
                  'Page Content', 'Technical']
        group_blocks = []
        for group in groups:
            items = [c for c in checklist_summary if c.get('group') == group]
            if not items:
                continue
            rows = []
            for cp in items:
                total = cp.get('total', 1) or 1
                p = round(cp.get('pass', 0) / total * 100)
                f = round(cp.get('fail', 0) / total * 100)
                i_ = round(cp.get('info', 0) / total * 100)
                s = round(cp.get('skipped', 0) / total * 100)
                m = round(cp.get('manual', 0) / total * 100)
                # Build a flat horizontal bar using table cells (email-safe)
                bar = (
                    f'<table cellspacing="0" cellpadding="0" border="0" '
                    f'style="border-collapse:collapse;width:140px;height:8px;'
                    f'border-radius:4px;overflow:hidden"><tr>'
                    + (f'<td bgcolor="#48bb78" style="height:8px" width="{p}%"></td>' if p else '')
                    + (f'<td bgcolor="#f56565" style="height:8px" width="{f}%"></td>' if f else '')
                    + (f'<td bgcolor="#4299e1" style="height:8px" width="{i_}%"></td>' if i_ else '')
                    + (f'<td bgcolor="#a0aec0" style="height:8px" width="{s}%"></td>' if s else '')
                    + (f'<td bgcolor="#ed8936" style="height:8px" width="{m}%"></td>' if m else '')
                    + '</tr></table>'
                )
                counts = []
                if cp.get('fail'): counts.append(f'<span style="color:#c53030;font-weight:600">{cp["fail"]} fail</span>')
                if cp.get('pass'): counts.append(f'<span style="color:#2f855a;font-weight:600">{cp["pass"]} pass</span>')
                if cp.get('skipped'): counts.append(f'<span style="color:#a0aec0">{cp["skipped"]} skip</span>')
                if cp.get('manual'): counts.append(f'<span style="color:#c05621">{cp["manual"]} manual</span>')
                rows.append(f"""
<tr>
  <td style="padding:5px 4px;font-size:11px;color:#718096;width:24px">{cp.get('number', '')}</td>
  <td style="padding:5px 4px;font-size:12px;color:#2d3748;font-weight:500">{_e(cp.get('label', ''))}</td>
  <td style="padding:5px 4px">{bar}</td>
  <td style="padding:5px 4px;font-size:11px;color:#718096;text-align:right;white-space:nowrap">
    {' · '.join(counts) or '—'}
  </td>
</tr>""")
            group_blocks.append(f"""
<div style="font-size:10.5px;font-weight:700;text-transform:uppercase;
            letter-spacing:.4px;color:#4a5568;margin:14px 0 4px;
            padding-bottom:3px;border-bottom:1px solid #e2e8f0">{_e(group)}</div>
<table cellspacing="0" cellpadding="0" border="0" style="width:100%;border-collapse:collapse">
{''.join(rows)}
</table>""")

        checklist_html = f"""
<h2 style="font-size:17px;color:#2d3748;margin:28px 0 4px">27-Point Checklist Summary</h2>
<p style="font-size:13px;color:#718096;margin:0 0 12px">
  Site-wide compliance across all {page_count} crawled page(s).
  Bars show pass / fail / info / skipped / manual share.
</p>
{''.join(group_blocks)}
<div style="font-size:11px;color:#4a5568;margin-top:10px;padding-top:8px;
            border-top:1px solid #e2e8f0">
  <span style="display:inline-block;width:8px;height:8px;background:#48bb78;border-radius:50%;margin-right:4px"></span>Pass &nbsp;
  <span style="display:inline-block;width:8px;height:8px;background:#f56565;border-radius:50%;margin-right:4px"></span>Fail &nbsp;
  <span style="display:inline-block;width:8px;height:8px;background:#4299e1;border-radius:50%;margin-right:4px"></span>Info &nbsp;
  <span style="display:inline-block;width:8px;height:8px;background:#a0aec0;border-radius:50%;margin-right:4px"></span>Skipped &nbsp;
  <span style="display:inline-block;width:8px;height:8px;background:#ed8936;border-radius:50%;margin-right:4px"></span>Manual
</div>"""

    # ---- Per-Page Detail (NEW inline block) ----
    # Render every page with all its issues, flat (no JS accordion).
    # If the cumulative body grows past ~95 KB we trim and add a notice.
    pages_html = ""
    pages_truncated = False
    INLINE_BODY_CAP = 95_000  # bytes — Gmail clips at ~102 KB
    if results:
        sev_bg = {'critical': '#fff0f0', 'warning': '#fffbe6', 'info': '#f0f7ff'}
        sev_bd = {'critical': '#e53e3e', 'warning': '#dd6b20', 'info': '#3182ce'}
        sev_label_color = {'critical': '#c53030', 'warning': '#c05621', 'info': '#2b6cb0'}

        # We estimate the rest-of-email size to know how much room we have
        # for per-page content. Recommendations + checklist are biggest.
        overhead_est = len(recs_html) + len(checklist_html) + 4000  # 4KB for shell

        rendered_pages = []
        running_size = overhead_est

        for r in results:
            issues = r.get('issues', [])
            sc = r.get('scores', {}).get('overall', 0)
            sc_fg = '#22543d' if sc >= 80 else ('#7b341e' if sc >= 55 else '#742a2a')
            sc_bg = '#c6f6d5' if sc >= 80 else ('#feebc8' if sc >= 55 else '#fed7d7')

            # Build issues list HTML for this page
            issues_inner = []
            if issues:
                sev_order = {'critical': 0, 'warning': 1, 'info': 2}
                for iss in sorted(issues, key=lambda x: sev_order.get(x.get('severity'), 9)):
                    sev = iss.get('severity', 'info')
                    fix_html = ''
                    if iss.get('fix'):
                        fix_html = (
                            f'<div style="background:#f7fafc;border-radius:4px;padding:6px 9px;'
                            f'margin-top:5px;font-family:monospace;font-size:11px;'
                            f'color:#2d3748;white-space:pre-wrap;word-break:break-word">'
                            f'<span style="font-size:9px;font-weight:700;color:#38a169;'
                            f'letter-spacing:.4px">FIX</span><br>{_e(iss["fix"])}</div>'
                        )
                    issues_inner.append(f"""
<div style="padding:8px 11px;border-radius:5px;background:{sev_bg.get(sev, '#fff')};
            border-left:3px solid {sev_bd.get(sev, '#ccc')};margin-bottom:6px">
  <div style="display:flex;gap:6px;margin-bottom:3px;flex-wrap:wrap">
    <span style="background:#edf2f7;color:#4a5568;font-size:9px;font-weight:700;
                 padding:2px 5px;border-radius:8px">{_e(iss.get('category', '').upper())}</span>
    <span style="background:{sev_label_color.get(sev, '#666')};color:#fff;
                 font-size:9px;font-weight:700;padding:2px 5px;border-radius:8px">
      {sev.upper()}
    </span>
    <b style="font-size:12px;color:#2d3748">{_e(iss.get('title', ''))}</b>
  </div>
  <p style="font-size:11.5px;color:#718096;line-height:1.45;margin:0">
    {_e(iss.get('description', ''))}
  </p>
  {fix_html}
</div>""")
            else:
                issues_inner.append(
                    '<p style="color:#48bb78;font-size:12px;margin:6px 0">'
                    '✓ No issues found on this page.</p>'
                )

            issues_html_block = ''.join(issues_inner)
            crit_c = sum(1 for i in issues if i.get('severity') == 'critical')
            warn_c = sum(1 for i in issues if i.get('severity') == 'warning')
            info_c = sum(1 for i in issues if i.get('severity') == 'info')

            page_block = f"""
<div style="border:1px solid #e2e8f0;border-radius:7px;padding:14px 16px;
            margin-bottom:12px;background:#fff">
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
    <div style="width:38px;height:38px;border-radius:50%;background:{sc_bg};
                color:{sc_fg};font-weight:700;font-size:13px;
                display:flex;align-items:center;justify-content:center;flex-shrink:0">
      {sc}
    </div>
    <div style="flex:1;min-width:200px">
      <div style="font-weight:600;font-size:13px;color:#2d3748;
                  word-break:break-word;margin-bottom:2px">
        {_e(r.get('title') or '(no title)')}
      </div>
      <a href="{_e(r.get('url', '#'))}" style="font-size:11.5px;color:#3182ce;
         text-decoration:none;word-break:break-all">{_e(r.get('url', ''))}</a>
    </div>
  </div>
  <div style="display:flex;gap:10px;font-size:11px;color:#718096;margin-bottom:8px;flex-wrap:wrap">
    <span style="color:#c53030;font-weight:600">{crit_c} critical</span>
    <span style="color:#c05621;font-weight:600">{warn_c} warning</span>
    <span>{info_c} info</span>
    <span>·</span>
    <span>{r.get('word_count', 0)} words</span>
    <span>·</span>
    <span>{r.get('response_time', 0):.2f}s</span>
    <span>·</span>
    <span style="background:#ebf4ff;color:#2b6cb0;padding:1px 6px;
                 border-radius:3px;font-size:10px;font-weight:600">
      {_e(r.get('industry', '?').upper())}
    </span>
  </div>
  {issues_html_block}
</div>"""

            running_size += len(page_block)
            if running_size > INLINE_BODY_CAP:
                pages_truncated = True
                break
            rendered_pages.append(page_block)

        if rendered_pages:
            truncation_notice = ''
            if pages_truncated:
                truncation_notice = f"""
<div style="background:#fffbeb;border:1px solid #f6e05e;border-radius:7px;
            padding:12px 14px;margin-top:12px;font-size:12.5px;color:#7b341e">
  <strong>Showing {len(rendered_pages)} of {len(results)} pages inline.</strong>
  Email body size limit reached. The complete per-page detail for all
  {len(results)} pages is in the attached <strong>audit-report.html</strong>
  and <strong>audit-report.xlsx</strong> files, or view the full interactive
  report at the link below.
</div>"""
            pages_html = f"""
<h2 style="font-size:17px;color:#2d3748;margin:28px 0 4px">Per-Page Detail</h2>
<p style="font-size:13px;color:#718096;margin:0 0 14px">
  Every audited page with every issue, severity, description, and suggested fix.
</p>
{''.join(rendered_pages)}
{truncation_notice}"""

    # ---- Compose final email body ----
    subject = (
        f"Audit ready: {site_url} — Grade {grade} "
        f"({critical} critical, {warnings} warnings)"
    )
    html_body = f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
color:#1a202c;max-width:760px;margin:0 auto;padding:24px 20px;background:#f7fafc">
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

    {recs_html}
    {checklist_html}
    {pages_html}

    <!-- CTA to full report + attachment note -->
    <p style="margin:28px 0 6px">
      <a href="{results_url}" style="display:inline-block;padding:12px 26px;
      background:#3182ce;color:#fff;border-radius:6px;text-decoration:none;
      font-weight:600;font-size:14px">View Live Interactive Report →</a>
    </p>
    <p style="color:#718096;font-size:12px;margin:6px 0 0">
      The downloadable HTML, Excel, and CSV reports are <strong>attached</strong>
      to this email. They contain the complete per-page data and work offline.
      The live link above is available for 1 hour after generation.
    </p>

    <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 16px">
    <p style="color:#a0aec0;font-size:11.5px;margin:0">
      Sent by Audit Wizard. You're receiving this because you entered this
      email address when starting the audit.
    </p>
  </div>
</body></html>"""

    # ---- Build attachments ----
    attachments: list[dict] = []
    if report_html_bytes:
        attachments.append({
            "filename": "audit-report.html",
            "content": report_html_bytes,
            "content_type": "text/html",
        })
    if report_xlsx_bytes:
        attachments.append({
            "filename": "audit-report.xlsx",
            "content": report_xlsx_bytes,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
    if report_csv_bytes:
        attachments.append({
            "filename": "audit-issues.csv",
            "content": report_csv_bytes,
            "content_type": "text/csv",
        })

    return send_email(to_email, subject, html_body, attachments=attachments or None)


def _e(s) -> str:
    """HTML-escape helper, also used inline above."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
