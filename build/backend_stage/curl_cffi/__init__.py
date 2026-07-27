"""curl_cffi 兼容 shim(Android / Chaquopy 移植层)。

Chaquopy 没有 curl_cffi 的 Android 原生构建(它依赖 libcurl-impersonate),
这里用 httpx 实现项目用到的 requests 子集 API。

注意:此 shim 不具备 TLS 指纹伪装(impersonate)能力,对 Cloudflare 强校验的
上游(如 chatgpt.com)请求可能被拦截。调用方应像对待普通网络错误一样处理。
"""

from . import requests  # noqa: F401
from .requests import Session, Response  # noqa: F401

__version__ = "0.0.0-android-shim"
