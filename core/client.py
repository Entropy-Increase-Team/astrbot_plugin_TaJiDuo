from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx


class TaJiDuoApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_code: Optional[int] = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_code = response_code
        self.response_body = response_body

    @property
    def is_auth_error(self) -> bool:
        return self.status_code == 401 or self.response_code == 401


class TaJiDuoClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        request_timeout_ms: int = 15000,
        community_task_timeout_ms: int = 300000,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.request_timeout_ms = max(int(request_timeout_ms or 15000), 1000)
        self.community_task_timeout_ms = max(
            int(community_task_timeout_ms or 300000), self.request_timeout_ms
        )
        self._client = httpx.AsyncClient(
            timeout=self.request_timeout_ms / 1000,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        fwt: str = "",
        platform_id: str = "",
        platform_user_id: str = "",
        timeout_ms: Optional[int] = None,
        with_api_key: bool = True,
    ) -> Any:
        headers: Dict[str, str] = {}
        if with_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key
        if fwt:
            headers["X-Framework-Token"] = fwt
        if platform_id:
            headers["X-Platform-Id"] = platform_id
        if platform_user_id:
            headers["X-Platform-User-Id"] = platform_user_id

        try:
            response = await self._client.request(
                method=method,
                url=f"{self.base_url}{path}",
                params={k: v for k, v in (params or {}).items() if v not in ("", None)},
                json={k: v for k, v in (json_data or {}).items() if v not in ("", None)},
                headers=headers,
                timeout=max(int(timeout_ms or self.request_timeout_ms), 1000) / 1000,
            )
        except httpx.HTTPError as exc:
            raise TaJiDuoApiError(str(exc)) from exc

        try:
            body = response.json()
        except ValueError:
            body = response.text

        if response.status_code >= 400:
            message = ""
            if isinstance(body, dict):
                message = str(body.get("message") or f"HTTP {response.status_code}")
            if not message:
                message = f"HTTP {response.status_code}"
            raise TaJiDuoApiError(
                message,
                status_code=response.status_code,
                response_code=body.get("code") if isinstance(body, dict) else None,
                response_body=body,
            )

        if isinstance(body, dict) and "code" in body:
            if body.get("code") != 0:
                raise TaJiDuoApiError(
                    str(body.get("message") or f"业务错误 {body.get('code')}"),
                    status_code=response.status_code,
                    response_code=body.get("code"),
                    response_body=body,
                )
            return body.get("data", {})
        return body

    async def send_captcha(self, phone: str, device_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/login/tajiduo/captcha/send",
            json_data={"phone": phone, "deviceId": device_id},
        )

    async def create_session(
        self,
        *,
        phone: str,
        captcha: str,
        device_id: str,
        platform_id: str,
        platform_user_id: str,
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/login/tajiduo/session",
            json_data={"phone": phone, "captcha": captcha, "deviceId": device_id},
            platform_id=platform_id,
            platform_user_id=platform_user_id,
        )

    async def refresh_session(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/login/tajiduo/refresh",
            fwt=fwt,
        )

    async def get_profile(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/login/tajiduo/profile",
            fwt=fwt,
        )

    async def list_accounts(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/login/tajiduo/accounts",
            fwt=fwt,
        )

    async def set_primary_account(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/login/tajiduo/accounts/primary",
            fwt=fwt,
        )

    async def delete_account(self, current_fwt: str, target_fwt: str = "") -> Dict[str, Any]:
        token = str(target_fwt or current_fwt).strip()
        return await self._request(
            "DELETE",
            f"/api/v1/login/tajiduo/accounts/{quote(token, safe='')}",
            fwt=str(current_fwt or token).strip(),
        )

    async def list_games(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games",
            fwt=fwt,
        )

    async def list_redeem_codes(
        self, game_code: str = "", include_expired: bool = False
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/redeem-codes",
            params={
                "gameCode": game_code,
                "includeExpired": include_expired if include_expired else None,
            },
        )

    async def list_shop_goods(
        self,
        fwt: str,
        *,
        version: int = 0,
        count: int = 20,
        tab: str = "all",
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/shop/goods",
            fwt=fwt,
            params={"version": version, "count": count, "tab": tab},
        )

    async def get_shop_goods_detail(self, goods_id: str, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/shop/goods/{quote(goods_id, safe='')}",
            fwt=fwt,
        )

    async def get_shop_coin_state(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/shop/coin/state",
            fwt=fwt,
        )

    async def get_shop_game_roles(self, fwt: str, game_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/shop/game-roles",
            fwt=fwt,
            params={"gameId": game_id},
        )

    async def shop_exchange(
        self,
        fwt: str,
        *,
        goods_id: str,
        game_id: str,
        role_id: str,
        count: int = 1,
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/games/shop/exchange",
            fwt=fwt,
            json_data={
                "goodsId": goods_id,
                "gameId": game_id,
                "roleId": role_id,
                "count": max(int(count or 1), 1),
            },
        )

    async def roles(self, game_key: str, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/roles",
            fwt=fwt,
        )

    async def bind_game_role(self, fwt: str, *, game_code: str, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/games/roles/bind",
            fwt=fwt,
            json_data={"gameCode": game_code, "roleId": role_id},
        )

    async def sign_reward_records(
        self,
        fwt: str,
        *,
        game_code: str = "",
        count: int = 10,
        version: int = 0,
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/sign/reward-records",
            fwt=fwt,
            params={
                "gameCode": game_code,
                "count": max(int(count or 10), 1),
                "version": max(int(version or 0), 0),
            },
        )

    async def sign_state(self, game_key: str, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/sign/state",
            fwt=fwt,
        )

    async def sign_rewards(
        self, game_key: str, fwt: str, *, role_id: str = ""
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/sign/rewards",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def sign_resign_info(
        self, game_key: str, fwt: str, *, role_id: str = ""
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/sign/resign-info",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def sign_game(self, game_key: str, fwt: str, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/games/{game_key}/sign/game",
            fwt=fwt,
            json_data={"roleId": role_id},
        )

    async def sign_resign(self, game_key: str, fwt: str, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/games/{game_key}/sign/resign",
            fwt=fwt,
            json_data={"roleId": role_id},
        )

    async def huanta_record_card(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/huanta/record-card",
            fwt=fwt,
        )

    async def huanta_role_record(
        self, fwt: str, *, role_id: str, record_type: str = "0"
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/huanta/role-record",
            fwt=fwt,
            params={"roleId": role_id, "type": record_type},
        )

    async def huanta_set_role_record_display(
        self,
        fwt: str,
        *,
        role_id: str,
        record_type: str,
        value: str = "",
        values: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"roleId": role_id, "type": record_type}
        if value:
            payload["value"] = value
        if values:
            payload["values"] = values
        return await self._request(
            "POST",
            "/api/v1/games/huanta/role-record/display",
            fwt=fwt,
            json_data=payload,
        )

    async def yihuan_record_card(self, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/record-card",
            fwt=fwt,
        )

    async def yihuan_role_home(self, fwt: str, *, role_id: str = "") -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/role-home",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def yihuan_characters(self, fwt: str, *, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/characters",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def yihuan_achieve_progress(self, fwt: str, *, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/achieve-progress",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def yihuan_area_progress(self, fwt: str, *, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/area-progress",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def yihuan_real_estate(self, fwt: str, *, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/real-estate",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def yihuan_vehicles(self, fwt: str, *, role_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/yihuan/vehicles",
            fwt=fwt,
            params={"roleId": role_id},
        )

    async def community_tasks(
        self, game_key: str, fwt: str, gid: int = 2
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/community/tasks",
            params={"gid": gid},
            fwt=fwt,
        )

    async def community_sign_state(self, game_key: str, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/community/sign/state",
            fwt=fwt,
        )

    async def community_exp_level(self, game_key: str, fwt: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/community/exp/level",
            fwt=fwt,
        )

    async def community_exp_records(
        self, game_key: str, fwt: str, *, count: int = 10, version: int = 0
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/community/exp/records",
            fwt=fwt,
            params={"count": max(int(count or 10), 1), "version": max(int(version or 0), 0)},
        )

    async def shop_coin_income_records(
        self, fwt: str, *, size: int = 10, version: int = 0
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/shop/coin/records/income",
            fwt=fwt,
            params={"size": max(int(size or 10), 1), "version": max(int(version or 0), 0)},
        )

    async def shop_coin_consume_records(
        self, fwt: str, *, size: int = 10, version: int = 0
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/games/shop/coin/records/consume",
            fwt=fwt,
            params={"size": max(int(size or 10), 1), "version": max(int(version or 0), 0)},
        )

    async def community_sign_submit(
        self,
        game_key: str,
        fwt: str,
        *,
        action_delay_ms: int,
        step_delay_ms: int,
    ) -> Dict[str, Any]:
        timeout_ms = max(
            self.community_task_timeout_ms,
            60000 + (max(int(action_delay_ms), 0) * 10) + (max(int(step_delay_ms), 0) * 5),
        )
        return await self._request(
            "POST",
            f"/api/v1/games/{game_key}/community/sign/all",
            fwt=fwt,
            json_data={
                "actionDelayMs": max(int(action_delay_ms), 0),
                "stepDelayMs": max(int(step_delay_ms), 0),
            },
            timeout_ms=timeout_ms,
        )

    async def community_sign_task(
        self, game_key: str, fwt: str, task_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/games/{game_key}/community/sign/tasks/{quote(task_id, safe='')}",
            fwt=fwt,
        )
