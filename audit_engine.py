"""
Audit engine — the heart of the wizard.

Refactored from website_auditor_v2's audit_agent.py. Same logic, but reorganised
for web use: the Crawler and Analyzer accept progress callbacks so the Flask app
can stream live progress to the browser, and the report generators take a
results list and return strings/bytes (no direct file writing).

Public API:
    Crawler(base_url, max_pages, threads, delay, timeout, skip_ssl, on_progress, should_stop)
        .crawl() -> list[dict]
    Analyzer(industry='auto')
        .analyze(page) -> dict
    ReportGen()
        .html(results, site_url) -> str
        .excel(results) -> bytes
        .csv_report(results) -> str
    detect_industry(url, text) -> str
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Queue
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import openpyxl
from openpyxl.styles import Font, PatternFill
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


# ============================================================
#  Industry profiles
# ============================================================

INDUSTRY_PROFILES = {
    "metals": {
        "name": "Metals / Steel / Industrial",
        "keywords": ["steel", "pipe", "tube", "plate", "bar", "flange", "fitting",
                     "alloy", "grade", "astm", "asme", "ss", "stainless"],
        "required_sections": ["chemical composition", "mechanical properties",
                              "dimensions", "equivalent grades", "applications", "standards"],
    },
    "ecommerce": {
        "name": "E-Commerce / Online Store",
        "keywords": ["buy", "cart", "checkout", "product", "price", "shop", "order", "shipping"],
        "required_sections": ["price", "description", "specifications", "reviews", "shipping"],
    },
    "saas": {
        "name": "SaaS / Software",
        "keywords": ["software", "platform", "dashboard", "api", "pricing", "free trial", "demo"],
        "required_sections": ["features", "pricing", "testimonials", "faq"],
    },
    "healthcare": {
        "name": "Healthcare / Medical",
        "keywords": ["doctor", "hospital", "clinic", "patient", "treatment", "medical", "appointment"],
        "required_sections": ["services", "doctors", "appointment", "contact"],
    },
    "realestate": {
        "name": "Real Estate",
        "keywords": ["property", "flat", "apartment", "villa", "bhk", "sqft", "rent", "buy"],
        "required_sections": ["price", "location", "amenities", "contact"],
    },
    "generic": {
        "name": "Generic Website",
        "keywords": [],
        "required_sections": ["about", "contact", "services"],
    },
}


# ============================================================
#  Metals reference data
# ============================================================

ASTM_GRADES = {
    "304":  {"C": "0.08", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "18.0-20.0", "Ni": "8.0-10.5"},
    "304L": {"C": "0.030", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "18.0-20.0", "Ni": "8.0-12.0"},
    "316":  {"C": "0.08", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "16.0-18.0", "Ni": "10.0-14.0", "Mo": "2.0-3.0"},
    "316L": {"C": "0.030", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "16.0-18.0", "Ni": "10.0-14.0", "Mo": "2.0-3.0"},
    "321":  {"C": "0.08", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "17.0-19.0", "Ni": "9.0-12.0", "Ti": "5xC min"},
    "347":  {"C": "0.08", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "17.0-19.0", "Ni": "9.0-13.0"},
    "310S": {"C": "0.08", "Mn": "2.00", "Si": "1.50", "P": "0.045", "S": "0.030",
             "Cr": "24.0-26.0", "Ni": "19.0-22.0"},
    "317L": {"C": "0.030", "Mn": "2.00", "Si": "0.75", "P": "0.045", "S": "0.030",
             "Cr": "18.0-20.0", "Ni": "11.0-15.0", "Mo": "3.0-4.0"},
    "904L": {"C": "0.020", "Mn": "2.00", "Si": "1.00", "P": "0.045", "S": "0.035",
             "Cr": "19.0-23.0", "Ni": "23.0-28.0", "Mo": "4.0-5.0"},
    "2205": {"C": "0.030", "Mn": "2.00", "Si": "1.00", "P": "0.030", "S": "0.020",
             "Cr": "22.0-23.0", "Ni": "4.5-6.5", "Mo": "3.0-3.5", "N": "0.14-0.20"},
    "2507": {"C": "0.030", "Mn": "1.20", "Si": "0.80", "P": "0.035", "S": "0.020",
             "Cr": "24.0-26.0", "Ni": "6.0-8.0", "Mo": "3.0-5.0"},
    "A36":  {"C": "0.26", "P": "0.04", "S": "0.05"},
    "A53":  {"C": "0.25", "Mn": "0.95", "P": "0.05", "S": "0.045"},
    "A106": {"C": "0.30", "Mn": "0.29-1.06", "Si": "0.10", "P": "0.035", "S": "0.035"},
}

MECHANICAL_PROPS = {
    "304":  {"tensile": "515 MPa min", "yield": "205 MPa min", "elongation": "40% min", "hardness": "201 HB max"},
    "304L": {"tensile": "485 MPa min", "yield": "170 MPa min", "elongation": "40% min", "hardness": "201 HB max"},
    "316":  {"tensile": "515 MPa min", "yield": "205 MPa min", "elongation": "40% min", "hardness": "217 HB max"},
    "316L": {"tensile": "485 MPa min", "yield": "170 MPa min", "elongation": "40% min", "hardness": "217 HB max"},
    "321":  {"tensile": "515 MPa min", "yield": "205 MPa min", "elongation": "40% min", "hardness": "217 HB max"},
    "310S": {"tensile": "515 MPa min", "yield": "205 MPa min", "elongation": "40% min", "hardness": "217 HB max"},
    "904L": {"tensile": "490 MPa min", "yield": "220 MPa min", "elongation": "35% min", "hardness": "—"},
    "2205": {"tensile": "620 MPa min", "yield": "450 MPa min", "elongation": "25% min", "hardness": "293 HB max"},
    "2507": {"tensile": "800 MPa min", "yield": "550 MPa min", "elongation": "15% min", "hardness": "310 HB max"},
}

EQUIVALENT_GRADES = {
    "304":  {"UNS": "S30400", "EN": "1.4301", "DIN": "X5CrNi18-10", "JIS": "SUS304", "BS": "304S31"},
    "304L": {"UNS": "S30403", "EN": "1.4307", "DIN": "X2CrNi19-11", "JIS": "SUS304L", "BS": "304S11"},
    "316":  {"UNS": "S31600", "EN": "1.4401", "DIN": "X5CrNiMo17-12-2", "JIS": "SUS316", "BS": "316S31"},
    "316L": {"UNS": "S31603", "EN": "1.4404", "DIN": "X2CrNiMo17-12-2", "JIS": "SUS316L", "BS": "316S11"},
    "321":  {"UNS": "S32100", "EN": "1.4541", "DIN": "X6CrNiTi18-10", "JIS": "SUS321", "BS": "321S31"},
    "310S": {"UNS": "S31008", "EN": "1.4845", "DIN": "X8CrNi25-21", "JIS": "SUS310S", "BS": "310S24"},
    "904L": {"UNS": "N08904", "EN": "1.4539", "DIN": "X1NiCrMoCu25-20-5", "JIS": "SUS890L", "BS": "904S13"},
    "2205": {"UNS": "S32205", "EN": "1.4462", "DIN": "X2CrNiMoN22-5-3", "JIS": "SUS329J3L", "BS": "—"},
    "2507": {"UNS": "S32750", "EN": "1.4410", "DIN": "X2CrNiMoN25-7-4", "JIS": "—", "BS": "—"},
}

ASTM_STANDARDS = {
    "pipe":     ["ASTM A312", "ASTM A790", "ASTM A358", "ASTM A409", "ASME SA312"],
    "tube":     ["ASTM A213", "ASTM A249", "ASTM A269", "ASTM A270", "ASTM A554", "ASME SA213"],
    "plate":    ["ASTM A240", "ASTM A480", "ASME SA240"],
    "bar":      ["ASTM A276", "ASTM A479", "ASTM A484", "ASME SA479"],
    "fitting":  ["ASTM A403", "ASTM A182", "ASTM A815", "ASME SA403"],
    "flange":   ["ASTM A182", "ASTM A240", "ASME SA182"],
    "sheet":    ["ASTM A240", "ASTM A167", "ASTM A480"],
    "fastener": ["ASTM A193", "ASTM A194", "ASTM F593", "ASME SA193"],
}

GRADE_RE = re.compile(
    r'\b(SS\s*304L?|SS\s*316L?|SS\s*321|SS\s*310S?|SS\s*317L?|SS\s*904L|'
    r'304L?|316L?|321|310S?|317L?|904L|2205|2507|347|'
    r'A312|A213|A240|A276|A403|A182|A358|A790|A106|A53|A36|'
    r'ASTM\s+[A-Z]\d+|ASME\s+SA\d+|EN\s+1\.\d{4}|JIS\s+SUS\d+)\b',
    re.IGNORECASE,
)

SKIP_EXT = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico',
    '.css', '.js', '.zip', '.gz', '.mp4', '.mp3', '.woff', '.woff2', '.ttf', '.eot',
}

SEV_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


# ============================================================
#  Helpers
# ============================================================

def detect_industry(url: str, text: str) -> str:
    """Pick the industry profile whose keywords best match the page."""
    combined = (url + " " + text[:3000]).lower()
    scores = {
        k: sum(1 for kw in v["keywords"] if kw in combined)
        for k, v in INDUSTRY_PROFILES.items() if k != "generic"
    }
    best = max(scores, key=scores.get) if scores else "generic"
    return best if scores.get(best, 0) > 0 else "generic"


def _iss(category: str, severity: str, title: str, description: str, fix: str = "") -> dict:
    """Build an issue dict."""
    return {
        "category": category,
        "severity": severity,
        "title": title,
        "description": description,
        "fix": fix,
    }


def score_color(s: int) -> tuple[str, str]:
    """Return (foreground, background) hex color for a score 0-100."""
    if s >= 80:
        return ('#22543d', '#c6f6d5')
    if s >= 55:
        return ('#7b341e', '#feebc8')
    return ('#742a2a', '#fed7d7')


# ============================================================
#  Crawler
# ============================================================

class Crawler:
    """
    Same-origin crawler with parallel fetching, link extraction, and progress
    callbacks. Honours a stop signal so the Flask app can cancel mid-crawl.

    Args:
        base_url: starting URL
        max_pages: hard cap on pages to fetch
        threads: parallel fetcher count (1-10)
        delay: pause between batches (seconds)
        timeout: per-request timeout (seconds)
        skip_ssl: bypass SSL verification (for sites with bad certs)
        on_progress: callback(current, total, current_url) - called after each page
        should_stop: callable() -> bool - return True to abort the crawl
    """

    def __init__(
        self,
        base_url: str,
        max_pages: int = 100,
        threads: int = 5,
        delay: float = 0.3,
        timeout: int = 15,
        skip_ssl: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        parsed = urlparse(base_url)
        self.base_url = base_url.rstrip('/')
        self.base_domain = parsed.netloc
        self.max_pages = max_pages
        self.threads = max(1, min(10, threads))
        self.delay = delay
        self.timeout = timeout
        self.skip_ssl = skip_ssl
        self.on_progress = on_progress or (lambda c, t, u: None)
        self.should_stop = should_stop or (lambda: False)

        self.visited: set[str] = {self.base_url}
        self.q: Queue = Queue()
        self.q.put(self.base_url)
        self.pages: list[dict] = []
        self.lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers['User-Agent'] = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/121.0 Safari/537.36 audit-wizard/1.0'
        )

    def fetch(self, url: str) -> Optional[dict]:
        """Fetch a single URL and return a page dict (or None for non-HTML)."""
        try:
            t0 = time.time()
            r = self.session.get(
                url, timeout=self.timeout,
                verify=not self.skip_ssl, allow_redirects=True,
            )
            ct = r.headers.get('content-type', '')
            if 'text/html' not in ct.lower():
                return None
            return {
                'url': url,
                'status': r.status_code,
                'html': r.text,
                'response_time': time.time() - t0,
                'content_length': len(r.content),
            }
        except Exception as e:
            return {
                'url': url, 'status': 0, 'html': '',
                'response_time': 0, 'error': str(e),
            }

    def extract_links(self, html: str, base: str) -> set[str]:
        """Extract same-domain links from a page, normalised and deduped."""
        soup = BeautifulSoup(html, 'lxml')
        out: set[str] = set()
        for a in soup.find_all('a', href=True):
            href = a['href'].split('#')[0].split('?')[0].strip()
            if not href:
                continue
            full = urljoin(base, href)
            p = urlparse(full)
            if p.netloc != self.base_domain:
                continue
            if any(p.path.lower().endswith(e) for e in SKIP_EXT):
                continue
            if p.scheme not in ('http', 'https'):
                continue
            clean = p.scheme + '://' + p.netloc + p.path.rstrip('/')
            if clean and clean not in self.visited:
                out.add(clean)
        return out

    def crawl(self) -> list[dict]:
        """Run the crawl. Returns the list of fetched pages."""
        empty_rounds = 0
        while len(self.pages) < self.max_pages and empty_rounds < 4:
            if self.should_stop():
                break

            batch: list[str] = []
            while not self.q.empty() and len(batch) < self.threads:
                try:
                    batch.append(self.q.get_nowait())
                except Exception:
                    break

            if not batch:
                empty_rounds += 1
                time.sleep(0.5)
                continue
            empty_rounds = 0

            with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futures = {ex.submit(self.fetch, u): u for u in batch}
                for fut in as_completed(futures):
                    if self.should_stop():
                        break
                    res = fut.result()
                    if res and res.get('html') and res.get('status') == 200:
                        with self.lock:
                            if len(self.pages) < self.max_pages:
                                self.pages.append(res)
                                self.on_progress(
                                    len(self.pages), self.max_pages, res['url']
                                )
                                for lk in self.extract_links(res['html'], res['url']):
                                    if lk not in self.visited:
                                        self.visited.add(lk)
                                        self.q.put(lk)

            if self.delay:
                time.sleep(self.delay)

        return self.pages


# ============================================================
#  Page analyzer
# ============================================================

class Analyzer:
    """
    Analyse a fetched page for SEO, HTML, performance, content, and
    industry-specific issues.
    """

    def __init__(self, industry: str = 'auto'):
        self.industry = industry

    def analyze(self, pg: dict) -> dict:
        url = pg.get('url', '')
        html = pg.get('html', '')
        if not html:
            return {
                'url': url, 'title': '[blocked]', 'issues': [], 'scores': {},
                'word_count': 0, 'grades_found': [], 'response_time': 0,
                'industry': '—', 'status': pg.get('status', 0),
            }

        soup = BeautifulSoup(html, 'lxml')
        text = soup.get_text(' ', strip=True)
        ind = self.industry if self.industry != 'auto' else detect_industry(url, text)

        issues: list[dict] = []
        scores: dict[str, int] = {}

        # Universal checks (every industry gets these)
        r = self._check_seo(soup, text, url)
        issues += r['i']; scores['seo'] = r['s']
        r = self._check_html(soup, url)
        issues += r['i']; scores['html'] = r['s']
        r = self._check_performance(pg, soup)
        issues += r['i']; scores['performance'] = r['s']
        r = self._check_content(soup, text, ind)
        issues += r['i']; scores['content'] = r['s']

        # Industry-specific checks
        if ind == 'metals':
            grades = list(set(GRADE_RE.findall(text)))
            issues += self._check_chemical(text, grades)
            issues += self._check_mechanical(text, grades)
            issues += self._check_astm_standards(text, url)
            issues += self._check_equivalents(text, grades)
            scores['chemical']   = max(0, 100 - len([i for i in issues if i['category'] == 'chemical']) * 15)
            scores['mechanical'] = max(0, 100 - len([i for i in issues if i['category'] == 'mechanical']) * 15)
            scores['standards']  = max(0, 100 - len([i for i in issues if i['category'] == 'astm']) * 12)
            scores['equivalent'] = max(0, 100 - len([i for i in issues if i['category'] == 'equivalent']) * 10)
        elif ind == 'ecommerce':
            r = self._check_ecommerce(soup, text)
            issues += r['i']; scores['ecommerce'] = r['s']
        elif ind == 'saas':
            r = self._check_saas(soup, text)
            issues += r['i']; scores['saas'] = r['s']
        elif ind == 'healthcare':
            r = self._check_healthcare(soup, text)
            issues += r['i']; scores['healthcare'] = r['s']
        elif ind == 'realestate':
            r = self._check_realestate(soup, text)
            issues += r['i']; scores['realestate'] = r['s']

        scores['overall'] = round(sum(scores.values()) / max(1, len(scores)))

        title_tag = soup.find('title')
        title = title_tag.get_text(strip=True) if title_tag else ''
        grades_found = list(set(GRADE_RE.findall(text))) if ind == 'metals' else []

        return {
            'url': url, 'title': title, 'issues': issues, 'scores': scores,
            'word_count': len(text.split()), 'grades_found': grades_found,
            'response_time': pg.get('response_time', 0),
            'industry': ind, 'status': pg.get('status', 200),
        }

    # ---------- SEO ----------
    def _check_seo(self, soup, text, url) -> dict:
        issues: list[dict] = []
        score = 100

        title_tag = soup.find('title')
        title = title_tag.get_text(strip=True) if title_tag else ''
        if not title:
            issues.append(_iss('seo', 'critical', 'Title tag missing',
                               'Most basic SEO element. Search results show this.',
                               '<title>Product Name | Brand</title>'))
            score -= 25
        elif len(title) < 30:
            issues.append(_iss('seo', 'warning', f'Title too short ({len(title)} chars)',
                               '50-60 characters is ideal for search snippets.',
                               f'Expand: "{title} | Grade | Company"'))
            score -= 10
        elif len(title) > 65:
            issues.append(_iss('seo', 'warning', f'Title too long ({len(title)} chars)',
                               'Google truncates after ~65 characters.',
                               'Trim to 55-60 chars'))
            score -= 5

        md = soup.find('meta', attrs={'name': re.compile(r'^description$', re.I)})
        desc = md.get('content', '').strip() if md else ''
        if not desc:
            issues.append(_iss('seo', 'critical', 'Meta description missing',
                               'No search snippet will be shown.',
                               '<meta name="description" content="150 char description...">'))
            score -= 20
        elif len(desc) < 80:
            issues.append(_iss('seo', 'warning', f'Meta description too short ({len(desc)} chars)',
                               '120-158 chars is ideal.',
                               f'Expand: "{desc[:50]}..."'))
            score -= 8
        elif len(desc) > 160:
            issues.append(_iss('seo', 'info', f'Meta description too long ({len(desc)} chars)',
                               'Google truncates after ~158 chars.', 'Trim it down'))
            score -= 3

        h1s = soup.find_all('h1')
        if not h1s:
            issues.append(_iss('seo', 'critical', 'H1 missing',
                               'Every page needs one main heading.',
                               '<h1>Main Page Heading</h1>'))
            score -= 20
        elif len(h1s) > 1:
            issues.append(_iss('seo', 'warning', f'{len(h1s)} H1 tags (only 1 needed)',
                               'Multiple H1s confuse search engines.',
                               'Convert extra H1s to H2 or H3'))
            score -= 10

        if not soup.find('link', rel='canonical'):
            issues.append(_iss('seo', 'warning', 'Canonical link missing',
                               'Risk of duplicate content penalties.',
                               f'<link rel="canonical" href="{url}">'))
            score -= 8

        if not soup.find('meta', property='og:title'):
            issues.append(_iss('seo', 'info', 'Open Graph tags missing',
                               'No social media share preview.',
                               '<meta property="og:title" content="...">\n'
                               '<meta property="og:image" content="...">'))
            score -= 5

        if not soup.find('script', type='application/ld+json'):
            issues.append(_iss('seo', 'warning', 'Schema markup missing',
                               'No rich snippets in search results.',
                               '{"@context":"https://schema.org","@type":"WebPage","name":"..."}'))
            score -= 10

        no_alt = [i for i in soup.find_all('img') if not i.get('alt', '').strip()]
        if no_alt:
            issues.append(_iss('seo', 'warning',
                               f'{len(no_alt)} images without alt text',
                               'Image SEO and accessibility are reduced.',
                               '<img src="..." alt="Descriptive text">'))
            score -= min(15, len(no_alt) * 2)

        rob = soup.find('meta', attrs={'name': re.compile(r'^robots$', re.I)})
        if rob and 'noindex' in (rob.get('content', '') or '').lower():
            issues.append(_iss('seo', 'critical', 'Page is set to NOINDEX',
                               'Google will NOT index this page!',
                               'Remove noindex or change to "index,follow"'))
            score -= 40

        if not soup.find_all('h2'):
            issues.append(_iss('seo', 'info', 'No H2 headings',
                               'Content structure is weak.',
                               '<h2>Section Heading</h2>'))
            score -= 5

        return {'i': issues, 's': max(0, score)}

    # ---------- HTML ----------
    def _check_html(self, soup, url) -> dict:
        issues: list[dict] = []
        score = 100

        if not soup.find('meta', charset=True):
            issues.append(_iss('html', 'warning', 'Charset missing',
                               'Encoding issues may occur.',
                               '<meta charset="UTF-8">'))
            score -= 8

        if not soup.find('meta', attrs={'name': 'viewport'}):
            issues.append(_iss('html', 'critical', 'Viewport meta missing',
                               'Mobile rendering will be broken.',
                               '<meta name="viewport" content="width=device-width, initial-scale=1.0">'))
            score -= 20

        if not url.startswith('https'):
            issues.append(_iss('html', 'critical', 'No HTTPS',
                               'No SSL — Google penalises non-HTTPS sites.',
                               "Enable Let's Encrypt SSL on your hosting (free)"))
            score -= 25

        if soup.find(string=re.compile(r'lorem ipsum', re.I)):
            issues.append(_iss('html', 'warning', 'Lorem ipsum text found',
                               'Draft placeholder content is live.',
                               'Replace with real content'))
            score -= 15

        for f in soup.find_all('form'):
            if not f.get('action', ''):
                issues.append(_iss('html', 'warning', 'Form action missing',
                                   'Form will not submit anywhere.',
                                   '<form action="/submit" method="POST">'))
                score -= 10
                break

        if not (soup.find('link', rel=re.compile('icon', re.I))
                or soup.find('link', rel='shortcut icon')):
            issues.append(_iss('html', 'info', 'Favicon missing',
                               'No tab icon will appear in browsers.',
                               '<link rel="icon" href="/favicon.ico">'))
            score -= 5

        return {'i': issues, 's': max(0, score)}

    # ---------- Performance ----------
    def _check_performance(self, pg, soup) -> dict:
        issues: list[dict] = []
        score = 100
        rt = pg.get('response_time', 0)
        sz = pg.get('content_length', 0)

        if rt > 3:
            issues.append(_iss('performance', 'critical',
                               f'Slow page load ({rt:.1f}s)',
                               'Google ranks slower than 3s lower.',
                               'Compress images, use CDN, enable caching'))
            score -= 30
        elif rt > 1.5:
            issues.append(_iss('performance', 'warning',
                               f'Page load somewhat slow ({rt:.1f}s)',
                               '1-3s is improvable.',
                               'Lazy-load images, compress assets'))
            score -= 15

        if sz > 500000:
            issues.append(_iss('performance', 'warning',
                               f'Page size large ({sz // 1024} KB)',
                               '500+ KB is slow on mobile networks.',
                               'Compress images, remove unused CSS/JS'))
            score -= 10

        return {'i': issues, 's': max(0, score)}

    # ---------- Content quality ----------
    def _check_content(self, soup, text, ind) -> dict:
        issues: list[dict] = []
        score = 100
        words = len(text.split())

        if words < 300:
            issues.append(_iss('content', 'warning',
                               f'Thin content ({words} words)',
                               'Aim for 500+ words on key pages.',
                               'Add specifications, FAQs, applications'))
            score -= 20

        profile = INDUSTRY_PROFILES.get(ind, INDUSTRY_PROFILES['generic'])
        for sec in profile['required_sections']:
            if sec not in text.lower():
                issues.append(_iss('content', 'info',
                                   f'"{sec.title()}" section missing',
                                   f'Important section for {ind} sites.',
                                   f'Add a "{sec.title()}" section'))
                score -= 5

        has_contact = bool(re.search(
            r'(\+?[\d\s\-\(\)]{10,15})|([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)', text
        ))
        if not has_contact:
            issues.append(_iss('content', 'warning',
                               'Contact info missing (no phone/email visible)',
                               'Buyers cannot contact you.',
                               'Display phone and email clearly'))
            score -= 15

        ctas = ['contact', 'enquire', 'quote', 'buy now', 'order', 'call us',
                'whatsapp', 'request', 'get started', 'free trial', 'book', 'subscribe']
        if not any(c in text.lower() for c in ctas):
            issues.append(_iss('content', 'warning', 'No CTA detected',
                               'Users have no clear next step.',
                               'Add: "Get Quote" / "Contact Us" / "Enquire Now"'))
            score -= 10

        return {'i': issues, 's': max(0, score)}

    # ---------- Metals: chemical composition ----------
    def _check_chemical(self, text, grades) -> list[dict]:
        issues: list[dict] = []
        for raw in set(g.upper().replace(' ', '') for g in grades):
            clean = raw.replace('SS', '').strip()
            ref = ASTM_GRADES.get(clean)
            if not ref:
                continue

            if 'carbon' not in text.lower():
                issues.append(_iss('chemical', 'critical',
                                   f'{raw}: Carbon % missing',
                                   'Chemical composition table lacks Carbon.',
                                   f'Carbon (C): {ref.get("C", "")} max'))
                continue

            m = re.search(r'[Cc]arbon[^\d]*(\d+\.?\d*)', text)
            if m and ref.get('C'):
                try:
                    val = float(m.group(1))
                    rmax = float(re.findall(r'[\d.]+', ref['C'])[-1])
                    if val > rmax + 0.001:
                        issues.append(_iss('chemical', 'critical',
                                           f'{raw}: Carbon out of spec ({val}% > {rmax}% max)',
                                           'Wrong value vs ASTM specification.',
                                           f'Carbon: {ref["C"]} max'))
                except Exception:
                    pass

            for elem, label in [('Cr', 'Chromium'), ('Ni', 'Nickel'), ('Mo', 'Molybdenum')]:
                if ref.get(elem) and label not in text:
                    issues.append(_iss('chemical', 'warning',
                                       f'{raw}: {label} missing',
                                       f'{label} is important for this grade.',
                                       f'{label} ({elem}): {ref[elem]}'))

        if not grades:
            issues.append(_iss('chemical', 'info', 'No grade detected',
                               'Mention grade clearly.',
                               'e.g. SS 304, SS 316L, ASTM A312 TP304'))
        return issues

    # ---------- Metals: mechanical properties ----------
    def _check_mechanical(self, text, grades) -> list[dict]:
        issues: list[dict] = []
        if not any(t in text.lower() for t in ['tensile', 'yield', 'elongation', 'mpa', 'hardness']):
            issues.append(_iss('mechanical', 'critical',
                               'Mechanical properties table missing',
                               'Buyers need tensile/yield/elongation data.',
                               'Add table: Tensile | Yield | Elongation | Hardness'))
            return issues

        for raw in set(g.upper().replace(' ', '') for g in grades):
            clean = raw.replace('SS', '').strip()
            ref = MECHANICAL_PROPS.get(clean)
            if not ref:
                continue
            for key, label in [('tensile', 'Tensile Strength'), ('yield', 'Yield Strength'),
                               ('elongation', 'Elongation'), ('hardness', 'Hardness')]:
                if key not in text.lower():
                    issues.append(_iss('mechanical', 'warning',
                                       f'{raw}: {label} missing',
                                       f'Mechanical table lacks {label}.',
                                       f'{label}: {ref.get(key, "—")} (per ASTM)'))
        return issues

    # ---------- Metals: ASTM standards ----------
    def _check_astm_standards(self, text, url) -> list[dict]:
        issues: list[dict] = []
        ul = url.lower()
        product_type = next(
            (p for p in ASTM_STANDARDS if p in ul or p in text.lower()),
            None,
        )
        if product_type:
            missing = [s for s in ASTM_STANDARDS[product_type]
                       if s.replace(' ', '') not in text.replace(' ', '')]
            if missing:
                issues.append(_iss('astm', 'warning',
                                   f'Missing standards: {", ".join(missing[:3])}',
                                   f'Required for {product_type} pages.',
                                   f'Add: {" | ".join(missing)}'))
        if not any(s in text for s in ['ASTM', 'ASME', 'DIN', 'EN ', 'JIS']):
            issues.append(_iss('astm', 'critical', 'No standards mentioned',
                               'No ASTM/ASME/DIN/EN/JIS at all.',
                               'Add: ASTM A312 / ASME SA312 / EN 10217-7'))
        return issues

    # ---------- Metals: equivalent grades ----------
    def _check_equivalents(self, text, grades) -> list[dict]:
        issues: list[dict] = []
        for raw in set(g.upper().replace(' ', '') for g in grades):
            clean = raw.replace('SS', '').strip()
            ref = EQUIVALENT_GRADES.get(clean)
            if not ref:
                continue
            missing = next((k for k, v in ref.items() if v and v not in text), None)
            if missing:
                issues.append(_iss('equivalent', 'info',
                                   f'{raw}: {missing} equivalent missing',
                                   'International buyers need equivalents.',
                                   f'UNS {ref.get("UNS", "")} | EN {ref.get("EN", "")} | '
                                   f'DIN {ref.get("DIN", "")} | JIS {ref.get("JIS", "")}'))
        return issues

    # ---------- E-commerce ----------
    def _check_ecommerce(self, soup, text) -> dict:
        issues: list[dict] = []
        score = 100
        if not re.search(r'₹|Rs\.?|\$|€|£|price|cost', text, re.I):
            issues.append(_iss('ecommerce', 'warning', 'Price missing',
                               'Product page without visible price.',
                               'Show price or "Get Quote" button'))
            score -= 20
        if not soup.find('script', type='application/ld+json'):
            issues.append(_iss('ecommerce', 'warning', 'Product schema missing',
                               'Will not appear in Google Shopping.',
                               '{"@type":"Product","name":"...","offers":{"price":"..."}}'))
            score -= 15
        if len(soup.find_all('img')) < 2:
            issues.append(_iss('ecommerce', 'warning', 'Few product images',
                               'Need 3-5+ images per product.',
                               'Add multiple angles, zoom view'))
            score -= 10
        if 'review' not in text.lower():
            issues.append(_iss('ecommerce', 'info', 'Reviews missing',
                               'No social proof.',
                               'Add customer reviews/ratings section'))
            score -= 8
        return {'i': issues, 's': max(0, score)}

    # ---------- SaaS ----------
    def _check_saas(self, soup, text) -> dict:
        issues: list[dict] = []
        score = 100
        if 'pricing' not in text.lower():
            issues.append(_iss('saas', 'warning', 'No pricing info',
                               'Transparent pricing builds trust.',
                               'Add a pricing page link or table'))
            score -= 20
        if not any(t in text.lower() for t in ['testimonial', 'review', 'customer', 'client']):
            issues.append(_iss('saas', 'warning', 'No social proof',
                               'No testimonials visible.',
                               'Add testimonials, client logos, ratings'))
            score -= 15
        if not any(t in text.lower() for t in ['free trial', 'demo', 'get started', 'signup']):
            issues.append(_iss('saas', 'warning', 'No trial/demo CTA',
                               'SaaS sites need a clear conversion path.',
                               'Add "Start Free Trial" or "Book Demo" button'))
            score -= 10
        return {'i': issues, 's': max(0, score)}

    # ---------- Healthcare ----------
    def _check_healthcare(self, soup, text) -> dict:
        issues: list[dict] = []
        score = 100
        if not re.search(r'\d{10}|\+91|\+1', text):
            issues.append(_iss('healthcare', 'critical', 'Phone number missing',
                               'No emergency contact visible.',
                               'Show phone prominently in header'))
            score -= 25
        if 'appointment' not in text.lower() and 'book' not in text.lower():
            issues.append(_iss('healthcare', 'critical', 'Appointment booking missing',
                               'No way to book online.',
                               'Add a "Book Appointment" button'))
            score -= 20
        if not soup.find('script', type='application/ld+json'):
            issues.append(_iss('healthcare', 'warning', 'Medical schema missing',
                               'Clinic info will not appear richly in Google.',
                               '{"@type":"MedicalOrganization","telephone":"..."}'))
            score -= 12
        return {'i': issues, 's': max(0, score)}

    # ---------- Real estate ----------
    def _check_realestate(self, soup, text) -> dict:
        issues: list[dict] = []
        score = 100
        if not re.search(r'₹|cr|lakh|sqft|bhk', text, re.I):
            issues.append(_iss('realestate', 'warning', 'Price/size info missing',
                               'Property details are unclear.',
                               'Add price, area (sqft), BHK clearly'))
            score -= 20
        img_count = len(soup.find_all('img'))
        if img_count < 5:
            issues.append(_iss('realestate', 'warning',
                               f'Too few property images ({img_count})',
                               'Need 8-10+ photos.',
                               'Add: exterior, interior, floor plan, amenities'))
            score -= 15
        if 'map' not in text.lower() and 'location' not in text.lower():
            issues.append(_iss('realestate', 'warning', 'Location/map missing',
                               'No Google Maps embed.',
                               'Embed Google Maps and list nearby landmarks'))
            score -= 15
        return {'i': issues, 's': max(0, score)}


# ============================================================
#  Report generators (HTML, Excel, CSV — for download)
# ============================================================

class ReportGen:
    """Generate downloadable reports in HTML, Excel, and CSV formats."""

    def html(self, results: list[dict], site_url: str) -> str:
        """Return a complete standalone HTML report (string)."""
        now = datetime.now().strftime('%d %b %Y %I:%M %p')
        tp = len(results)
        ti = sum(len(r['issues']) for r in results)
        crit = sum(sum(1 for i in r['issues'] if i['severity'] == 'critical') for r in results)
        warn = sum(sum(1 for i in r['issues'] if i['severity'] == 'warning') for r in results)
        avg = round(sum(r['scores'].get('overall', 0) for r in results) / max(1, tp))

        sev_bg = {'critical': '#fff0f0', 'warning': '#fffbe6', 'info': '#f0f7ff'}
        sev_bd = {'critical': '#e53e3e', 'warning': '#dd6b20', 'info': '#3182ce'}
        sev_bdg = {'critical': '#c53030', 'warning': '#c05621', 'info': '#2b6cb0'}

        pages_html = ''
        for idx, r in enumerate(results):
            sc = r['scores'].get('overall', 0)
            fg, bg = score_color(sc)
            crit_c = sum(1 for i in r['issues'] if i['severity'] == 'critical')
            warn_c = sum(1 for i in r['issues'] if i['severity'] == 'warning')
            info_c = sum(1 for i in r['issues'] if i['severity'] == 'info')

            bars = ''.join(
                f'<div style="display:flex;align-items:center;gap:6px;margin:3px 0">'
                f'<span style="width:95px;font-size:10px;color:#718096;'
                f'text-transform:uppercase;letter-spacing:.3px">{cat}</span>'
                f'<div style="flex:1;height:5px;background:#e2e8f0;border-radius:3px">'
                f'<div style="width:{v}%;height:100%;background:{score_color(v)[0]};'
                f'border-radius:3px"></div></div>'
                f'<b style="width:26px;text-align:right;font-size:11px;'
                f'color:{score_color(v)[0]}">{v}</b></div>'
                for cat, v in r['scores'].items() if cat != 'overall'
            )

            iss_html = ''
            for i in sorted(r['issues'], key=lambda x: SEV_ORDER.get(x['severity'], 9)):
                fix_html = ''
                if i.get('fix'):
                    fe = (i['fix'].replace('&', '&amp;')
                                  .replace('<', '&lt;').replace('>', '&gt;'))
                    fix_html = (
                        f'<div style="background:#f7fafc;border-radius:4px;padding:7px 10px;'
                        f'margin-top:5px"><span style="font-size:9px;font-weight:700;'
                        f'color:#38a169;letter-spacing:.4px">FIX</span>'
                        f'<pre style="font-size:11px;margin:3px 0 0;white-space:pre-wrap;'
                        f'word-break:break-all;font-family:monospace;color:#2d3748">{fe}</pre></div>'
                    )
                iss_html += (
                    f'<div style="padding:9px 11px;border-radius:5px;'
                    f'background:{sev_bg.get(i["severity"], "#fff")};'
                    f'border-left:3px solid {sev_bd.get(i["severity"], "#ccc")};'
                    f'margin-bottom:5px"><div style="display:flex;align-items:flex-start;'
                    f'gap:6px;margin-bottom:4px">'
                    f'<span style="background:#edf2f7;color:#4a5568;font-size:9px;'
                    f'font-weight:700;padding:2px 5px;border-radius:8px;'
                    f'white-space:nowrap">{i["category"].upper()}</span>'
                    f'<span style="background:{sev_bdg.get(i["severity"], "#666")};'
                    f'color:#fff;font-size:9px;font-weight:700;padding:2px 5px;'
                    f'border-radius:8px;white-space:nowrap">{i["severity"].upper()}</span>'
                    f'<b style="font-size:12px;color:#2d3748">{i["title"]}</b></div>'
                    f'<p style="font-size:12px;color:#718096;line-height:1.4;'
                    f'margin:0">{i["description"]}</p>{fix_html}</div>'
                )

            pages_html += (
                f'<div class="pc" id="pc{idx}" data-score="{sc}" data-crit="{crit_c}">'
                f'<div class="ph" onclick="t({idx})">'
                f'<div style="width:40px;height:40px;border-radius:50%;background:{bg};'
                f'color:{fg};font-weight:700;font-size:13px;display:flex;align-items:center;'
                f'justify-content:center;flex-shrink:0">{sc}</div>'
                f'<div style="flex:1;min-width:0">'
                f'<div style="font-weight:600;font-size:13px;overflow:hidden;'
                f'text-overflow:ellipsis;white-space:nowrap">{r["title"] or "(No Title)"}</div>'
                f'<div style="font-size:11px;color:#3182ce;overflow:hidden;'
                f'text-overflow:ellipsis;white-space:nowrap">{r["url"]}</div>'
                f'<div style="display:flex;gap:8px;margin-top:3px;font-size:11px;'
                f'color:#718096;flex-wrap:wrap">'
                f'<span style="color:#c53030;font-weight:600">{crit_c} Critical</span>'
                f'<span style="color:#c05621;font-weight:600">{warn_c} Warn</span>'
                f'<span>{info_c} Info</span>'
                f'<span>{r["word_count"]}w</span>'
                f'<span>{r["response_time"]:.2f}s</span>'
                f'<span style="background:#ebf4ff;color:#2b6cb0;padding:1px 5px;'
                f'border-radius:3px;font-size:10px">{r.get("industry", "?").upper()}</span>'
                f'</div></div>'
                f'<div style="font-size:18px;color:#a0aec0" id="ti{idx}">▾</div></div>'
                f'<div id="pb{idx}" style="display:none;border-top:1px solid #f0f0f0;'
                f'padding:12px 14px"><div style="margin-bottom:10px">{bars}</div>'
                f'{iss_html or "<p style=\'color:#a0aec0;font-size:13px\'>No issues found.</p>"}</div>'
                f'</div>'
            )

        fgav, _ = score_color(avg)
        return (
            '<!DOCTYPE html><html><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Audit — {site_url}</title>'
            '<style>*{box-sizing:border-box;margin:0;padding:0}'
            'body{font-family:system-ui,sans-serif;background:#f7fafc;color:#2d3748;font-size:14px}'
            '.bar{background:#1a202c;color:#fff;padding:12px 18px}'
            '.bar h1{font-size:16px;font-weight:600}'
            '.bar small{color:#a0aec0;font-size:11px}'
            '.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:14px 18px}'
            '.stat{background:#fff;border-radius:8px;padding:12px;text-align:center;'
            'box-shadow:0 1px 2px rgba(0,0,0,.06)}'
            '.stat .n{font-size:24px;font-weight:700}'
            '.stat .l{font-size:10px;color:#718096;text-transform:uppercase;'
            'letter-spacing:.4px;margin-top:2px}'
            '.tb{padding:0 18px 10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}'
            '.fb{padding:5px 11px;border-radius:14px;border:1.5px solid #e2e8f0;'
            'background:#fff;cursor:pointer;font-size:11px;font-weight:500;color:#4a5568}'
            '.fb.on{border-color:#3182ce;background:#ebf8ff;color:#2b6cb0}'
            '.sr{flex:1;min-width:160px;padding:5px 11px;border:1.5px solid #e2e8f0;'
            'border-radius:14px;font-size:12px}'
            '.pg{padding:0 18px 24px;display:flex;flex-direction:column;gap:7px}'
            '.pc{background:#fff;border-radius:9px;box-shadow:0 1px 3px rgba(0,0,0,.06);overflow:hidden}'
            '.ph{display:flex;align-items:center;gap:10px;padding:11px 13px;cursor:pointer}'
            '.ph:hover{background:#f7fafc}'
            '@media(max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}}'
            '</style></head><body>'
            f'<div class="bar"><h1>Website Audit Report</h1>'
            f'<small>{site_url} &nbsp;·&nbsp; {now} &nbsp;·&nbsp; {tp} pages</small></div>'
            f'<div class="stats">'
            f'<div class="stat"><div class="n">{tp}</div><div class="l">Pages</div></div>'
            f'<div class="stat"><div class="n" style="color:#c53030">{crit}</div>'
            f'<div class="l">Critical</div></div>'
            f'<div class="stat"><div class="n" style="color:#c05621">{warn}</div>'
            f'<div class="l">Warnings</div></div>'
            f'<div class="stat"><div class="n">{ti}</div><div class="l">Total Issues</div></div>'
            f'<div class="stat"><div class="n" style="color:{fgav}">{avg}</div>'
            f'<div class="l">Avg Score</div></div></div>'
            f'<div class="tb">'
            f'<button class="fb on" onclick="f(\'all\',this)">All ({tp})</button>'
            f'<button class="fb" onclick="f(\'crit\',this)">Critical Only</button>'
            f'<button class="fb" onclick="f(\'low\',this)">Score &lt;60</button>'
            f'<button class="fb" onclick="f(\'good\',this)">Score &ge;80</button>'
            f'<input class="sr" placeholder="Search URL / title..." oninput="s(this.value)"></div>'
            f'<div class="pg" id="pg">{pages_html}</div>'
            '<script>'
            'function t(i){var b=document.getElementById("pb"+i),'
            'tc=document.getElementById("ti"+i);'
            'if(b.style.display==="none"){b.style.display="block";'
            'tc.style.transform="rotate(180deg)"}else{b.style.display="none";'
            'tc.style.transform=""}}'
            'function f(type,btn){document.querySelectorAll(".fb").forEach(b=>'
            'b.classList.remove("on"));btn.classList.add("on");'
            'document.querySelectorAll(".pc").forEach(c=>{var sc=+c.dataset.score,'
            'cr=+c.dataset.crit;c.style.display=(type==="all"||(type==="crit"&&cr>0)||'
            '(type==="low"&&sc<60)||(type==="good"&&sc>=80))?"":"none"})}'
            'function s(q){q=q.toLowerCase();document.querySelectorAll(".pc").forEach(c=>{'
            'c.style.display=c.querySelector(".ph").textContent.toLowerCase().includes(q)?"":"none"})}'
            '</script></body></html>'
        )

    def excel(self, results: list[dict]) -> bytes:
        """Return the Excel report as bytes (so Flask can stream it)."""
        wb = openpyxl.Workbook()
        hf = PatternFill('solid', fgColor='1A202C')
        hft = Font(color='FFFFFF', bold=True, size=10)

        ws = wb.active
        ws.title = 'Summary'
        ws.append(['URL', 'Title', 'Industry', 'Score', 'SEO', 'HTML', 'Perf', 'Content',
                   'Chemical', 'Mech', 'Standards', 'Equiv', 'Critical', 'Warnings',
                   'Words', 'Load(s)'])
        for c in ws[1]:
            c.fill, c.font = hf, hft

        for r in results:
            sc = r['scores']
            ws.append([
                r['url'], r['title'], r.get('industry', ''),
                sc.get('overall', 0), sc.get('seo', 0), sc.get('html', 0),
                sc.get('performance', 0), sc.get('content', 0),
                sc.get('chemical', '-'), sc.get('mechanical', '-'),
                sc.get('standards', '-'), sc.get('equivalent', '-'),
                sum(1 for i in r['issues'] if i['severity'] == 'critical'),
                sum(1 for i in r['issues'] if i['severity'] == 'warning'),
                r['word_count'], round(r['response_time'], 2),
            ])

        for row in ws.iter_rows(min_row=2):
            for ci in [3, 4, 5, 6, 7, 8, 9, 10, 11]:
                cell = row[ci]
                if isinstance(cell.value, (int, float)):
                    v = cell.value
                    fc = '1D9E75' if v >= 80 else ('BA7517' if v >= 55 else 'E24B4A')
                    cell.font = Font(color=fc, bold=True)

        # All Issues sheet
        wi = wb.create_sheet('All Issues')
        wi.append(['URL', 'Title', 'Category', 'Severity', 'Issue', 'Description', 'Fix'])
        for c in wi[1]:
            c.fill, c.font = hf, hft

        sfills = {
            'critical': PatternFill('solid', fgColor='FEE2E2'),
            'warning':  PatternFill('solid', fgColor='FEF3C7'),
            'info':     PatternFill('solid', fgColor='DBEAFE'),
        }
        for r in results:
            for i in sorted(r['issues'], key=lambda x: SEV_ORDER.get(x['severity'], 9)):
                wi.append([r['url'], r['title'], i['category'], i['severity'],
                           i['title'], i['description'], i.get('fix', '')])
                for c in wi[wi.max_row]:
                    c.fill = sfills.get(i['severity'], PatternFill())

        # Chemical reference sheet
        wc = wb.create_sheet('Chemical Reference')
        wc.append(['Grade', 'C', 'Mn', 'Si', 'P', 'S', 'Cr', 'Ni', 'Mo', 'N/Ti'])
        for c in wc[1]:
            c.fill, c.font = hf, hft
        for g, p in ASTM_GRADES.items():
            wc.append([g, p.get('C', ''), p.get('Mn', ''), p.get('Si', ''),
                       p.get('P', ''), p.get('S', ''), p.get('Cr', ''),
                       p.get('Ni', ''), p.get('Mo', ''), p.get('N', p.get('Ti', ''))])

        # Equivalent grades sheet
        we = wb.create_sheet('Equivalent Grades')
        we.append(['Grade', 'UNS', 'EN', 'DIN', 'JIS', 'BS'])
        for c in we[1]:
            c.fill, c.font = hf, hft
        for g, eq in EQUIVALENT_GRADES.items():
            we.append([g, eq.get('UNS', ''), eq.get('EN', ''),
                       eq.get('DIN', ''), eq.get('JIS', ''), eq.get('BS', '')])

        # Auto-size columns
        for ws2 in wb.worksheets:
            for col in ws2.columns:
                width = min(max(len(str(c.value or '')) for c in col) + 3, 55)
                ws2.column_dimensions[col[0].column_letter].width = width

        # Save to bytes buffer
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def csv_report(self, results: list[dict]) -> str:
        """Return a CSV summary as a string."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['URL', 'Title', 'Industry', 'Score', 'SEO', 'HTML',
                    'Performance', 'Content', 'Critical', 'Warnings', 'Words', 'Load(s)'])
        for r in results:
            sc = r['scores']
            w.writerow([
                r['url'], r['title'], r.get('industry', ''),
                sc.get('overall', 0), sc.get('seo', 0), sc.get('html', 0),
                sc.get('performance', 0), sc.get('content', 0),
                sum(1 for i in r['issues'] if i['severity'] == 'critical'),
                sum(1 for i in r['issues'] if i['severity'] == 'warning'),
                r['word_count'], round(r['response_time'], 2),
            ])
        return buf.getvalue()
