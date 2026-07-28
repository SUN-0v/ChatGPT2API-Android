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


# 生图协议补丁: 跟进 ChatGPT Web 当前模型、Sentinel PoW 与 SSE 结果结构。
IMAGE_PATCHES: list[tuple[str, str, str]] = [
    (
        "config.json",
        '  "global_system_prompt": "",\n',
        '  "global_system_prompt": "",\n  "default_upstream_model_name": "gpt-5-5",\n',
    ),
    (
        "services/config.py",
        '''    @property
    def images_dir(self) -> Path:''',
        '''    @property
    def default_upstream_model_name(self) -> str:
        return str(self.data.get("default_upstream_model_name") or "gpt-5-5").strip() or "gpt-5-5"

    @property
    def images_dir(self) -> Path:''',
    ),
    (
        "services/config.py",
        '        data["global_system_prompt"] = self.global_system_prompt\n',
        '        data["global_system_prompt"] = self.global_system_prompt\n'
        '        data["default_upstream_model_name"] = self.default_upstream_model_name\n',
    ),
    (
        "services/openai_backend_api.py",
        'DEFAULT_CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"\n'
        'DEFAULT_CLIENT_BUILD_NUMBER = "5955942"',
        'DEFAULT_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"\n'
        'DEFAULT_CLIENT_BUILD_NUMBER = "6708908"',
    ),
    (
        "services/openai_backend_api.py",
        '        fp.setdefault("impersonate", "edge101")',
        '        fp.setdefault("impersonate", "chrome110")',
    ),
    (
        "services/openai_backend_api.py",
        '''        if base_model == "gpt-image-2":
            return "gpt-5-3"''',
        '''        if base_model == "gpt-image-2":
            return config.default_upstream_model_name''',
    ),
    (
        "services/openai_backend_api.py",
        '''            if metadata.get("async_task_type") != "image_gen":
                continue
            if content.get("content_type") != "multimodal_text":
                continue
            file_ids, sediment_ids = [], []
            for part in content.get("parts") or []:
                text = (part.get("asset_pointer") or "") if isinstance(part, dict) else (
                    part if isinstance(part, str) else "")
                for hit in file_pat.findall(text):
                    if hit not in file_ids:
                        file_ids.append(hit)
                for hit in sed_pat.findall(text):
                    if hit not in sediment_ids:
                        sediment_ids.append(hit)''',
        '''            content_text = json.dumps(content, ensure_ascii=False)
            has_image_pointer = bool(file_pat.search(content_text) or sed_pat.search(content_text))
            if metadata.get("async_task_type") != "image_gen" and not has_image_pointer:
                continue
            file_ids, sediment_ids = [], []
            for hit in file_pat.findall(content_text):
                if hit not in file_ids:
                    file_ids.append(hit)
            for hit in sed_pat.findall(content_text):
                if hit not in sediment_ids:
                    sediment_ids.append(hit)''',
    ),
    (
        "utils/helper.py",
        '''def iter_sse_payloads(response: requests.Response) -> Iterator[str]:
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload:
            yield payload''',
        '''def iter_sse_payloads(response: requests.Response) -> Iterator[str]:
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
        if not line:
            if data_lines:
                yield "\\n".join(data_lines)
                data_lines = []
            continue
        if not line.startswith("data:"):
            continue
        data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\\n".join(data_lines)''',
    ),
    (
        "utils/pow.py",
        'DOCUMENT_KEYS = ["_reactListeningo743lnnpvdg", "location"]',
        'DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]\n'
        'SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]',
    ),
    ("utils/pow.py", "        random.choice([3000, 4000, 5000]),", "        sum(random.choice(SCREEN_RESOLUTIONS)),"),
    ("utils/pow.py", "        4294705152,\n        0,", "        4294705152,\n        1,"),
    (
        "utils/pow.py",
        '        "en-US,es-US,en,es",\n        0,',
        '        "en-US,es-US,en,es",\n        random.random(),',
    ),
    (
        "utils/pow.py",
        '''        random.choice(CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
    ]''',
        '''        random.choice(CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
        0, 0, 0, 0, 0, 0,
        0,
    ]''',
    ),
    (
        "utils/pow.py",
        '''    seed = format(random.random())
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build)
    answer, _ = _pow_generate(seed, "0fffff", config)
    return "gAAAAAC" + answer''',
        '''    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build)
    return "gAAAAAC" + pybase64.b64encode(
        json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()''',
    ),
    (
        "services/protocol/conversation.py",
        '''def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\\s*:\\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids = re.findall(r"(file[-_][A-Za-z0-9]+)", payload)
    sediment_ids = re.findall(r"sediment://([A-Za-z0-9_-]+)", payload)
    return conversation_id, file_ids, sediment_ids''',
        '''FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\\b(file[-_](?!service\\b)[A-Za-z0-9_-]+)\\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\\s*:\\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    add_unique(file_ids, FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids''',
    ),
    (
        "services/protocol/conversation.py",
        '''def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    return author.get("role") == "tool" and metadata.get("async_task_type") == "image_gen"''',
        '''def iter_event_messages(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("author"), dict) and isinstance(value.get("content"), dict):
            yield value
        for child in value.values():
            yield from iter_event_messages(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_event_messages(child)


def is_image_tool_event(event: dict[str, Any]) -> bool:
    for message in iter_event_messages(event):
        metadata = message.get("metadata") or {}
        author = message.get("author") or {}
        if author.get("role") != "tool":
            continue
        if metadata.get("async_task_type") == "image_gen":
            return True
        content = message.get("content") or {}
        if any(
            isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
            )
            for part in content.get("parts") or []
        ):
            return True
    return False


def is_user_message_event(event: dict[str, Any]) -> bool:
    return any(
        str((message.get("author") or {}).get("role") or "").lower() == "user"
        for message in iter_event_messages(event)
    )''',
    ),
    (
        "services/protocol/conversation.py",
        '''    if isinstance(event, dict) and is_image_tool_event(event):
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)''',
        '''    is_patch = isinstance(event, dict) and event.get("o") == "patch"
    is_user_message = isinstance(event, dict) and is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_message)
        or (is_patch and not is_user_message and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)''',
    ),
    (
        "services/protocol/conversation.py",
        '''    if message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message)


def stream_codex_image_outputs''',
        '''    if message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message)
        return
    raise RuntimeError("Upstream image generation returned no image result")


def stream_codex_image_outputs''',
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
    for rel, old, new in PYDANTIC_PATCHES + GIT_PATCHES + IMAGE_PATCHES + CF_PATCHES:
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
    data_dir = stage / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "image_owners.json").write_text("{}", encoding="utf-8")
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
