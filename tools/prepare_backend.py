#!/usr/bin/env python3
"""准备打进 APK 的后端: 拷贝上游源码 -> 打 Android 兼容补丁 -> 加入 shim -> 生成 backend.zip。

用法: python3 tools/prepare_backend.py <上游仓库目录> <输出zip路径> [web_out目录]

补丁原则: 全部为显式字符串替换, 命中失败立即报错(防止上游更新后静默漏补)。
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PORT_DIR = PROJECT_ROOT / "port"

# 需要拷贝进 APK 的上游文件/目录
BACKEND_ITEMS = ["main.py", "config.json", "VERSION", "api", "services", "utils", "scripts"]

# ---------------------------------------------------------------------------
# pydantic v2 -> v1 补丁 (Chaquopy 无 pydantic-core, 只能用纯 Python 的 v1)
# 每项: (相对路径, 旧文本, 新文本)
# ---------------------------------------------------------------------------
PYDANTIC_PATCHES: list[tuple[str, str, str]] = [
    # api/ai.py
    ("api/ai.py", "from pydantic import BaseModel, ConfigDict, Field", "from pydantic import BaseModel, Field"),
    ("api/ai.py", "    model_config = ConfigDict(extra=\"allow\")\n", "    class Config:\n        extra = \"allow\"\n"),
    ("api/ai.py", "body.model_dump(mode=\"python\")", "body.dict()"),
    # api/system.py
    ("api/system.py", "from pydantic import BaseModel, ConfigDict", "from pydantic import BaseModel"),
    ("api/system.py", "    model_config = ConfigDict(extra=\"allow\")\n", "    class Config:\n        extra = \"allow\"\n"),
    ("api/system.py", "body.model_dump(mode=\"python\")", "body.dict()"),
    # api/accounts.py
    ("api/accounts.py", "body.model_dump(exclude_none=True)", "body.dict(exclude_none=True)"),
    # api/register.py
    ("api/register.py", "body.model_dump(exclude_none=True)", "body.dict(exclude_none=True)"),
]

# GitPython 容错补丁: Android 无 git 可执行文件, import git 即失败, 不能让它拖垮启动
GIT_PATCHES: list[tuple[str, str, str]] = [
    (
        "services/storage/factory.py",
        "from services.storage.git_storage import GitStorageBackend",
        "_GIT_IMPORT_ERROR = None\n"
        "try:\n"
        "    from services.storage.git_storage import GitStorageBackend\n"
        "except Exception as _exc:  # Android: GitPython 在无 git 二进制的环境 import 即失败\n"
        "    GitStorageBackend = None\n"
        "    _GIT_IMPORT_ERROR = _exc",
    ),
    (
        "services/storage/factory.py",
        '    elif backend_type == "git":\n        # Git 仓库存储\n',
        '    elif backend_type == "git":\n        # Git 仓库存储\n'
        "        if GitStorageBackend is None:\n"
        '            raise ValueError(f"git 存储后端在当前环境不可用(缺少 git 可执行文件): {_GIT_IMPORT_ERROR}")\n',
    ),
]


# Cloudflare clearance 桥补丁: chatgpt.com 按 TLS 指纹校验, httpx 无伪装能力,
# 接入 cf_bypass(WebView clearance 桥)使对话链路可用。
CF_PATCHES: list[tuple[str, str, str]] = [
    (
        "services/openai_backend_api.py",
        "from curl_cffi import requests\nfrom PIL import Image",
        "from curl_cffi import requests\n\n"
        "try:  # Android: Cloudflare clearance 桥(桌面环境无此模块, 静默跳过)\n"
        "    import cf_bypass\n"
        "except Exception:  # noqa: BLE001\n"
        "    cf_bypass = None\n"
        "from PIL import Image",
    ),
    (
        "services/openai_backend_api.py",
        '''    def _bootstrap_headers(self) -> Dict[str, str]:
        """构造首页预热请求头。"""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": self.session.headers["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": self.session.headers["Sec-Ch-Ua-Mobile"],
            "Sec-Ch-Ua-Platform": self.session.headers["Sec-Ch-Ua-Platform"],
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }''',
        '''    def _bootstrap_headers(self) -> Dict[str, str]:
        """构造首页预热请求头。"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        # Android clearance 模式下部分 sec-ch-ua* 头会被移除, 这里容错读取
        for key in ("Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform"):
            value = self.session.headers.get(key)
            if value:
                headers[key] = value
        return headers''',
    ),
    (
        "services/openai_backend_api.py",
        '''    def _bootstrap(self) -> None:
        """预热首页，并提取 PoW 相关脚本引用。"""
        response = self.session.get(
            self.base_url + "/",
            headers=self._bootstrap_headers(),
            timeout=30,
        )
        ensure_ok(response, "bootstrap")''',
        '''    def _apply_cf_clearance(self, force: bool = False) -> bool:
        """Android 专用: 经 WebView 桥获取 cf_clearance 并对齐会话指纹。

        桌面环境(真 curl_cffi, 自带 TLS 指纹伪装)为空操作。
        force=True 时忽略缓存强制刷新(用于 403 后重试)。
        """
        if cf_bypass is None or not cf_bypass.available():
            return False
        ua = cf_bypass.apply_to_session(self.session, force=force)
        if not ua:
            return False
        if ua != self.user_agent:
            self.user_agent = ua
            self.fp["user-agent"] = ua
        headers = self.session.headers
        headers["User-Agent"] = ua
        # WebView 是 Android Chromium: 移除与 UA 矛盾的 Windows/Edge 高熵头,
        # 仅保留与真实设备一致的低熵提示
        for key in (
                "Sec-Ch-Ua",
                "Sec-Ch-Ua-Arch",
                "Sec-Ch-Ua-Bitness",
                "Sec-Ch-Ua-Full-Version",
                "Sec-Ch-Ua-Full-Version-List",
                "Sec-Ch-Ua-Platform-Version",
                "Sec-Ch-Ua-Model",
        ):
            if key in headers:
                del headers[key]
        headers["Sec-Ch-Ua-Mobile"] = "?1"
        headers["Sec-Ch-Ua-Platform"] = '"Android"'
        rebuild = getattr(self.session, "rebuild_client", None)
        if callable(rebuild):
            rebuild()
        return True

    def _bootstrap(self) -> None:
        """预热首页，并提取 PoW 相关脚本引用。"""
        self._apply_cf_clearance()
        try:
            response = self.session.get(
                self.base_url + "/",
                headers=self._bootstrap_headers(),
                timeout=30,
            )
        except Exception as exc:
            hint = ""
            if cf_bypass is not None and cf_bypass.available():
                hint = ("(WebView 已取到 clearance 但连接仍被重置, 多为 httpx 与系统网络出口不一致: "
                        "请在设置中配置与系统/VPN 相同的代理后重试)")
            raise RuntimeError(f"bootstrap 连接失败: {exc.__class__.__name__}: {exc} {hint}") from exc
        if response.status_code in (403, 503) and self._apply_cf_clearance(force=True):
            print(f"[cf] bootstrap {response.status_code}, 已刷新 Cloudflare clearance, 重试一次", flush=True)
            response = self.session.get(
                self.base_url + "/",
                headers=self._bootstrap_headers(),
                timeout=30,
            )
        if response.status_code in (403, 503):
            # 首页被拒不再致命: 部分网络下上游只拦截 HTML 导航页,
            # /backend-api/* 接口仍可直连(真机实测); 回退默认 PoW 脚本,
            # 由后续真实请求(sentinel/conversation)判定可用性。
            print(f"[cf] bootstrap 首页仍 {response.status_code}, 回退默认 PoW 脚本继续", flush=True)
            self.pow_script_sources = [DEFAULT_POW_SCRIPT]
            self.pow_data_build = ""
            return
        ensure_ok(response, "bootstrap")''',
    ),
    (
        # httpx 读不到 Android 系统代理: WebView 能过 CF 但 httpx 直连被重置(SSL EOF),
        # 未手动配置代理时自动跟随系统代理, 与 WebView 出口对齐(clearance 绑定出口 IP)
        "services/proxy_service.py",
        '''class ProxySettingsStore:
    def build_session_kwargs(self, **session_kwargs) -> dict[str, object]:
        proxy = config.get_proxy_settings()
        if proxy:
            session_kwargs["proxy"] = proxy
        return session_kwargs''',
        '''def _android_system_proxy() -> str:
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
        return session_kwargs''',
    ),
]


def copy_backend(src_repo: Path, stage: Path) -> None:
    for item in BACKEND_ITEMS:
        s = src_repo / item
        d = stage / item
        if not s.exists():
            print(f"!! 上游缺少 {item}, 跳过")
            continue
        if s.is_dir():
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(s, d)


def apply_patches(stage: Path) -> None:
    for rel, old, new in PYDANTIC_PATCHES + GIT_PATCHES + CF_PATCHES:
        path = stage / rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise SystemExit(f"补丁未命中: {rel}: {old[:60]!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"补丁 {rel}: {old[:48]!r} x{count}")


def add_port_layer(stage: Path, web_out: Path | None) -> None:
    shims = PORT_DIR / "shims"
    for item in shims.iterdir():
        d = stage / item.name
        if item.is_dir():
            shutil.copytree(item, d, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(item, d)
    shutil.copy2(PORT_DIR / "android_entry.py", stage / "android_entry.py")
    if web_out is not None:
        if not (web_out / "index.html").exists():
            raise SystemExit(f"web_out 无效(缺少 index.html): {web_out}")
        shutil.copytree(web_out, stage / "web_dist")
        print(f"前端静态资源: {web_out} -> web_dist/")
    else:
        print("!! 未提供前端构建产物, APK 将没有 Web 面板(仅 API)")


def make_zip(stage: Path, out_zip: Path) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())
    size_mb = out_zip.stat().st_size / 1024 / 1024
    print(f"生成 {out_zip} ({size_mb:.1f} MB)")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    src_repo = Path(sys.argv[1]).resolve()
    out_zip = Path(sys.argv[2]).resolve()
    web_out = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None

    stage = PROJECT_ROOT / "build" / "backend_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    copy_backend(src_repo, stage)
    apply_patches(stage)
    add_port_layer(stage, web_out)
    make_zip(stage, out_zip)


if __name__ == "__main__":
    main()
