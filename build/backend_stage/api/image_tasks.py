from __future__ import annotations

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import (
    apply_image_account_policy,
    consume_user_quota,
    refund_user_quota,
    require_identity,
    resolve_image_base_url,
)
from services.content_filter import check_request
from services.image_task_service import image_task_service
from services.log_service import LoggedCall


class ImageGenerationTaskRequest(BaseModel):
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    size: str | None = None
    resolution: str | None = None


class ImageTaskCancelRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks")
    async def list_image_tasks(
        ids: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_task_service.list_tasks, identity, _parse_task_ids(ids))

    @router.post("/api/image-tasks/cancel")
    async def cancel_image_tasks(
        body: ImageTaskCancelRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        ids = [task_id.strip() for task_id in body.ids if task_id and task_id.strip()]
        return await run_in_threadpool(image_task_service.cancel_tasks, identity, ids)

    @router.post("/api/image-tasks/generations")
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload: dict[str, object] = {"model": body.model, "resolution": body.resolution}
        apply_image_account_policy(identity, payload)
        # 前端每张图独立提交一次任务，按 1 扣；额度不足直接 402，
        # 不要等 submit_generation 跑完才发现没额度。
        consume_user_quota(identity, 1)
        # 后续任意 fail-fast 路径都要把这 1 张退掉，避免参数错误也白扣
        try:
            await filter_or_log(LoggedCall(identity, "/api/image-tasks/generations", body.model, "文生图任务", request_text=body.prompt), body.prompt)
            return await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=body.client_task_id,
                prompt=body.prompt,
                model=str(payload.get("model") or body.model),
                size=body.size,
                resolution=str(payload.get("resolution") or body.resolution or "") or None,
                plan_type=str(payload.get("plan_type") or "").strip() or None,
                allowed_plan_types=payload.get("allowed_plan_types"),
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            refund_user_quota(identity, 1)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except HTTPException:
            # filter_or_log / submit_generation 抛出的 HTTPException：
            # 内容审查 / 上游号池忙 / 参数错都属于"还没真发请求就失败"，应退款。
            # _run_task 异步路径的失败由 image_task_service._refund_one 自己退，不在这条链路里。
            refund_user_quota(identity, 1)
            raise

    @router.post("/api/image-tasks/edits")
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
        image: list[UploadFile] | None = File(default=None),
        image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
        client_task_id: str = Form(...),
        prompt: str = Form(...),
        model: str = Form(default="gpt-image-2"),
        size: str | None = Form(default=None),
        resolution: str | None = Form(default=None),
    ):
        identity = require_identity(authorization)
        payload: dict[str, object] = {"model": model, "resolution": resolution}
        apply_image_account_policy(identity, payload)
        # 同样按 1 张扣；前端会拆成多次提交，所以这里不需要乘以 n。
        consume_user_quota(identity, 1)
        try:
            await filter_or_log(LoggedCall(identity, "/api/image-tasks/edits", model, "图生图任务", request_text=prompt), prompt)
            uploads = [*(image or []), *(image_list or [])]
            if not uploads:
                raise HTTPException(status_code=400, detail={"error": "image file is required"})
            images: list[tuple[bytes, str, str]] = []
            for upload in uploads:
                image_data = await upload.read()
                if not image_data:
                    raise HTTPException(status_code=400, detail={"error": "image file is empty"})
                images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
            return await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=str(payload.get("model") or model),
                size=size,
                resolution=str(payload.get("resolution") or resolution or "") or None,
                plan_type=str(payload.get("plan_type") or "").strip() or None,
                allowed_plan_types=payload.get("allowed_plan_types"),
                base_url=resolve_image_base_url(request),
                images=images,
            )
        except ValueError as exc:
            refund_user_quota(identity, 1)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except HTTPException:
            refund_user_quota(identity, 1)
            raise

    return router
