from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from api.support import require_identity
from services.video_metadata_service import get_video_cover, get_video_metadata


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/video/metadata")
    async def metadata(
        url: str = Query(..., min_length=1),
        authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        try:
            return await run_in_threadpool(get_video_metadata, url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.get("/api/video/cover")
    async def cover(
        url: str = Query(..., min_length=1),
    ):
        try:
            content, media_type = await run_in_threadpool(get_video_cover, url)
            return Response(
                content=content,
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    return router
