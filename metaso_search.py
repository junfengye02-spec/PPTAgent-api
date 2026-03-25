"""
秘塔搜索 MCP Tool — 替代 Tavily 搜索
使用 Metaso API (metaso.cn) 提供 search_web / fetch_url / download_file 功能，
接口签名与 PPTAgent 原生 search.py 保持一致。
"""

import asyncio
import os
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import aiohttp
import httpx
import markdownify
from fake_useragent import UserAgent
from fastmcp import FastMCP
from PIL import Image
from playwright.async_api import TimeoutError
from trafilatura import extract

from deeppresenter.utils.constants import (
    MAX_RETRY_INTERVAL,
    MCP_CALL_TIMEOUT,
    RETRY_TIMES,
)
from deeppresenter.utils.log import debug, set_logger, warning
from deeppresenter.utils.webview import PlaywrightConverter

mcp = FastMCP(name="Search")

METASO_API_KEY = os.getenv("METASO_API_KEY", "")
METASO_API_URL = "https://metaso.cn/api/v1/search"
FAKE_UA = UserAgent()

debug(f"Metaso search tool loaded, API key: {METASO_API_KEY[:12]}...")


async def metaso_request(query: str, max_results: int = 5) -> dict[str, Any]:
    """调用秘塔搜索 API"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {METASO_API_KEY}",
        "User-Agent": FAKE_UA.random,
    }
    payload = {
        "q": query,
        "searchFile": False,
        "includeSummary": False,
        "conciseSnippet": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            METASO_API_URL, headers=headers, json=payload
        ) as response:
            if response.status == 200:
                return await response.json()
            body = await response.text()
            if response.status == 429:
                await asyncio.sleep(MAX_RETRY_INTERVAL)
            warning(f"Metaso Error [{response.status}] body={body}")
            response.raise_for_status()
    raise RuntimeError("Metaso request failed")


@mcp.tool()
async def search_web(
    query: str,
    max_results: int = 3,
    time_range: Literal["month", "year"] | None = None,
) -> dict:
    """
    Search the web

    Args:
        query: Search keywords
        max_results: Maximum number of search results, default 3
        time_range: Time range filter for search results, can be "month", "year", or None

    Returns:
        dict: Dictionary containing search results
    """
    last_error = None
    for attempt in range(RETRY_TIMES):
        try:
            data = await metaso_request(query, max_results)
            webpages = data.get("webpages", [])[:max_results]
            results = [
                {
                    "url": item.get("link", ""),
                    "content": item.get("snippet", ""),
                }
                for item in webpages
            ]
            return {
                "query": query,
                "total_results": len(results),
                "results": results,
            }
        except Exception as e:
            last_error = e
            warning(f"Metaso search attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(min(2**attempt, MAX_RETRY_INTERVAL))

    raise RuntimeError(f"Metaso search failed after {RETRY_TIMES} retries") from last_error


@mcp.tool()
async def search_images(query: str) -> dict:
    """
    Search for web images
    """
    # 秘塔搜索返回的结果中没有专门的图片搜索，使用网页搜索中的图片信息
    try:
        data = await metaso_request(query, max_results=6)
        webpages = data.get("webpages", [])
        images = [
            {
                "url": item.get("link", ""),
                "description": item.get("snippet", "")[:200],
            }
            for item in webpages
            if item.get("link")
        ][:4]
        return {
            "query": query,
            "total_results": len(images),
            "images": images,
        }
    except Exception as e:
        warning(f"Metaso image search failed: {e}")
        return {"query": query, "total_results": 0, "images": []}


@mcp.tool()
async def fetch_url(url: str, body_only: bool = True) -> str:
    """
    Fetch web page content

    Args:
        url: Target URL
        body_only: If True, return only main content; otherwise return full page, default True
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            resp = await client.head(url)
            if resp.status_code >= 400:
                resp = await client.get(url, stream=True)
            content_type = resp.headers.get("Content-Type", "").lower()
            content_dispo = resp.headers.get("Content-Disposition", "").lower()
            if "attachment" in content_dispo or "filename=" in content_dispo:
                return f"URL {url} is a downloadable file (Content-Disposition: {content_dispo})"
            if not content_type.startswith("text/html"):
                return f"URL {url} returned {content_type}, not a web page"
        except Exception:
            pass

    async with PlaywrightConverter() as converter:
        try:
            await converter.page.goto(
                url, wait_until="domcontentloaded", timeout=MCP_CALL_TIMEOUT // 2 * 1000
            )
            html = await converter.page.content()
        except TimeoutError:
            return f"Timeout when loading URL: {url}"
        except Exception as e:
            return f"Failed to load URL {url}: {e}"

    markdown = markdownify.markdownify(html, heading_style=markdownify.ATX)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if body_only:
        result = extract(
            html,
            output_format="markdown",
            with_metadata=True,
            include_links=True,
            include_images=True,
            include_tables=True,
        )
        return result or markdown
    return markdown


@mcp.tool()
async def download_file(url: str, output_file: str) -> str:
    """
    Download a file from a URL and save it to a local path.
    """
    workspace = Path(os.getcwd())
    output_path = Path(output_file).resolve()
    assert output_path.is_relative_to(workspace), (
        f"Access denied: path outside allowed workspace: {workspace}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = Path(output_path).suffix.lower()
    ext_format_map = Image.registered_extensions()
    for retry in range(RETRY_TIMES):
        try:
            await asyncio.sleep(retry)
            async with httpx.AsyncClient(
                headers={"User-Agent": FAKE_UA.random},
                follow_redirects=True,
                verify=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = await response.aread()
            try:
                with Image.open(BytesIO(data)) as img:
                    img.load()
                    save_format = ext_format_map.get(suffix, img.format)
                    note = ""
                    if img.format == "WEBP" or suffix == ".webp":
                        output_path = output_path.with_suffix(".png")
                        save_format = "PNG"
                        note = " (converted from WEBP to PNG)"
                    img.save(output_path, format=save_format)
                    width, height = img.size
                    return f"File downloaded to {output_path} (resolution: {width}x{height}){note}"
            except Exception:
                with open(output_path, "wb") as f:
                    f.write(data)
            break
        except Exception:
            pass
    else:
        return f"Failed to download file from {url}"
    return f"File downloaded to {output_path}"


if __name__ == "__main__":
    assert len(sys.argv) == 2, "Usage: python metaso_search.py <workspace>"
    work_dir = Path(sys.argv[1])
    assert work_dir.exists(), f"Workspace {work_dir} does not exist."
    os.chdir(work_dir)
    set_logger(f"search-{work_dir.stem}", work_dir / ".history" / "search.log")
    mcp.run(show_banner=False)
