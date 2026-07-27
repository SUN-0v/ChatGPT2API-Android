from __future__ import annotations

import json
import threading
from typing import Any

from services.config import DATA_DIR

# 图片 → 生成时的 prompt 文本。跟 image_owners.json 同套路：
# 单独一份 JSON，运行时按 rel 路径快速反查。
#
# 为什么不和 image_owners 合并：
#   - owner 是个稳定 string，prompt 是用户写的多行任意文本，混在一起读写都尴尬
#   - prompt 缺失时不影响图片正常展示和归属，只是发布画廊 / 复用时拿不到原文
#
# 文件结构：{ "<rel>": "<prompt 原文>" }
PROMPTS_FILE = DATA_DIR / "image_prompts.json"

_lock = threading.RLock()


def _ensure_file() -> None:
    PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PROMPTS_FILE.exists():
        PROMPTS_FILE.write_text("{}", encoding="utf-8")


def load_prompts() -> dict[str, str]:
    _ensure_file()
    try:
        data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _save_locked(data: dict[str, str]) -> None:
    _ensure_file()
    tmp = PROMPTS_FILE.with_suffix(PROMPTS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PROMPTS_FILE)


def set_prompts(rels: list[str], prompt: str) -> None:
    text = (prompt or "").strip()
    if not text:
        return
    cleaned = [r.strip().lstrip("/") for r in rels if r and r.strip()]
    if not cleaned:
        return
    with _lock:
        data = load_prompts()
        changed = False
        for rel in cleaned:
            if data.get(rel) != text:
                data[rel] = text
                changed = True
        if changed:
            _save_locked(data)


def remove_prompts(rels: list[str]) -> None:
    cleaned = [r.strip().lstrip("/") for r in rels if r and r.strip()]
    if not cleaned:
        return
    with _lock:
        data = load_prompts()
        changed = False
        for rel in cleaned:
            if data.pop(rel, None) is not None:
                changed = True
        if changed:
            _save_locked(data)


def get_prompt(image_rel: str) -> str:
    rel = (image_rel or "").strip().lstrip("/")
    if not rel:
        return ""
    return load_prompts().get(rel, "")


def _extract_rels(data: list[Any]) -> list[str]:
    """跟 image_owners_service._extract_rels 同款逻辑：从 data 列表里抠 rel。"""
    rels: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        marker = "/images/"
        idx = url.find(marker)
        if idx < 0:
            continue
        rel = url[idx + len(marker):].split("?", 1)[0].split("#", 1)[0].strip().lstrip("/")
        if rel:
            rels.append(rel)
    return rels


def record_prompt_for_result(prompt: str | None, data: Any, *, is_edit: bool = False) -> None:
    """生成/编辑成功后调用：把这次的 prompt 存进所有产出图的归属表。
    - prompt 为空：跳过（后端有审查 / 拼装的场景不应覆盖原文，由调用方决定要不要传）
    - data 不是 list：跳过
    - is_edit=True：除了正常落 prompt 外，还把这批 rel 标记为「图生图产出」。
      画廊发布时检测到该标记会强制把 prompt 落成空串，因为图生图的 prompt 是相对
      参考图的修改指令，离开参考图对其它用户没有复用价值。
    """
    text = (prompt or "").strip()
    if not text or not isinstance(data, list) or not data:
        # prompt 为空也得标 is_edit：图生图允许空 prompt（仅靠参考图修改），
        # 这种情况照样得让画廊知道是图生图。
        if is_edit and isinstance(data, list) and data:
            try:
                from services.image_edits_service import mark_edits

                rels = _extract_rels(data)
                if rels:
                    mark_edits(rels)
            except Exception:
                pass
        return
    rels = _extract_rels(data)
    if not rels:
        return
    try:
        set_prompts(rels, text)
    except Exception:
        # 写失败不影响接口返回
        pass
    if is_edit:
        try:
            from services.image_edits_service import mark_edits

            mark_edits(rels)
        except Exception:
            pass
