"""Global outbound proxy helpers for upstream ChatGPT and CPA requests."""

from __future__ import annotations

import time
from urllib.parse import urlparse

from curl_cffi.requests import Session

from services.config import config


def _android_system_proxy() -> str:
    """Android: 读取系统代理, 返回 "http://host:port"; 非 Android / 无系统代理返回 ""。

    背景: WebView 走系统代理/VPN 能访问 chatgpt.com, 而 httpx 读不到 Android
    系统代理会直连, 导致连接被重置(SSL EOF)。cf_clearance 又与出口 IP 绑定,
    两侧必须走同一出口。
    """
    try:
        from com.chatgpt2api.server import CfClearanceHelper  # Chaquopy import hook

        raw = str(CfClearanceHelper.getSystemProxy() or "").strip()
        if raw:
            return "http://" + raw
    except Exception:
        pass
    return ""


class ProxySettingsStore:
    def build_session_kwargs(self, **session_kwargs) -> dict[str, object]:
        # 手动配置的代理优先; 未配置时自动跟随 Android 系统代理(桌面环境返回 "")
        proxy = config.get_proxy_settings() or _android_system_proxy()
        if proxy:
            session_kwargs["proxy"] = proxy
        return session_kwargs


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_valid_proxy_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https", "socks5", "socks5h"} and bool(parsed.netloc)


def test_proxy(url: str, *, timeout: float = 15.0) -> dict:
    candidate = _clean(url)
    if not candidate:
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "proxy url is required"}
    if not _is_valid_proxy_url(candidate):
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "invalid proxy url"}
    session = Session(impersonate="edge101", verify=True, proxy=candidate)
    started = time.perf_counter()
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"user-agent": "Mozilla/5.0 (chatgpt2api proxy test)"},
            timeout=timeout,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": response.status_code < 500,
            "status": int(response.status_code),
            "latency_ms": latency_ms,
            "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "status": 0,
            "latency_ms": latency_ms,
            "error": str(exc) or exc.__class__.__name__,
        }
    finally:
        session.close()

proxy_settings = ProxySettingsStore()

