from __future__ import annotations

from curl_cffi import requests
from fastapi import HTTPException

from services.config import config
from services.proxy_service import proxy_settings

DEFAULT_REVIEW_PROMPT = "判断用户请求是否允许。只回答 ALLOW 或 REJECT。"


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_text(value.get(key)) for key in ("text", "input_text", "content", "input", "instructions", "system", "prompt"))
    return ""


def request_text(*values: object) -> str:
    return "\n".join(part for value in values if (part := _text(value).strip()))


def check_request(text: str) -> None:
    text = str(text or "")
    if not text:
        return
    for word in config.sensitive_words:
        if word in text:
            raise HTTPException(status_code=400, detail={"error": "检测到敏感词，拒绝本次任务"})
    review = config.ai_review
    if not review.get("enabled"):
        return
    base_url = str(review.get("base_url") or "").strip().rstrip("/")
    api_key = str(review.get("api_key") or "").strip()
    model = str(review.get("model") or "").strip()
    if not base_url or not api_key or not model:
        raise HTTPException(status_code=400, detail={"error": "ai review config is incomplete"})
    prompt = str(review.get("prompt") or DEFAULT_REVIEW_PROMPT).strip()
    content = f"{prompt}\n\n用户请求:\n{text}\n\n只回答 ALLOW 或 REJECT。"
    try:
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0},
            timeout=60,
            **proxy_settings.build_session_kwargs(),
        )
        result = str(response.json()["choices"][0]["message"]["content"]).strip().lower()
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"ai review failed: {exc}"}) from exc
    if result.startswith(("allow", "pass", "true", "yes", "通过", "允许", "安全")):
        return
    raise HTTPException(status_code=400, detail={"error": "AI 审核未通过，拒绝本次任务"})
