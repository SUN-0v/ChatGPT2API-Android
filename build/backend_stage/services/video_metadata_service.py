from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from curl_cffi import requests


_CACHE_TTL_SECONDS = 600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
}
_BILIBILI_SHORT_HOSTS = {
    "b23.tv",
    "bili2233.cn",
}


def _clean_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported url")
    return url


def cover_proxy_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    return f"/api/video/cover?url={quote(value, safe='')}"


def _host(value: str) -> str:
    return urlparse(value).hostname.lower() if urlparse(value).hostname else ""


def _is_bilibili_host(host: str) -> bool:
    return host in _BILIBILI_HOSTS or host.endswith(".bilibili.com")


def _extract_bilibili_video(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _is_bilibili_host(host):
        raise ValueError("not a bilibili video url")
    segments = [item for item in parsed.path.split("/") if item]
    try:
        video_index = next(i for i, item in enumerate(segments) if item.lower() == "video")
    except StopIteration as exc:
        raise ValueError("not a bilibili video url") from exc
    video_id = segments[video_index + 1] if video_index + 1 < len(segments) else ""
    if not re.match(r"^(BV[0-9A-Za-z]+|av\d+)$", video_id, re.IGNORECASE):
        raise ValueError("unsupported bilibili video id")
    return video_id, parsed.query


def _resolve_short_url(url: str) -> str:
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        },
        allow_redirects=True,
        timeout=15,
    )
    response.close()
    return str(response.url or url)


def _page_from_query(query: str) -> str:
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key in {"p", "page"} and value:
            return value
    return "1"


def get_video_metadata(url: str) -> dict[str, Any]:
    source_url = _clean_url(url)
    now = time.time()
    cached = _cache.get(source_url)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    host = _host(source_url)
    resolved_url = source_url
    if host in _BILIBILI_SHORT_HOSTS:
        resolved_url = _resolve_short_url(source_url)
        host = _host(resolved_url)
    if not _is_bilibili_host(host):
        raise ValueError("unsupported video host")

    video_id, query = _extract_bilibili_video(resolved_url)
    page = _page_from_query(query)
    is_av = video_id.lower().startswith("av")
    params = {"aid": video_id[2:]} if is_av else {"bvid": video_id}
    response = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json",
        },
        timeout=20,
    )
    payload = response.json()
    response.close()
    if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
        raise ValueError(str(payload.get("message") or "bilibili metadata failed"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("bilibili metadata missing data")
    bvid = str(data.get("bvid") or ("" if is_av else video_id)).strip()
    aid = str(data.get("aid") or (video_id[2:] if is_av else "")).strip()
    canonical_id = bvid or f"av{aid}"
    embed_query = f"bvid={bvid}" if bvid else f"aid={aid}"
    raw_thumb = str(data.get("pic") or "").strip()
    result = {
        "provider": "bilibili",
        "id": canonical_id,
        "title": str(data.get("title") or "").strip(),
        "thumb_url": cover_proxy_url(raw_thumb) if raw_thumb else "",
        "raw_thumb_url": raw_thumb,
        "watch_url": f"https://www.bilibili.com/video/{canonical_id}",
        "embed_url": f"https://player.bilibili.com/player.html?{embed_query}&p={page}&autoplay=1",
    }
    _cache[source_url] = (now, result)
    if resolved_url != source_url:
        _cache[resolved_url] = (now, result)
    return result


def get_video_cover(url: str) -> tuple[bytes, str]:
    source_url = _clean_url(url)
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if not (host.endswith("hdslb.com") or host.endswith("bilibili.com") or host.endswith("bilivideo.com")):
        raise ValueError("unsupported cover host")
    response = requests.get(
        source_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
        timeout=20,
    )
    try:
        if not (200 <= response.status_code < 300):
            raise ValueError(f"cover fetch failed: {response.status_code}")
        content_type = str(response.headers.get("content-type") or "image/jpeg").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise ValueError("cover response is not image")
        return response.content, content_type
    finally:
        response.close()
