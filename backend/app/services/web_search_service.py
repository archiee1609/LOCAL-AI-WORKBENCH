import re
import time
import html
from html import unescape
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import httpx

from app.core.config import settings
from app.core.logging import logger

_SEARCH_CACHE: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}
_PAGE_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL = 600.0  # 10 minutes cache


class WebSearchService:
    """
    High-performance, privacy-conscious live web search and web extraction service.
    Queries live search feeds, Wikipedia knowledge bases, and extracts clean article text
    to augment local LLM inferences with up-to-date facts and data.
    """

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """
        Executes a live web search for query.
        Returns a list of structured items: [{'title': str, 'url': str, 'snippet': str, 'source': str}].
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        cache_key = f"{clean_q.lower()}_{max_results}"
        now = time.time()
        if cache_key in _SEARCH_CACHE:
            cached_data, timestamp = _SEARCH_CACHE[cache_key]
            if now - timestamp < _CACHE_TTL:
                return cached_data

        results: List[Dict[str, Any]] = []

        # 1. Primary Engine: Bing RSS Feed (live web, no rate-limiting, clean summaries)
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=self.headers) as client:
                res = await client.get("https://www.bing.com/search", params={"q": clean_q, "format": "rss"})
                if res.status_code == 200 and res.text:
                    root = ET.fromstring(res.text)
                    items = root.findall(".//item")
                    for it in items[:max_results]:
                        t_node = it.find("title")
                        l_node = it.find("link")
                        d_node = it.find("description")
                        
                        raw_title = t_node.text if t_node is not None and t_node.text else ""
                        raw_url = l_node.text if l_node is not None and l_node.text else ""
                        raw_desc = d_node.text if d_node is not None and d_node.text else ""

                        clean_title = unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
                        clean_snippet = unescape(re.sub(r"<[^>]+>", "", raw_desc)).strip()
                        domain = urlparse(raw_url).netloc.replace("www.", "")

                        if clean_title and raw_url:
                            results.append({
                                "title": clean_title,
                                "url": raw_url,
                                "snippet": clean_snippet,
                                "source": domain
                            })
        except Exception as e:
            logger.warning(f"Bing search notice for query '{clean_q}': {e}")

        # 2. Secondary Knowledge Engine: Wikipedia API (for technical, biographical, or encyclopedia concepts)
        if len(results) < max_results:
            try:
                wiki_headers = {"User-Agent": "LocalAIWorkbench/1.0 (internal-local-rag)"}
                async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers=wiki_headers) as client:
                    w_res = await client.get(
                        "https://en.wikipedia.org/w/api.php",
                        params={
                            "action": "query",
                            "list": "search",
                            "srsearch": clean_q,
                            "format": "json",
                            "utf8": 1
                        }
                    )
                    if w_res.status_code == 200:
                        w_items = w_res.json().get("query", {}).get("search", [])
                        for w_item in w_items[:max_results - len(results)]:
                            w_title = w_item.get("title", "")
                            w_snippet = unescape(re.sub(r"<[^>]+>", "", w_item.get("snippet", ""))).strip()
                            w_url = f"https://en.wikipedia.org/wiki/{w_title.replace(' ', '_')}"
                            results.append({
                                "title": f"{w_title} (Wikipedia)",
                                "url": w_url,
                                "snippet": w_snippet,
                                "source": "wikipedia.org"
                            })
            except Exception as ex:
                logger.debug(f"Wikipedia search notice: {ex}")

        # 3. Tertiary Engine: DuckDuckGo Instant Answers
        if not results:
            try:
                async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers=self.headers) as client:
                    d_res = await client.get(
                        "https://api.duckduckgo.com/",
                        params={"q": clean_q, "format": "json", "no_html": 1}
                    )
                    if d_res.status_code == 200:
                        d_json = d_res.json()
                        abstract = d_json.get("AbstractText", "")
                        abs_url = d_json.get("AbstractURL", "")
                        if abstract and abs_url:
                            results.append({
                                "title": d_json.get("Heading", clean_q),
                                "url": abs_url,
                                "snippet": abstract,
                                "source": urlparse(abs_url).netloc.replace("www.", "")
                            })
            except Exception as ex:
                logger.debug(f"DuckDuckGo API notice: {ex}")

        _SEARCH_CACHE[cache_key] = (results, now)
        return results

    async def fetch_page(self, url: str, max_chars: int = 4000) -> Dict[str, Any]:
        """
        Fetches a web page and extracts clean, readable text.
        """
        clean_url = url.strip()
        if not clean_url:
            return {"error": "Empty URL provided", "content": ""}

        now = time.time()
        if clean_url in _PAGE_CACHE:
            cached_text, timestamp = _PAGE_CACHE[clean_url]
            if now - timestamp < _CACHE_TTL:
                return {"url": clean_url, "content": cached_text, "cached": True}

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=self.headers) as client:
                res = await client.get(clean_url)
                if res.status_code != 200:
                    return {"url": clean_url, "error": f"HTTP {res.status_code}", "content": ""}

                raw_html = res.text
                # Remove scripts, styles, navigations
                text = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
                # Strip remaining tags
                text = re.sub(r"<[^>]+>", " ", text)
                # Unescape HTML entities & normalize whitespace
                text = unescape(text)
                text = " ".join(text.split())

                if len(text) > max_chars:
                    text = text[:max_chars] + "...\n[Content truncated for context limits]"

                _PAGE_CACHE[clean_url] = (text, now)
                return {"url": clean_url, "content": text, "status": "success"}
        except Exception as e:
            return {"url": clean_url, "error": str(e), "content": ""}

    def format_search_context(self, query: str, results: List[Dict[str, Any]]) -> str:
        """
        Builds high-density Markdown context block for model ingestion with citation markers.
        """
        if not results:
            return f"No live search results found for query: '{query}'."

        lines = [f"### 🌐 Live Web Search Results for: '{query}'\n"]
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "Untitled")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            source = item.get("source", "web")
            lines.append(f"[{idx}] **{title}** ({source})\nLink: {url}\nSummary: {snippet}\n")

        lines.append(
            "Instructions for AI:\n"
            "- Synthesize the above live web findings directly into your answer.\n"
            "- Provide accurate, up-to-date facts based on these sources.\n"
            "- Reference sources with bracketed numbers [1], [2] corresponding to the search results."
        )
        return "\n".join(lines)

    def detect_search_intent(self, text: str) -> Optional[str]:
        """
        Detects if user query explicitly or implicitly requires a live web search.
        Returns the extracted search query or None.
        """
        cleaned = text.strip()
        lower = cleaned.lower()

        # 1. Explicit /search command
        if lower.startswith(("/search ", "!search ", "/web ")):
            return cleaned.split(maxsplit=1)[1].strip()

        # 2. Explicit keywords
        explicit_patterns = [
            r"^(?:search\s+(?:the\s+)?web\s+(?:for\s+)?|google\s+|browse\s+(?:the\s+)?web\s+for\s+)(.+)$",
            r"^what\s+(?:is|are)\s+the\s+latest\s+(.+)$",
            r"^latest\s+news\s+(?:on|about)\s+(.+)$",
            r"^current\s+weather\s+(?:in|for)\s+(.+)$",
            r"^current\s+(?:stock\s+)?price\s+of\s+(.+)$"
        ]
        for pat in explicit_patterns:
            m = re.match(pat, lower, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        # 3. Temporal trigger phrases
        temporal_keywords = [
            "latest news", "today's news", "current price", "current weather",
            "release date of", "who is the current", "what happened today",
            "stock price of", "recent benchmarks"
        ]
        if any(tk in lower for tk in temporal_keywords):
            # Clean prompt for search
            q = re.sub(r"^(please\s+|can\s+you\s+|tell\s+me\s+)?", "", cleaned, flags=re.IGNORECASE).strip()
            return q

        return None


web_search_service = WebSearchService()
