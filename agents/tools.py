"""
Implémentations des outils web pour les agents.
Utilisés via le mécanisme tool_use de l'API Anthropic.

Dépendances :
    pip install duckduckgo-search requests beautifulsoup4
"""

import json
import requests
import urllib3
from bs4 import BeautifulSoup

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


def web_fetch(url: str, method: str = "GET", body: dict = None, timeout: int = 12) -> str:
    """
    Fetch une URL. Retourne HTTP status + contenu (tronqué à 4000 chars).
    Supporte GET et POST avec body JSON (pour les APIs Workday, SmartRecruiters...).
    """
    try:
        if method == "POST":
            h = {**HEADERS, "Content-Type": "application/json"}
            r = requests.post(url, json=body, headers=h, timeout=timeout, verify=False)
        else:
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False,
                             allow_redirects=True)

        content_type = r.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                data = r.json()
                text = json.dumps(data, indent=2)
            except Exception:
                text = r.text
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)

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
