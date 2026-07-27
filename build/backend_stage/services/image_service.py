from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

from services.config import config
from services.image_owners_service import load_owners, remove_owners
from services.image_prompts_service import load_prompts, remove_prompts
from services.image_tags_service import load_tags, remove_tags

THUMBNAIL_SIZE = (320, 320)


def _cleanup_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _safe_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return path


def _thumbnail_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    return config.image_thumbnails_dir / f"{rel}.png"


def thumbnail_url(base_url: str, relative_path: str) -> str:
    return f"{base_url.rstrip('/')}/image-thumbnails/{_safe_relative_path(relative_path)}"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def ensure_thumbnail(relative_path: str) -> Path:
    source = _safe_image_path(relative_path)
    target = _thumbnail_path(relative_path)
    source_mtime = source.stat().st_mtime
    if target.exists() and target.stat().st_mtime >= source_mtime:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            image.save(target, format="PNG", optimize=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="failed to create thumbnail") from exc
    return target


def get_thumbnail_response(relative_path: str) -> FileResponse:
    return FileResponse(ensure_thumbnail(relative_path))


def get_image_download_response(relative_path: str) -> FileResponse:
    path = _safe_image_path(relative_path)
    return FileResponse(path, filename=path.name)


def cleanup_image_thumbnails() -> int:
    thumbnails_root = config.image_thumbnails_dir
    images_root = config.images_dir
    removed = 0
    for path in thumbnails_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(thumbnails_root).as_posix()
        if not rel.endswith(".png") or not (images_root / rel[:-4]).exists():
            path.unlink()
            removed += 1
    _cleanup_empty_dirs(thumbnails_root)
    return removed


def count_total_images() -> int:
    """图片管理页"未归属"数量统计用：纯粹数 images_dir 下文件数。
    比 _image_items 轻量，避开了打开文件取尺寸的 IO。"""
    root = config.images_dir
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _image_items(start_date: str = "", end_date: str = "", owner: str = "", admin_ids: set[str] | None = None) -> list[dict[str, object]]:
    items = []
    root = config.images_dir
    owner_filter = (owner or "").strip()
    owners_map = load_owners() if owner_filter else {}
    admin_set = admin_ids or set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        day = "-".join(parts[:3]) if len(parts) >= 4 else datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        if owner_filter:
            owner_id = owners_map.get(rel, "")
            # 前端约定的两个特殊桶：
            #   __admin__   = 管理员生成（owner_id 落在 admin 集合里）
            #   __unowned__ = 真孤儿（image_owners.json 里没记录，多半是老数据 / 写归属表失败）
            if owner_filter == "__unowned__":
                if owner_id:
                    continue
            elif owner_filter == "__admin__":
                if owner_id not in admin_set:
                    continue
            elif owner_id != owner_filter:
                continue
        dimensions = _image_dimensions(path)
        items.append({
            "rel": rel,
            "path": rel,
            "name": path.name,
            "date": day,
            "size": path.stat().st_size,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
        })
    items.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return items


def list_images(
    base_url: str,
    start_date: str = "",
    end_date: str = "",
    owner: str = "",
    admin_ids: set[str] | None = None,
) -> dict[str, object]:
    config.cleanup_old_images()
    cleanup_image_thumbnails()
    all_tags = load_tags()
    owners_map = load_owners()
    prompts_map = load_prompts()
    admin_set = admin_ids or set()
    items = []
    for item in _image_items(start_date, end_date, owner, admin_set):
        rel = str(item["path"])
        owner_id = owners_map.get(rel, "")
        items.append({
            **item,
            "url": f"{base_url.rstrip('/')}/images/{rel}",
            "thumbnail_url": thumbnail_url(base_url, rel),
            "tags": all_tags.get(rel, []),
            "owner_id": owner_id,
            # 标记给前端：admin 桶里的图都用同一种 badge 文案"管理员"，不暴露具体 admin id
            "is_admin_owner": bool(owner_id) and owner_id in admin_set,
            # 生成时记下来的 prompt 原文。老数据没记录就空字符串。
            # web "我的作品"页据此一键复用 / 发布画廊；为空时让前端弹窗手填。
            "prompt": prompts_map.get(rel, ""),
        })
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        groups.setdefault(str(item["date"]), []).append(item)
    return {"items": items, "groups": [{"date": key, "items": value} for key, value in groups.items()]}


def delete_images(
    paths: list[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    owner: str = "",
    all_matching: bool = False,
    admin_ids: set[str] | None = None,
) -> dict[str, int]:
    root = config.images_dir.resolve()
    targets = (
        [str(item["path"]) for item in _image_items(start_date, end_date, owner, admin_ids or set())]
        if all_matching
        else (paths or [])
    )
    removed = 0
    cleared_rels: list[str] = []
    for item in targets:
        path = (root / item).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            for thumbnail in (_thumbnail_path(item), config.image_thumbnails_dir / _safe_relative_path(item)):
                if thumbnail.is_file():
                    thumbnail.unlink()
            remove_tags(item)
            cleared_rels.append(item)
            removed += 1
    if cleared_rels:
        remove_owners(cleared_rels)
        remove_prompts(cleared_rels)
    _cleanup_empty_dirs(root)
    _cleanup_empty_dirs(config.image_thumbnails_dir)
    return {"removed": removed}


def download_images_zip(paths: list[str]) -> io.BytesIO:
    root = config.images_dir.resolve()
    buf = io.BytesIO()
    added = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            rel = _safe_relative_path(item)
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            name = path.name
            if name in used_names:
                stem = path.stem
                suffix = path.suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                name = f"{stem}_{counter}{suffix}"
            used_names.add(name)
            zf.write(path, name)
            added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no images found")
    buf.seek(0)
    return buf
