# Audit Wizard

A web app that crawls any website, audits each page for SEO, HTML, performance, and content issues, and produces a downloadable interactive report. Special checks for metals, e-commerce, SaaS, healthcare, and real estate sites.

Sister project to `optimizer-wizard` — same design language, similar deploy.

[![CI](https://github.com/YOUR_ORG/YOUR_REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_ORG/YOUR_REPO/actions/workflows/ci.yml)

---

## What it does

A 3-step wizard:

1. **Enter a URL** — plus optional industry, target keyword, deep-audit toggle, page count cap, and email address.
2. **Watch the live progress** — crawl phase, then analyse phase. Cancel anytime.
3. **Review the results** — score cards, the 27-point checklist summary (site-wide pass/fail), filterable per-page issue list, and download links for HTML / Excel / CSV. Optional: link emailed to the address you provided.

### The 27-point checklist

Every page is graded against a 27-point industrial SEO checklist organised into 6 groups:

| Group | Checkpoints |
|---|---|
| On-page SEO | URL structure · page title · meta description · indexability · canonical · H1 · breadcrumbs · subheadings |
| Images | File names · alt text · weight (≤ 100 KB, deep mode only) |
| Content | Internal linking · keyword usage · content originality (manual review) |
| Product Tables | Specifications · chemical · mechanical · equivalent · size · technical-spec |
| Page Content | Applications · CTA button · FAQs · contact info |
| Technical | Schema · mobile-friendly · inquiry form |

Each checkpoint reports one of five statuses per page:
**Pass** ✓, **Fail** ✗, **Info** ⓘ (minor), **Skipped** (deep audit / target keyword not provided), **Manual** (cannot be auto-checked, e.g. originality).

### Other layers

Universal checks (every page) include SEO basics, HTML hygiene (HTTPS, viewport, charset, lorem-ipsum), performance (response time, page size), and content depth (word count, contact info, CTAs).

Industry-specific checks layered on top:

- **Metals** — chemical composition vs ASTM specs (304/304L/316/316L/321/347/310S/317L/904L/2205/2507 + carbon steel A36/A53/A106), mechanical properties presence, ASTM/ASME standards mention, equivalent grades (UNS/EN/DIN/JIS/BS).
- **E-commerce** — price, product schema, image count, reviews.
- **SaaS** — pricing, social proof, trial/demo CTA.
- **Healthcare** — phone, appointment booking, medical schema.
- **Real estate** — price/size info, property images, location/map.

### Optional inputs on Step 1

- **Target keyword** — if provided, the checklist's "Keyword Usage" item checks density (1–3 %), placement in title/H1, and over-stuffing. Leave blank to skip.
- **Deep audit** — off by default. When on, also HEAD-checks every image (≤ 100 KB) and validates that CTA buttons link to working URLs. Adds ~5–10 s per page; enable for thorough pre-launch audits.

---

## Architecture

The audit takes 30 seconds to 15 minutes depending on the page count. Render's request timeout is ~100 seconds, so a synchronous request would fail. Instead:

```
Browser              Flask                    Background Thread
-------              -----                    -----------------
POST /step1   ───►   create Job(id)
                     start thread             ──► crawl pages
                     redirect to /step2/<id>      analyse each page
                                                  generate reports
GET /step2/<id>      render progress page
   ↓
   poll every 2s
GET /api/job/<id>/status  ◄── reads job.current/total/phase
                                                  job.status = "done"
   ↓ (when status == done)                        send email if requested
window.location = /job/<id>/results
GET /job/<id>/results  render results dashboard
```

This is in `jobs.py`. The job state is process-local — works on one Gunicorn worker only. If you scale beyond that, swap in Redis.

---

## Deployment paths

### Option A — Render Blueprint (recommended)

The repo includes `render.yaml`, so deploy is 3 clicks.

1. Push this repo to GitHub.
2. Go to [dashboard.render.com/blueprints](https://dashboard.render.com/blueprints) → **New Blueprint**.
3. Connect this repo. Render reads `render.yaml` automatically.
4. After the blueprint applies, go to the service → **Environment** → set:
   - `RESEND_API_KEY` (optional, for email — see "Email setup" below).
   - `PUBLIC_BASE_URL` — set this to your live URL after the first deploy succeeds. For example: `https://audit-wizard-XXXX.onrender.com`. Used in email links.
5. Click Deploy. Wait ~3 minutes.

Free tier limits:
- **Spin-down** after 15 minutes idle. Next request takes ~50 seconds to wake up.
- **512 MB RAM.** Caps out around 100 pages safely. The blueprint sets `MAX_PAGES_LIMIT=100` for this reason.
- **No persistent disk.** Audit jobs are in-memory and disappear on restart.

To support 1000-page crawls and remove cold starts, upgrade to Render Starter ($7/month). Then bump `MAX_PAGES_LIMIT` to 1000 in the Environment tab.

### Option B — Docker (deploy anywhere)

```bash
docker build -t audit-wizard .
docker run -p 8000:8000 \
  -e RESEND_API_KEY=re_xxx \
  -e PUBLIC_BASE_URL=https://your-host.com \
  audit-wizard
```

Works on Cloud Run, Fly.io, ECS, your own VM, or k8s.

### Option C — Local dev

```bash
git clone <this-repo>
cd audit-wizard
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # edit if you want email
python app.py                                         # http://localhost:5000
```

---

## Email setup (5 minutes)

Email is **optional**. Without an API key, the app still works — emails just go to a local `outbox.jsonl` file instead of being sent.

To enable real sending:

1. Sign up free at [resend.com](https://resend.com) (login with Google / GitHub).
2. Dashboard → API Keys → Create API Key. Copy the value (starts with `re_`).
3. On Render → your service → Environment → set `RESEND_API_KEY` to that value.
4. Save. Render auto-redeploys.

The default sender is `onboarding@resend.dev`, Resend's verified test sender. Works immediately. Emails will likely land in spam at first; tell your team to mark "Not spam" once.

For production-grade deliverability:
1. Resend → Domains → Add domain → add the DNS records they show to your domain registrar.
2. Once verified, on Render set `EMAIL_FROM=Reports <reports@yourdomain.com>`.

Resend free tier is 3,000 emails/month and 100/day — far more than a small team needs.

---

## Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `RESEND_API_KEY` | optional | _(none)_ | Resend API key. Without it, emails go to `outbox.jsonl`. |
| `EMAIL_FROM` | optional | `Audit Wizard <onboarding@resend.dev>` | From address. Default works without domain setup. |
| `PUBLIC_BASE_URL` | recommended | _(none)_ | Your live URL. Used in email links. |
| `MAX_PAGES_LIMIT` | optional | `100` | Hard cap on max-pages. Bump to 1000 on paid tier. |
| `DEFAULT_MAX_PAGES` | optional | `30` | What the form pre-fills. |
| `FLASK_SECRET_KEY` | recommended | random per process | Stable session secret in production. |
| `LOG_LEVEL` | optional | `INFO` | DEBUG / INFO / WARNING / ERROR. |
| `PORT` | optional | `5000` (dev) / `8000` (Docker) | Bind port. |

---

## Project structure

```
audit-wizard/
├── app.py                    Flask routes + wizard flow
├── audit_engine.py           Crawler, Analyzer, ReportGen (the audit logic)
├── jobs.py                   Background job manager (in-memory + thread-safe)
├── email_sender.py           Resend integration with outbox fallback
├── wsgi.py                   Gunicorn entry point
├── requirements.txt
├── Dockerfile
├── Procfile
├── render.yaml               Render Blueprint
├── .env.example
├── .gitignore, .dockerignore
├── LICENSE
├── README.md
├── templates/
│   ├── base.html             Shared layout + header + stepper
│   ├── step1.html            URL/industry/pages/email form
│   ├── step2.html            Live progress with polling
│   ├── step3_results.html    Score cards + downloads + embedded interactive report
│   └── step3_failed.html     Error page
├── static/
│   └── style.css
├── tests/
│   └── test_audit_engine.py  39 offline tests
└── .github/workflows/
    └── ci.yml                Pytest + Docker build on every push
```

---

## How a crawl works

1. **Seed** with the URL the user provided.
2. **Fetch** in parallel (5 threads by default), collect HTML if `Content-Type: text/html`.
3. **Extract links** from each page: same domain only, http/https only, no PDFs/images/scripts/archives.
4. **Add new links to the queue.** Continue until `max_pages` reached, or queue is empty for 4 rounds.
5. **Analyse** each page: SEO + HTML + performance + content (universal), plus industry-specific checks.
6. **Score** per category + an overall score (mean of all categories, 0-100).
7. **Generate** HTML / Excel / CSV reports. Email if requested.

For metals sites specifically, the analyzer matches against three databases:
- `ASTM_GRADES` — chemical composition limits (C, Mn, Si, P, S, Cr, Ni, Mo, etc.)
- `MECHANICAL_PROPS` — tensile, yield, elongation, hardness specs
- `EQUIVALENT_GRADES` — UNS, EN, DIN, JIS, BS designations

If a page mentions grade 304 but the chemical table omits Carbon, that's a critical issue. If it mentions 316L but doesn't list Molybdenum, that's a warning. Etc.

---

## Limitations and gotchas

- **Render free tier sleeps after 15 min idle.** First request after that takes ~50s to wake. Tell your team to expect this on the first audit each session.
- **In-memory job state** — if the Render instance restarts, in-flight audits are lost. The user gets a "job not found" if they had the progress tab open. Acceptable for a small team tool; not for high-availability.
- **Single Gunicorn worker (`--workers 1`)** — required for the in-memory job dict. Threads (`--threads 4`) handle concurrent users fine.
- **Reports auto-purge after 1 hour.** Tell your team to download the HTML/Excel before closing the tab if they need to keep it.
- **Robots.txt is NOT respected** — this is an internal audit tool, not a polite crawler. Tell your team to only audit sites they own or have permission to scan.
- **JavaScript-rendered content is missed** — we use `requests` + `BeautifulSoup`, not a headless browser. Single-page apps with client-rendered content will show empty pages. If your team needs SPA support, that's an upgrade to Playwright (different stack, much heavier).

---

## Running tests

```bash
pip install pytest
pytest -v tests/
```

39 tests, all offline (no network needed). They cover the analyzer, report generators, job manager, Flask routes, and reference databases. CI runs them on Python 3.11 and 3.12 on every push.

---

## Updating the live app

```bash
# make code changes
git add .
git commit -m "describe what changed"
git push
```

Render auto-deploys every push to `main`. CI runs first, so if tests fail you'll see a red ❌ on the commit. Push only when CI is green to avoid breaking your team's URL.

---

## Troubleshooting

- **"Could not fetch any pages"** — the URL may be wrong, the site may be down, or it might be blocking automated user agents. Try a different URL to confirm. The user agent is set to a real Chrome string (see `audit_engine.py`'s `Crawler.__init__`).
- **Audit hangs at "Crawling..."** — the site is slow or large. Wait. The progress bar updates every 2 seconds. Cancel button stops it.
- **"Job not found" mid-audit** — Render restarted the instance. Free tier does this after 15 min idle. Solution: keep the tab open or upgrade to Starter (no spin-down).
- **Email not arriving** — check `RESEND_API_KEY` is set in Render → Environment. Check spam folder. Check the Resend dashboard's Logs tab to see if it was actually sent. Without a verified domain, deliverability is mediocre.
- **All pages have score 0** — usually means the site blocks crawlers. Look at the Logs tab in Render to see HTTP errors.

---

## License

MIT — see [LICENSE](LICENSE).
