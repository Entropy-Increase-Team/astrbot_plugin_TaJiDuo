import asyncio
import json
import os
from typing import Any, Dict, List


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

    async def get_session(self, identity_key: str) -> Dict[str, Any]:
        async with self._lock:
            sessions = await self._read_json(self.sessions_path, {})
            return dict(sessions.get(identity_key, {}))

    async def save_session(self, identity_key: str, session: Dict[str, Any]) -> None:
        async with self._lock:
            sessions = await self._read_json(self.sessions_path, {})
            sessions[identity_key] = session
            await self._write_json(self.sessions_path, sessions)

    async def delete_session(self, identity_key: str) -> None:
        async with self._lock:
            sessions = await self._read_json(self.sessions_path, {})
            sessions.pop(identity_key, None)
            await self._write_json(self.sessions_path, sessions)

    async def list_sessions(self) -> List[Dict[str, Any]]:
        async with self._lock:
            sessions = await self._read_json(self.sessions_path, {})
            return list(sessions.values())

    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            state = await self._read_json(self.state_path, {})
            return state.get(key, default)

    async def set_state(self, key: str, value: Any) -> None:
        async with self._lock:
            state = await self._read_json(self.state_path, {})
            state[key] = value
            await self._write_json(self.state_path, state)
