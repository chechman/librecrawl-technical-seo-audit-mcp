#!/usr/bin/env python3
"""
LibreCrawl MCP Server
Wraps LibreCrawl REST API as Claude MCP tools for full-site SEO auditing.
Source: https://github.com/adityaarsharma/librecrawl-mcp
"""

import os
import json
import time
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from html.parser import HTMLParser
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("librecrawl-mcp")

BASE            = f"http://127.0.0.1:{os.getenv('LIBRECRAWL_PORT', '5080')}"
MCP_PORT        = int(os.getenv('MCP_PORT', '5081'))
REPORTS_DIR     = Path(os.getenv('REPORTS_DIR', Path.home() / 'librecrawl-reports'))
PSI_API_KEY     = os.getenv('PAGESPEED_API_KEY', '')   # Google PageSpeed Insights
PSI_API_BASE    = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

_client = None


# ── HTTP client ───────────────────────────────────────────────────────────────

def get_client():
    """Return authenticated httpx.Client. Re-auths automatically on 401."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(timeout=30, follow_redirects=True)
        _client.post(f"{BASE}/api/login", json={"username": "mcp-user"}).raise_for_status()
    return _client


def call(method, path, **kwargs):
    global _client
    r = get_client().request(method, f"{BASE}{path}", **kwargs)
    if r.status_code == 401:
        _client = None
        r = get_client().request(method, f"{BASE}{path}", **kwargs)
    r.raise_for_status()
    return r.json()


# ── Report generator ──────────────────────────────────────────────────────────

def _build_report(pages: list, base_url: str, crawl_id: int) -> str:
    """Generate a structured Markdown SEO audit report from crawl export data."""

    domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    total  = len(pages)

    # ── Categorise pages ──────────────────────────────────────────────────────
    status_buckets   = defaultdict(list)
    missing_title    = []
    missing_meta     = []
    missing_h1       = []
    long_title       = []
    short_title      = []
    long_meta        = []
    short_meta       = []
    thin_content     = []
    dup_titles       = defaultdict(list)
    dup_metas        = defaultdict(list)
    slow_pages       = []
    issues_by_page   = {}

    for p in pages:
        url    = p.get("url", "")
        status = p.get("status_code", 0)
        title  = (p.get("title") or "").strip()
        meta   = (p.get("meta_description") or "").strip()
        h1     = (p.get("h1") or "").strip()
        words  = p.get("word_count", 0) or 0
        rt     = p.get("response_time_ms", 0) or 0
        issues = p.get("issues_detected") or []

        status_buckets[str(status)[:1] + "xx"].append(url)

        if not title:             missing_title.append(url)
        if not meta:              missing_meta.append(url)
        if not h1:                missing_h1.append(url)
        if title and len(title) > 60:  long_title.append((url, title))
        if title and len(title) < 30:  short_title.append((url, title))
        if meta and len(meta) > 160:   long_meta.append((url, meta))
        if meta and 0 < len(meta) < 70: short_meta.append((url, meta))
        if 0 < words < 300:       thin_content.append((url, words))
        if rt > 3000:             slow_pages.append((url, rt))

        if title:  dup_titles[title].append(url)
        if meta:   dup_metas[meta].append(url)

        if issues:
            issues_by_page[url] = issues if isinstance(issues, list) else [issues]

    dup_titles = {t: urls for t, urls in dup_titles.items() if len(urls) > 1}
    dup_metas  = {m: urls for m, urls in dup_metas.items()  if len(urls) > 1}

    broken   = status_buckets.get("4xx", []) + status_buckets.get("5xx", [])
    redirect = status_buckets.get("3xx", [])
    ok       = status_buckets.get("2xx", [])

    total_issues = (len(missing_title) + len(missing_meta) + len(missing_h1) +
                    len(long_title) + len(short_title) + len(thin_content) +
                    len(dup_titles) + len(broken) + len(slow_pages))

    # ── Build Markdown ────────────────────────────────────────────────────────
    lines = []
    def h(level, text): lines.append(f"\n{'#' * level} {text}\n")
    def li(text):       lines.append(f"- {text}")
    def sep():          lines.append("\n---\n")

    # Header
    lines.append(f"# SEO Audit Report — {domain}")
    lines.append(f"**Generated:** {now}  |  **Crawl ID:** {crawl_id}  |  **Pages:** {total}\n")
    sep()

    # Summary scorecard
    h(2, "📊 Summary")
    lines.append(f"| Metric | Count | Status |")
    lines.append(f"|--------|-------|--------|")
    lines.append(f"| Pages crawled | {total} | |")
    lines.append(f"| 200 OK | {len(ok)} | {'✅' if len(ok) == total else '⚠️'} |")
    lines.append(f"| Broken (4xx/5xx) | {len(broken)} | {'✅' if not broken else '🔴'} |")
    lines.append(f"| Redirects (3xx) | {len(redirect)} | {'✅' if not redirect else '⚠️'} |")
    lines.append(f"| Missing title | {len(missing_title)} | {'✅' if not missing_title else '🔴'} |")
    lines.append(f"| Missing meta desc | {len(missing_meta)} | {'✅' if not missing_meta else '🔴'} |")
    lines.append(f"| Missing H1 | {len(missing_h1)} | {'✅' if not missing_h1 else '🔴'} |")
    lines.append(f"| Title too long (>60) | {len(long_title)} | {'✅' if not long_title else '⚠️'} |")
    lines.append(f"| Title too short (<30) | {len(short_title)} | {'✅' if not short_title else '⚠️'} |")
    lines.append(f"| Thin content (<300w) | {len(thin_content)} | {'✅' if not thin_content else '⚠️'} |")
    lines.append(f"| Duplicate titles | {len(dup_titles)} | {'✅' if not dup_titles else '🔴'} |")
    lines.append(f"| Slow pages (>3s) | {len(slow_pages)} | {'✅' if not slow_pages else '⚠️'} |")
    lines.append("")

    sep()

    # ── Critical Issues ───────────────────────────────────────────────────────
    h(2, "🔴 Critical — Fix First")

    # Broken links
    if broken:
        h(3, f"Broken Pages ({len(broken)})")
        lines.append("> **Fix:** 301 to the correct URL, or remove internal links pointing here.\n")
        lines.append("| URL | Status |")
        lines.append("|-----|--------|")
        for url in broken:
            s = next((p.get("status_code","?") for p in pages if p.get("url") == url), "?")
            lines.append(f"| `{url}` | {s} |")
        lines.append("")

    # Duplicate titles
    if dup_titles:
        h(3, f"Duplicate Titles ({len(dup_titles)} groups)")
        lines.append("> **Fix:** Give each page a unique title. Redirect duplicates if they're the same page.\n")
        for title, urls in list(dup_titles.items())[:10]:
            lines.append(f"**\"{title[:70]}\"**")
            for u in urls:
                li(f"`{u}`")
            lines.append("")

    # Missing titles
    if missing_title:
        h(3, f"Missing Title Tag ({len(missing_title)} pages)")
        lines.append("> **Fix:** Add a unique `<title>` tag (50–60 chars) to each page.\n")
        for url in missing_title[:20]:
            li(f"`{url}`")
        if len(missing_title) > 20:
            lines.append(f"… and {len(missing_title)-20} more")
        lines.append("")

    sep()

    # ── Warnings ──────────────────────────────────────────────────────────────
    h(2, "⚠️ Warnings — High Impact")

    # Missing meta descriptions
    if missing_meta:
        h(3, f"Missing Meta Description ({len(missing_meta)} pages)")
        lines.append("> **Fix:** Add a unique meta description (120–155 chars) to each page. Directly improves CTR.\n")
        for url in missing_meta[:30]:
            li(f"`{url}`")
        if len(missing_meta) > 30:
            lines.append(f"… and {len(missing_meta)-30} more")
        lines.append("")

    # Missing H1
    if missing_h1:
        h(3, f"Missing H1 ({len(missing_h1)} pages)")
        lines.append("> **Fix:** Add exactly one `<h1>` per page matching the primary keyword.\n")
        for url in missing_h1[:20]:
            li(f"`{url}`")
        if len(missing_h1) > 20:
            lines.append(f"… and {len(missing_h1)-20} more")
        lines.append("")

    # Long titles
    if long_title:
        h(3, f"Title Too Long — over 60 chars ({len(long_title)} pages)")
        lines.append("> **Fix:** Shorten to 50–60 chars. Google truncates anything longer.\n")
        lines.append("| URL | Title (truncated) | Length |")
        lines.append("|-----|-------------------|--------|")
        for url, title in long_title[:20]:
            lines.append(f"| `{url}` | {title[:60]}… | {len(title)} |")
        if len(long_title) > 20:
            lines.append(f"| … | {len(long_title)-20} more | |")
        lines.append("")

    # Short titles
    if short_title:
        h(3, f"Title Too Short — under 30 chars ({len(short_title)} pages)")
        lines.append("> **Fix:** Expand titles to 50–60 chars. Include target keyword.\n")
        lines.append("| URL | Title | Length |")
        lines.append("|-----|-------|--------|")
        for url, title in short_title[:15]:
            lines.append(f"| `{url}` | {title} | {len(title)} |")
        lines.append("")

    # Thin content
    if thin_content:
        h(3, f"Thin Content — under 300 words ({len(thin_content)} pages)")
        lines.append("> **Fix:** Expand with useful content, or add `noindex` if it's a utility page.\n")
        lines.append("| URL | Words |")
        lines.append("|-----|-------|")
        for url, words in sorted(thin_content, key=lambda x: x[1])[:20]:
            lines.append(f"| `{url}` | {words} |")
        lines.append("")

    # Slow pages
    if slow_pages:
        h(3, f"Slow Response Time — over 3s ({len(slow_pages)} pages)")
        lines.append("> **Fix:** Check server caching, image sizes, and plugin bloat. Target <1s TTFB.\n")
        lines.append("| URL | Response Time |")
        lines.append("|-----|--------------|")
        for url, rt in sorted(slow_pages, key=lambda x: -x[1])[:20]:
            lines.append(f"| `{url}` | {rt:,}ms |")
        lines.append("")

    sep()

    # ── Redirects ─────────────────────────────────────────────────────────────
    if redirect:
        h(2, f"↪️ Redirects ({len(redirect)} pages)")
        lines.append("> **Fix:** Update internal links to point to the final destination URL.\n")
        for url in redirect[:20]:
            li(f"`{url}`")
        if len(redirect) > 20:
            lines.append(f"… and {len(redirect)-20} more")
        lines.append("")
        sep()

    # ── All Pages ─────────────────────────────────────────────────────────────
    h(2, "📋 All Pages")
    lines.append("| Status | URL | Title | Words | Issues |")
    lines.append("|--------|-----|-------|-------|--------|")

    # Sort: broken first, then by depth
    sorted_pages = sorted(pages, key=lambda p: (
        0 if str(p.get("status_code","")).startswith("4") else
        1 if str(p.get("status_code","")).startswith("5") else
        2 if str(p.get("status_code","")).startswith("3") else 3,
        p.get("depth", 99)
    ))

    for p in sorted_pages[:300]:
        url    = p.get("url", "")
        status = p.get("status_code", "?")
        title  = (p.get("title") or "")[:50] or "—"
        words  = p.get("word_count", 0) or 0
        issue_list = p.get("issues_detected") or []
        issue_count = len(issue_list) if isinstance(issue_list, list) else (1 if issue_list else 0)
        status_icon = "🔴" if str(status).startswith(("4","5")) else "↪️" if str(status).startswith("3") else "✅"
        lines.append(f"| {status_icon} {status} | `{url}` | {title} | {words} | {issue_count} |")

    if len(pages) > 300:
        lines.append(f"| … | {len(pages)-300} more pages not shown | | | |")

    lines.append("")
    sep()

    # ── Fix Priority Checklist ────────────────────────────────────────────────
    h(2, "✅ Fix Priority Checklist")
    lines.append("Copy this into your task tracker:\n")

    priority = 1
    if broken:
        lines.append(f"- [ ] **P{priority}** Fix {len(broken)} broken pages (4xx/5xx)")
        priority += 1
    if dup_titles:
        lines.append(f"- [ ] **P{priority}** Resolve {len(dup_titles)} duplicate title groups")
        priority += 1
    if missing_title:
        lines.append(f"- [ ] **P{priority}** Add title tags to {len(missing_title)} pages")
        priority += 1
    if missing_meta:
        lines.append(f"- [ ] **P{priority}** Add meta descriptions to {len(missing_meta)} pages")
        priority += 1
    if missing_h1:
        lines.append(f"- [ ] **P{priority}** Add H1 to {len(missing_h1)} pages")
        priority += 1
    if long_title:
        lines.append(f"- [ ] **P{priority}** Shorten {len(long_title)} titles to ≤60 chars")
        priority += 1
    if short_title:
        lines.append(f"- [ ] **P{priority}** Expand {len(short_title)} short titles to 50–60 chars")
        priority += 1
    if thin_content:
        lines.append(f"- [ ] **P{priority}** Address {len(thin_content)} thin content pages")
        priority += 1
    if slow_pages:
        lines.append(f"- [ ] **P{priority}** Fix {len(slow_pages)} slow pages (>3s response time)")
        priority += 1
    if redirect:
        lines.append(f"- [ ] **P{priority}** Update internal links for {len(redirect)} redirects")
        priority += 1
    if dup_metas:
        lines.append(f"- [ ] **P{priority}** Fix {len(dup_metas)} duplicate meta descriptions")
        priority += 1

    lines.append("")
    lines.append(f"---\n*Generated by [librecrawl-mcp](https://github.com/adityaarsharma/librecrawl-mcp)*")

    return "\n".join(lines)


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def librecrawl_audit(url: str, max_pages: int = 500) -> dict:
    """
    Full SEO audit in one call — crawls the site, waits for completion,
    exports results, and saves a Markdown report file.

    Use this for "audit X" requests. Returns the report file path + summary.
    For manual step-by-step control use librecrawl_start_crawl instead.

    Args:
        url: Full URL to crawl (e.g. https://example.com)
        max_pages: Max pages (default 500)
    """
    # Start
    call("POST", "/api/save_settings", json={
        "enableJavaScript": False,
        "maxUrls": max_pages,
        "maxDepth": 5,
        "crawlDelay": 0.5,
        "followRedirects": True,
        "crawlExternalLinks": False,
    })
    result = call("POST", "/api/start_crawl", json={"url": url})
    crawl_id = result.get("crawl_id")

    if not result.get("success"):
        return {"success": False, "error": result.get("message", "Failed to start crawl")}

    # Poll until done (max 20 min)
    deadline = time.time() + 1200
    crawled  = 0
    while time.time() < deadline:
        time.sleep(8)
        d     = call("GET", "/api/crawl_status")
        stats = d.get("stats", {})
        crawled = stats.get("crawled", 0)
        if not d.get("is_running", True) and crawled > 0:
            break

    # Export
    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": ["url", "status_code", "title", "meta_description",
                   "h1", "word_count", "canonical_url", "depth",
                   "issues_detected", "response_time_ms"],
    }, timeout=120)
    r.raise_for_status()
    export = r.json()

    pages = export if isinstance(export, list) else export.get("urls", export.get("pages", []))

    if not pages:
        return {
            "success": False,
            "crawl_id": crawl_id,
            "crawled": crawled,
            "error": "Export returned no pages. Crawl may still be running — try librecrawl_generate_report(crawl_id) in 30s.",
        }

    # Generate and save MD report
    report_md  = _build_report(pages, url, crawl_id or 0)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    domain     = url.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
    timestamp  = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = REPORTS_DIR / f"{domain}-{timestamp}.md"
    report_path.write_text(report_md, encoding="utf-8")

    # Quick summary
    broken = sum(1 for p in pages if str(p.get("status_code","")).startswith(("4","5")))
    no_meta = sum(1 for p in pages if not (p.get("meta_description") or "").strip())
    no_h1   = sum(1 for p in pages if not (p.get("h1") or "").strip())

    return {
        "success": True,
        "crawl_id": crawl_id,
        "pages_crawled": len(pages),
        "report_file": str(report_path),
        "summary": {
            "broken_pages": broken,
            "missing_meta_description": no_meta,
            "missing_h1": no_h1,
        },
        "next": f"Open {report_path} to see the full report with fix checklist.",
    }


@mcp.tool()
def librecrawl_generate_report(crawl_id: int = None) -> dict:
    """
    Generate a Markdown SEO report from a completed crawl.
    Saves the report as a .md file and returns the path.

    Args:
        crawl_id: ID from librecrawl_start_crawl (optional — uses current crawl if omitted)
    """
    base_url = ""

    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")
        # Try to get base_url from status
        try:
            d = call("GET", "/api/crawl_status")
            base_url = d.get("stats", {}).get("baseUrl", "")
        except Exception:
            pass

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": ["url", "status_code", "title", "meta_description",
                   "h1", "word_count", "canonical_url", "depth",
                   "issues_detected", "response_time_ms"],
    }, timeout=120)
    r.raise_for_status()
    export = r.json()

    pages = export if isinstance(export, list) else export.get("urls", export.get("pages", []))

    if not pages:
        return {"success": False, "error": "No pages found. Is the crawl complete?"}

    if not base_url and pages:
        from urllib.parse import urlparse
        parsed = urlparse(pages[0].get("url", ""))
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    report_md  = _build_report(pages, base_url, crawl_id or 0)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    domain     = base_url.replace("https://","").replace("http://","").rstrip("/").split("/")[0]
    timestamp  = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = REPORTS_DIR / f"{domain}-{timestamp}.md"
    report_path.write_text(report_md, encoding="utf-8")

    return {
        "success": True,
        "report_file": str(report_path),
        "pages": len(pages),
    }


@mcp.tool()
def librecrawl_start_crawl(url: str, max_pages: int = 500) -> dict:
    """
    Start a crawl manually. Returns crawl_id immediately — crawl runs async.
    Poll librecrawl_get_status() until done, then librecrawl_generate_report(crawl_id).

    Use librecrawl_audit() instead if you want a one-call full audit.

    Args:
        url: Full URL to crawl (e.g. https://example.com)
        max_pages: Max pages (default 500)
    """
    call("POST", "/api/save_settings", json={
        "enableJavaScript": False,
        "maxUrls": max_pages,
        "maxDepth": 5,
        "crawlDelay": 0.5,
        "followRedirects": True,
        "crawlExternalLinks": False,
    })
    result   = call("POST", "/api/start_crawl", json={"url": url})
    crawl_id = result.get("crawl_id")
    return {
        "success": result.get("success"),
        "crawl_id": crawl_id,
        "message": result.get("message"),
        "next": f"Poll librecrawl_get_status() until is_running=False, then librecrawl_generate_report({crawl_id})",
    }


@mcp.tool()
def librecrawl_get_status() -> dict:
    """
    Poll current crawl progress. Repeat until is_running=False.
    Returns: is_running, crawled, queued, issues, base_url
    """
    d     = call("GET", "/api/crawl_status")
    stats = d.get("stats", {})
    return {
        "is_running": d.get("is_running", False),
        "crawled":    stats.get("crawled", 0),
        "queued":     stats.get("queued", 0),
        "issues":     stats.get("issues", 0),
        "base_url":   stats.get("baseUrl", ""),
    }


@mcp.tool()
def librecrawl_export_results(crawl_id: int = None) -> dict:
    """
    Export raw crawl JSON. For a formatted report use librecrawl_generate_report() instead.

    Args:
        crawl_id: ID from librecrawl_start_crawl (optional)
    """
    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": ["url", "status_code", "title", "meta_description",
                   "h1", "word_count", "canonical_url", "depth", "issues_detected"],
    }, timeout=120)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def librecrawl_list_crawls() -> dict:
    """List all saved crawls with URL, crawl_id, and timestamp."""
    return call("GET", "/api/crawls/list")


@mcp.tool()
def librecrawl_stop_crawl() -> dict:
    """Stop the currently running crawl."""
    return call("POST", "/api/stop_crawl")


# ── PageSpeed Insights helper ─────────────────────────────────────────────────

def _fetch_psi(url: str, strategy: str = "mobile") -> dict:
    """Fetch Core Web Vitals + performance score from Google PSI API."""
    if not PSI_API_KEY:
        return {"error": "PAGESPEED_API_KEY not set. Add it to your environment."}
    params = {"url": url, "key": PSI_API_KEY, "strategy": strategy,
              "category": ["performance", "seo", "accessibility", "best-practices"]}
    try:
        r = httpx.get(PSI_API_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"error": str(e)}

    lhr   = data.get("lighthouseResult", {})
    cats  = lhr.get("categories", {})
    audits = lhr.get("audits", {})
    fcp_data = data.get("loadingExperience", {}).get("metrics", {})

    def score(cat): return round((cats.get(cat, {}).get("score") or 0) * 100)
    def ms(audit):
        v = audits.get(audit, {}).get("numericValue")
        return round(v) if v else None
    def rating(audit):
        return audits.get(audit, {}).get("displayValue", "")

    # CWV from field data (real users via CrUX)
    field = {}
    for metric, key in [("LCP","LARGEST_CONTENTFUL_PAINT_MS"),("FID","FIRST_INPUT_DELAY_MS"),
                         ("CLS","CUMULATIVE_LAYOUT_SHIFT_SCORE"),("INP","INTERACTION_TO_NEXT_PAINT"),
                         ("FCP","FIRST_CONTENTFUL_PAINT_MS"),("TTFB","EXPERIMENTAL_TIME_TO_FIRST_BYTE")]:
        m = fcp_data.get(key, {})
        if m:
            field[metric] = {"value": m.get("percentile"), "category": m.get("category")}

    # Lab data (Lighthouse simulation)
    lab = {
        "FCP_ms":  ms("first-contentful-paint"),
        "LCP_ms":  ms("largest-contentful-paint"),
        "TBT_ms":  ms("total-blocking-time"),
        "CLS":     audits.get("cumulative-layout-shift", {}).get("numericValue"),
        "Speed_Index_ms": ms("speed-index"),
        "TTI_ms":  ms("interactive"),
    }

    # Top opportunities
    opps = []
    for audit_id, audit in audits.items():
        if audit.get("details", {}).get("type") == "opportunity":
            savings = audit.get("details", {}).get("overallSavingsMs", 0) or 0
            if savings > 200:
                opps.append({"title": audit.get("title"), "savings_ms": round(savings)})
    opps.sort(key=lambda x: -x["savings_ms"])

    return {
        "url": url,
        "strategy": strategy,
        "scores": {
            "performance":    score("performance"),
            "seo":            score("seo"),
            "accessibility":  score("accessibility"),
            "best_practices": score("best-practices"),
        },
        "field_data_cwv": field,
        "lab_data": {k: v for k, v in lab.items() if v is not None},
        "top_opportunities": opps[:5],
    }


# ── Schema.org / JSON-LD parser ───────────────────────────────────────────────

class _JsonLdExtractor(HTMLParser):
    """Extract all <script type="application/ld+json"> blocks from HTML."""
    def __init__(self):
        super().__init__()
        self._in_ld = False
        self._buf   = []
        self.schemas = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs_dict = dict(attrs)
            if attrs_dict.get("type") == "application/ld+json":
                self._in_ld = True
                self._buf   = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            raw = "".join(self._buf).strip()
            if raw:
                try:
                    obj = json.loads(raw)
                    items = obj if isinstance(obj, list) else [obj]
                    for item in items:
                        schema_type = item.get("@type", "Unknown")
                        self.schemas.append({"type": schema_type, "data": item})
                except Exception:
                    pass

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)


def _extract_schema(url: str) -> list:
    """Fetch a page and extract all JSON-LD schema.org objects."""
    try:
        r = httpx.get(url, follow_redirects=True, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; SEO-bot/1.0)"})
        parser = _JsonLdExtractor()
        parser.feed(r.text)
        return parser.schemas
    except Exception as e:
        return [{"error": str(e)}]


# ── New MCP tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def librecrawl_pagespeed(url: str, strategy: str = "mobile") -> dict:
    """
    Get Core Web Vitals + Lighthouse scores for a URL via Google PageSpeed Insights.
    Requires PAGESPEED_API_KEY env var (free: console.cloud.google.com).

    Returns: performance/SEO/accessibility scores, LCP/CLS/FCP/TBT lab data,
    real-user field data (CrUX), and top speed opportunities.

    Args:
        url:      Full URL to test (e.g. https://example.com/page)
        strategy: "mobile" (default) or "desktop"
    """
    return _fetch_psi(url, strategy)


@mcp.tool()
def librecrawl_pagespeed_audit(urls: list, strategy: str = "mobile") -> dict:
    """
    Run PageSpeed Insights on multiple URLs and return a ranked report.
    Throttled to 1 req/sec to stay within Google's free quota.
    Requires PAGESPEED_API_KEY env var.

    Args:
        urls:     List of URLs to test (recommended: top 10–20 pages)
        strategy: "mobile" (default) or "desktop"
    """
    if not PSI_API_KEY:
        return {"error": "PAGESPEED_API_KEY not set. Get one free at console.cloud.google.com → APIs → PageSpeed Insights API."}

    results = []
    for url in urls[:25]:   # cap at 25 to avoid quota burn
        result = _fetch_psi(url, strategy)
        results.append(result)
        time.sleep(1.1)     # 1 req/sec = safe for free quota

    # Sort by performance score ascending (worst first)
    results.sort(key=lambda x: x.get("scores", {}).get("performance", 100))

    summary = {
        "tested": len(results),
        "avg_performance": round(sum(r.get("scores",{}).get("performance",0) for r in results) / len(results)) if results else 0,
        "poor_performance": sum(1 for r in results if r.get("scores",{}).get("performance",0) < 50),
        "needs_improvement": sum(1 for r in results if 50 <= r.get("scores",{}).get("performance",0) < 90),
        "good": sum(1 for r in results if r.get("scores",{}).get("performance",0) >= 90),
    }
    return {"summary": summary, "results": results}


@mcp.tool()
def librecrawl_schema_check(url: str) -> dict:
    """
    Extract and validate all Schema.org / JSON-LD structured data from a page.
    No API key required — parses the live page directly.

    Returns all schema types found, their data, and what Google rich results they enable.

    Args:
        url: Full URL to check (e.g. https://example.com/blog/post)
    """
    schemas = _extract_schema(url)

    # Map schema types to rich results they unlock
    RICH_RESULTS = {
        "Article":           "Article rich result — date, author, image in SERP",
        "BlogPosting":       "Article rich result",
        "Product":           "Product snippet — price, availability, ratings",
        "Review":            "Review snippet — star rating in SERP",
        "AggregateRating":   "Star ratings in SERP",
        "FAQPage":           "FAQ accordion directly in SERP (massive CTR boost)",
        "HowTo":             "How-to steps in SERP",
        "BreadcrumbList":    "Breadcrumb path shown in SERP URL",
        "WebSite":           "Sitelinks searchbox in SERP",
        "Organization":      "Knowledge panel — logo, contacts",
        "Person":            "Author knowledge panel",
        "LocalBusiness":     "Local business panel — address, hours, maps",
        "SoftwareApplication": "App rating + price in SERP",
        "VideoObject":       "Video thumbnail in SERP",
        "Event":             "Event date/location in SERP",
        "JobPosting":        "Job listing in Google Jobs",
        "Recipe":            "Recipe rich card — time, calories, ratings",
        "Course":            "Course info in SERP",
    }

    found_types = [s.get("type") for s in schemas if "type" in s]
    rich_results_unlocked = [RICH_RESULTS[t] for t in found_types if t in RICH_RESULTS]
    missing_opportunities = [
        f"{t}: {desc}" for t, desc in RICH_RESULTS.items()
        if t not in found_types and t in ["FAQPage", "BreadcrumbList", "Article", "Product", "Review"]
    ]

    return {
        "url": url,
        "schema_count": len(schemas),
        "types_found": found_types,
        "rich_results_enabled": rich_results_unlocked,
        "missing_opportunities": missing_opportunities,
        "schemas": schemas,
    }


@mcp.tool()
def librecrawl_schema_audit(urls: list) -> dict:
    """
    Check Schema.org structured data across multiple URLs.
    No API key required.

    Args:
        urls: List of URLs to check (pass top pages from a crawl export)
    """
    results    = []
    no_schema  = []
    type_count = defaultdict(int)

    for url in urls[:50]:
        schemas = _extract_schema(url)
        types   = [s.get("type") for s in schemas if "type" in s]
        for t in types:
            type_count[t] += 1
        if not types:
            no_schema.append(url)
        results.append({"url": url, "types": types, "count": len(schemas)})
        time.sleep(0.3)

    return {
        "pages_checked":    len(results),
        "pages_no_schema":  len(no_schema),
        "schema_type_breakdown": dict(sorted(type_count.items(), key=lambda x: -x[1])),
        "pages_missing_schema": no_schema[:30],
        "results": results,
    }


@mcp.tool()
def librecrawl_append_gsc_section(report_path: str, gsc_data: dict) -> dict:
    """
    Append a Google Search Console errors section to an existing MD audit report.

    Workflow:
      1. Run librecrawl_audit(url) → get report_path
      2. Use the gsc-posi connector to pull GSC data for the domain
      3. Pass both here to merge GSC errors into the audit report

    gsc_data should contain any/all of:
      - coverage_errors: list of {url, type, last_crawled} (Indexing > Coverage in GSC)
      - crawl_errors:    list of {url, response_code, last_crawled}
      - search_issues:   list of strings (manual actions, security issues)
      - performance:     dict with clicks/impressions/ctr/position (optional summary)

    Args:
        report_path: Path returned by librecrawl_audit() or librecrawl_generate_report()
        gsc_data:    GSC data dict pulled from gsc-posi connector
    """
    path = Path(report_path)
    if not path.exists():
        return {"success": False, "error": f"Report not found: {report_path}"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["\n\n---\n", f"## 🔍 Google Search Console Errors\n",
             f"*Pulled: {now}*\n"]

    # Coverage / Indexing errors
    coverage = gsc_data.get("coverage_errors") or gsc_data.get("indexing_errors") or []
    if coverage:
        # Group by error type
        by_type = defaultdict(list)
        for item in coverage:
            err_type = item.get("type") or item.get("reason") or "Unknown"
            url = item.get("url") or item.get("inspectionUrl") or ""
            by_type[err_type].append(url)

        lines.append(f"### Indexing Errors ({len(coverage)} URLs)\n")
        for err_type, urls in sorted(by_type.items(), key=lambda x: -len(x[1])):
            lines.append(f"**{err_type}** — {len(urls)} URLs")
            fix_hint = {
                "Submitted URL not found (404)": "Fix: Remove from sitemap, or 301 redirect to correct URL.",
                "Submitted URL seems to be a Soft 404": "Fix: Return real content or a proper 404 status code.",
                "Redirect error": "Fix: Fix the redirect chain — ensure it resolves to a 200 in ≤2 hops.",
                "Server error (5xx)": "Fix: Check server logs. 5xx errors block indexing.",
                "Blocked by robots.txt": "Fix: Remove `Disallow` rule if the page should be indexed.",
                "Blocked due to access forbidden (403)": "Fix: Allow Googlebot access or remove from sitemap.",
                "Crawled - currently not indexed": "Fix: Improve content quality, add internal links, check canonical.",
                "Discovered - currently not indexed": "Fix: Add internal links to push Googlebot to crawl.",
                "Alternate page with proper canonical tag": "Info: These are intentionally canonicalized — verify it's correct.",
                "Duplicate without user-selected canonical": "Fix: Add `<link rel='canonical'>` pointing to the preferred URL.",
                "Page with redirect": "Info: These redirect — update internal links to point to final URL.",
                "Excluded by 'noindex' tag": "Info: Verify these pages should be noindexed.",
            }.get(err_type, "Fix: Investigate in GSC Coverage report.")
            lines.append(f"> {fix_hint}\n")
            for u in urls[:10]:
                if u:
                    lines.append(f"- `{u}`")
            if len(urls) > 10:
                lines.append(f"- … and {len(urls)-10} more")
            lines.append("")
    else:
        lines.append("### Indexing Errors\n")
        lines.append("✅ No coverage errors found in provided GSC data.\n")

    # Crawl errors
    crawl_errors = gsc_data.get("crawl_errors") or []
    if crawl_errors:
        lines.append(f"### Crawl Errors ({len(crawl_errors)})\n")
        lines.append("| URL | Status | Last Crawled |")
        lines.append("|-----|--------|-------------|")
        for item in crawl_errors[:20]:
            url = item.get("url", "")
            code = item.get("response_code") or item.get("status", "?")
            last = item.get("last_crawled") or item.get("lastCrawled", "—")
            lines.append(f"| `{url}` | {code} | {last} |")
        if len(crawl_errors) > 20:
            lines.append(f"| … | {len(crawl_errors)-20} more | |")
        lines.append("")

    # Manual actions / security issues
    issues = gsc_data.get("search_issues") or gsc_data.get("manual_actions") or []
    if issues:
        lines.append(f"### ⚠️ Manual Actions / Security Issues\n")
        for issue in issues:
            lines.append(f"- 🚨 {issue}")
        lines.append("")

    # Performance summary (optional)
    perf = gsc_data.get("performance") or {}
    if perf:
        lines.append("### Search Performance Summary\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if "clicks" in perf:      lines.append(f"| Clicks (28d) | {perf['clicks']:,} |")
        if "impressions" in perf: lines.append(f"| Impressions (28d) | {perf['impressions']:,} |")
        if "ctr" in perf:         lines.append(f"| CTR | {perf['ctr']:.1%} |")
        if "position" in perf:    lines.append(f"| Avg Position | {perf['position']:.1f} |")
        lines.append("")

    # GSC fix checklist
    gsc_priority = 1
    checklist = []
    coverage_errors_by_sev = {}
    for item in coverage:
        t = item.get("type") or item.get("reason") or "Unknown"
        coverage_errors_by_sev[t] = coverage_errors_by_sev.get(t, 0) + 1

    critical_types = ["Submitted URL not found (404)", "Server error (5xx)", "Redirect error"]
    warning_types  = ["Submitted URL seems to be a Soft 404", "Crawled - currently not indexed",
                      "Duplicate without user-selected canonical"]

    for t in critical_types:
        if t in coverage_errors_by_sev:
            checklist.append(f"- [ ] **P{gsc_priority} (GSC)** Fix {coverage_errors_by_sev[t]}x '{t}'")
            gsc_priority += 1
    for t in warning_types:
        if t in coverage_errors_by_sev:
            checklist.append(f"- [ ] **P{gsc_priority} (GSC)** Resolve {coverage_errors_by_sev[t]}x '{t}'")
            gsc_priority += 1

    if checklist:
        lines.append("### GSC Fix Checklist\n")
        lines.extend(checklist)
        lines.append("")

    gsc_section = "\n".join(lines)
    with open(path, "a", encoding="utf-8") as f:
        f.write(gsc_section)

    return {
        "success": True,
        "report_file": str(path),
        "gsc_section_added": True,
        "coverage_errors": len(coverage),
        "crawl_errors": len(crawl_errors),
        "manual_actions": len(issues),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=MCP_PORT, log_level="info")
