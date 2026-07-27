from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from services.config import DATA_DIR

# 图片 → 创建者用户密钥 ID 的归属表。
# 之所以独立放一份 JSON、不和 image_tags 合并：
#   - tags 是用户/管理员手动打的，owner 是任务成功时由后端写入；语义不同，混在一起后续维护更乱
#   - 没有 owner 也不影响图片本身正常展示，只是该图筛不出"按用户"
# 文件结构：{ "<rel>": "<owner_key_id>" }
OWNERS_FILE = DATA_DIR / "image_owners.json"

_lock = threading.RLock()


def _ensure_file() -> None:
    OWNERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not OWNERS_FILE.exists():
        OWNERS_FILE.write_text("{}", encoding="utf-8")


def load_owners() -> dict[str, str]:
    _ensure_file()
    try:
        data = json.loads(OWNERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # 防御：只保留 str → str 形态
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and v}


def _save_locked(data: dict[str, str]) -> None:
    _ensure_file()
    tmp = OWNERS_FILE.with_suffix(OWNERS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OWNERS_FILE)


def set_owner(image_rel: str, owner_id: str) -> None:
    rel = (image_rel or "").strip().lstrip("/")
    owner = (owner_id or "").strip()
    if not rel or not owner:
        return
    with _lock:
        data = load_owners()
        if data.get(rel) == owner:
            return
        data[rel] = owner
        _save_locked(data)


def set_owners(rels: list[str], owner_id: str) -> None:
    owner = (owner_id or "").strip()
    if not owner:
        return
    cleaned = [r.strip().lstrip("/") for r in rels if r and r.strip()]
    if not cleaned:
        return
    with _lock:
        data = load_owners()
        changed = False
        for rel in cleaned:
            if data.get(rel) != owner:
                data[rel] = owner
                changed = True
        if changed:
            _save_locked(data)


def remove_owner(image_rel: str) -> None:
    rel = (image_rel or "").strip().lstrip("/")
    if not rel:
        return
    with _lock:
        data = load_owners()
        if data.pop(rel, None) is not None:
            _save_locked(data)


def remove_owners(rels: list[str]) -> None:
    cleaned = [r.strip().lstrip("/") for r in rels if r and r.strip()]
    if not cleaned:
        return
    with _lock:
        data = load_owners()
        changed = False
        for rel in cleaned:
            if data.pop(rel, None) is not None:
                changed = True
        if changed:
            _save_locked(data)


def get_owner(image_rel: str) -> str:
    rel = (image_rel or "").strip().lstrip("/")
    if not rel:
        return ""
    return load_owners().get(rel, "")


def owner_counts() -> dict[str, int]:
    """统计每个 owner 当前拥有的图片数。
    上层在和 list_images 结果对齐前直接读这里更轻量；要严格匹配文件存在性
    交给上层。"""
    counts: dict[str, int] = {}
    for owner in load_owners().values():
        if not owner:
            continue
        counts[owner] = counts.get(owner, 0) + 1
    return counts


def _extract_rels(data: list[Any]) -> list[str]:
    """从生成结果 data 列表里把 image url 的 `/images/<rel>` 抠出来。
    上游可能给绝对 URL 也可能给相对路径，只要包含 `/images/` 都能命中。"""
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


def record_owner_for_result(identity: dict[str, Any] | None, data: Any) -> None:
    """生成/编辑成功后调用：根据 identity 把生成的图都挂上 owner。
    - 普通用户：owner = 该用户密钥 id
    - 管理员：owner = "admin"（旧 auth_key）或具体 admin 密钥 id，下拉里会聚合到"管理员"项
    - 拿不到 identity / id：什么都不做，那一批图就成孤儿（前端"未归属"桶）
    """
    if not isinstance(identity, dict) or not isinstance(data, list) or not data:
        return
    owner_id = str(identity.get("id") or "").strip()
    if not owner_id:
        return
    rels = _extract_rels(data)
    if not rels:
        return
    try:
        set_owners(rels, owner_id)
    except Exception:
        # 写归属表失败不影响上游接口返回；下次列表时该图就显示为"未归属"
        pass
