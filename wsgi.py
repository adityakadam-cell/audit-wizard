"""
Production WSGI entry point for Gunicorn.

Used by render.yaml's startCommand:
    python -m gunicorn wsgi:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 200

CRITICAL: --workers 1 is intentional. The audit job state (jobs._jobs dict)
is process-local. With multiple workers, a polling request might land on a
different worker than the one running the job, and the user would see
"job not found." For a small team tool, single worker + threads is fine.
If you ever need to scale beyond one worker, swap jobs.py's in-memory dict
for Redis.
"""

from app import app

if __name__ == "__main__":
    app.run()
