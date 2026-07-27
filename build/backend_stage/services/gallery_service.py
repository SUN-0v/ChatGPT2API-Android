from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from services.config import config
from services.content_filter import check_request
from services.image_edits_service import is_edit as _rel_is_edit

# 画廊条目存储 schema（每条都是 dict 落进 storage.gallery_items）
#
# {
#   "id": "<uuid hex>",
#   "image_rel": "2026/05/21/abc.png",
#   "publisher_id": "<auth key id 或 'admin'>",
#   "publisher_name": "<展示名>",
#   "prompt": "<可空。图生图条目强制为空，因为相对参考图的修改指令脱离参考图无复用价值>",
#   "model": "gpt-image-2",
#   "size": "1:1",
#   "width": 0,                # 可选，前端瀑布流计算用
#   "height": 0,
#   "is_edit": false,          # true = 图生图产出，前端据此把 prompt 区换成提示文案
#   "created_at": 1716277200,  # epoch seconds
#   "status": "visible" | "hidden",
# }
#
# 选用扁平 dict 而非 ORM 模型，跟 accounts/auth_keys 一致；列表筛选/排序在 service 层
# 内存里做。预期画廊条目数远小于历史图片总数，单文件 / 单表都能 hold 住。


_lock = threading.RLock()


def _now_ts() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """把存储里读出来的条目补齐字段、规范类型，避免上层每次都判 None。"""
    if not isinstance(item, dict):
        return {}
    return {
        "id": str(item.get("id") or "").strip(),
        "image_rel": str(item.get("image_rel") or "").strip().lstrip("/"),
        "publisher_id": str(item.get("publisher_id") or "").strip(),
        "publisher_name": str(item.get("publisher_name") or "").strip(),
        "prompt": str(item.get("prompt") or ""),
        "model": str(item.get("model") or "").strip(),
        "size": str(item.get("size") or "").strip(),
        "width": int(item.get("width") or 0) if isinstance(item.get("width"), (int, float)) else 0,
        "height": int(item.get("height") or 0) if isinstance(item.get("height"), (int, float)) else 0,
        "is_edit": bool(item.get("is_edit")),
        "created_at": int(item.get("created_at") or 0) if isinstance(item.get("created_at"), (int, float)) else 0,
        "status": str(item.get("status") or "visible").strip().lower() or "visible",
    }


def _load_all() -> list[dict[str, Any]]:
    raw = config.get_storage_backend().load_gallery_items() or []
    return [_normalize(item) for item in raw if isinstance(item, dict) and item.get("id")]


def _save_all(items: list[dict[str, Any]]) -> None:
    config.get_storage_backend().save_gallery_items(items)


def _public_view(
    item: dict[str, Any],
    image_base_url: str,
    *,
    viewer_id: str = "",
) -> dict[str, Any]:
    """对外返回时把 image_rel 拼成完整 URL，前端不用感知 /images 前缀；
    publisher_id 不暴露给非本人，避免被遍历密钥 id（仅返回展示名）。

    viewer_id：当前请求者的 identity.id；非空时用于对比 publisher_id 派生
    is_mine 布尔，让前端知道"这条是我发的"——据此显示"撤回发布"按钮，
    无需把 publisher_id 本身泄露出去。
    """
    rel = item.get("image_rel") or ""
    url = f"{image_base_url.rstrip('/')}/images/{rel}" if rel else ""
    pid = (item.get("publisher_id") or "").strip()
    vid = (viewer_id or "").strip()
    return {
        "id": item.get("id"),
        "url": url,
        "image_rel": rel,
        "prompt": item.get("prompt"),
        "model": item.get("model"),
        "size": item.get("size"),
        "width": item.get("width"),
        "height": item.get("height"),
        "is_edit": bool(item.get("is_edit")),
        "publisher_name": item.get("publisher_name"),
        "created_at": item.get("created_at"),
        "status": item.get("status"),
        "is_mine": bool(vid) and pid == vid,
    }


def publish(
    *,
    image_rel: str,
    publisher_id: str,
    publisher_name: str,
    prompt: str,
    model: str = "",
    size: str = "",
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    """新建一条画廊。同一 (publisher_id, image_rel) 已发过则直接返回旧记录，
    避免用户多次点击/重发产生重复行。

    敏感词同步过滤，AI review 不在这里做（画廊刷流量大，不能阻塞 publish）；
    AI review 由后台流程在 status=visible 时再异步审核（可选，非本期）。
    """
    rel = (image_rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("image_rel is required")
    pid = (publisher_id or "").strip()
    if not pid:
        raise ValueError("publisher_id is required")
    # prompt 允许为空 —— 图生图 / 历史无 prompt 数据 / 用户主动留空 都合法。
    # 客户端（web /works、移动端 History）UI 层用对话框可选补齐，最终是否填
    # 由用户决定，后端不再强制非空。
    text = (prompt or "").strip()

    # 图生图（image edits）：prompt 是相对参考图的修改指令，离开参考图对其它
    # 用户毫无复用价值（"换个浅色版" 看不到原图就是垃圾文本）。所以 publish 时
    # 检测到 rel 在 image_edits set 里就强制把 prompt 落空，前端展示成
    # "提示词依赖参考图，无法独立复用"提示卡。
    is_edit_flag = False
    try:
        is_edit_flag = _rel_is_edit(rel)
    except Exception:
        is_edit_flag = False
    if is_edit_flag:
        text = ""

    # 命中敏感词直接抛 HTTPException(400)，调用方让 router 自然冒泡即可。
    # 空 prompt 时跳过敏感词检查（无内容可查）。
    if text:
        check_request(text)

    with _lock:
        items = _load_all()
        for existing in items:
            if existing["publisher_id"] == pid and existing["image_rel"] == rel:
                # 同人同图重复发布：把内容字段刷新一下（用户可能改了 prompt），
                # 同时把 status 拉回 visible（万一之前被自己撤回过）。
                existing.update(
                    {
                        "prompt": text,
                        "model": (model or "").strip(),
                        "size": (size or "").strip(),
                        "width": int(width or 0),
                        "height": int(height or 0),
                        "is_edit": is_edit_flag,
                        "status": "visible",
                    }
                )
                _save_all(items)
                return existing

        new_item = {
            "id": _new_id(),
            "image_rel": rel,
            "publisher_id": pid,
            "publisher_name": (publisher_name or "").strip() or "匿名",
            "prompt": text,
            "model": (model or "").strip(),
            "size": (size or "").strip(),
            "width": int(width or 0),
            "height": int(height or 0),
            "is_edit": is_edit_flag,
            "created_at": _now_ts(),
            "status": "visible",
        }
        items.append(new_item)
        _save_all(items)
        return new_item


def unpublish(item_id: str, *, requester_id: str, is_admin: bool) -> bool:
    """用户撤回 / 管理员删除。本人可删自己的；admin 可删任意。
    返回 True = 真删了；False = 不存在 / 无权限。"""
    iid = (item_id or "").strip()
    if not iid:
        return False
    with _lock:
        items = _load_all()
        idx = next((i for i, it in enumerate(items) if it["id"] == iid), -1)
        if idx < 0:
            return False
        target = items[idx]
        if not is_admin and target["publisher_id"] != requester_id:
            return False
        items.pop(idx)
        _save_all(items)
        return True


def admin_set_status(item_id: str, status: str) -> bool:
    """admin 后台软下架 / 恢复。status: visible | hidden。
    返回 True = 改了；False = 不存在 / 状态没变。"""
    iid = (item_id or "").strip()
    next_status = (status or "").strip().lower()
    if not iid or next_status not in ("visible", "hidden"):
        return False
    with _lock:
        items = _load_all()
        idx = next((i for i, it in enumerate(items) if it["id"] == iid), -1)
        if idx < 0:
            return False
        if items[idx]["status"] == next_status:
            return False
        items[idx]["status"] = next_status
        _save_all(items)
        return True


def list_feed(
    *,
    cursor: str | None,
    limit: int,
    image_base_url: str,
    include_hidden: bool = False,
    viewer_id: str = "",
) -> dict[str, Any]:
    """游标分页：按 created_at desc, id desc 排序。
    cursor = "<created_at>:<id>"，下一页带的就是上页最后一条的 cursor。

    比起 offset 分页：列表追加 / 删除时不会跳条 / 重复条；管理员后台
    频繁下架某条也能稳定翻页。

    viewer_id：当前请求者的 id，透传到 _public_view 让 is_mine 字段有意义。
    """
    items = _load_all()
    if not include_hidden:
        items = [it for it in items if it["status"] == "visible"]
    items.sort(key=lambda it: (it["created_at"], it["id"]), reverse=True)

    start_idx = 0
    if cursor:
        # cursor 解析失败就当无 cursor 处理，让用户能从头开始
        try:
            ts_str, cid = cursor.split(":", 1)
            ts = int(ts_str)
            for i, it in enumerate(items):
                if (it["created_at"], it["id"]) < (ts, cid):
                    start_idx = i
                    break
            else:
                start_idx = len(items)
        except Exception:
            start_idx = 0

    page_size = max(1, min(int(limit or 20), 100))
    page = items[start_idx : start_idx + page_size]
    next_cursor = ""
    if start_idx + page_size < len(items) and page:
        last = page[-1]
        next_cursor = f"{last['created_at']}:{last['id']}"

    return {
        "items": [_public_view(it, image_base_url, viewer_id=viewer_id) for it in page],
        "next_cursor": next_cursor,
    }


def get_item(
    item_id: str,
    image_base_url: str,
    *,
    include_hidden: bool = False,
    viewer_id: str = "",
) -> dict[str, Any] | None:
    iid = (item_id or "").strip()
    if not iid:
        return None
    for it in _load_all():
        if it["id"] != iid:
            continue
        if not include_hidden and it["status"] != "visible":
            return None
        return _public_view(it, image_base_url, viewer_id=viewer_id)
    return None


def is_published(*, image_rel: str, publisher_id: str) -> dict[str, Any] | None:
    """给"我的作品"卡片用：查这张图当前 user 有没有发过画廊。
    返回原始 record（含 status），让前端能区分 visible/hidden 决定显示
    "已发布"还是"已被下架"。"""
    rel = (image_rel or "").strip().lstrip("/")
    pid = (publisher_id or "").strip()
    if not rel or not pid:
        return None
    for it in _load_all():
        if it["publisher_id"] == pid and it["image_rel"] == rel:
            return it
    return None


def is_published_batch(
    *,
    image_rels: list[str],
    publisher_id: str,
    check_any_publisher: bool = False,
) -> dict[str, dict[str, Any]]:
    """批量查"哪些 rel 发过画廊"。

    默认（check_any_publisher=False）按 publisher_id 严格过滤：用于"我的作品"
    页 reload 时播种 publishStates，避免 N 张图发 N 次单条请求把浏览器并发数撑满。

    check_any_publisher=True：忽略 publisher_id，只要这张图被任何用户发过就算
    "已发布"。给 admin 图片管理页用：admin 在管理任何用户的图，只关心"这张图
    在画廊里有没有露面"，不需要区分是谁发的。该模式下 publisher_id 可空。

    同一 rel 被多人发过的极端情况下（理论可能，鉴权层不允许，但防御一下），
    返回最新一条 record（按 created_at desc 取首条），让前端拿到最新状态。

    返回 dict[rel] = record，未发布的 rel 不在 key 里。
    传入空列表返回 {}，调用方据此短路。
    """
    pid = (publisher_id or "").strip()
    if not check_any_publisher and not pid:
        return {}
    if not image_rels:
        return {}
    # 规范化输入：strip + lstrip("/") 与 publish 写入时保持一致
    wanted = {(r or "").strip().lstrip("/") for r in image_rels}
    wanted.discard("")
    if not wanted:
        return {}
    candidates: list[dict[str, Any]] = []
    for it in _load_all():
        if not check_any_publisher and it["publisher_id"] != pid:
            continue
        if it["image_rel"] in wanted:
            candidates.append(it)
    # 多人发同一张图时取最新一条
    candidates.sort(key=lambda it: (it["created_at"], it["id"]), reverse=True)
    out: dict[str, dict[str, Any]] = {}
    for it in candidates:
        rel = it["image_rel"]
        if rel not in out:
            out[rel] = it
    return out
