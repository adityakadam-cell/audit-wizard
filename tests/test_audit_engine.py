"""
Offline tests for the audit wizard.

These run without network — synthetic HTML pages are fed straight into the
Analyzer. The Crawler isn't tested live (network); we only verify it
constructs without errors.
"""

import io
import pytest

from audit_engine import (
    Analyzer, Crawler, ReportGen, detect_industry,
    INDUSTRY_PROFILES, ASTM_GRADES, MECHANICAL_PROPS, EQUIVALENT_GRADES,
    GRADE_RE, score_color,
)
import jobs


# ============================================================
#  Helpers
# ============================================================

def make_page(url: str, html: str, response_time: float = 0.5,
              content_length: int = 1000, status: int = 200) -> dict:
    """Build a fake page dict shaped like Crawler.fetch() returns."""
    return {
        "url": url,
        "html": html,
        "response_time": response_time,
        "content_length": content_length or len(html),
        "status": status,
    }


SAMPLE_METALS_HTML = """\
<html>
<head>
  <title>SS 304 Seamless Pipe Manufacturer | Example Co</title>
  <meta name="description" content="Premium SS 304 stainless steel seamless pipes manufactured to ASTM A312 standards. Various sizes for industrial use worldwide.">
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://example.com/304-pipe">
  <link rel="icon" href="/favicon.ico">
  <script type="application/ld+json">{"@type":"Product"}</script>
  <meta property="og:title" content="SS 304 Pipe">
</head>
<body>
  <h1>SS 304 Seamless Pipe</h1>
  <h2>Chemical Composition</h2>
  <p>Carbon (C): 0.08 max, Manganese (Mn): 2.00, Chromium (Cr): 18-20%, Nickel (Ni): 8-10.5%</p>
  <h2>Mechanical Properties</h2>
  <p>Tensile Strength: 515 MPa min, Yield Strength: 205 MPa min, Elongation: 40% min, Hardness: 201 HB max</p>
  <h2>Standards</h2>
  <p>Manufactured to ASTM A312 / ASME SA312. Equivalent grades: UNS S30400, EN 1.4301, DIN X5CrNi18-10, JIS SUS304.</p>
  <h2>Applications</h2>
  <p>Used in chemical plants, dairy equipment, and food processing industries.</p>
  <h2>Dimensions</h2>
  <p>Available in OD 6mm to 600mm, wall thickness 1mm to 60mm.</p>
  <h2>Contact</h2>
  <p>Phone: +91-9999999999, Email: sales@example.com. Get a quote today!</p>
  <img src="pipe1.jpg" alt="SS 304 pipe close-up">
  <img src="pipe2.jpg" alt="Pipe stack">
</body></html>
"""


# ============================================================
#  detect_industry
# ============================================================

def test_detect_industry_metals():
    text = "stainless steel pipe ASTM A312 grade 304 chromium"
    assert detect_industry("https://x.com/pipe", text) == "metals"


def test_detect_industry_ecommerce():
    text = "buy now add to cart checkout shipping price"
    assert detect_industry("https://shop.com/product", text) == "ecommerce"


def test_detect_industry_generic_when_unmatched():
    text = "lorem ipsum dolor sit amet"
    assert detect_industry("https://x.com", text) == "generic"


# ============================================================
#  Analyzer — universal checks
# ============================================================

def test_analyzer_returns_overall_score():
    page = make_page("https://example.com", SAMPLE_METALS_HTML)
    a = Analyzer(industry="metals")
    r = a.analyze(page)
    assert "overall" in r["scores"]
    assert 0 <= r["scores"]["overall"] <= 100


def test_analyzer_detects_metals_industry_with_auto():
    page = make_page("https://example.com/pipe", SAMPLE_METALS_HTML)
    a = Analyzer(industry="auto")
    r = a.analyze(page)
    assert r["industry"] == "metals"


def test_analyzer_returns_required_fields():
    page = make_page("https://example.com", SAMPLE_METALS_HTML)
    a = Analyzer()
    r = a.analyze(page)
    for field in ["url", "title", "issues", "scores", "word_count",
                  "grades_found", "response_time", "industry"]:
        assert field in r, f"missing field: {field}"


def test_analyzer_handles_empty_html():
    page = make_page("https://example.com", "", status=403)
    a = Analyzer()
    r = a.analyze(page)
    assert r["title"] == "[blocked]"
    assert r["issues"] == []


def test_analyzer_flags_missing_title():
    html = "<html><body><h1>Hello</h1></body></html>"
    a = Analyzer(industry="generic")
    r = a.analyze(make_page("https://x.com", html))
    titles = [i["title"] for i in r["issues"]]
    assert any("Title tag missing" in t for t in titles)


def test_analyzer_flags_missing_h1():
    html = "<html><head><title>OK title</title></head><body><p>no h1</p></body></html>"
    a = Analyzer(industry="generic")
    r = a.analyze(make_page("https://x.com", html))
    titles = [i["title"] for i in r["issues"]]
    assert any("H1 missing" in t for t in titles)


def test_analyzer_flags_noindex_as_critical():
    html = """<html><head><title>X</title><meta name="robots" content="noindex">
              </head><body><h1>X</h1></body></html>"""
    a = Analyzer(industry="generic")
    r = a.analyze(make_page("https://x.com", html))
    noindex_issues = [i for i in r["issues"] if "NOINDEX" in i["title"]]
    assert len(noindex_issues) == 1
    assert noindex_issues[0]["severity"] == "critical"


def test_analyzer_flags_http_as_critical():
    html = "<html><head><title>X</title></head><body><h1>x</h1></body></html>"
    a = Analyzer(industry="generic")
    r = a.analyze(make_page("http://insecure.com", html))
    http_issues = [i for i in r["issues"] if "HTTPS" in i["title"]]
    assert len(http_issues) >= 1


def test_analyzer_flags_slow_response_time():
    html = "<html><head><title>OK</title></head><body><h1>Slow</h1></body></html>"
    page = make_page("https://x.com", html, response_time=4.5)
    a = Analyzer(industry="generic")
    r = a.analyze(page)
    perf_issues = [i for i in r["issues"] if i["category"] == "performance"]
    assert any("Slow" in i["title"] for i in perf_issues)


# ============================================================
#  Analyzer — metals-specific checks
# ============================================================

def test_metals_check_misses_when_chemical_table_absent():
    html = """<html><head><title>SS 304 Pipe Manufacturer ASTM</title></head>
              <body><h1>SS 304 Pipe</h1><p>We make 304 pipes</p></body></html>"""
    a = Analyzer(industry="metals")
    r = a.analyze(make_page("https://x.com/304-pipe", html))
    cats = [i["category"] for i in r["issues"]]
    assert "chemical" in cats
    assert "mechanical" in cats


def test_metals_check_finds_grade_via_regex():
    matches = GRADE_RE.findall("Our SS 316L tubes meet ASTM A213 specifications")
    assert any("316L" in m or "316" in m for m in matches)
    assert any("A213" in m for m in matches)


# ============================================================
#  ReportGen
# ============================================================

@pytest.fixture
def sample_results():
    a = Analyzer(industry="auto")
    return [
        a.analyze(make_page("https://example.com/", SAMPLE_METALS_HTML)),
        a.analyze(make_page("https://example.com/about",
                            "<html><head><title>About Example Co</title></head>"
                            "<body><h1>About</h1><p>" + ("text " * 100) + "</p></body></html>")),
    ]


def test_reportgen_html_returns_complete_document(sample_results):
    gen = ReportGen()
    html = gen.html(sample_results, "https://example.com")
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "Website Audit Report" in html
    assert "https://example.com/" in html


def test_reportgen_excel_returns_valid_xlsx_bytes(sample_results):
    gen = ReportGen()
    blob = gen.excel(sample_results)
    assert isinstance(blob, bytes)
    # XLSX files are zip files — start with PK
    assert blob[:2] == b"PK"
    # Verify openpyxl can read it back
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert "Summary" in wb.sheetnames
    assert "All Issues" in wb.sheetnames
    assert "Chemical Reference" in wb.sheetnames
    assert "Equivalent Grades" in wb.sheetnames


def test_reportgen_csv_has_header_and_rows(sample_results):
    gen = ReportGen()
    csv_text = gen.csv_report(sample_results)
    lines = csv_text.strip().split("\n")
    assert lines[0].startswith("URL,Title")
    assert len(lines) >= len(sample_results) + 1  # header + rows


# ============================================================
#  Score color
# ============================================================

def test_score_color_returns_green_for_high_score():
    fg, bg = score_color(85)
    assert fg.startswith("#22")  # dark green


def test_score_color_returns_red_for_low_score():
    fg, bg = score_color(40)
    assert fg.startswith("#74")  # dark red


# ============================================================
#  Reference data sanity
# ============================================================

def test_astm_grades_database_has_common_grades():
    for g in ["304", "304L", "316", "316L", "2205"]:
        assert g in ASTM_GRADES


def test_mechanical_props_match_grades():
    # Every grade with mechanical props should also have chemical data
    for g in MECHANICAL_PROPS:
        assert g in ASTM_GRADES, f"Grade {g} has mechanical but no chemical data"


def test_equivalent_grades_have_uns():
    for g, eq in EQUIVALENT_GRADES.items():
        assert "UNS" in eq


def test_industry_profiles_all_have_required_keys():
    for k, p in INDUSTRY_PROFILES.items():
        assert "name" in p
        assert "keywords" in p
        assert "required_sections" in p


# ============================================================
#  Job manager
# ============================================================

def test_job_create_assigns_id_and_starts_pending():
    j = jobs.create_job("https://x.com", "auto", 30)
    assert j.id
    assert j.status == "pending"
    assert j.url == "https://x.com"
    assert j.max_pages == 30


def test_job_get_returns_created_job():
    j = jobs.create_job("https://y.com", "auto", 10)
    same = jobs.get_job(j.id)
    assert same is j


def test_job_get_unknown_id_returns_none():
    assert jobs.get_job("does-not-exist") is None


def test_job_progress_updates():
    j = jobs.create_job("https://z.com", "auto", 50)
    j.update_progress(7, 50, "https://z.com/page-7")
    s = j.to_status_dict()
    assert s["current"] == 7
    assert s["total"] == 50
    assert s["current_url"] == "https://z.com/page-7"
    assert s["percent"] == 14


def test_job_cancel_signals_stop():
    j = jobs.create_job("https://c.com", "auto", 5)
    assert not j.should_stop()
    j.cancel()
    assert j.should_stop()


def test_get_summary_stats_handles_empty_results():
    j = jobs.create_job("https://x.com", "auto", 10)
    stats = jobs.get_summary_stats(j)
    assert stats["page_count"] == 0
    assert stats["avg_score"] == 0


# ============================================================
#  Crawler construction (no network)
# ============================================================

def test_crawler_construction_does_not_crash():
    c = Crawler("https://example.com", max_pages=10)
    assert c.base_url == "https://example.com"
    assert c.base_domain == "example.com"
    assert c.max_pages == 10


def test_crawler_threads_clamped_to_safe_range():
    c1 = Crawler("https://x.com", threads=0)
    c2 = Crawler("https://x.com", threads=99)
    assert c1.threads == 1
    assert c2.threads == 10


def test_crawler_extract_links_filters_other_domains():
    c = Crawler("https://example.com", max_pages=10)
    html = """<html><body>
        <a href="/about">About</a>
        <a href="https://example.com/contact">Contact</a>
        <a href="https://other-site.com/page">Other</a>
        <a href="mailto:hi@example.com">Email</a>
        <a href="file.pdf">PDF (skip)</a>
    </body></html>"""
    links = c.extract_links(html, "https://example.com/")
    # Only same-domain http(s) links, no PDFs
    assert "https://example.com/about" in links
    assert "https://example.com/contact" in links
    assert not any("other-site.com" in u for u in links)
    assert not any(".pdf" in u for u in links)


# ============================================================
#  Flask routes
# ============================================================

@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_root_redirects_to_step1(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/step1" in r.location


def test_step1_renders_form(client):
    r = client.get("/step1")
    assert r.status_code == 200
    assert b"<form" in r.data
    assert b"Website URL" in r.data


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok"}


def test_step1_post_creates_job_and_redirects(client):
    r = client.post(
        "/step1",
        data={"url": "https://example.com", "industry": "auto",
              "max_pages": "5", "email": ""},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/step2/" in r.location


def test_step1_post_rejects_invalid_email(client):
    r = client.post(
        "/step1",
        data={"url": "https://example.com", "industry": "auto",
              "max_pages": "5", "email": "not-an-email"},
        follow_redirects=False,
    )
    # Re-renders the form, doesn't redirect
    assert r.status_code == 200
    assert b"invalid" in r.data.lower()


def test_unknown_job_status_returns_404(client):
    r = client.get("/api/job/unknown_id_123/status")
    assert r.status_code == 404


def test_unknown_download_returns_404(client):
    r = client.get("/job/unknown_id_123/download.html")
    assert r.status_code == 404
