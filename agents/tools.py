"""
Implémentations des outils web pour les agents.
Utilisés via le mécanisme tool_use de l'API Anthropic.

Dépendances :
    pip install duckduckgo-search requests beautifulsoup4
"""

import json
import re
import requests
import urllib3
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from ddgs import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Implémentations ────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> str:
    """Recherche web via DuckDuckGo. Supporte l'opérateur site:."""
    if not HAS_DDG:
        return "ERROR: duckduckgo_search not installed. Run: pip install duckduckgo-search"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        lines = []
        for r in results:
            snippet = r.get('body', '')[:200]
            lines.append(f"URL: {r['href']}\nTitle: {r['title']}\nSnippet: {snippet}")
        text = "\n---\n".join(lines)
        if len(text) > 2000:
            text = text[:2000] + "\n... [truncated]"
        return text
    except Exception as e:
        return f"Search error: {e}"


@retry(
    retry=retry_if_exception_type((
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _do_request(method: str, url: str, body: dict, timeout: int) -> requests.Response:
    if method == "POST":
        h = {**HEADERS, "Content-Type": "application/json"}
        return requests.post(url, json=body, headers=h, timeout=timeout, verify=False)
    return requests.get(url, headers=HEADERS, timeout=timeout, verify=False,
                        allow_redirects=True)


def web_fetch(url: str, method: str = "GET", body: dict = None, timeout: int = 12) -> str:
    """
    Fetch une URL. Retourne HTTP status + contenu (tronqué à 4000 chars).
    Supporte GET et POST avec body JSON (pour les APIs Workday, SmartRecruiters...).
    """
    try:
        r = _do_request(method, url, body, timeout)

        content_type = r.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                data = r.json()
                text = json.dumps(data, indent=2)
            except Exception:
                text = r.text
        else:
            # Scan raw HTML for ATS signals before BeautifulSoup strips them.
            # Covers: URL patterns in href/src/redirects + JS embed variables.
            _ATS_PATTERNS = [
                # Workday — URL redirect or iframe src
                r'[\w-]+\.wd\d+\.myworkdayjobs\.com/[\w/%-]+',
                # SmartRecruiters — job board links or widget script src
                r'(?:jobs|careers)\.smartrecruiters\.com/[\w-]+',
                # Greenhouse — embed script "?for=token" or boardURI
                r'boards\.greenhouse\.io/embed/job_board/js\?for=[\w-]+',
                r'boards(?:-api)?(?:\.eu)?\.greenhouse\.io/[\w/v-]+',
                r'job-boards(?:\.eu)?\.greenhouse\.io/[\w-]+',
                # Greenhouse JS object: Grnhse.Settings.boardURI
                r'boardURI["\s:]+["\']https?://[^"\']*greenhouse\.io/[\w-]+',
                # Lever
                r'jobs\.lever\.co/[\w-]+',
                # Ashby (modern ATS, growing)
                r'[\w-]+\.ashbyhq\.com/[\w-]+',
                # Taleo
                r'[\w-]+\.taleo\.net',
                # SAP SuccessFactors
                r'[\w-]+\.successfactors\.(?:com|eu)/careers',
            ]
            ats_hits = list({m for pat in _ATS_PATTERNS for m in re.findall(pat, r.text)})

            # JSON-LD JobPosting — structured data embedded for Google indexing.
            # Present on most modern job pages regardless of ATS.
            # Contains: title, hiringOrganization, jobLocation, description, datePosted.
            jsonld_jobs = []
            for block in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                r.text, re.DOTALL | re.IGNORECASE
            ):
                try:
                    obj = json.loads(block)
                    items = obj if isinstance(obj, list) else [obj]
                    for item in items:
                        if item.get("@type") in ("JobPosting", "jobPosting"):
                            jsonld_jobs.append({
                                "title": item.get("title", ""),
                                "org": (item.get("hiringOrganization") or {}).get("name", ""),
                                "location": str((item.get("jobLocation") or {}).get("address", "")),
                                "date": item.get("datePosted", ""),
                            })
                except Exception:
                    pass

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            prefix_parts = []
            if ats_hits:
                prefix_parts.append("ATS URLS FOUND: " + " | ".join(ats_hits))
            if jsonld_jobs:
                prefix_parts.append(
                    "JSON-LD JobPostings found: "
                    + json.dumps(jsonld_jobs[:5], ensure_ascii=False)
                )
            if prefix_parts:
                text = "\n".join(prefix_parts) + "\n\n" + text

        if len(text) > 4000:
            text = text[:4000] + f"\n... [truncated — total {len(text)} chars]"

        return f"HTTP {r.status_code}\n\n{text}"

    except requests.exceptions.Timeout:
        return f"TIMEOUT after {timeout}s: {url}"
    except Exception as e:
        return f"Fetch error: {type(e).__name__}: {e}"


# ── Schémas pour l'API Anthropic ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web. Supports site: operator for domain-specific searches. "
            "Example: site:company.com trader jobs"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query. Use site: to search within a domain.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 10, max 20)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch content from a URL. "
            "Use method=POST with body for API calls (e.g. Workday /wday/cxs/... endpoint). "
            "Returns HTTP status code + content or JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST"],
                    "description": "HTTP method (default GET)",
                },
                "body": {
                    "type": "object",
                    "description": "JSON body for POST requests",
                },
            },
            "required": ["url"],
        },
    },
]

# Agent 1 n'a besoin que de web_search (pas de web_fetch)
SEARCH_ONLY_TOOLS = [TOOLS[0]]


def execute_tool(name: str, input_data: dict) -> str:
    """Dispatcher — appelé par la boucle agentique."""
    if name == "web_search":
        return web_search(input_data["query"], input_data.get("max_results", 10))
    elif name == "web_fetch":
        return web_fetch(
            input_data["url"],
            input_data.get("method", "GET"),
            input_data.get("body"),
        )
    return f"Unknown tool: {name}"
