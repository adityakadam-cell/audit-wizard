"""
Background job manager.

Audits take 1-15 minutes depending on page count. Render's request timeout is
~100 seconds. So we run the audit in a Python thread, store progress in an
in-memory dict, and let the browser poll a status endpoint.

Job lifecycle:
    pending  → queued, not yet started
    running  → crawler/analyzer working
    done     → results ready, downloadable for 1 hour
    failed   → an error occurred (message stored on the job)
    cancelled → user clicked stop

Caveats:
    * In-memory only. If Render restarts the service, jobs are lost. For a
      small team tool that's fine; large-team or 24/7 use needs Redis.
    * Single Gunicorn worker required. With multiple workers each has its
      own _jobs dict, so the polling endpoint may not see the job another
      worker created. Our render.yaml uses `--workers 1`.
    * Memory: each job holds the crawled HTML in memory. ~100 pages ≈ 50 MB.
      We auto-purge jobs older than 1 hour to avoid leaks.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Optional

log = logging.getLogger("audit-wizard.jobs")

# How long completed jobs stay in memory (seconds). Reports remain
# downloadable during this window.
JOB_TTL_SECONDS = 3600  # 1 hour


class Job:
    """A single audit job with thread-safe progress updates."""

    __slots__ = (
        "id", "url", "industry", "max_pages", "email", "status",
        "phase", "current", "total", "current_url", "started_at",
        "finished_at", "error", "results", "report_html", "report_xlsx",
        "report_csv", "target_keyword", "deep_audit", "_lock", "_stop_flag",
    )

    def __init__(self, url: str, industry: str, max_pages: int, email: str = "",
                 target_keyword: str = "", deep_audit: bool = False):
        self.id = secrets.token_urlsafe(12)
        self.url = url
        self.industry = industry
        self.max_pages = max_pages
        self.email = email
        self.target_keyword = target_keyword
        self.deep_audit = deep_audit
        self.status = "pending"     # pending / running / done / failed / cancelled
        self.phase = "queued"       # queued / crawling / analyzing / generating / done
        self.current = 0
        self.total = max_pages
        self.current_url = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.results: list[dict] = []
        self.report_html: Optional[str] = None
        self.report_xlsx: Optional[bytes] = None
        self.report_csv: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()

    def to_status_dict(self) -> dict:
        """Return a JSON-serialisable status snapshot for the polling endpoint."""
        with self._lock:
            elapsed = (self.finished_at or time.time()) - self.started_at
            pct = round(100 * self.current / max(1, self.total)) if self.total else 0
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "current": self.current,
                "total": self.total,
                "current_url": self.current_url,
                "percent": pct,
                "elapsed_seconds": round(elapsed, 1),
                "error": self.error,
                "url": self.url,
            }

    def update_progress(self, current: int, total: int, current_url: str = ""):
        """Called by the crawler/analyzer to report progress. Thread-safe."""
        with self._lock:
            self.current = current
            self.total = total
            self.current_url = current_url

    def set_phase(self, phase: str):
        with self._lock:
            self.phase = phase
            log.info(f"[job {self.id}] phase → {phase}")

    def cancel(self):
        """Signal the worker thread to stop ASAP."""
        log.info(f"[job {self.id}] cancellation requested")
        self._stop_flag.set()

    def should_stop(self) -> bool:
        return self._stop_flag.is_set()


# ============================================================
#  Job registry (process-local)
# ============================================================

_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def create_job(url: str, industry: str, max_pages: int, email: str = "",
               target_keyword: str = "", deep_audit: bool = False) -> Job:
    job = Job(url, industry, max_pages, email,
              target_keyword=target_keyword, deep_audit=deep_audit)
    with _jobs_lock:
        _jobs[job.id] = job
        _purge_old_jobs_locked()
    log.info(
        f"[job {job.id}] created for {url} "
        f"({max_pages} pages, industry={industry}, "
        f"keyword={'yes' if target_keyword else 'no'}, "
        f"deep={'yes' if deep_audit else 'no'})"
    )
    return job


def get_job(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return _jobs.get(job_id)


def _purge_old_jobs_locked():
    """Drop jobs older than JOB_TTL_SECONDS. Called inside _jobs_lock."""
    now = time.time()
    stale_ids = [
        jid for jid, j in _jobs.items()
        if j.finished_at and (now - j.finished_at) > JOB_TTL_SECONDS
    ]
    for jid in stale_ids:
        del _jobs[jid]
        log.info(f"[job {jid}] purged (older than {JOB_TTL_SECONDS}s)")


# ============================================================
#  Worker function
# ============================================================

def run_audit_in_background(
    job: Job,
    on_complete: Optional[Callable[[Job], None]] = None,
):
    """
    Spawn a daemon thread that runs the full audit pipeline. Returns
    immediately. The thread updates job progress as it goes.

    on_complete is called once the job finishes (success, failure, or cancel)
    so the caller can do post-processing — e.g. send a notification email.
    """
    def worker():
        # Module-level import would be cleaner but kept here to be defensive
        # against import-time errors in audit_engine surfacing in app boot.
        from audit_engine import Crawler, Analyzer, ReportGen

        try:
            job.status = "running"
            job.set_phase("crawling")  # Combined crawl+analyze phase

            # ---- STREAMING CRAWL + ANALYZE ----
            # Critical for Render free tier (512 MB RAM): instead of
            # fetching ALL pages into memory then analyzing them in a
            # second pass, we now:
            #   1. Fetch one page (or a small batch in parallel)
            #   2. Analyze immediately
            #   3. Drop the HTML — keep only the lightweight result dict
            # Peak memory drops ~5×, which is the difference between
            # auditing 30 pages (the old limit) and 100 pages on free
            # tier without getting OOM-killed.
            crawler = Crawler(
                base_url=job.url,
                max_pages=job.max_pages,
                threads=5,
                delay=0.3,
                timeout=15,
                skip_ssl=False,
                on_progress=lambda c, t, u: job.update_progress(c, t, u),
                should_stop=job.should_stop,
            )
            analyzer = Analyzer(
                industry=job.industry,
                target_keyword=job.target_keyword,
                deep_audit=job.deep_audit,
            )

            results: list[dict] = []
            pages_seen = 0
            for page in crawler.crawl_streaming():
                if job.should_stop():
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    if on_complete:
                        on_complete(job)
                    return

                # Analyze this page right now while we have its HTML.
                # Per-page exception handling: a single bad page should
                # never kill the whole audit.
                try:
                    result = analyzer.analyze(page)
                    results.append(result)
                except Exception as e:
                    log.warning(
                        f"[job {job.id}] skipped {page.get('url', '?')}: {e}"
                    )
                    # Keep a stub so the page count in the report is honest
                    results.append({
                        'url': page.get('url', ''),
                        'title': '[analyze error]',
                        'issues': [],
                        'scores': {'overall': 0},
                        'word_count': 0, 'grades_found': [],
                        'response_time': page.get('response_time', 0),
                        'industry': '—',
                        'status': page.get('status', 0),
                        'checklist': [],
                        'analyze_error': str(e),
                    })

                # CRITICAL: drop the HTML now. Without this, Python keeps
                # the bytes alive until the loop iteration ends — for
                # large product pages that's tens of MB held unnecessarily.
                page['html'] = ''
                del page

                pages_seen += 1

            if job.should_stop():
                job.status = "cancelled"
                job.finished_at = time.time()
                if on_complete:
                    on_complete(job)
                return

            if not results:
                job.status = "failed"
                job.error = (
                    "Could not fetch any pages. Check that the URL is "
                    "reachable and that the site doesn't block crawlers."
                )
                job.finished_at = time.time()
                if on_complete:
                    on_complete(job)
                return

            job.results = results

            # ---- Generate reports ----
            job.set_phase("generating")
            gen = ReportGen()
            job.report_html = gen.html(results, job.url)
            job.report_xlsx = gen.excel(results)
            job.report_csv = gen.csv_report(results)

            # ---- Done ----
            job.set_phase("done")
            job.status = "done"
            job.finished_at = time.time()
            log.info(
                f"[job {job.id}] complete: "
                f"{len(results)} pages, "
                f"{round(job.finished_at - job.started_at, 1)}s elapsed"
            )

            if on_complete:
                try:
                    on_complete(job)
                except Exception:
                    log.exception(f"[job {job.id}] on_complete callback failed")

        except Exception as e:
            log.exception(f"[job {job.id}] worker crashed")
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.finished_at = time.time()
            if on_complete:
                try:
                    on_complete(job)
                except Exception:
                    pass

    t = threading.Thread(target=worker, name=f"audit-{job.id}", daemon=True)
    t.start()
    return t


def get_summary_stats(job: Job) -> dict:
    """Compute summary numbers for the results page."""
    if not job.results:
        return {
            "page_count": 0, "avg_score": 0,
            "critical_total": 0, "warning_total": 0, "info_total": 0,
        }

    pc = len(job.results)
    avg = round(sum(r['scores'].get('overall', 0) for r in job.results) / pc)
    crit = sum(sum(1 for i in r['issues'] if i['severity'] == 'critical') for r in job.results)
    warn = sum(sum(1 for i in r['issues'] if i['severity'] == 'warning') for r in job.results)
    info = sum(sum(1 for i in r['issues'] if i['severity'] == 'info') for r in job.results)

    return {
        "page_count": pc, "avg_score": avg,
        "critical_total": crit, "warning_total": warn, "info_total": info,
    }
