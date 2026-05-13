"""
Audit Wizard — Flask web application.

A wizard-style website audit tool. Enter a URL, the app crawls it, runs
multi-category checks (SEO, HTML, performance, content, plus industry-specific
checks for metals/ecommerce/saas/healthcare/realestate), and generates an
interactive report you can view on screen or download as HTML/Excel/CSV.

Architecture note — why background jobs:
    Audits take 30 seconds to 15 minutes depending on page count. Render's
    request timeout is ~100 seconds. Doing the audit synchronously inside a
    request would fail for any non-trivial site. So we kick off audits in a
    background thread and let the browser poll a status endpoint.

Local dev:
    pip install -r requirements.txt
    cp .env.example .env       # fill in optional values
    python app.py              # http://localhost:5000

Production: see README.md
"""

import logging
import os
import secrets

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, abort, Response, flash,
)

# Optional .env support for local dev (no-op in production).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import jobs
import email_sender


# ===== Logging =====
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("audit-wizard")


# ===== Configuration =====
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
MAX_PAGES_LIMIT = int(os.environ.get("MAX_PAGES_LIMIT", 100))      # hard cap for free tier
DEFAULT_MAX_PAGES = int(os.environ.get("DEFAULT_MAX_PAGES", 30))   # what the form pre-fills


# ===== Flask app =====
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# ============================================================
#  Routes
# ============================================================

@app.route("/")
def home():
    return redirect(url_for("step1"))


@app.route("/healthz")
def healthz():
    """Liveness probe for Render / Docker / Cloud Run."""
    return {"status": "ok"}, 200


# ---- Step 1: URL & options ----
@app.route("/step1", methods=["GET", "POST"])
def step1():
    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        industry = (request.form.get("industry") or "auto").strip()
        try:
            max_pages = int(request.form.get("max_pages") or DEFAULT_MAX_PAGES)
        except ValueError:
            max_pages = DEFAULT_MAX_PAGES
        email = (request.form.get("email") or "").strip()
        target_keyword = (request.form.get("target_keyword") or "").strip()
        deep_audit = request.form.get("deep_audit") == "on"

        # Validate URL
        if not url:
            flash("Please enter a website URL.", "error")
            return render_template(
                "step1.html",
                default_max_pages=DEFAULT_MAX_PAGES,
                max_pages_limit=MAX_PAGES_LIMIT,
            )
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Clamp page count
        max_pages = max(1, min(MAX_PAGES_LIMIT, max_pages))

        # Validate industry
        valid = {"auto", "metals", "ecommerce", "saas", "healthcare", "realestate", "generic"}
        if industry not in valid:
            industry = "auto"

        # Validate email if provided
        if email and "@" not in email:
            flash("That email address looks invalid.", "error")
            return render_template(
                "step1.html",
                default_max_pages=DEFAULT_MAX_PAGES,
                max_pages_limit=MAX_PAGES_LIMIT,
                form_url=url, form_industry=industry,
                form_max_pages=max_pages, form_email=email,
                form_target_keyword=target_keyword,
                form_deep_audit=deep_audit,
            )

        # Cap target keyword length for safety
        if len(target_keyword) > 100:
            target_keyword = target_keyword[:100]

        # Create the job
        job = jobs.create_job(url, industry, max_pages, email,
                              target_keyword=target_keyword,
                              deep_audit=deep_audit)

        # Post-audit hook: send email with FULL inline detail + attachments.
        # Email contains: health card, top 5 recommendations with action steps,
        # 27-point checklist summary, every page with every issue inline,
        # AND the HTML/Excel/CSV reports as attachments.
        def on_complete(j):
            if not j.email or j.status != "done":
                return
            try:
                # Lazy-import here so audit_engine failures don't break app boot.
                from audit_engine import (
                    top_recommendations, overall_health_summary,
                    aggregate_checklist,
                )
                summary = jobs.get_summary_stats(j)
                recs = top_recommendations(j.results, n=5)
                health = overall_health_summary(j.results)
                checklist_summary = aggregate_checklist(j.results)

                # Pull the generated reports off the job — these were
                # produced during the "generating" phase and stored on the
                # job object. Attaching them gives users the complete data
                # in formats they can save / forward / analyze.
                report_html_bytes = (
                    j.report_html.encode("utf-8") if j.report_html else None
                )
                report_csv_bytes = (
                    j.report_csv.encode("utf-8") if j.report_csv else None
                )
                report_xlsx_bytes = j.report_xlsx  # already bytes

                ok, msg = email_sender.send_audit_complete(
                    j.email, j.url, j.id, summary,
                    recommendations=recs,
                    health=health,
                    results=j.results,
                    checklist_summary=checklist_summary,
                    report_html_bytes=report_html_bytes,
                    report_xlsx_bytes=report_xlsx_bytes,
                    report_csv_bytes=report_csv_bytes,
                )
                log.info(f"[job {j.id}] email send: ok={ok} msg={msg}")
            except Exception as e:
                log.exception(f"[job {j.id}] failed to send completion email: {e}")

        # Kick off background worker
        jobs.run_audit_in_background(job, on_complete=on_complete)

        return redirect(url_for("step2", job_id=job.id))

    return render_template(
        "step1.html",
        default_max_pages=DEFAULT_MAX_PAGES,
        max_pages_limit=MAX_PAGES_LIMIT,
    )


# ---- Step 2: Live progress ----
@app.route("/step2/<job_id>")
def step2(job_id):
    job = jobs.get_job(job_id)
    if not job:
        flash("That audit was not found or has expired.", "error")
        return redirect(url_for("step1"))
    return render_template("step2.html", job=job)


@app.route("/api/job/<job_id>/status")
def api_job_status(job_id):
    """Polled by step2.html every 2 seconds while the audit runs."""
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job.to_status_dict())


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    job.cancel()
    return jsonify({"status": "cancelling"})


# ---- Step 3 / Results ----
@app.route("/job/<job_id>/results")
def step3_results(job_id):
    job = jobs.get_job(job_id)
    if not job:
        flash("That audit was not found or has expired.", "error")
        return redirect(url_for("step1"))

    if job.status in ("pending", "running"):
        return redirect(url_for("step2", job_id=job.id))

    if job.status == "failed":
        return render_template("step3_failed.html", job=job)

    summary = jobs.get_summary_stats(job)

    # Build the site-wide checklist aggregation (27 items × pass/fail counts)
    from audit_engine import aggregate_checklist
    checklist_summary = aggregate_checklist(job.results)

    return render_template(
        "step3_results.html",
        job=job,
        summary=summary,
        checklist_summary=checklist_summary,
    )


# ---- Embedded interactive report (used by the Results page iframe) ----
@app.route("/job/<job_id>/report.html")
def report_html(job_id):
    job = jobs.get_job(job_id)
    if not job or not job.report_html:
        abort(404)
    return Response(job.report_html, mimetype="text/html")


# ---- Downloads ----
@app.route("/job/<job_id>/download.<fmt>")
def download(job_id, fmt):
    job = jobs.get_job(job_id)
    if not job or job.status != "done":
        abort(404)

    safe = job.url.replace("://", "_").replace("/", "_").replace(":", "_")[:60]

    if fmt == "html":
        if not job.report_html:
            abort(404)
        return Response(
            job.report_html, mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="audit_{safe}.html"'},
        )
    elif fmt == "xlsx":
        if not job.report_xlsx:
            abort(404)
        return Response(
            job.report_xlsx,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="audit_{safe}.xlsx"'},
        )
    elif fmt == "csv":
        if not job.report_csv:
            abort(404)
        return Response(
            job.report_csv, mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="audit_{safe}.csv"'},
        )
    abort(404)


# ===================== Local dev entry =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print(" Website Audit Wizard (development server)")
    print(f" Open in your browser:  http://localhost:{port}")
    print(" For production, use:   gunicorn wsgi:app")
    print("=" * 60 + "\n")
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False, threaded=True)
