from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.image_owners_service import record_owner_for_result
from services.image_prompts_service import record_prompt_for_result
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import openai_v1_image_edit, openai_v1_image_generations

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TASK_STATUS_CANCELED = "canceled"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR, TASK_STATUS_CANCELED}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
VALID_STATUSES = {
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_ERROR,
    TASK_STATUS_CANCELED,
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "resolution": task.get("resolution"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("error"):
        item["error"] = task.get("error")
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        retention_days_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        base_url: str,
        resolution: str | None = None,
        plan_type: str | None = None,
        allowed_plan_types: object = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": size,
            "resolution": resolution,
            "plan_type": plan_type,
            "allowed_plan_types": allowed_plan_types,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        base_url: str,
        images: list[tuple[bytes, str, str]],
        resolution: str | None = None,
        plan_type: str | None = None,
        allowed_plan_types: object = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "images": images,
            "model": model,
            "n": 1,
            "size": size,
            "resolution": resolution,
            "plan_type": plan_type,
            "allowed_plan_types": allowed_plan_types,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def cancel_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        """标记任务为已取消。

        - queued: 直接置为 canceled，工作线程启动时会发现并跳过实际请求
        - running: 置为 canceled，工作线程会在请求结束后丢弃结果而不写入
        - 终态(success/error/canceled): 不动

        每条真正被取消（queued / running 翻 canceled）的任务都退还 1 张入口预扣额度。
        终态条目不退——success 已经出图了不能扣回去，error/canceled 已经退过了。
        """
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        canceled: list[str] = []
        skipped: list[str] = []
        missing_ids: list[str] = []
        with self._lock:
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                    continue
                status = task.get("status")
                if status in TERMINAL_STATUSES:
                    skipped.append(task_id)
                    continue
                task["status"] = TASK_STATUS_CANCELED
                task["error"] = "已取消"
                task["updated_at"] = _now_iso()
                canceled.append(task_id)
            if canceled:
                self._save_locked()
        # 退款放到锁外做：DataStore / DB 写盘期间不持有 self._lock，
        # 避免与 _run_task 失败分支同时拿锁形成竞态。
        for _ in canceled:
            self._refund_one(identity)
        return {"canceled": canceled, "skipped": skipped, "missing_ids": missing_ids}

    def _refund_one(self, identity: dict[str, object]) -> None:
        """退还 1 张入口预扣额度。
        admin / unlimited / 匿名身份内部 noop；普通用户的 used 减 1 不会跌破 0。
        所有异常吞掉——退款失败不该影响主流程的错误响应。
        """
        role = str(identity.get("role") or "").strip().lower()
        item_id = str(identity.get("id") or "").strip()
        if role == "admin" or not item_id or item_id == "admin":
            return
        try:
            # 延迟 import 避免 services 间循环引用
            from services.auth_service import auth_service
            auth_service.refund_quota(item_id, 1)
        except Exception:
            pass

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "size": _clean(payload.get("size")),
                "resolution": _clean(payload.get("resolution")),
                "created_at": now,
                "updated_at": now,
            }
            self._tasks[key] = task
            self._save_locked()
            should_start = True

        if should_start:
            thread = threading.Thread(
                target=self._run_task,
                args=(key, mode, payload, dict(identity), _clean(payload.get("model"), "gpt-image-2")),
                name=f"image-task-{task_id[:16]}",
                daemon=True,
            )
            thread.start()
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
    ) -> None:
        # 启动前检查：若任务已被取消，直接结束
        with self._lock:
            task = self._tasks.get(key)
            if task is None or task.get("status") == TASK_STATUS_CANCELED:
                return

        started = time.time()
        self._update_task(key, status=TASK_STATUS_RUNNING, error="")
        try:
            handler = self.edit_handler if mode == "edit" else self.generation_handler
            result = handler(payload)
            # 请求结束后再检查：若期间被取消，丢弃结果不写回
            with self._lock:
                task = self._tasks.get(key)
                if task is None or task.get("status") == TASK_STATUS_CANCELED:
                    return
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            if not isinstance(data, list) or not data:
                message = _clean(result.get("message")) or "image task returned no image data"
                raise RuntimeError(message)
            self._update_task(key, status=TASK_STATUS_SUCCESS, data=data, error="")
            # 任务真正成功后再写归属表，避免给失败的临时落盘也挂上 owner。
            # admin / 匿名身份不写，由 record_owner_for_result 内部判断。
            record_owner_for_result(identity, data)
            # prompt 文本同步写一份，给"我的作品"页 / 画廊发布功能复用。
            # mode=="edit" 时标记为图生图，画廊发布时会自动把 prompt 落空——
            # 图生图的 prompt 是相对参考图的指令，离开参考图对外人没复用价值。
            record_prompt_for_result(
                payload.get("prompt"), data, is_edit=(mode == "edit")
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                request_preview=request_text(payload.get("prompt")),
                size=_clean(payload.get("size")),
                resolution=_clean(payload.get("resolution")),
                urls=_collect_image_urls(data),
            )
        except Exception as exc:
            # 请求异常时也要让"已取消"优先，不要把取消覆盖成 error
            with self._lock:
                task = self._tasks.get(key)
                if task is not None and task.get("status") == TASK_STATUS_CANCELED:
                    return
            error_message = str(exc) or "image task failed"
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[])
            # 上游真失败：退还入口预扣的 1 张额度。
            # admin / unlimited 在 _refund_one 内部 noop；普通用户的 used 减 1 不会跌破 0。
            self._refund_one(identity)
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                request_preview=request_text(payload.get("prompt")),
                size=_clean(payload.get("size")),
                resolution=_clean(payload.get("resolution")),
                status="failed",
                error=error_message,
            )

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        size: str = "",
        resolution: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if size:
            detail["size"] = size
        if resolution:
            detail["resolution"] = resolution
        if error:
            detail["error"] = error
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            task.update(updates)
            task["updated_at"] = _now_iso()
            self._save_locked()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in VALID_STATUSES:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "size": _clean(item.get("size")),
                "resolution": _clean(item.get("resolution")),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
            }
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"tasks": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.get("status") in UNFINISHED_STATUSES:
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            self._tasks.pop(key, None)
        return bool(removed_keys)


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
