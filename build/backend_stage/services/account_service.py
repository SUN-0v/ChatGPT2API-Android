from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from threading import Condition, Lock
from typing import Any
from datetime import datetime, timedelta, timezone

from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _rate_limit_reset_at(cls, account: dict) -> datetime | None:
        return cls._parse_datetime(account.get("rate_limit_reset_at"))

    @classmethod
    def _is_runtime_rate_limited(cls, account: dict) -> bool:
        reset_at = cls._rate_limit_reset_at(account)
        return bool(reset_at and datetime.now(timezone.utc) < reset_at)

    @staticmethod
    def _is_rate_limit_error(exc: Exception | str) -> bool:
        if getattr(exc, "status_code", None) == 429:
            return True
        text = str(exc or "").lower()
        return (
            "status=429" in text
            or "http 429" in text
            or "too many requests" in text
            or "rate_limit_exceeded" in text
            or "usage_limit_reached" in text
        )

    @classmethod
    def _is_image_account_available(cls, account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "异常"}:
            return False
        if cls._is_runtime_rate_limited(account):
            return False
        if account.get("status") == "限流" and not cls._rate_limit_reset_at(account):
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        return str(value or "web").strip().lower() or "web"

    @staticmethod
    def _clean_string(value: object) -> str:
        return str(value or "").strip()

    @classmethod
    def _looks_like_codex_oauth_payload(cls, payload: dict) -> bool:
        raw_type = cls._clean_string(payload.get("type") or payload.get("export_type")).lower()
        if raw_type == "codex":
            return True
        if cls._clean_string(payload.get("refresh_token")) and cls._clean_string(payload.get("id_token")):
            return True
        if cls._clean_string(payload.get("account_id") or payload.get("chatgpt_account_id")) and cls._clean_string(payload.get("refresh_token")):
            return True
        return False

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key) or raw

    def _search_account_type(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("plan_type", "account_plan", "account_type", "subscription_type", "type"):
                plan = self._normalize_account_type(payload.get(key))
                if plan:
                    return plan
            for value in payload.values():
                plan = self._search_account_type(value)
                if plan:
                    return plan
        elif isinstance(payload, list):
            for value in payload:
                plan = self._search_account_type(value)
                if plan:
                    return plan
        return None

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        normalized["type"] = normalized.get("type") or "free"
        normalized["type"] = self._normalize_account_type(normalized["type"]) or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        # initial_quota：注册时拿到的总额度。每次 normalize 取 max(已有, 当前 quota)，
        # 这样首次拉到额度时自动落库；之后 mark_image_result 扣减 quota 不会拉低它；
        # 账号续约导致 quota 涨上去也会自动跟上。
        existing_initial = int(normalized.get("initial_quota") or 0)
        normalized["initial_quota"] = max(existing_initial, int(normalized["quota"]))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        if not source_type and self._looks_like_codex_oauth_payload(normalized):
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["rate_limited_at"] = normalized.get("rate_limited_at") or None
        normalized["rate_limit_reset_at"] = normalized.get("rate_limit_reset_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        mailbox = normalized.get("mailbox")
        normalized["mailbox"] = mailbox if isinstance(mailbox, dict) else None
        normalized["password"] = normalized.get("password") or None
        normalized["refresh_token"] = normalized.get("refresh_token") or None
        normalized["id_token"] = normalized.get("id_token") or None
        normalized["account_id"] = normalized.get("account_id") or normalized.get("chatgpt_account_id") or None
        normalized["expires_at"] = normalized.get("expires_at") or normalized.get("expired") or None
        normalized["client_id"] = normalized.get("client_id") or None
        normalized["created_at"] = normalized.get("created_at") or None
        return normalized

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types):
                    raise RuntimeError(
                        f"no available {plan_type or source_type or ''} image quota".replace("  ", " ").strip()
                        if plan_type or source_type else "no available image quota"
                    )
                tokens = self._list_available_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
    ) -> str:
        attempted_tokens: set[str] = set(excluded_tokens or set())
        while True:
            access_token = self._acquire_next_candidate_token(
                excluded_tokens=attempted_tokens,
                plan_type=plan_type,
                source_type=source_type,
                plan_types=plan_types,
            )
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception as exc:
                if self._is_rate_limit_error(exc):
                    self.mark_image_rate_limited(
                        access_token,
                        error=str(exc),
                        headers=getattr(exc, "headers", None),
                        body=getattr(exc, "body", None),
                    )
                else:
                    self.release_image_slot(access_token)
                continue
            active_token = str((account or {}).get("access_token") or access_token)
            if (
                    self._is_image_account_available(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                return active_token
            self.release_image_slot(active_token)

    def list_available_text_account_types(self) -> list[str]:
        with self._lock:
            types = {
                normalized
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and account.get("access_token")
                   and (normalized := self._normalize_account_type(account.get("type")))
            }
        order = ["free", "Plus", "Pro", "ProLite", "Team", "Enterprise"]
        return sorted(types, key=lambda item: (order.index(item) if item in order else len(order), item.lower()))

    def get_text_access_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and self._account_matches_plan_type(account, plan_type)
                   and self._account_matches_any_plan_type(account, plan_types)
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
            return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0})
            return False
        removed = bool(self.delete_accounts([access_token], delete_mailboxes=True)["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0})
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _prepare_account_payload(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 type=codex 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if not payload.get("source_type") and AccountService._looks_like_codex_oauth_payload(payload):
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def add_account_items(self, items: list[dict]) -> dict:
        payloads = [
            payload
            for item in items
            if (payload := self._prepare_account_payload(item)) is not None
        ]
        return self._add_account_payloads(payloads)

    def add_accounts(
            self,
            tokens: list[str],
            account_records: list[dict] | None = None,
            source_type: str = "web",
    ) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}
        record_by_token = {
            self._account_payload_token(record): dict(record)
            for record in account_records or []
            if isinstance(record, dict) and self._account_payload_token(record)
        }
        payloads = []
        for token in tokens:
            payload = {
                "access_token": token,
                "source_type": self._normalize_source_type(source_type),
                **record_by_token.get(token, {}),
            }
            payloads.append(payload)
        return self._add_account_payloads(payloads)

    def _add_account_payloads(self, payloads: list[dict]) -> dict:
        deduped: dict[str, dict] = {}
        for payload in payloads:
            prepared = self._prepare_account_payload(payload)
            if prepared is None:
                continue
            access_token = self._account_payload_token(prepared)
            if not access_token:
                continue
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **prepared, "access_token": access_token}

        if not deduped:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token, payload in deduped.items():
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    current = {}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not current.get("created_at") and not incoming.get("created_at"):
                    incoming["created_at"] = datetime.now(timezone.utc).isoformat()
                elif not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                account = self._normalize_account(
                    {
                        **current,
                        **incoming,
                        "access_token": access_token,
                        "type": str(incoming.get("type") or current.get("type") or "free"),
                    }
                )
                if account is not None:
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def delete_accounts(self, tokens: list[str], delete_mailboxes: bool = False) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "mailboxes_removed": 0, "mailbox_errors": [], "items": self.list_accounts()}
        removed_accounts: list[dict] = []
        with self._lock:
            removed = 0
            for token in target_set:
                account = self._accounts.pop(token, None)
                if account is not None:
                    removed += 1
                    removed_accounts.append(dict(account))
            for token in target_set:
                self._image_inflight.pop(token, None)
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        mailbox_result = self._delete_account_mailboxes(removed_accounts) if delete_mailboxes else {"removed": 0, "errors": []}
        return {
            "removed": removed,
            "mailboxes_removed": mailbox_result["removed"],
            "mailbox_errors": mailbox_result["errors"],
            "items": items,
        }

    def _delete_account_mailboxes(self, accounts: list[dict]) -> dict:
        if not accounts:
            return {"removed": 0, "errors": []}
        try:
            from services.register import mail_provider
            from services.register import openai_register
        except Exception as exc:
            return {"removed": 0, "errors": [{"email": "", "error": str(exc)}]}

        removed = 0
        errors = []
        for account in accounts:
            email = str(account.get("email") or "").strip()
            mailbox = account.get("mailbox") if isinstance(account.get("mailbox"), dict) else {}
            if not mailbox and email:
                mailbox = {"address": email}
            if not str(mailbox.get("address") or "").strip():
                errors.append({"email": email, "error": "缺少邮箱地址，无法删除邮箱账号"})
                continue
            try:
                if mail_provider.delete_mailbox(openai_register.config["mail"], mailbox):
                    removed += 1
                    log_service.add(LOG_TYPE_ACCOUNT, "删除异常账号对应邮箱", {"email": email})
            except Exception as exc:
                errors.append({"email": email, "error": str(exc)})
                log_service.add(LOG_TYPE_ACCOUNT, "删除异常账号对应邮箱失败", {"email": email, "error": str(exc)})
        return {"removed": removed, "errors": errors}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            merged = {**current, **updates, "access_token": access_token}
            if str(updates.get("status") or "").strip() == "正常":
                merged["rate_limited_at"] = None
                merged["rate_limit_reset_at"] = None
            account = self._normalize_account(merged)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                            {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                next_item["rate_limited_at"] = None
                next_item["rate_limit_reset_at"] = None
                if not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                if not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    @staticmethod
    def _header_value(headers: object, key: str) -> str:
        if not isinstance(headers, dict):
            return ""
        key_lower = key.lower()
        for raw_key, value in headers.items():
            if str(raw_key).lower() != key_lower:
                continue
            if isinstance(value, (list, tuple)):
                return str(value[0] if value else "").strip()
            return str(value or "").strip()
        return ""

    @classmethod
    def _seconds_from_header(cls, headers: object, key: str) -> int | None:
        value = cls._header_value(headers, key)
        if not value:
            return None
        try:
            seconds = int(float(value))
        except ValueError:
            return None
        return seconds if seconds > 0 else None

    @classmethod
    def _parse_retry_after(cls, headers: object, now: datetime) -> datetime | None:
        value = cls._header_value(headers, "retry-after")
        if not value:
            return None
        try:
            seconds = int(float(value))
            return now + timedelta(seconds=max(1, seconds))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(value)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _parse_codex_rate_limit_reset(cls, headers: object, now: datetime) -> datetime | None:
        windows: dict[str, dict[str, float | int]] = {}
        for prefix in ("primary", "secondary"):
            used_raw = cls._header_value(headers, f"x-codex-{prefix}-used-percent")
            reset_seconds = cls._seconds_from_header(headers, f"x-codex-{prefix}-reset-after-seconds")
            window_minutes = cls._seconds_from_header(headers, f"x-codex-{prefix}-window-minutes")
            if reset_seconds is None:
                continue
            try:
                used_percent = float(used_raw) if used_raw else 0.0
            except ValueError:
                used_percent = 0.0
            scope = "5h" if window_minutes is not None and window_minutes <= 300 else "7d"
            windows[scope] = {"used_percent": used_percent, "reset_seconds": reset_seconds}
        if not windows:
            return None
        for scope in ("7d", "5h"):
            item = windows.get(scope)
            if item and float(item.get("used_percent") or 0) >= 100:
                return now + timedelta(seconds=int(item["reset_seconds"]))
        max_reset = max(int(item["reset_seconds"]) for item in windows.values())
        return now + timedelta(seconds=max_reset) if max_reset > 0 else None

    @classmethod
    def _parse_body_rate_limit_reset(cls, body: object, now: datetime) -> datetime | None:
        candidates: list[object] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    key_text = str(key or "").lower()
                    if key_text in {
                        "resets_at",
                        "reset_at",
                        "reset_time",
                        "rate_limit_reset_at",
                        "retry_after",
                        "reset_after",
                    }:
                        candidates.append(child)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        if isinstance(body, str):
            try:
                decoded = json.loads(body)
            except Exception:
                decoded = None
            if decoded is not None:
                walk(decoded)
        else:
            walk(body)

        for value in candidates:
            if isinstance(value, (int, float)):
                if value > 1_000_000_000:
                    return datetime.fromtimestamp(float(value), timezone.utc)
                if value > 0:
                    return now + timedelta(seconds=float(value))
            parsed = cls._parse_datetime(value)
            if parsed is not None:
                return parsed.astimezone(timezone.utc)
            text = str(value or "").strip()
            if not text:
                continue
            try:
                number = float(text)
            except ValueError:
                continue
            if number > 1_000_000_000:
                return datetime.fromtimestamp(number, timezone.utc)
            if number > 0:
                return now + timedelta(seconds=number)
        return None

    @classmethod
    def _rate_limit_reset_from_error(
            cls,
            headers: object = None,
            body: object = None,
            reset_at: object = None,
            cooldown_seconds: int | None = None,
    ) -> datetime:
        now = datetime.now(timezone.utc)
        parsed_reset = cls._parse_datetime(reset_at)
        if parsed_reset is not None:
            return parsed_reset.astimezone(timezone.utc)
        for candidate in (
            cls._parse_codex_rate_limit_reset(headers, now),
            cls._parse_retry_after(headers, now),
            cls._parse_body_rate_limit_reset(body, now),
        ):
            if candidate is not None:
                return candidate.astimezone(timezone.utc)
        seconds = max(1, min(7200, int(cooldown_seconds or 5)))
        return now + timedelta(seconds=seconds)

    def mark_image_rate_limited(
            self,
            access_token: str,
            error: str = "",
            headers: object = None,
            body: object = None,
            reset_at: object = None,
            cooldown_seconds: int | None = None,
    ) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        rate_limited_at = datetime.now(timezone.utc)
        rate_limit_reset_at = self._rate_limit_reset_from_error(headers, body, reset_at, cooldown_seconds)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["status"] = "限流"
            next_item["rate_limited_at"] = self._format_datetime(rate_limited_at)
            next_item["rate_limit_reset_at"] = self._format_datetime(rate_limit_reset_at)
            next_item["restore_at"] = next_item.get("restore_at") or next_item["rate_limit_reset_at"]
            next_item["fail"] = int(next_item.get("fail") or 0) + 1
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[access_token] = account
            self._save_accounts()
        log_service.add(LOG_TYPE_ACCOUNT, "账号触发上游 429 限流", {
            "token": anonymize_token(access_token),
            "rate_limit_reset_at": account.get("rate_limit_reset_at"),
            "error": str(error or "")[:500],
        })
        return dict(account)

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            refreshed_token = self.refresh_oauth_access_token(access_token)
            if not refreshed_token:
                self.remove_invalid_token(access_token, event)
                raise
            result = OpenAIBackendAPI(refreshed_token).get_user_info()
            access_token = refreshed_token
        return self.update_account(access_token, result)

    def refresh_oauth_access_token(self, access_token: str) -> str:
        account = self.get_account(access_token)
        if not account:
            return ""
        refresh_token = self._clean_string(account.get("refresh_token"))
        if not refresh_token:
            return ""
        try:
            from services.openai_backend_api import refresh_codex_oauth_token
            token_payload = refresh_codex_oauth_token(
                refresh_token,
                client_id=self._clean_string(account.get("client_id")),
            )
        except Exception as exc:
            log_service.add(LOG_TYPE_ACCOUNT, "OAuth 刷新失败", {
                "token": anonymize_token(access_token),
                "error": str(exc),
            })
            return ""
        new_access_token = self._clean_string(token_payload.get("access_token"))
        if not new_access_token:
            return ""
        payload = {
            **account,
            **token_payload,
            "access_token": new_access_token,
            "source_type": account.get("source_type") or "codex",
            "export_type": account.get("export_type") or ("codex" if self._normalize_source_type(account.get("source_type")) == "codex" else ""),
            "type": account.get("type") or "free",
        }
        normalized = self._normalize_account(payload)
        if normalized is None:
            return ""
        with self._lock:
            self._accounts.pop(access_token, None)
            self._accounts[new_access_token] = normalized
            inflight = int(self._image_inflight.pop(access_token, 0))
            if inflight:
                self._image_inflight[new_access_token] = int(self._image_inflight.get(new_access_token, 0)) + inflight
            self._save_accounts()
        return new_access_token

    def refresh_accounts(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts"): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                try:
                    account = future.result()
                except Exception as exc:
                    if self._is_rate_limit_error(exc):
                        self.mark_image_rate_limited(
                            token,
                            error=str(exc),
                            headers=getattr(exc, "headers", None),
                            body=getattr(exc, "body", None),
                        )
                    errors.append({"token": anonymize_token(token), "error": str(exc)})
                    continue
                if account is not None:
                    refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


account_service = AccountService(config.get_storage_backend())
