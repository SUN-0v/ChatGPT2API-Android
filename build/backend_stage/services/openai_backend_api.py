import base64
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from curl_cffi import requests

try:  # Android: Cloudflare clearance 桥(桌面环境无此模块, 静默跳过)
    import cf_bypass
except Exception:  # noqa: BLE001
    cf_bypass = None
from PIL import Image

from services.account_service import account_service
from services.config import config
from services.proxy_service import proxy_settings
from utils.helper import ensure_ok, iter_sse_payloads, new_uuid, split_image_model
from utils.log import logger
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources
from utils.turnstile import solve_turnstile_token


class InvalidAccessTokenError(RuntimeError):
    pass


@dataclass
class ChatRequirements:
    """保存一次对话请求所需的 sentinel token。"""
    token: str
    proof_token: str = ""
    turnstile_token: str = ""
    so_token: str = ""
    raw_finalize: Optional[Dict[str, Any]] = None


DEFAULT_CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
DEFAULT_CLIENT_BUILD_NUMBER = "5955942"
DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
CODEX_IMAGE_RESPONSES_MODEL = "gpt-5.5"
CODEX_IMAGE_INSTRUCTIONS = "You are an image generation assistant."
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
SEARCH_MODEL = "gpt-5-5"
SEARCH_TIMEOUT_SECS = 300.0
SEARCH_POLL_INTERVAL_SECS = 0.8
SEARCH_DONE_STATUS = {"finished_successfully", "finished_partial_completion"}
SEARCH_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([^"]+)"')
SEARCH_URL_RE = re.compile(r"https?://[^\s\"'<>）)\]}]+")


def refresh_codex_oauth_token(refresh_token: str, client_id: str = "") -> Dict[str, Any]:
    refresh_token = str(refresh_token or "").strip()
    if not refresh_token:
        raise RuntimeError("refresh_token is required")
    client_id = str(client_id or "").strip() or CODEX_OAUTH_CLIENT_ID
    session = requests.Session(**proxy_settings.build_session_kwargs(verify=True))
    try:
        response = session.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=60,
        )
    finally:
        session.close()
    if response.status_code != 200:
        raise RuntimeError(f"oauth refresh failed: HTTP {response.status_code}, body={response.text[:200]}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("oauth refresh response missing access_token")
    now = int(time.time())
    expires_in = int(payload.get("expires_in") or 0)
    result = {
        "access_token": str(payload.get("access_token") or "").strip(),
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or "").strip() or None,
        "token_type": str(payload.get("token_type") or "").strip() or None,
        "expires_in": expires_in,
        "expires_at": now + expires_in if expires_in > 0 else None,
        "client_id": client_id,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source_type": "codex",
    }
    auth_claims = OpenAIBackendAPI._decode_jwt_payload(str(result.get("access_token") or "")).get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        result["account_id"] = str(auth_claims.get("chatgpt_account_id") or "").strip() or None
    id_claims = OpenAIBackendAPI._decode_jwt_payload(str(result.get("id_token") or ""))
    if id_claims:
        result["email"] = str(id_claims.get("email") or "").strip() or None
    return result


class OpenAIBackendAPI:
    """ChatGPT Web 后端封装。

    说明：
    - 传入 `access_token` 时，聊天和模型列表都会走已登录链路
      例如 `/backend-api/sentinel/chat-requirements`、`/backend-api/conversation`
    - 不传 `access_token` 时，会走未登录链路
      例如 `/backend-anon/sentinel/chat-requirements`、`/backend-anon/conversation`
    - `stream_conversation()` 是底层统一流式入口
    - 协议兼容转换放在 `services.protocol`
    """

    def __init__(self, access_token: str = "") -> None:
        """初始化后端客户端。

        参数：
        - `access_token`：可选。传入后表示使用已登录链路；不传则使用未登录链路。
        """
        self.base_url = "https://chatgpt.com"
        self.client_version = DEFAULT_CLIENT_VERSION
        self.client_build_number = DEFAULT_CLIENT_BUILD_NUMBER
        self.access_token = access_token
        self.fp = self._build_fp()
        self.user_agent = self.fp["user-agent"]
        self.device_id = self.fp["oai-device-id"]
        self.session_id = self.fp["oai-session-id"]
        self.pow_script_sources: list[str] = []
        self.pow_data_build = ""
        self.session = requests.Session(**proxy_settings.build_session_kwargs(
            impersonate=self.fp["impersonate"],
            verify=True,
        ))
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": self.fp["sec-ch-ua"],
            "Sec-Ch-Ua-Arch": '"x86"',
            "Sec-Ch-Ua-Bitness": '"64"',
            "Sec-Ch-Ua-Full-Version": '"143.0.3650.96"',
            "Sec-Ch-Ua-Full-Version-List": '"Microsoft Edge";v="143.0.3650.96", "Chromium";v="143.0.7499.147", "Not A(Brand";v="24.0.0.0"',
            "Sec-Ch-Ua-Mobile": self.fp["sec-ch-ua-mobile"],
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": self.fp["sec-ch-ua-platform"],
            "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "OAI-Device-Id": self.device_id,
            "OAI-Session-Id": self.session_id,
            "OAI-Language": "zh-CN",
            "OAI-Client-Version": self.client_version,
            "OAI-Client-Build-Number": self.client_build_number,
        })
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def _build_fp(self) -> Dict[str, str]:
        account = account_service.get_account(self.access_token) if self.access_token else {}
        account = account if isinstance(account, dict) else {}
        raw_fp = account.get("fp")
        fp = {str(k).lower(): str(v) for k, v in raw_fp.items()} if isinstance(raw_fp, dict) else {}
        for key in (
                "user-agent",
                "impersonate",
                "oai-device-id",
                "oai-session-id",
                "sec-ch-ua",
                "sec-ch-ua-mobile",
                "sec-ch-ua-platform",
        ):
            value = str(account.get(key) or "").strip()
            if value:
                fp[key] = value
        fp.setdefault(
            "user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
        )
        fp.setdefault("impersonate", "edge101")
        fp.setdefault("oai-device-id", new_uuid())
        fp.setdefault("oai-session-id", new_uuid())
        fp.setdefault("sec-ch-ua", '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"')
        fp.setdefault("sec-ch-ua-mobile", "?0")
        fp.setdefault("sec-ch-ua-platform", '"Windows"')
        return fp

    def _headers(self, path: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造请求头，并补上 web 端要求的 target path/route。"""
        headers = dict(self.session.headers)
        headers["X-OpenAI-Target-Path"] = path
        headers["X-OpenAI-Target-Route"] = path
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _decode_jwt_payload(token: str) -> Dict[str, Any]:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
            data = json.loads(decoded)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @classmethod
    def _chatgpt_account_id(cls, token: str) -> str:
        auth = cls._decode_jwt_payload(token).get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            return str(auth.get("chatgpt_account_id") or "").strip()
        return ""

    def _codex_headers(self, path: str, installation_id: str) -> Dict[str, str]:
        """构造 Codex Responses 请求头。"""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "originator": "Codex Desktop",
            "x-openai-internal-codex-residency": "us",
            "x-client-request-id": new_uuid(),
            "x-codex-installation-id": installation_id,
            "OpenAI-Beta": "responses_websockets=2026-02-06",
            "User-Agent": "Codex Desktop/26.519.81530 (win32; x64)",
            "sec-ch-ua": '"Chromium";v="146", "Not:A-Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "X-OpenAI-Target-Path": path,
            "X-OpenAI-Target-Route": path,
        }
        account_id = self._chatgpt_account_id(self.access_token)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return headers

    @staticmethod
    def _extract_quota_and_restore_at(limits_progress: list[Any]) -> tuple[int, str | None, bool]:
        for item in limits_progress:
            if isinstance(item, dict) and item.get("feature_name") == "image_gen":
                return int(item.get("remaining") or 0), str(item.get("reset_after") or "") or None, False
        return 0, None, True

    def _get_me(self) -> Dict[str, Any]:
        path = "/backend-api/me"
        response = self.session.get(self.base_url + path, headers=self._headers(path), timeout=20)
        if response.status_code != 200:
            if response.status_code == 401:
                raise InvalidAccessTokenError(f"{path} failed: HTTP {response.status_code}")
            raise RuntimeError(f"{path} failed: HTTP {response.status_code}")
        return response.json()

    def _get_conversation_init(self) -> Dict[str, Any]:
        path = "/backend-api/conversation/init"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json"}),
            json={
                "gizmo_id": None,
                "requested_default_model": None,
                "conversation_id": None,
                "timezone_offset_min": -480,
            },
            timeout=20,
        )
        if response.status_code != 200:
            if response.status_code == 401:
                raise InvalidAccessTokenError(f"{path} failed: HTTP {response.status_code}")
            raise RuntimeError(f"{path} failed: HTTP {response.status_code}")
        return response.json()

    def _get_default_account(self) -> Dict[str, Any]:
        route = "/backend-api/accounts/check/v4-2023-04-27"
        response = self.session.get(self.base_url + route + "?timezone_offset_min=-480", headers=self._headers(route),
                                    timeout=20)
        if response.status_code != 200:
            if response.status_code == 401:
                raise InvalidAccessTokenError(f"{route} failed: HTTP {response.status_code}")
            raise RuntimeError(f"/backend-api/accounts/check failed: HTTP {response.status_code}")
        payload = response.json()
        logger.debug({"event": "backend_user_info_account_payload", "account_payload": payload})
        return ((payload.get("accounts") or {}).get("default") or {}).get("account") or {}

    def get_user_info(self) -> Dict[str, Any]:
        """获取当前 token 的账号信息。"""
        if not self.access_token:
            raise RuntimeError("access_token is required")
        logger.debug({"event": "backend_user_info_start"})
        with ThreadPoolExecutor(max_workers=3) as executor:
            me_future = executor.submit(self._get_me)
            init_future = executor.submit(self._get_conversation_init)
            account_future = executor.submit(self._get_default_account)
            me_payload, init_payload, default_account = me_future.result(), init_future.result(), account_future.result()

        plan_type = str(default_account.get("plan_type") or "free")

        limits_progress = init_payload.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        quota, restore_at, image_quota_unknown = self._extract_quota_and_restore_at(limits_progress)
        result = {
            "email": me_payload.get("email"),
            "user_id": me_payload.get("id"),
            "type": plan_type,
            "quota": quota,
            "image_quota_unknown": image_quota_unknown,
            "limits_progress": limits_progress,
            "default_model_slug": init_payload.get("default_model_slug"),
            "restore_at": restore_at,
            "status": "正常" if image_quota_unknown and plan_type.lower() != "free" else ("限流" if quota == 0 else "正常"),
        }
        logger.debug({
            "event": "backend_user_info_result",
            "email": result.get("email"),
            "user_id": result.get("user_id"),
            "type": result.get("type"),
            "quota": result.get("quota"),
            "image_quota_unknown": result.get("image_quota_unknown"),
            "default_model_slug": result.get("default_model_slug"),
            "restore_at": result.get("restore_at"),
            "status": result.get("status"),
        })
        return result

    def _bootstrap_headers(self) -> Dict[str, str]:
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
        return headers

    def _build_requirements(self, data: Dict[str, Any], source_p: str = "") -> ChatRequirements:
        """把 sentinel 响应整理成后续对话需要的 token 集合。"""
        if (data.get("arkose") or {}).get("required"):
            raise RuntimeError("chat requirements requires arkose token, which is not implemented")

        proof_token = ""
        proof_info = data.get("proofofwork") or {}
        if proof_info.get("required"):
            proof_token = build_proof_token(
                proof_info.get("seed", ""),
                proof_info.get("difficulty", ""),
                self.user_agent,
                script_sources=self.pow_script_sources,
                data_build=self.pow_data_build,
            )

        turnstile_token = ""
        turnstile_info = data.get("turnstile") or {}
        if turnstile_info.get("required") and turnstile_info.get("dx"):
            turnstile_token = solve_turnstile_token(turnstile_info["dx"], source_p) or ""

        return ChatRequirements(
            token=data.get("token", ""),
            proof_token=proof_token,
            turnstile_token=turnstile_token,
            so_token=data.get("so_token", ""),
            raw_finalize=data,
        )

    def _conversation_headers(self, path: str, requirements: ChatRequirements) -> Dict[str, str]:
        """根据当前 requirements 构造对话 SSE 请求头。"""
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "OpenAI-Sentinel-Chat-Requirements-Token": requirements.token,
        }
        if requirements.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
        if requirements.turnstile_token:
            headers["OpenAI-Sentinel-Turnstile-Token"] = requirements.turnstile_token
        if requirements.so_token:
            headers["OpenAI-Sentinel-SO-Token"] = requirements.so_token
        return self._headers(path, headers)

    def _api_messages_to_conversation_messages(
            self,
            messages: list[Dict[str, Any]],
            system_hints: Optional[list[str]] = None,
    ) -> list[Dict[str, Any]]:
        """把标准 chat messages 转成 web conversation 所需的 messages。"""
        system_hints = system_hints or []
        conversation_messages = []
        last_user_index = max(
            (idx for idx, item in enumerate(messages) if str(item.get("role") or "user") == "user"),
            default=-1,
        )
        for idx, item in enumerate(messages):
            role = item.get("role", "user")
            content = item.get("content", "")
            metadata = None
            if system_hints and idx == last_user_index and role == "user":
                metadata = {
                    "developer_mode_connector_ids": [],
                    "selected_github_repos": [],
                    "selected_all_github_repos": False,
                    "system_hints": system_hints,
                    "serialization_metadata": {"custom_symbol_offsets": []},
                }
            if isinstance(content, str):
                message = {
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": [content]},
                }
                if metadata:
                    message["metadata"] = metadata
                conversation_messages.append(message)
                continue
            if not isinstance(content, list):
                raise RuntimeError("only string or list message content is supported")
            text_parts: list[str] = []
            image_inputs: list[tuple[bytes, str]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "")
                if part_type == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part_type == "image":
                    data = part.get("data")
                    mime = str(part.get("mime") or "image/png")
                    if isinstance(data, (bytes, bytearray)):
                        image_inputs.append((bytes(data), mime))
            if not image_inputs:
                message = {
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": ["".join(text_parts)]},
                }
                if metadata:
                    message["metadata"] = metadata
                conversation_messages.append(message)
                continue
            if not self.access_token:
                raise RuntimeError("authenticated upstream account required for image input")
            uploaded: list[Dict[str, Any]] = []
            for idx, (data, mime) in enumerate(image_inputs, start=1):
                ext_part = mime.split("/", 1)[1].split("+")[0] if "/" in mime else "png"
                extension = "jpg" if ext_part == "jpeg" else (ext_part or "png")
                b64 = base64.b64encode(data).decode("ascii")
                uploaded.append(self._upload_image(f"data:{mime};base64,{b64}", f"image_{idx}.{extension}"))
            parts: list[Any] = []
            for ref in uploaded:
                parts.append({
                    "content_type": "image_asset_pointer",
                    "asset_pointer": f"file-service://{ref['file_id']}",
                    "width": ref["width"],
                    "height": ref["height"],
                    "size_bytes": ref["file_size"],
                })
            text = "".join(text_parts)
            if text:
                parts.append(text)
            message = {
                "id": new_uuid(),
                "author": {"role": role},
                "content": {"content_type": "multimodal_text", "parts": parts},
                "metadata": {
                    "attachments": [{
                        "id": ref["file_id"],
                        "mimeType": ref["mime_type"],
                        "name": ref["file_name"],
                        "size": ref["file_size"],
                        "width": ref["width"],
                        "height": ref["height"],
                    } for ref in uploaded],
                },
            }
            if metadata:
                message["metadata"].update(metadata)
            conversation_messages.append(message)
        return conversation_messages

    def _conversation_payload(self, messages: list[Dict[str, Any]], model: str, timezone: str,
                              history_and_training_disabled: bool = True,
                              system_hints: Optional[list[str]] = None,
                              conversation_id: str = "",
                              parent_message_id: str = "") -> Dict[str, Any]:
        """把标准 messages 构造成 web 对话请求体。

        history_and_training_disabled=True 走临时聊天链路（默认，给 /v1/* 兼容接口用）；
        =False 才会拿到带 conversation_id 的常规会话，也是触发 image_gen 工具的前提。"""
        system_hints = system_hints or []
        payload = {
            "action": "next",
            "messages": self._api_messages_to_conversation_messages(messages, system_hints),
            "model": model,
            "parent_message_id": parent_message_id or new_uuid(),
            "conversation_mode": {"kind": "primary_assistant"},
            "conversation_origin": None,
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "force_use_sse": True,
            "history_and_training_disabled": history_and_training_disabled,
            "reset_rate_limits": False,
            "suggestions": [],
            "supported_encodings": ["v1"] if system_hints else [],
            "system_hints": system_hints,
            "timezone": timezone,
            "timezone_offset_min": -480,
            "variant_purpose": "comparison_implicit",
            "websocket_request_id": new_uuid(),
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 120,
                "page_height": 900,
                "page_width": 1400,
                "pixel_ratio": 2,
                "screen_height": 1440,
                "screen_width": 2560,
            },
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return payload

    def _conversation_parent_message_id(self, conversation_id: str) -> str:
        if not conversation_id:
            return ""
        conversation = self._get_conversation(conversation_id)
        current_node = str(conversation.get("current_node") or "").strip()
        if current_node:
            return current_node
        mapping = conversation.get("mapping") if isinstance(conversation.get("mapping"), dict) else {}
        latest_id = ""
        latest_time = -1.0
        for node_id, node in mapping.items():
            message = (node or {}).get("message") if isinstance(node, dict) else None
            if not isinstance(message, dict):
                continue
            create_time = float(message.get("create_time") or 0.0)
            if create_time >= latest_time:
                latest_time = create_time
                latest_id = str(node_id or message.get("id") or "")
        return latest_id

    def _image_model_slug(self, model: str) -> str:
        """把标准图片模型名映射到底层 model slug。"""
        _, base_model = split_image_model(model)
        if not base_model:
            return "auto"
        if base_model == "gpt-image-2":
            return "gpt-5-3"
        if base_model == CODEX_IMAGE_MODEL:
            return base_model
        return "auto"

    def _image_headers(self, path: str, requirements: ChatRequirements, conduit_token: str = "", accept: str = "*/*") -> \
            Dict[str, str]:
        """构造图片链路请求头。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "OpenAI-Sentinel-Chat-Requirements-Token": requirements.token,
        }
        if requirements.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
        if conduit_token:
            headers["X-Conduit-Token"] = conduit_token
        if accept == "text/event-stream":
            headers["X-Oai-Turn-Trace-Id"] = new_uuid()
        return self._headers(path, headers)

    def _prepare_image_conversation(self, prompt: str, requirements: ChatRequirements, model: str) -> str:
        """为图片生成准备 conduit token。"""
        path = "/backend-api/f/conversation/prepare"
        model_slug = self._image_model_slug(model)
        logger.info({
            "event": "image_web_prepare_request",
            "path": path,
            "model": model,
            "model_slug": model_slug,
            "system_hints": ["picture_v2"],
            "prompt_length": len(prompt or ""),
        })
        payload = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": new_uuid(),
            "model": model_slug,
            "client_prepare_state": "success",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["picture_v2"],
            "partial_query": {
                "id": new_uuid(),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements),
            json=payload,
            timeout=60,
        )
        ensure_ok(response, path)
        conduit_token = str(response.json().get("conduit_token") or "")
        logger.info({
            "event": "image_web_prepare_response",
            "path": path,
            "model": model,
            "model_slug": model_slug,
            "has_conduit_token": bool(conduit_token),
        })
        return conduit_token

    def _decode_image_base64(self, image: str) -> bytes:
        """把 base64 图片字符串或本地路径解码成二进制。"""
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            file_path = Path(os.path.expanduser(image))
            if file_path.exists() and file_path.is_file():
                return file_path.read_bytes()
        payload = image.split(",", 1)[1] if image.startswith("data:") and "," in image else image
        return base64.b64decode(payload)

    def _upload_image(self, image: str, file_name: str = "image.png") -> Dict[str, Any]:
        """上传一张 base64 图片，返回底层文件元数据。"""
        data = self._decode_image_base64(image)
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            candidate_path = Path(os.path.expanduser(image))
            if candidate_path.exists() and candidate_path.is_file():
                file_name = candidate_path.name
        image = Image.open(BytesIO(data))
        width, height = image.size
        mime_type = Image.MIME.get(image.format, "image/png")
        path = "/backend-api/files"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            json={"file_name": file_name, "file_size": len(data), "use_case": "multimodal", "width": width,
                  "height": height},
            timeout=60,
        )
        ensure_ok(response, path)
        upload_meta = response.json()
        time.sleep(0.5)
        response = self.session.put(
            upload_meta["upload_url"],
            headers={
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.8",
            },
            data=data,
            timeout=120,
        )
        ensure_ok(response, "image_upload")
        path = f"/backend-api/files/{upload_meta['file_id']}/uploaded"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            data="{}",
            timeout=60,
        )
        ensure_ok(response, path)
        return {
            "file_id": upload_meta["file_id"],
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def _start_image_generation(
            self,
            prompt: str,
            requirements: ChatRequirements,
            conduit_token: str,
            model: str,
            references: Optional[list[Dict[str, Any]]] = None,
            image_size: str | None = None,
            image_resolution: str | None = None,
    ) -> requests.Response:
        """启动图片生成或编辑的 SSE 请求。"""
        references = references or []
        model_slug = self._image_model_slug(model)
        parts = [{
            "content_type": "image_asset_pointer",
            "asset_pointer": f"file-service://{item['file_id']}",
            "width": item["width"],
            "height": item["height"],
            "size_bytes": item["file_size"],
        } for item in references]
        parts.append(prompt)
        content = {"content_type": "multimodal_text", "parts": parts} if references else {"content_type": "text",
                                                                                          "parts": [prompt]}
        metadata = {
            "developer_mode_connector_ids": [],
            "selected_github_repos": [],
            "selected_all_github_repos": False,
            "system_hints": ["picture_v2"],
            "serialization_metadata": {"custom_symbol_offsets": []},
        }
        if references:
            metadata["attachments"] = [{
                "id": item["file_id"],
                "mimeType": item["mime_type"],
                "name": item["file_name"],
                "size": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            } for item in references]
        payload = {
            "action": "next",
            "messages": [{
                "id": new_uuid(),
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": content,
                "metadata": metadata,
            }],
            "parent_message_id": new_uuid(),
            "model": model_slug,
            "client_prepare_state": "sent",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": ["picture_v2"],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 1200,
                "page_height": 1072,
                "page_width": 1724,
                "pixel_ratio": 1.2,
                "screen_height": 1440,
                "screen_width": 2560,
                "app_name": "chatgpt.com",
            },
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        }
        path = "/backend-api/f/conversation"
        logger.info({
            "event": "image_web_generation_request",
            "path": path,
            "model": model,
            "model_slug": model_slug,
            "requested_size": image_size or "",
            "requested_resolution": image_resolution or "",
            "system_hints": payload.get("system_hints"),
            "metadata_system_hints": metadata.get("system_hints"),
            "reference_count": len(references),
            "content_type": content.get("content_type"),
            "prompt_length": len(prompt or ""),
            "has_conduit_token": bool(conduit_token),
        })
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json=payload,
            timeout=300,
            stream=True,
        )
        ensure_ok(response, path)
        return response

    def _get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """获取完整 conversation 详情。"""
        path = f"/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        return response.json()

    def _extract_image_tool_records(self, data: Dict[str, Any]) -> list[Dict[str, Any]]:
        """从 conversation 明细里提取图片工具输出记录。"""
        mapping = data.get("mapping") or {}
        file_pat = re.compile(r"file-service://([A-Za-z0-9_-]+)")
        sed_pat = re.compile(r"sediment://([A-Za-z0-9_-]+)")
        records = []
        for message_id, node in mapping.items():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            metadata = message.get("metadata") or {}
            content = message.get("content") or {}
            if author.get("role") != "tool":
                continue
            if metadata.get("async_task_type") != "image_gen":
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
                        sediment_ids.append(hit)
            parts_summary = []
            for part in content.get("parts") or []:
                if isinstance(part, dict):
                    parts_summary.append({
                        "content_type": part.get("content_type") or "",
                        "asset_pointer": part.get("asset_pointer") or "",
                        "width": part.get("width"),
                        "height": part.get("height"),
                        "size_bytes": part.get("size_bytes") or part.get("size"),
                    })
                    continue
                parts_summary.append({"type": type(part).__name__, "length": len(str(part or ""))})
            record = {
                "message_id": message_id,
                "create_time": message.get("create_time") or 0,
                "file_ids": file_ids,
                "sediment_ids": sediment_ids,
                "metadata": {
                    key: metadata.get(key)
                    for key in (
                        "async_task_type",
                        "status",
                        "model_slug",
                        "image_generation_model",
                        "image_size",
                        "size",
                        "width",
                        "height",
                    )
                    if key in metadata
                },
                "parts": parts_summary,
            }
            logger.debug({
                "event": "image_tool_record",
                "message_id": message_id,
                "record": record,
            })
            records.append(record)
        return sorted(records, key=lambda item: item["create_time"])

    def _poll_image_results(self, conversation_id: str, timeout_secs: float = 120.0) -> tuple[list[str], list[str]]:
        """轮询 conversation，直到拿到图片文件 id 或超时。"""
        start = time.time()
        attempt = 0
        logger.info({"event": "image_poll_start", "conversation_id": conversation_id, "timeout_secs": timeout_secs})
        while time.time() - start < timeout_secs:
            attempt += 1
            conversation = self._get_conversation(conversation_id)
            file_ids, sediment_ids = [], []
            for record in self._extract_image_tool_records(conversation):
                for file_id in record["file_ids"]:
                    if file_id not in file_ids:
                        file_ids.append(file_id)
                for sediment_id in record["sediment_ids"]:
                    if sediment_id not in sediment_ids:
                        sediment_ids.append(sediment_id)
            logger.debug({"event": "image_poll_check", "conversation_id": conversation_id, "attempt": attempt,
                          "file_ids": file_ids, "sediment_ids": sediment_ids})
            if file_ids:
                logger.info({"event": "image_poll_hit", "conversation_id": conversation_id, "file_ids": file_ids,
                             "sediment_ids": sediment_ids})
                return file_ids, sediment_ids
            if sediment_ids:
                logger.info({"event": "image_poll_hit", "conversation_id": conversation_id, "file_ids": [],
                             "sediment_ids": sediment_ids})
                return [], sediment_ids
            logger.debug({"event": "image_poll_wait", "conversation_id": conversation_id,
                          "elapsed_secs": round(time.time() - start, 1)})
            time.sleep(4)
        logger.info({"event": "image_poll_timeout", "conversation_id": conversation_id, "timeout_secs": timeout_secs})
        return [], []

    def _get_file_download_url(self, file_id: str) -> str:
        """获取文件下载地址。"""
        path = f"/backend-api/files/{file_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _get_attachment_download_url(self, conversation_id: str, attachment_id: str) -> str:
        """通过 conversation 附件接口获取下载地址。"""
        path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _resolve_image_urls(self, conversation_id: str, file_ids: list[str], sediment_ids: list[str]) -> list[str]:
        """把图片结果 id 解析成可下载 URL。"""
        urls = []
        skip_patterns = {"file_upload"}
        for file_id in file_ids:
            if file_id in skip_patterns:
                logger.debug({
                    "event": "image_file_id_skipped",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
                continue
            try:
                url = self._get_file_download_url(file_id)
            except Exception as exc:
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                    "error": repr(exc),
                })
                continue
            if url:
                urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
        if urls or not conversation_id:
            logger.debug({
                "event": "image_urls_resolved",
                "conversation_id": conversation_id,
                "file_ids": file_ids,
                "sediment_ids": sediment_ids,
                "urls": urls,
            })
            return urls
        for sediment_id in sediment_ids:
            try:
                url = self._get_attachment_download_url(conversation_id, sediment_id)
            except Exception as exc:
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                    "error": repr(exc),
                })
                continue
            if url:
                urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                })
        logger.debug({
            "event": "image_urls_resolved",
            "conversation_id": conversation_id,
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "urls": urls,
        })
        return urls

    def resolve_conversation_image_urls(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
            poll: bool = True,
    ) -> list[str]:
        file_ids = [item for item in file_ids if item != "file_upload"]
        sediment_ids = list(sediment_ids)
        if poll and conversation_id and not file_ids and not sediment_ids:
            logger.info({"event": "image_resolve_poll_needed", "conversation_id": conversation_id})
            polled_file_ids, polled_sediment_ids = self._poll_image_results(conversation_id,
                                                                            config.image_poll_timeout_secs)
            file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
            sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
        return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)

    def download_image_bytes(self, urls: list[str]) -> list[bytes]:
        images = []
        for url in urls:
            response = self.session.get(url, timeout=120)
            ensure_ok(response, "image_download")
            images.append(response.content)
        return images

    def generate_codex_image(
            self,
            prompt: str,
            image_size: str,
            model: str = "gpt-image-2",
            images: Optional[list[str]] = None,
            quality: str | None = None,
            output_format: str | None = None,
            background: str | None = None,
    ) -> list[Dict[str, Any]]:
        """通过 Codex Responses image_generation 工具生成图片。"""
        if not self.access_token:
            raise RuntimeError("access_token is required for codex image generation")
        images = images or []
        path = "/backend-api/codex/responses"
        installation_id = new_uuid()
        content: list[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            content.append({
                "type": "input_image",
                "image_url": "data:image/png;base64," + image,
                "detail": "auto",
            })
        tool: Dict[str, Any] = {
            "type": "image_generation",
            "model": model or "gpt-image-2",
            "size": image_size,
        }
        if quality:
            tool["quality"] = quality
        if output_format:
            tool["output_format"] = output_format
        if background:
            tool["background"] = background
        body = {
            "model": CODEX_IMAGE_RESPONSES_MODEL,
            "input": [{"role": "user", "content": content}],
            "instructions": CODEX_IMAGE_INSTRUCTIONS,
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
            "client_metadata": {"x-codex-installation-id": installation_id},
        }
        logger.info({
            "event": "codex_image_generation_request",
            "path": path,
            "responses_model": CODEX_IMAGE_RESPONSES_MODEL,
            "image_model": tool["model"],
            "image_size": image_size,
            "input_image_count": len(images),
            "prompt_length": len(prompt or ""),
        })
        response = self.session.post(
            self.base_url + path,
            headers=self._codex_headers(path, installation_id),
            json=body,
            timeout=300,
            stream=True,
        )
        ensure_ok(response, path)
        try:
            items = self._collect_codex_image_items(iter_sse_payloads(response))
        finally:
            response.close()
        logger.info({
            "event": "codex_image_generation_response",
            "path": path,
            "image_size": image_size,
            "image_count": len(items),
        })
        return items

    def _collect_codex_image_items(self, payloads: Iterator[str]) -> list[Dict[str, Any]]:
        items: list[Dict[str, Any]] = []
        completed_output: list[Dict[str, Any]] = []
        for payload_text in payloads:
            if payload_text == "[DONE]":
                break
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("type") or "")
            if event_type in {"response.failed", "error"}:
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                message = str(error.get("message") or payload.get("message") or "Codex image generation failed")
                raise RuntimeError(message)
            if event_type == "response.output_item.done":
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                if item.get("type") == "image_generation_call" and item.get("result"):
                    items.append(dict(item))
                continue
            if event_type == "response.completed":
                response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                output = response.get("output") if isinstance(response.get("output"), list) else []
                completed_output = [dict(item) for item in output if isinstance(item, dict)]
        if items:
            return items
        return [
            item
            for item in completed_output
            if item.get("type") == "image_generation_call" and item.get("result")
        ]

    def search(
            self,
            prompt: str,
            model: str = SEARCH_MODEL,
            timeout_secs: float = SEARCH_TIMEOUT_SECS,
            poll_interval_secs: float = SEARCH_POLL_INTERVAL_SECS,
    ) -> Dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("access_token is required for search")
        conduit_token = self._prepare_search_conversation(prompt, model)
        self._bootstrap()
        conversation_id = self._run_search_conversation(prompt, conduit_token, model)
        return self._wait_search_result(conversation_id, timeout_secs, poll_interval_secs)

    def stream_search_payloads(
            self,
            prompt: str,
            model: str = SEARCH_MODEL,
    ) -> Iterator[str]:
        if not self.access_token:
            raise RuntimeError("access_token is required for search")
        conduit_token = self._prepare_search_conversation(prompt, model)
        self._bootstrap()
        response = self._start_search_conversation(prompt, conduit_token, model)
        ensure_ok(response, "/backend-api/f/conversation")
        try:
            yield from iter_sse_payloads(response)
        finally:
            response.close()

    def _prepare_search_conversation(self, prompt: str, model: str) -> str:
        path = "/backend-api/f/conversation/prepare"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {
                "Accept": "*/*",
                "Content-Type": "application/json",
                "X-Conduit-Token": "no-token",
            }),
            json={
                "action": "next",
                "fork_from_shared_post": False,
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "system_hints": ["search"],
                "partial_query": {
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                },
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {"app_name": "chatgpt.com"},
            },
            timeout=60,
        )
        ensure_ok(response, path)
        token = str(response.json().get("conduit_token") or "")
        if not token:
            raise RuntimeError("missing conduit_token")
        return token

    def _start_search_conversation(self, prompt: str, conduit_token: str, model: str):
        requirements = self._get_chat_requirements()
        path = "/backend-api/f/conversation"
        return self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [{
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {"content_type": "text", "parts": [prompt]},
                    "metadata": {
                        "developer_mode_connector_ids": [],
                        "selected_github_repos": [],
                        "selected_all_github_repos": False,
                        "system_hints": ["search"],
                        "serialization_metadata": {"custom_symbol_offsets": []},
                    },
                }],
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "force_use_search": True,
                "client_reported_search_source": "conversation_composer_web_icon",
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 36,
                    "page_height": 925,
                    "page_width": 886,
                    "pixel_ratio": 2,
                    "screen_height": 1440,
                    "screen_width": 2560,
                    "app_name": "chatgpt.com",
                },
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
            },
            timeout=300,
            stream=True,
        )

    def _run_search_conversation(self, prompt: str, conduit_token: str, model: str) -> str:
        path = "/backend-api/f/conversation"
        response = self._start_search_conversation(prompt, conduit_token, model)
        ensure_ok(response, path)
        conversation_id = ""
        try:
            for payload in iter_sse_payloads(response):
                conversation_id = conversation_id or self._find_search_value(payload, "conversation_id")
                if payload == "[DONE]":
                    break
        finally:
            response.close()
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _wait_search_result(
            self,
            conversation_id: str,
            timeout_secs: float,
            poll_interval_secs: float,
    ) -> Dict[str, Any]:
        deadline = time.time() + timeout_secs
        last_result: Dict[str, Any] | None = None
        last_answer = ""
        stable_hits = 0
        while time.time() < deadline:
            try:
                last_result = self._extract_search_result(
                    conversation_id,
                    self._get_search_conversation(conversation_id),
                )
            except RuntimeError as exc:
                text = str(exc)
                if not any(f"status={code}" in text for code in (404, 409, 423, 429, 500, 502, 503, 504)):
                    raise
            if last_result and last_result.get("answer"):
                if last_result.get("status") in SEARCH_DONE_STATUS:
                    return last_result
                answer = str(last_result.get("answer") or "")
                stable_hits = stable_hits + 1 if answer == last_answer else 0
                last_answer = answer
                if stable_hits >= 2:
                    return last_result
            time.sleep(poll_interval_secs)
        if last_result:
            return last_result
        raise RuntimeError(f"timed out waiting for search result: {conversation_id}")

    def _get_search_conversation(self, conversation_id: str) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=headers, timeout=60)
        ensure_ok(response, path)
        return response.json()

    def _extract_search_result(self, conversation_id: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        messages = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            if ((message.get("author") or {}).get("role") or "") == "assistant":
                messages.append(message)
        message = max(messages, key=lambda item: float(item.get("create_time") or 0.0)) if messages else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
        answer = self._search_message_text(message)
        sources = self._extract_search_sources(message)
        for url in SEARCH_URL_RE.findall(answer):
            url = self._clean_search_url(url)
            if url and all(item["url"] != url for item in sources):
                sources.append({"title": "", "url": url, "snippet": "", "source_type": ""})
        return {
            "conversation_id": conversation_id,
            "status": str(
                finish_details.get("type")
                or metadata.get("status")
                or self._find_search_value(message, "status")
                or ""
            ).strip(),
            "answer": answer,
            "sources": sources,
            "assistant_message_id": str(message.get("id") or ""),
            "create_time": float(message.get("create_time") or 0.0),
        }

    def _extract_search_sources(self, payload: Any) -> list[Dict[str, str]]:
        sources: list[Dict[str, str]] = []
        for obj in self._walk_search_dicts(payload):
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            url = self._clean_search_url(obj.get("url") or obj.get("link") or obj.get("source_url") or metadata.get("url"))
            if url and all(item["url"] != url for item in sources):
                sources.append({
                    "title": str(obj.get("title") or obj.get("name") or obj.get("source") or "").strip(),
                    "url": url,
                    "snippet": str(obj.get("snippet") or obj.get("text") or obj.get("description") or "").strip(),
                    "source_type": str(obj.get("type") or obj.get("source_type") or "").strip(),
                })
        return sources

    def _search_message_text(self, message: Any) -> str:
        content = message.get("content") if isinstance(message, dict) else {}
        parts = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "summary", "content") if part.get(key))
        elif isinstance(content, str):
            parts.append(content)
        return "\n".join(part.strip() for part in parts if str(part).strip()).strip()

    def _find_search_value(self, payload: Any, key: str) -> str:
        if isinstance(payload, str):
            match = SEARCH_CONVERSATION_ID_RE.search(payload) if key == "conversation_id" else None
            if match:
                return match.group(1)
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            return next((found for item in payload.values() if (found := self._find_search_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_search_value(item, key))), "")
        return ""

    def _walk_search_dicts(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            return [payload, *(item for value in payload.values() for item in self._walk_search_dicts(value))]
        if isinstance(payload, list):
            return [item for value in payload for item in self._walk_search_dicts(value)]
        return []

    def _clean_search_url(self, value: Any) -> str:
        return str(value or "").strip().rstrip(".,;，。；")

    def stream_conversation(
            self,
            messages: Optional[list[Dict[str, Any]]] = None,
            model: str = "auto",
            prompt: str = "",
            images: Optional[list[str]] = None,
            system_hints: Optional[list[str]] = None,
            history_and_training_disabled: bool = True,
            conversation_id: str = "",
            image_size: str | None = None,
            image_resolution: str | None = None,
    ) -> Iterator[str]:
        system_hints = system_hints or []
        if "picture_v2" in system_hints:
            yield from self._stream_picture_conversation(
                prompt,
                model,
                images or [],
                image_size=image_size,
                image_resolution=image_resolution,
            )
            return

        normalized = messages or [{"role": "user", "content": prompt}]
        self._bootstrap()
        requirements = self._get_chat_requirements()
        path, timezone = self._chat_target()
        upstream_cid = str(conversation_id or "").strip()
        parent_message_id = self._conversation_parent_message_id(upstream_cid) if upstream_cid else ""
        payload = self._conversation_payload(
            normalized,
            model,
            timezone,
            history_and_training_disabled,
            system_hints,
            upstream_cid,
            parent_message_id,
        )
        response = self.session.post(
            self.base_url + path,
            headers=self._conversation_headers(path, requirements),
            json=payload,
            timeout=300,
            stream=True,
        )
        ensure_ok(response, path)
        try:
            yield from iter_sse_payloads(response)
        finally:
            response.close()

    def delete_conversation(self, conversation_id: str) -> None:
        """把指定 conversation 标记为不可见，对齐官网"删除会话"行为。
        仅在 access_token 模式下可用；任何失败都吞掉，调用方按"尽力而为"语义处理。"""
        cid = str(conversation_id or "").strip()
        if not cid or not self.access_token:
            return
        path = f"/backend-api/conversation/{cid}"
        try:
            response = self.session.patch(
                self.base_url + path,
                headers=self._headers(path, {"Content-Type": "application/json"}),
                json={"is_visible": False},
                timeout=30,
            )
            response.close()
        except Exception:
            pass

    def _stream_picture_conversation(
            self,
            prompt: str,
            model: str,
            images: list[str],
            image_size: str | None = None,
            image_resolution: str | None = None,
    ) -> Iterator[str]:
        if not self.access_token:
            raise RuntimeError("access_token is required for image endpoints")
        logger.info({
            "event": "image_web_stream_start",
            "model": model,
            "model_slug": self._image_model_slug(model),
            "requested_size": image_size or "",
            "requested_resolution": image_resolution or "",
            "reference_count": len(images),
            "route": "/backend-api/f/conversation",
        })
        references = [self._upload_image(image, f"image_{idx}.png") for idx, image in enumerate(images, start=1)]
        self._bootstrap()
        requirements = self._get_chat_requirements()
        conduit_token = self._prepare_image_conversation(prompt, requirements, model)
        response = self._start_image_generation(
            prompt,
            requirements,
            conduit_token,
            model,
            references,
            image_size=image_size,
            image_resolution=image_resolution,
        )
        try:
            yield from iter_sse_payloads(response)
        finally:
            response.close()

    def _apply_cf_clearance(self, force: bool = False) -> bool:
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
        response = self.session.get(
            self.base_url + "/",
            headers=self._bootstrap_headers(),
            timeout=30,
        )
        if response.status_code in (403, 503) and self._apply_cf_clearance(force=True):
            print(f"[cf] bootstrap {response.status_code}, 已刷新 Cloudflare clearance, 重试一次", flush=True)
            response = self.session.get(
                self.base_url + "/",
                headers=self._bootstrap_headers(),
                timeout=30,
            )
        ensure_ok(response, "bootstrap")
        self.pow_script_sources, self.pow_data_build = parse_pow_resources(response.text)
        if not self.pow_script_sources:
            self.pow_script_sources = [DEFAULT_POW_SCRIPT]

    def _get_chat_requirements(self) -> ChatRequirements:
        """获取当前模式对话所需的 sentinel token。"""
        path = "/backend-api/sentinel/chat-requirements" if self.access_token else "/backend-anon/sentinel/chat-requirements"
        context = "auth_chat_requirements" if self.access_token else "noauth_chat_requirements"
        body = {"p": build_legacy_requirements_token(self.user_agent, self.pow_script_sources, self.pow_data_build)}
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json"}),
            json=body,
            timeout=30,
        )
        ensure_ok(response, context)
        requirements = self._build_requirements(response.json(), "" if self.access_token else body["p"])
        if not requirements.token:
            message = "missing auth chat requirements token" if self.access_token else "missing chat requirements token"
            raise RuntimeError(f"{message}: {requirements.raw_finalize}")
        return requirements

    def _chat_target(self) -> tuple[str, str]:
        if self.access_token:
            return "/backend-api/conversation", "Asia/Shanghai"
        return "/backend-anon/conversation", "America/Los_Angeles"

    def list_models(self) -> Dict[str, Any]:
        """返回当前模式下可用模型，格式对齐 OpenAI `/v1/models`。"""
        self._bootstrap()
        path = "/backend-api/models?history_and_training_disabled=false" if self.access_token else (
            "/backend-anon/models?iim=false&is_gizmo=false"
        )
        route = "/backend-api/models" if self.access_token else "/backend-anon/models"
        context = "auth_models" if self.access_token else "anon_models"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(route),
            timeout=30,
        )
        ensure_ok(response, context)
        data = []
        seen = set()
        for item in response.json().get("models", []):
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug", "")).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            data.append({
                "id": slug,
                "object": "model",
                "created": int(item.get("created") or 0),
                "owned_by": str(item.get("owned_by") or "chatgpt"),
                "permission": [],
                "root": slug,
                "parent": None,
            })
        data.sort(key=lambda item: item["id"])
        return {"object": "list", "data": data}
