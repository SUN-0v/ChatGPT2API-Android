from __future__ import annotations

import json
import threading

from services.config import DATA_DIR

# 标记一组 rel 是不是「图生图（image edits）」产出。
# 单独存 set：图生图的 prompt 是相对参考图的修改指令（"换个浅色版"、"加个帽子"），
# 离开参考图就是垃圾文本。我们不持久化参考图（占盘且复用率极低），所以发布到
# 画廊时画廊条目里的"prompt 文本"对其它用户毫无价值——publish 时查这个 set，
# 命中就把 prompt 强制落空，画廊详情页改为提示"提示词依赖参考图，无法独立复用"。
#
# 文件结构：{ "rels": ["2026/05/22/abc.png", ...] }
# 多进程下小概率写丢，但失败也只是 fallback 为"显示原 prompt 文本"，
# 不致命；下次 publish 仍能正常命中。
EDITS_FILE = DATA_DIR / "image_edits.json"

_lock = threading.RLock()


def _ensure_file() -> None:
    EDITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not EDITS_FILE.exists():
        EDITS_FILE.write_text('{"rels": []}\n', encoding="utf-8")


def _load_set() -> set[str]:
    _ensure_file()
    try:
        data = json.loads(EDITS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    rels = data.get("rels") if isinstance(data, dict) else None
    if not isinstance(rels, list):
        return set()
    return {str(r) for r in rels if isinstance(r, str) and r}


def _save_locked(s: set[str]) -> None:
    _ensure_file()
    tmp = EDITS_FILE.with_suffix(EDITS_FILE.suffix + ".tmp")
    payload = {"rels": sorted(s)}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(EDITS_FILE)


def mark_edits(rels: list[str]) -> None:
    """把这批 rel 标记为图生图产出。"""
    cleaned = [r.strip().lstrip("/") for r in rels if r and r.strip()]
    cleaned = [r for r in cleaned if r]
    if not cleaned:
        return
    with _lock:
        s = _load_set()
        before = len(s)
        s.update(cleaned)
        if len(s) != before:
            _save_locked(s)


def is_edit(rel: str) -> bool:
    r = (rel or "").strip().lstrip("/")
    if not r:
        return False
    return r in _load_set()


def remove_edits(rels: list[str]) -> None:
    """跟图片删除/清理路径配套使用，避免文件越长越大。"""
    cleaned = {r.strip().lstrip("/") for r in rels if r and r.strip()}
    cleaned.discard("")
    if not cleaned:
        return
    with _lock:
        s = _load_set()
        before = len(s)
        s -= cleaned
        if len(s) != before:
            _save_locked(s)
