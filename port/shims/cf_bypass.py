"""Cloudflare clearance 桥(Android 专用, 桌面环境全部为空操作)。

背景: chatgpt.com 受 Cloudflare 防护, 按 TLS/HTTP2 指纹校验客户端。
httpx 无指纹伪装能力 -> 直接 403(managed challenge)。

工作原理(FlareSolverr 同款模式):
  1) Java CfClearanceHelper 用 WebView(真实 Chromium 内核)加载 chatgpt.com,
     等待 Cloudflare 自动放行并签发 cf_clearance Cookie;
  2) 取回 cf_clearance 等 Cookie 与 WebView 的真实 User-Agent;
  3) 注入 curl_cffi-shim 的 Session, 后续 httpx 请求凭 clearance 通过 CF。

注意: cf_clearance 与 UA、出口 IP 绑定, 使用期间不要切换 UA/代理。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

_TTL_SECONDS = 15 * 60       # cf_clearance 保守有效期, 过期重新获取
_FETCH_TIMEOUT_MS = 90000    # WebView 等待 CF 放行的上限
_TARGET = "https://chatgpt.com/"

_lock = threading.Lock()
_cache: dict[str, Any] = {"ua": "", "cookies": [], "ts": 0.0}
_java_cls: Any = None
_java_checked = False


def _java_helper():
    """返回 Java CfClearanceHelper 类; 非 Android / 不可用时返回 None。"""
    global _java_cls, _java_checked
    if _java_checked:
        return _java_cls
    _java_checked = True
    try:
        from com.chatgpt2api.server import CfClearanceHelper  # Chaquopy import hook

        _java_cls = CfClearanceHelper
    except Exception:
        _java_cls = None
    return _java_cls


def available() -> bool:
    """当前环境是否有 WebView 桥可用(即是否在 Android 上运行)。"""
    return _java_helper() is not None


def get_clearance(force: bool = False) -> Optional[dict]:
    """获取(或复用) clearance。

    成功返回 {"ua": str, "cookies": [(name, value), ...]}; 失败返回 None。
    """
    helper = _java_helper()
    if helper is None:
        return None
    with _lock:
        if not force and _cache["ua"] and (time.time() - _cache["ts"] < _TTL_SECONDS):
            return {"ua": _cache["ua"], "cookies": list(_cache["cookies"])}
        print("[cf_bypass] 正在通过 WebView 获取 Cloudflare clearance(约需数秒)...", flush=True)
        try:
            raw = helper.fetch(_TARGET, _FETCH_TIMEOUT_MS)
            data = json.loads(str(raw))
        except Exception as exc:
            print(f"[cf_bypass] 获取失败: {exc.__class__.__name__}: {exc}", flush=True)
            return None
        if not data.get("ok"):
            print(
                f"[cf_bypass] 获取失败: {data.get('error') or 'unknown'}"
                "(若出口网络被拦截, 请在设置中配置代理后重试)",
                flush=True,
            )
            return None
        pairs = []
        for part in str(data.get("cookie") or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip():
                pairs.append((name.strip(), value.strip()))
        ua = str(data.get("ua") or "").strip()
        if not ua or not any(n == "cf_clearance" for n, _ in pairs):
            print("[cf_bypass] WebView 未取到 cf_clearance, Cloudflare 可能要求交互验证", flush=True)
            return None
        _cache.update({"ua": ua, "cookies": pairs, "ts": time.time()})
        print(f"[cf_bypass] clearance 获取成功 (UA: {ua})", flush=True)
        return {"ua": ua, "cookies": list(pairs)}


def apply_to_session(session: Any, force: bool = False) -> Optional[str]:
    """把 clearance Cookie 注入 curl_cffi-shim Session。

    成功返回应使用的 User-Agent(WebView 真实 UA), 失败返回 None。
    """
    data = get_clearance(force=force)
    if not data:
        return None
    try:
        for name, value in data["cookies"]:
            session.cookies.set(name, value, domain=".chatgpt.com", path="/")
    except Exception as exc:
        print(f"[cf_bypass] Cookie 注入失败: {exc.__class__.__name__}: {exc}", flush=True)
        return None
    return data["ua"] or None
