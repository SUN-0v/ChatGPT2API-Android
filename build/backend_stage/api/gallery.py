from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import (
    extract_bearer_token,
    require_admin,
    require_identity,
    resolve_image_base_url,
)
from services import gallery_service
from services.image_owners_service import get_owner


class PublishRequest(BaseModel):
    image_rel: str = Field(..., description="后端 image_owners 的 rel 路径，例如 2026/05/21/abc.png")
    prompt: str = ""
    model: str = ""
    size: str = ""
    width: int = 0
    height: int = 0


class PublishedBatchRequest(BaseModel):
    image_rels: list[str] = Field(default_factory=list, description="待查询的 rel 列表")


def _identity_view(identity: dict) -> tuple[str, str, bool]:
    """从 require_identity 返回的 dict 抠出 (id, 展示名, is_admin)。"""
    role = str(identity.get("role") or "").lower()
    pid = str(identity.get("id") or "").strip()
    name = str(identity.get("name") or "").strip()
    return pid, name, role == "admin"


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/gallery/feed")
    async def feed(
        request: Request,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        authorization: str | None = Header(default=None),
    ):
        """公共画廊。任何登录用户（admin / user key）都可读。
        默认只返回 status=visible；admin 加 ?include_hidden=true 可见全部。"""
        identity = require_identity(authorization)
        pid, _, is_admin = _identity_view(identity)
        # 是否包含被下架的：仅 admin 显式带参数才允许
        include_hidden = (
            is_admin
            and str(request.query_params.get("include_hidden") or "").lower() in ("1", "true", "yes")
        )
        base_url = resolve_image_base_url(request)
        return await run_in_threadpool(
            gallery_service.list_feed,
            cursor=cursor,
            limit=limit,
            image_base_url=base_url,
            include_hidden=include_hidden,
            viewer_id=pid,
        )

    @router.get("/api/gallery/items/{item_id}")
    async def item_detail(
        item_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        pid, _, is_admin = _identity_view(identity)
        base_url = resolve_image_base_url(request)
        item = await run_in_threadpool(
            gallery_service.get_item,
            item_id,
            base_url,
            include_hidden=is_admin,
            viewer_id=pid,
        )
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "条目不存在或已下架"})
        return {"item": item}

    @router.post("/api/gallery/publish")
    async def publish(
        body: PublishRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """发布到画廊。鉴权：登录用户都能发，但只能发自己拥有的图。
        - 校验 image_rel 在 image_owners 里的 owner 等于当前 identity.id
          否则 403（防止用别人的图占用画廊位）
        - 命中敏感词 → 400（gallery_service 会冒泡 HTTPException）
        - 同人同图重复发布 → 复用旧记录，幂等
        """
        identity = require_identity(authorization)
        pid, name, is_admin = _identity_view(identity)

        rel = body.image_rel.strip().lstrip("/")
        if not rel:
            raise HTTPException(status_code=400, detail={"error": "image_rel 不能为空"})

        # 普通用户必须是这张图的 owner；admin 可代发任意图（少见，但场景：精选展示）
        if not is_admin:
            owner = await run_in_threadpool(get_owner, rel)
            if owner != pid:
                raise HTTPException(status_code=403, detail={"error": "只能发布自己生成的图"})

        try:
            item = await run_in_threadpool(
                gallery_service.publish,
                image_rel=rel,
                publisher_id=pid or "admin",
                publisher_name=name or "管理员",
                prompt=body.prompt,
                model=body.model,
                size=body.size,
                width=body.width,
                height=body.height,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        # 返回时附上完整 url，让前端直接展示
        base_url = resolve_image_base_url(request)
        return {"item": gallery_service._public_view(item, base_url, viewer_id=pid)}

    @router.delete("/api/gallery/items/{item_id}")
    async def unpublish(
        item_id: str,
        authorization: str | None = Header(default=None),
    ):
        """用户撤回 / 管理员删除（硬删）。仅本人或 admin。"""
        identity = require_identity(authorization)
        pid, _, is_admin = _identity_view(identity)
        ok = await run_in_threadpool(
            gallery_service.unpublish, item_id, requester_id=pid, is_admin=is_admin
        )
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "条目不存在或无权限"})
        return {"ok": True}

    @router.post("/api/gallery/items/{item_id}/hide")
    async def admin_hide(
        item_id: str,
        authorization: str | None = Header(default=None),
    ):
        """admin 软下架。区别于 DELETE：不删原图、不丢数据，只把 status 改成 hidden，
        前台 feed 里看不到，但 admin 后台仍能看到（include_hidden=true）。
        发布者本人若再 publish 同一张图，service 会自动恢复成 visible。"""
        require_admin(authorization)
        ok = await run_in_threadpool(gallery_service.admin_set_status, item_id, "hidden")
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "条目不存在或状态未变"})
        return {"ok": True}

    @router.post("/api/gallery/items/{item_id}/unhide")
    async def admin_unhide(
        item_id: str,
        authorization: str | None = Header(default=None),
    ):
        """admin 把已下架的恢复成可见。"""
        require_admin(authorization)
        ok = await run_in_threadpool(gallery_service.admin_set_status, item_id, "visible")
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "条目不存在或状态未变"})
        return {"ok": True}

    @router.get("/api/gallery/published")
    async def my_published_lookup(
        image_rel: str = Query(..., description="image_owners 的 rel 路径"),
        authorization: str | None = Header(default=None),
    ):
        """给"我的作品"页查"这张图我发了没"。返回 {published: bool, item: {...}|null}。
        前端据此把卡片菜单切成"发布到画廊"或"已发布·撤回"。"""
        identity = require_identity(authorization)
        pid, _, _ = _identity_view(identity)
        rel = image_rel.strip().lstrip("/")
        record = await run_in_threadpool(
            gallery_service.is_published,
            image_rel=rel,
            publisher_id=pid or "admin",
        )
        if record is None:
            return {"published": False, "item": None}
        return {
            "published": record["status"] == "visible",
            "item": {
                "id": record["id"],
                "status": record["status"],
            },
        }

    @router.post("/api/gallery/published/batch")
    async def my_published_batch(
        body: PublishedBatchRequest,
        authorization: str | None = Header(default=None),
    ):
        """批量查"哪些 rel 发过画廊"。给"我的作品"页 reload 时一次播种 publishStates，
        避免逐张发单条请求被浏览器并发数撑满。

        返回 {[rel]: {published, id, status, publisher_name?}}，**只包含查到记录的
        rel**——未发布的 rel 不在结果 key 里，前端按 key 存在与否判定即可。

        admin 视角自动跨用户查询：admin 在图片管理页要管理任何用户的图，只关心
        "这张图被任何人发过没"，不区分发布者。普通 user 仍按自己 publisher_id 过滤。
        """
        identity = require_identity(authorization)
        pid, _, is_admin = _identity_view(identity)
        records = await run_in_threadpool(
            gallery_service.is_published_batch,
            image_rels=body.image_rels,
            publisher_id=pid or "admin",
            check_any_publisher=is_admin,
        )
        return {
            "items": {
                rel: {
                    "published": rec["status"] == "visible",
                    "id": rec["id"],
                    "status": rec["status"],
                    # admin 视角下额外把 publisher_name 暴露出来，方便管理页
                    # 角标 tooltip 显示"由 xx 发布"。普通 user 拿不到也没影响——
                    # 自己发的图本就知道是自己。
                    "publisher_name": rec.get("publisher_name") or "",
                }
                for rel, rec in records.items()
            }
        }

    return router
