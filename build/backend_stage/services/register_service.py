from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import openai_register
from services.register.openai_register import AccountDeletedError


REGISTER_FILE = DATA_DIR / "register.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {**openai_register.config, "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5, "enabled": False, "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": openai_register.config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, "current_quota": 0, "current_available": 0}}


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    cfg["fixed_password"] = str(cfg.get("fixed_password") or "")
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    return cfg


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False))

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._config = _normalize({**self._config, **updates})
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "fixed_password")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            self._config["enabled"] = True
            self._logs = []
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "fixed_password")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}", "yellow")
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            job_kind = str((self._config.get("stats") or {}).get("job_kind") or "")
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            if job_kind == "repair_abnormal":
                self._append_log("已请求停止异常账号修复，当前账号处理完后停止", "yellow")
            else:
                self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def repair_abnormal_accounts(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._append_log("已有注册机任务正在运行，暂不能启动异常账号修复", "yellow")
                return self.get()
            abnormal = [item for item in account_service.list_accounts() if item.get("status") == "异常"]
            if not abnormal:
                self._append_log("没有需要修复的异常账号", "yellow")
                return self.get()
            self._config["enabled"] = True
            self._logs = []
            self._config["stats"] = {
                "job_id": uuid.uuid4().hex,
                "job_kind": "repair_abnormal",
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": 1,
                **self._pool_metrics(),
                "started_at": _now(),
                "updated_at": _now(),
            }
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "fixed_password")})
            self._save()
            self._runner = threading.Thread(target=self._repair_abnormal_run, daemon=True, name="openai-register-repair")
            self._runner.start()
            self._append_log(f"异常账号修复任务启动，共 {len(abnormal)} 个账号", "yellow")
            return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if item.get("status") == "正常"]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = self._pool_metrics()
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _run(self) -> None:
        threads = int(self.get()["threads"])
        submitted, done, success, fail = 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self.get()
                while self.get()["enabled"] and not self._target_reached(cfg, submitted) and len(futures) < threads:
                    submitted += 1
                    futures.add(executor.submit(openai_register.worker, submitted))
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                if not futures and (not self.get()["enabled"] or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, return_when=FIRST_COMPLETED, timeout=2)
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        success += 1 if result.get("ok") else 0
                        fail += 0 if result.get("ok") else 1
                    except Exception:
                        fail += 1
                if not self.get()["enabled"]:
                    for future in futures:
                        future.cancel()
                    if futures:
                        self._append_log(f"已停止主调度，{len(futures)} 个在跑的任务后台跑完后自然结束（不再统计）", "yellow")
                    break
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")

    def _repair_abnormal_run(self) -> None:
        abnormal = [item for item in account_service.list_accounts() if item.get("status") == "异常"]
        total = len(abnormal)
        success = 0
        fail = 0
        processed = 0
        fixed_password = str(self.get().get("fixed_password") or "").strip()
        self._bump(running=1, done=0, success=0, fail=0)
        for index, account in enumerate(abnormal, start=1):
            if not self.get().get("enabled"):
                self._append_log("异常账号修复任务已停止，剩余账号未继续处理", "yellow")
                break
            processed = index
            old_token = str(account.get("access_token") or "").strip()
            email = str(account.get("email") or "").strip()
            mailbox = account.get("mailbox") if isinstance(account.get("mailbox"), dict) else {}
            if email and not mailbox:
                mailbox = {"address": email}
            password = str(account.get("password") or "").strip() or fixed_password
            if not email:
                fail += 1
                self._append_log(f"[修复{index}/{total}] 异常账号缺少邮箱，token={old_token[:12]}...，直接删除账号并尝试清理邮箱", "red")
                account_service.delete_accounts([old_token], delete_mailboxes=True)
                self._bump(done=index, success=success, fail=fail, **self._pool_metrics())
                continue

            registrar = openai_register.PlatformRegistrar(str(self.get().get("proxy") or ""))
            try:
                self._append_log(f"[修复{index}/{total}] 开始修复异常账号 {email}", "yellow")
                result = registrar.authenticate_existing(email, mailbox, password, index)
                new_token = str(result.get("access_token") or "").strip()
                if not new_token:
                    raise RuntimeError("登录认证成功但未返回 access_token")
                if new_token == old_token:
                    account_service.update_account(old_token, {**result, "status": "正常"})
                    refresh_result = account_service.refresh_accounts([old_token])
                    if refresh_result.get("errors"):
                        raise RuntimeError(str(refresh_result["errors"][0].get("error") or "刷新账号信息失败"))
                else:
                    account_service.add_accounts([new_token], [result])
                    refresh_result = account_service.refresh_accounts([new_token])
                    if refresh_result.get("errors"):
                        account_service.delete_accounts([new_token], delete_mailboxes=False)
                        raise RuntimeError(str(refresh_result["errors"][0].get("error") or "刷新账号信息失败"))
                    account_service.delete_accounts([old_token], delete_mailboxes=False)
                success += 1
                self._append_log(f"[修复{index}/{total}] {email} 修复成功，已移除旧异常账号，保留邮箱账号", "green")
            except AccountDeletedError as exc:
                fail += 1
                self._append_log(f"[修复{index}/{total}] 异常账号 {email} 已被删除（password/verify 403），清理账号和邮箱: {exc}", "red")
                account_service.delete_accounts([old_token], delete_mailboxes=True)
            except Exception as exc:
                fail += 1
                self._append_log(f"[修复{index}/{total}] 异常账号 {email} 登录认证失败，原因: {exc}", "red")
                account_service.delete_accounts([old_token], delete_mailboxes=True)
            finally:
                registrar.close()
                self._bump(done=index, success=success, fail=fail, **self._pool_metrics())
        self._bump(running=0, done=processed, success=success, fail=fail, finished_at=_now(), **self._pool_metrics())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"异常账号修复任务结束，成功{success}，失败{fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
