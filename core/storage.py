import asyncio
import json
import os
from typing import Any, Dict, List, Optional


ACCOUNT_FIELD_MAP = {
    "tjdUid": "tjd_uid",
    "tgdUid": "tgd_uid",
    "uid": "tjd_uid",
    "deviceId": "device_id",
    "platformId": "platform_id",
    "platformUserId": "platform_user_id",
    "isPrimary": "is_primary",
    "isActive": "is_active",
    "lastRefreshAt": "last_refresh_at",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}

ACCOUNT_ALLOWED_KEYS = {
    "identity_key",
    "framework_token",
    "fwt",
    "tjd_uid",
    "tgd_uid",
    "username",
    "nickname",
    "avatar",
    "introduce",
    "device_id",
    "platform_id",
    "platform_user_id",
    "self_id",
    "user_id",
    "last_origin",
    "is_primary",
    "is_active",
    "created_at",
    "updated_at",
    "bind_time",
    "last_sync",
    "last_refresh_at",
}

ENTRY_META_KEYS = {
    "identity_key",
    "platform_id",
    "self_id",
    "user_id",
    "last_origin",
    "created_at",
    "updated_at",
}


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


class SessionStorage:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.sessions_path = os.path.join(self.data_dir, "sessions.json")
        self.state_path = os.path.join(self.data_dir, "state.json")
        self._lock = asyncio.Lock()
        os.makedirs(self.data_dir, exist_ok=True)

    @staticmethod
    def build_identity_key(platform_id: str, self_id: str, user_id: str) -> str:
        return f"{platform_id}:{self_id}:{user_id}"

    @staticmethod
    def _normalize_account(
        account: Dict[str, Any],
        *,
        identity_key: str,
        default_primary: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(account, dict):
            return None

        merged = dict(account)
        for source_key, target_key in ACCOUNT_FIELD_MAP.items():
            if source_key in merged and target_key not in merged:
                merged[target_key] = merged[source_key]

        fwt = _safe_str(merged.get("framework_token") or merged.get("fwt"))
        if not fwt:
            return None

        normalized: Dict[str, Any] = {
            "identity_key": identity_key,
            "framework_token": fwt,
            "fwt": fwt,
            "is_primary": bool(merged.get("is_primary", default_primary)),
            "is_active": bool(merged.get("is_active", True)),
        }

        for key in ACCOUNT_ALLOWED_KEYS:
            value = merged.get(key)
            if value is not None:
                normalized[key] = value

        normalized["identity_key"] = identity_key
        normalized["framework_token"] = fwt
        normalized["fwt"] = fwt
        if not normalized.get("bind_time"):
            normalized["bind_time"] = normalized.get("created_at") or normalized.get("updated_at")
        return normalized

    @classmethod
    def _normalize_accounts(
        cls, accounts: Any, *, identity_key: str
    ) -> List[Dict[str, Any]]:
        source = accounts if isinstance(accounts, list) else [accounts]
        dedup: Dict[str, Dict[str, Any]] = {}
        for index, item in enumerate(source):
            normalized = cls._normalize_account(
                item,
                identity_key=identity_key,
                default_primary=index == 0,
            )
            if not normalized or normalized.get("is_active") is False:
                continue
            old = dedup.get(normalized["fwt"], {})
            dedup[normalized["fwt"]] = {**old, **normalized}

        result = list(dedup.values())
        if result and not any(item.get("is_primary") for item in result):
            result[0]["is_primary"] = True
        if result:
            primary_index = next(
                (index for index, item in enumerate(result) if item.get("is_primary")),
                0,
            )
            for index, item in enumerate(result):
                item["is_primary"] = index == primary_index
        return result

    @classmethod
    def _normalize_entry(
        cls, identity_key: str, entry: Any
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None

        if "accounts" in entry:
            accounts = cls._normalize_accounts(entry.get("accounts") or [], identity_key=identity_key)
            if not accounts:
                return None
            normalized = {key: entry.get(key) for key in ENTRY_META_KEYS if entry.get(key) is not None}
            normalized["identity_key"] = identity_key
            normalized["accounts"] = accounts
            primary = next((item for item in accounts if item.get("is_primary")), accounts[0])
            normalized["primary_fwt"] = primary["fwt"]
            if not normalized.get("created_at"):
                normalized["created_at"] = primary.get("created_at") or primary.get("updated_at")
            if not normalized.get("updated_at"):
                normalized["updated_at"] = primary.get("updated_at") or primary.get("created_at")
            return normalized

        account = cls._normalize_account(entry, identity_key=identity_key, default_primary=True)
        if not account:
            return None
        return {
            "identity_key": identity_key,
            "platform_id": entry.get("platform_id"),
            "self_id": entry.get("self_id"),
            "user_id": entry.get("user_id"),
            "last_origin": entry.get("last_origin"),
            "created_at": entry.get("created_at") or account.get("created_at"),
            "updated_at": entry.get("updated_at") or account.get("updated_at"),
            "primary_fwt": account["fwt"],
            "accounts": [account],
        }

    async def _read_json(self, path: str, default: Any) -> Any:
        def _read() -> Any:
            if not os.path.exists(path):
                return default
            try:
                with open(path, "r", encoding="utf-8") as file:
                    return json.load(file)
            except (OSError, ValueError):
                return default

        return await asyncio.to_thread(_read)

    async def _write_json(self, path: str, data: Any) -> None:
        def _write() -> None:
            temp_path = f"{path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)

        await asyncio.to_thread(_write)

    async def _read_sessions(self) -> Dict[str, Dict[str, Any]]:
        sessions = await self._read_json(self.sessions_path, {})
        if not isinstance(sessions, dict):
            return {}
        normalized: Dict[str, Dict[str, Any]] = {}
        changed = False
        for identity_key, entry in sessions.items():
            next_entry = self._normalize_entry(identity_key, entry)
            if next_entry:
                normalized[identity_key] = next_entry
            if next_entry != entry:
                changed = True
        if changed:
            await self._write_json(self.sessions_path, normalized)
        return normalized

    @staticmethod
    def _build_entry(
        identity_key: str,
        accounts: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_accounts = SessionStorage._normalize_accounts(accounts, identity_key=identity_key)
        if not normalized_accounts:
            return None
        primary = next((item for item in normalized_accounts if item.get("is_primary")), normalized_accounts[0])
        entry: Dict[str, Any] = {
            "identity_key": identity_key,
            "accounts": normalized_accounts,
            "primary_fwt": primary["fwt"],
        }
        for key in ENTRY_META_KEYS:
            value = (metadata or {}).get(key)
            if value is not None:
                entry[key] = value
        if not entry.get("created_at"):
            entry["created_at"] = primary.get("created_at") or primary.get("updated_at")
        if not entry.get("updated_at"):
            entry["updated_at"] = primary.get("updated_at") or primary.get("created_at")
        return entry

    async def get_accounts(self, identity_key: str) -> List[Dict[str, Any]]:
        async with self._lock:
            sessions = await self._read_sessions()
            entry = sessions.get(identity_key) or {}
            return [dict(item) for item in entry.get("accounts") or []]

    async def save_accounts(
        self,
        identity_key: str,
        accounts: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            sessions = await self._read_sessions()
            merged_meta = dict(sessions.get(identity_key) or {})
            merged_meta.update(metadata or {})
            entry = self._build_entry(identity_key, accounts, merged_meta)
            if entry is None:
                sessions.pop(identity_key, None)
                await self._write_json(self.sessions_path, sessions)
                return []
            sessions[identity_key] = entry
            await self._write_json(self.sessions_path, sessions)
            return [dict(item) for item in entry["accounts"]]

    async def add_or_update_account(
        self,
        identity_key: str,
        account: Dict[str, Any],
        *,
        set_primary: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            sessions = await self._read_sessions()
            current = sessions.get(identity_key) or {}
            current_accounts = current.get("accounts") or []
            normalized = self._normalize_account(account, identity_key=identity_key, default_primary=set_primary)
            if not normalized:
                return {}

            next_accounts: List[Dict[str, Any]] = []
            matched = False
            for item in current_accounts:
                if _safe_str(item.get("fwt")) == normalized["fwt"]:
                    merged = {**item, **normalized}
                    if set_primary:
                        merged["is_primary"] = True
                    next_accounts.append(merged)
                    matched = True
                else:
                    next_accounts.append({**item, "is_primary": False if set_primary else item.get("is_primary")})
            if not matched:
                next_accounts.append(normalized)
                if set_primary:
                    for item in next_accounts[:-1]:
                        item["is_primary"] = False
                    next_accounts[-1]["is_primary"] = True

            merged_meta = dict(current)
            merged_meta.update(metadata or {})
            entry = self._build_entry(identity_key, next_accounts, merged_meta)
            if entry is None:
                return {}
            sessions[identity_key] = entry
            await self._write_json(self.sessions_path, sessions)
            primary = next((item for item in entry["accounts"] if item.get("is_primary")), entry["accounts"][0])
            return dict(primary if set_primary else normalized)

    async def get_primary_account(self, identity_key: str) -> Dict[str, Any]:
        async with self._lock:
            sessions = await self._read_sessions()
            entry = sessions.get(identity_key) or {}
            accounts = entry.get("accounts") or []
            primary = next((item for item in accounts if item.get("is_primary")), None)
            return dict(primary or {})

    async def set_primary_account(self, identity_key: str, fwt: str) -> Dict[str, Any]:
        async with self._lock:
            sessions = await self._read_sessions()
            entry = sessions.get(identity_key) or {}
            accounts = entry.get("accounts") or []
            token = _safe_str(fwt)
            if not token:
                return {}
            found = False
            next_accounts = []
            for item in accounts:
                is_target = _safe_str(item.get("fwt")) == token
                found = found or is_target
                next_accounts.append({**item, "is_primary": is_target})
            if not found:
                return {}
            next_entry = self._build_entry(identity_key, next_accounts, entry)
            if next_entry is None:
                return {}
            sessions[identity_key] = next_entry
            await self._write_json(self.sessions_path, sessions)
            return dict(next(item for item in next_entry["accounts"] if item.get("is_primary")))

    async def remove_account(self, identity_key: str, fwt: str) -> Dict[str, Any]:
        async with self._lock:
            sessions = await self._read_sessions()
            entry = sessions.get(identity_key) or {}
            accounts = entry.get("accounts") or []
            token = _safe_str(fwt)
            removed = next((dict(item) for item in accounts if _safe_str(item.get("fwt")) == token), {})
            if not removed:
                return {}
            next_accounts = [item for item in accounts if _safe_str(item.get("fwt")) != token]
            next_entry = self._build_entry(identity_key, next_accounts, entry)
            if next_entry is None:
                sessions.pop(identity_key, None)
            else:
                sessions[identity_key] = next_entry
            await self._write_json(self.sessions_path, sessions)
            return removed

    async def clear_accounts(self, identity_key: str) -> None:
        async with self._lock:
            sessions = await self._read_sessions()
            sessions.pop(identity_key, None)
            await self._write_json(self.sessions_path, sessions)

    async def get_session(self, identity_key: str) -> Dict[str, Any]:
        return await self.get_primary_account(identity_key)

    async def save_session(self, identity_key: str, session: Dict[str, Any]) -> None:
        await self.add_or_update_account(identity_key, session, set_primary=True, metadata=session)

    async def delete_session(self, identity_key: str) -> None:
        await self.clear_accounts(identity_key)

    async def list_sessions(self) -> List[Dict[str, Any]]:
        async with self._lock:
            sessions = await self._read_sessions()
            result: List[Dict[str, Any]] = []
            for entry in sessions.values():
                accounts = entry.get("accounts") or []
                primary = next((item for item in accounts if item.get("is_primary")), None)
                if primary:
                    result.append(dict(primary))
            return result

    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            state = await self._read_json(self.state_path, {})
            return state.get(key, default)

    async def set_state(self, key: str, value: Any) -> None:
        async with self._lock:
            state = await self._read_json(self.state_path, {})
            state[key] = value
            await self._write_json(self.state_path, state)
