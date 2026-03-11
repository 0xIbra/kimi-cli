from pathlib import Path
from typing import override

import aiohttp
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field, ValidationError

from kimi_cli.config import Config
from kimi_cli.constant import USER_AGENT
from kimi_cli.soul.agent import Runtime
from kimi_cli.soul.toolset import get_current_tool_call_or_none
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.utils import ToolResultBuilder, load_desc
from kimi_cli.utils.aiohttp import new_client_session
from kimi_cli.utils.logging import logger


class Params(BaseModel):
    query: str = Field(description="The query text to search for.")
    limit: int = Field(
        description=(
            "The number of results to return. "
            "Typically you do not need to set this value. "
            "When the results do not contain what you need, "
            "you probably want to give a more concrete query."
        ),
        default=5,
        ge=1,
        le=20,
    )
    include_content: bool = Field(
        description=(
            "Whether to include the content of the web pages in the results. "
            "It can consume a large amount of tokens when this is set to True. "
            "You should avoid enabling this when `limit` is set to a large value."
        ),
        default=False,
    )
    time_range: str = Field(
        description=(
            "Time range filter for search results. Use this to get fresh results. "
            "Values: 'qdr:h' (past hour), 'qdr:d' (past day), 'qdr:w' (past week), "
            "'qdr:m' (past month), 'qdr:y' (past year). "
            "Leave empty for no time filter (all time). "
            "For current events, news, or prices use 'qdr:d'. "
            "For recent developments use 'qdr:w'."
        ),
        default="",
    )
    sources: list[str] = Field(
        description=(
            "Types of search results to return. "
            "Values: 'web' (general web pages), 'news' (news articles), 'images' (image results). "
            "Use ['news'] when the user asks about current events, breaking news, or market updates. "
            "Use ['web'] for general information, docs, or tutorials. "
            "Can combine multiple: ['web', 'news']."
        ),
        default_factory=list,
    )
    location: str = Field(
        description=(
            "Geo-target search results to a specific location. "
            "Use natural names like 'Russia', 'Germany', 'United States'. "
            "Set this when the user's question is region-specific or when local results would be more relevant."
        ),
        default="",
    )
    country: str = Field(
        description=(
            "ISO country code to localize results. "
            "Examples: 'US', 'RU', 'DE', 'GB'. "
            "Use 'RU' when the user writes in Russian or asks about Russian markets/regulations."
        ),
        default="",
    )


class SearchWeb(CallableTool2[Params]):
    name: str = "SearchWeb"
    description: str = load_desc(Path(__file__).parent / "search.md", {})
    params: type[Params] = Params

    def __init__(self, config: Config, runtime: Runtime):
        super().__init__()
        self._firecrawl = config.services.firecrawl_search
        self._moonshot = config.services.moonshot_search
        if self._firecrawl is None and self._moonshot is None:
            raise SkipThisTool()
        self._runtime = runtime

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if self._firecrawl is not None:
            result = await self._search_firecrawl(params)
            if not result.is_error:
                return result
            logger.warning("Firecrawl search failed, falling back to Moonshot: {error}", error=result.message)

        if self._moonshot is not None:
            return await self._search_moonshot(params)

        return ToolResultBuilder(max_line_length=None).error(
            "Search service is not configured.",
            brief="Search service not configured",
        )

    async def _search_firecrawl(self, params: Params) -> ToolReturnValue:
        assert self._firecrawl is not None
        builder = ToolResultBuilder(max_line_length=None)
        api_key = self._firecrawl.api_key.get_secret_value()
        if not api_key:
            return builder.error(
                "Firecrawl API key is not configured.",
                brief="Firecrawl not configured",
            )

        base_url = self._firecrawl.base_url.rstrip("/")
        url = f"{base_url}/search"

        body: dict = {
            "query": params.query,
            "limit": params.limit,
        }
        if params.time_range:
            body["tbs"] = params.time_range
        if params.sources:
            body["sources"] = params.sources
        if params.location:
            body["location"] = params.location
        if params.country:
            body["country"] = params.country
        if params.include_content:
            body["scrapeOptions"] = {"formats": ["markdown"]}

        try:
            async with (
                new_client_session() as session,
                session.post(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response,
            ):
                if response.status != 200:
                    resp_text = await response.text()
                    return builder.error(
                        f"Firecrawl search failed. Status: {response.status}. {resp_text[:200]}",
                        brief="Firecrawl search failed",
                    )

                try:
                    data = await response.json()
                except Exception:
                    return builder.error(
                        "Failed to parse Firecrawl response as JSON.",
                        brief="Firecrawl parse error",
                    )

        except aiohttp.ClientError as e:
            return builder.error(
                f"Firecrawl network error: {e}",
                brief="Firecrawl network error",
            )

        if not data.get("success", False):
            return builder.error(
                f"Firecrawl returned unsuccessful response: {data.get('warning', 'unknown error')}",
                brief="Firecrawl unsuccessful",
            )

        results = data.get("data", [])
        if not results:
            builder.write("No results found.\n")
            return builder.ok()

        for i, item in enumerate(results):
            if i > 0:
                builder.write("---\n\n")
            title = item.get("title", "")
            url_str = item.get("url", "")
            description = item.get("description", "")
            markdown = item.get("markdown", "")
            builder.write(f"Title: {title}\nURL: {url_str}\nSummary: {description}\n\n")
            if markdown and params.include_content:
                builder.write(f"{markdown}\n\n")

        return builder.ok()

    async def _search_moonshot(self, params: Params) -> ToolReturnValue:
        assert self._moonshot is not None
        builder = ToolResultBuilder(max_line_length=None)

        api_key = self._runtime.oauth.resolve_api_key(self._moonshot.api_key, self._moonshot.oauth)
        if not self._moonshot.base_url or not api_key:
            return builder.error(
                "Search service is not configured. You may want to try other methods to search.",
                brief="Search service not configured",
            )

        tool_call = get_current_tool_call_or_none()
        assert tool_call is not None, "Tool call is expected to be set"

        custom_headers = self._moonshot.custom_headers or {}

        async with (
            new_client_session() as session,
            session.post(
                self._moonshot.base_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Bearer {api_key}",
                    "X-Msh-Tool-Call-Id": tool_call.id,
                    **self._runtime.oauth.common_headers(),
                    **custom_headers,
                },
                json={
                    "text_query": params.query,
                    "limit": params.limit,
                    "enable_page_crawling": params.include_content,
                    "timeout_seconds": 30,
                },
            ) as response,
        ):
            if response.status != 200:
                return builder.error(
                    (
                        f"Failed to search. Status: {response.status}. "
                        "This may indicates that the search service is currently unavailable."
                    ),
                    brief="Failed to search",
                )

            try:
                results = _MoonshotResponse(**await response.json()).search_results
            except ValidationError as e:
                return builder.error(
                    (
                        f"Failed to parse search results. Error: {e}. "
                        "This may indicates that the search service is currently unavailable."
                    ),
                    brief="Failed to parse search results",
                )

        for i, result in enumerate(results):
            if i > 0:
                builder.write("---\n\n")
            builder.write(
                f"Title: {result.title}\nDate: {result.date}\n"
                f"URL: {result.url}\nSummary: {result.snippet}\n\n"
            )
            if result.content:
                builder.write(f"{result.content}\n\n")

        return builder.ok()


# --- Moonshot response models (kept for fallback) ---

class _MoonshotSearchResult(BaseModel):
    site_name: str
    title: str
    url: str
    snippet: str
    content: str = ""
    date: str = ""
    icon: str = ""
    mime: str = ""


class _MoonshotResponse(BaseModel):
    search_results: list[_MoonshotSearchResult]
