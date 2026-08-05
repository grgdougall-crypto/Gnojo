import json
from urllib.parse import urlparse

import requests

from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import OpenAIProvider


class ArticleSourceFinderError(ValueError):
    pass


class ArticleSourceFinderService:
    """Find reviewable, authoritative sources for a knowledge draft."""

    SEARCH_REDIRECT_HOSTS = {
        "vertexaisearch.cloud.google.com",
        "www.google.com",
        "google.com",
    }

    def __init__(self, providers=None, redirect_resolver=None):
        self.providers = providers
        self.redirect_resolver = redirect_resolver or self._resolve_search_redirect

    def find(self, article):
        if not isinstance(article, dict):
            raise ArticleSourceFinderError("An article draft is required.")
        prompt = self._prompt(article)
        errors = []
        for name, source in self._providers():
            try:
                provider = source() if isinstance(source, type) else source
                result = provider.find_authoritative_sources(prompt)
                suggestions = self._validate(result)
                if suggestions:
                    return {"provider": name, "suggestions": suggestions}
            except Exception as error:
                errors.append(f"{name}: {error}")
        raise ArticleSourceFinderError(
            "No authoritative source suggestions were available. "
            "Check the configured AI provider and try again."
        )

    def _providers(self):
        if self.providers is not None:
            return self.providers
        return (("Gemini Search", GeminiProvider), ("OpenAI Search", OpenAIProvider))

    def _prompt(self, article):
        context = {
            "title": article.get("title"),
            "category": article.get("category"),
            "overview": article.get("overview"),
            "checklist": article.get("checklist"),
            "related_topics": article.get("related_topics"),
        }
        return f"""
Use live web search to locate 1 to 3 current, authoritative primary-source reference pages for this technical support article.

Return only JSON with this shape:
{{"sources":[{{"title":"Exact page title","url":"https://...","publisher":"Organization","reason":"One sentence explaining which guidance it supports."}}]}}

Rules:
- Prefer official product documentation and support pages from the relevant vendor or standards organization.
- Do not return blogs, forums, social media, scraped copies, search-result pages, or AI-generated summaries.
- Return the exact final HTTPS page URL, not a guessed URL and not a home page.
- Each source must directly support a technical instruction or safety boundary in the draft.
- Do not claim a source was verified unless it appeared in the live search results.

ARTICLE:
{json.dumps(context, indent=2)}
""".strip()

    def _validate(self, result):
        raw_sources = result.get("sources") if isinstance(result, dict) else None
        if not isinstance(raw_sources, list):
            raise ArticleSourceFinderError("The provider returned an unexpected source list.")
        suggestions = []
        seen = set()
        for item in raw_sources[:5]:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            url = str(item.get("url") or "").strip()
            publisher = " ".join(str(item.get("publisher") or "").split())
            reason = " ".join(str(item.get("reason") or "").split())
            parsed = urlparse(url)
            initial_path_parts = [part for part in parsed.path.split("/") if part]
            if (
                len(title) < 5 or parsed.scheme != "https" or not parsed.netloc
                or len(initial_path_parts) < 2 or url in seen or len(reason) < 15
            ):
                continue
            try:
                url = self.redirect_resolver(url)
                parsed = urlparse(url)
            except Exception:
                continue
            path_parts = [part for part in parsed.path.split("/") if part]
            if parsed.scheme != "https" or not parsed.netloc or len(path_parts) < 2:
                continue
            seen.add(url)
            suggestions.append({
                "title": title,
                "url": url,
                "publisher": publisher or parsed.netloc,
                "reason": reason,
            })
            if len(suggestions) == 3:
                break
        if not suggestions:
            raise ArticleSourceFinderError("No usable authoritative sources were returned.")
        return suggestions

    def _resolve_search_redirect(self, url):
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=8,
            stream=True,
            headers={"User-Agent": "Gnojo-Source-Review/1.0"},
        )
        try:
            response.raise_for_status()
            final_url = response.url
            preview = b""
            for chunk in response.iter_content(chunk_size=8192):
                preview += chunk
                if len(preview) >= 131072:
                    break
        finally:
            response.close()
        parsed = urlparse(final_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ArticleSourceFinderError("The search result did not resolve to a secure source URL.")
        if parsed.netloc.lower() in self.SEARCH_REDIRECT_HOSTS:
            raise ArticleSourceFinderError("The search result did not expose its final source URL.")
        page_text = preview.decode("utf-8", errors="ignore").lower()
        missing_markers = (
            "sorry, page not found",
            "sorry, the page you requested cannot be found",
            "the page you requested could not be found",
            "404 - page not found",
            "404 not found",
            "the chosen document is not currently available",
            "this document is not currently available",
            "the requested document is not available",
        )
        if any(marker in page_text for marker in missing_markers):
            raise ArticleSourceFinderError("The suggested source resolves to a page-not-found response.")
        return final_url
