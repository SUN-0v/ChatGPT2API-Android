"""curl_cffi.requests 的 httpx 实现(子集)。

支持项目实际用到的 API:
- Session(impersonate=..., verify=..., proxy=..., headers=..., timeout=...)
- session.request/get/post/put/delete/patch/head(url, headers=, params=, data=,
  json=, content=, files=, timeout=, verify=, allow_redirects=, stream=)
- session.headers / session.cookies(支持 domain)/ session.close()
- Response: status_code / headers / text / content / json() / url / ok /
  iter_lines() / iter_content() / close()
- 模块级 get()/post() 等便捷函数
- errors.RequestsError 异常
"""

from __future__ import annotations

import json as _json
from typing import Any, Iterator, Optional

import httpx

__all__ = [
    "Session",
    "Response",
    "Cookies",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "request",
    "errors",
]

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

_LOGGED_IMPERSONATE_WARNING = False


def _warn_impersonate_once() -> None:
    global _LOGGED_IMPERSONATE_WARNING
    if not _LOGGED_IMPERSONATE_WARNING:
        _LOGGED_IMPERSONATE_WARNING = True
        print(
            "[curl_cffi-shim] 提示: Android 版使用 httpx 替代 curl_cffi, "
            "无 TLS 指纹伪装(impersonate); chatgpt.com 的 Cloudflare 校验 "
            "由 cf_bypass(WebView clearance 桥)负责, 此提示可忽略。",
            flush=True,
        )


class RequestsError(Exception):
    """对齐 curl_cffi.requests.errors.RequestsError。"""

    def __init__(self, message: str, code: int = 0, response: "Response | None" = None):
        super().__init__(message)
        self.code = code
        self.response = response


class _ErrorsModule:
    RequestsError = RequestsError
    RequestsIOError = RequestsError


errors = _ErrorsModule()

Cookies = httpx.Cookies


def _to_timeout(value: Any) -> httpx.Timeout:
    if isinstance(value, httpx.Timeout):
        return value
    if isinstance(value, (tuple, list)):
        connect = float(value[0]) if len(value) > 0 and value[0] is not None else 30.0
        read = float(value[1]) if len(value) > 1 and value[1] is not None else connect
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    if value is None:
        return httpx.Timeout(120.0)
    return httpx.Timeout(float(value))


class Response:
    def __init__(self, raw: httpx.Response, stream_cm: Any = None):
        self._raw = raw
        self._stream_cm = stream_cm

    # --- 基础属性 -------------------------------------------------------
    @property
    def status_code(self) -> int:
        return self._raw.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self._raw.headers

    @property
    def url(self) -> httpx.URL:
        return self._raw.url

    @property
    def ok(self) -> bool:
        return 200 <= self._raw.status_code < 400

    @property
    def reason(self) -> str:
        return self._raw.reason_phrase

    @property
    def cookies(self) -> httpx.Cookies:
        return self._raw.cookies

    @property
    def content(self) -> bytes:
        return self._raw.content

    @property
    def text(self) -> str:
        return self._raw.text

    @property
    def encoding(self) -> Optional[str]:
        return self._raw.encoding

    def json(self, **kwargs: Any) -> Any:
        if kwargs:
            return _json.loads(self.text, **kwargs)
        return self._raw.json()

    # --- 流式 -----------------------------------------------------------
    def iter_lines(self) -> Iterator[bytes]:
        return self._raw.iter_lines()

    def iter_content(self, chunk_size: int = 8192) -> Iterator[bytes]:
        return self._raw.iter_bytes(chunk_size=chunk_size)

    def iter_bytes(self, chunk_size: int = 8192) -> Iterator[bytes]:
        return self._raw.iter_bytes(chunk_size=chunk_size)

    # --- 生命周期 -------------------------------------------------------
    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            if self._stream_cm is not None:
                try:
                    self._stream_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._stream_cm = None

    def __del__(self) -> None:  # noqa: D105 - best effort cleanup
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Session:
    def __init__(
        self,
        impersonate: Any = None,
        verify: bool = True,
        proxy: Any = None,
        proxies: Any = None,
        timeout: Any = None,
        headers: Any = None,
        cookies: Any = None,
        **kwargs: Any,
    ):
        if impersonate:
            _warn_impersonate_once()
        self._verify = bool(verify)
        self._proxy = proxy or (proxies.get("https") if isinstance(proxies, dict) else proxies)
        self._default_timeout = timeout
        self.cookies = httpx.Cookies(cookies)
        base_headers = {"User-Agent": _DEFAULT_UA}
        if headers:
            base_headers.update(dict(headers))
        self.headers = httpx.Headers(base_headers)
        self._client = self._build_client(self._verify)

    # ------------------------------------------------------------------
    def _build_client(self, verify: bool) -> httpx.Client:
        client = httpx.Client(
            verify=verify,
            proxy=self._proxy,
            cookies=self.cookies,
            headers=self.headers,
            follow_redirects=False,
            timeout=_to_timeout(self._default_timeout),
        )
        # httpx.Client(cookies=...) 内部会拷贝 Cookies 对象(不共享底层 CookieJar),
        # 导致构造后对 session.cookies 的注入/响应 Set-Cookie 互不可见。
        # 这里强制共享, 对齐 curl_cffi 的会话语义(httpx 版本已在 gradle 中锁定)。
        client._cookies = self.cookies
        return client

    # ------------------------------------------------------------------
    def rebuild_client(self) -> None:
        """重建底层 httpx.Client。

        修改 session.headers 之后调用, 使 client 级默认头立即生效
        (httpx 在构造时拷贝 headers, 事后修改不会同步)。
        """
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._build_client(self._verify)

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        params: Any = None,
        data: Any = None,
        json: Any = None,
        content: Any = None,
        files: Any = None,
        headers: Any = None,
        timeout: Any = None,
        verify: Any = None,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: Any = None,
        **kwargs: Any,
    ) -> Response:
        client = self._client
        if verify is not None and bool(verify) != self._verify:
            # 极少数路径: 单次请求覆盖 verify, 用临时 client 共享 cookies
            client = self._build_client(bool(verify))
        req_kwargs: dict[str, Any] = {
            "params": params,
            "headers": headers,
            "follow_redirects": allow_redirects,
        }
        if json is not None:
            req_kwargs["json"] = json
        elif files is not None:
            req_kwargs["files"] = files
            if data is not None:
                req_kwargs["data"] = data
        elif content is not None:
            req_kwargs["content"] = content
        elif data is not None:
            req_kwargs["data"] = data
        if timeout is not None:
            req_kwargs["timeout"] = _to_timeout(timeout)

        try:
            if stream:
                cm = client.stream(method.upper(), url, **req_kwargs)
                raw = cm.__enter__()
                return Response(raw, stream_cm=cm)
            raw = client.request(method.upper(), url, **req_kwargs)
            return Response(raw)
        except RequestsError:
            raise
        except httpx.HTTPError as exc:
            raise RequestsError(f"{exc.__class__.__name__}: {exc}") from exc

    # ------------------------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Response:
        return self.request("PATCH", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        return self.request("OPTIONS", url, **kwargs)

    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --- 模块级便捷函数 ------------------------------------------------------

def request(method: str, url: str, **kwargs: Any) -> Response:
    with Session() as session:
        return session.request(method, url, **kwargs)


def get(url: str, **kwargs: Any) -> Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return request("POST", url, **kwargs)


def put(url: str, **kwargs: Any) -> Response:
    return request("PUT", url, **kwargs)


def delete(url: str, **kwargs: Any) -> Response:
    return request("DELETE", url, **kwargs)


def patch(url: str, **kwargs: Any) -> Response:
    return request("PATCH", url, **kwargs)


def head(url: str, **kwargs: Any) -> Response:
    return request("HEAD", url, **kwargs)


def options(url: str, **kwargs: Any) -> Response:
    return request("OPTIONS", url, **kwargs)
