import asyncio
import datetime as dt
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, At
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

from .core.client import TaJiDuoApiError, TaJiDuoClient
from .core.render import Renderer
from .core.storage import SessionStorage
from .core.web_login import TaJiDuoWebLoginServer

PHONE_RE = re.compile(r"^1\d{10}$")
CAPTCHA_RE = re.compile(r"^\d{6}$")
CAPTCHA_WAIT_TIMEOUT_MS = 300000

COMMUNITY_GAME_META = {
    "huanta": {
        "label": "幻塔",
        "logo": "img/bind/ht_link.png",
        "community_command": "幻塔社区签到",
        "query_command": "幻塔签到查询",
        "game_command": "幻塔签到",
    },
    "yihuan": {
        "label": "异环",
        "logo": "img/bind/yh_link.png",
        "community_command": "异环社区签到",
        "query_command": "异环签到查询",
        "game_command": "异环签到",
    },
}

SHOP_GAME_META = {
    "huanta": {
        "game_id": "1256",
        "label": "幻塔",
        "tabs": {"ht", "huanta", "tof"},
    },
    "yihuan": {
        "game_id": "1289",
        "label": "异环",
        "tabs": {"yh", "yihuan", "nte"},
    },
}


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def safe_str(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_phone(phone: str) -> str:
    text = safe_str(phone)
    if len(text) < 7:
        return text or "未填写"
    return f"{text[:3]} xxxx {text[-4:]}"


def mask_token(token: str) -> str:
    text = safe_str(token)
    if len(text) <= 12:
        return "******" if text else ""
    return f"{text[:6]}...{text[-6:]}"


def flatten_tasks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups = data.get("groups") or []
    tasks: List[Dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for item in group.get("items") or []:
            if isinstance(item, dict):
                tasks.append(item)
    return tasks


def format_task_value(task: Dict[str, Any]) -> str:
    complete_times = to_int(task.get("completeTimes"), 0)
    limit_times = to_int(task.get("limitTimes"), 0)
    target_times = to_int(task.get("targetTimes"), 0)
    target = limit_times or target_times
    if target > 0 and complete_times >= target:
        return "已完成"
    if target > 0:
        remaining = max(target - complete_times, 0)
        return f"{complete_times}/{target} | 剩余 {remaining}"
    return str(complete_times)


def build_render_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not tasks:
        return [
            {
                "index": "-",
                "name": "暂无任务数据",
                "value": "未返回",
            }
        ]
    result = []
    for index, task in enumerate(tasks, start=1):
        result.append(
            {
                "index": str(index),
                "name": safe_str(task.get("title") or task.get("taskKey"), "未命名任务"),
                "value": format_task_value(task),
            }
        )
    return result


def normalize_redeem_game_code(game_code: str) -> str:
    mapping = {
        "ht": "huanta",
        "huanta": "huanta",
        "tof": "huanta",
        "yh": "yihuan",
        "yihuan": "yihuan",
        "nte": "yihuan",
    }
    return mapping.get(safe_str(game_code).lower(), "")


def normalize_shop_tab(value: str) -> str:
    text = safe_str(value).lower()
    if text in {"", "all", "全部"}:
        return "all"
    if text in {"幻塔", "ht", "huanta", "tof"}:
        return "ht"
    if text in {"异环", "yh", "yihuan", "nte"}:
        return "yh"
    return text


def resolve_shop_game_from_tab(tab: str) -> Optional[Dict[str, str]]:
    normalized = normalize_shop_tab(tab)
    for item in SHOP_GAME_META.values():
        if normalized in item["tabs"]:
            return item
    return None


def is_redeem_expired(item: Dict[str, Any]) -> bool:
    expires_at = safe_str(item.get("expiresAt") or item.get("endAt") or item.get("endsAt"))
    if not expires_at:
        return False
    try:
        return dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= dt.datetime.now(
            dt.timezone.utc
        )
    except ValueError:
        return False


def to_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "list", "data", "detail", "records", "posts", "cards"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def data_body(value: Dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return value
    nested = value.get("data")
    if isinstance(nested, dict) and "data" in nested:
        return nested.get("data")
    return nested if nested is not None else value


def clean_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", safe_str(value)).strip()


def normalize_text(value: Any) -> str:
    return clean_spaces(value).lower().replace(" ", "")


def extract_role_id(value: Any) -> str:
    return safe_str(re.search(r"\d{5,}", safe_str(value)).group(0) if re.search(r"\d{5,}", safe_str(value)) else "")


def extract_after_prefix(text: str, prefixes: List[str]) -> str:
    raw = safe_str(text)
    for prefix in prefixes:
        if raw.startswith(prefix):
            return clean_spaces(raw[len(prefix) :])
    return raw


def yihuan_enum_label(value: Any) -> str:
    mapping = {
        "ITEM_QUALITY_ORANGE": "橙",
        "ITEM_QUALITY_PURPLE": "紫",
        "CHARACTER_ELEMENT_TYPE_COSMOS": "光",
        "CHARACTER_ELEMENT_TYPE_NATURE": "灵",
        "CHARACTER_ELEMENT_TYPE_INCANTATION": "咒",
        "CHARACTER_ELEMENT_TYPE_PSYCHE": "魂",
        "CHARACTER_ELEMENT_TYPE_LAKSHANA": "相",
        "CHARACTER_GROUP_TYPE_ONE": "分组1",
        "CHARACTER_GROUP_TYPE_TWO": "分组2",
        "CHARACTER_GROUP_TYPE_THREE": "分组3",
        "CHARACTER_GROUP_TYPE_FOUR": "分组4",
        "CHARACTER_GROUP_TYPE_FIVE": "分组5",
        "SSR": "SSR",
        "SR": "SR",
        "R": "R",
        "fire": "火",
        "water": "水",
        "wind": "风",
        "earth": "地",
        "thunder": "雷",
        "city": "都市",
        "wild": "野外",
    }
    text = safe_str(value)
    return mapping.get(text, text)


def yihuan_role_ring_color(item: Dict[str, Any]) -> str:
    quality = safe_str(item.get("quality")).upper()
    if quality in {"SSR", "ITEM_QUALITY_ORANGE"}:
        return "#f1c48a"
    if quality in {"SR", "ITEM_QUALITY_PURPLE"}:
        return "#b8a9ff"
    return "#d8e2ff"


def is_truthy_flag(value: Any) -> bool:
    return value in {True, 1, "1", "true", "True", "yes", "YES"}


def yihuan_progress_current(item: Dict[str, Any]) -> int:
    for key in ("progress", "current", "completeCnt", "ownCnt", "ownedCnt", "count"):
        value = item.get(key)
        if value not in (None, ""):
            return to_int(value, 0)
    detail = to_list(item.get("detail"))
    if detail:
        return sum(1 for child in detail if is_truthy_flag(child.get("own") or child.get("owned") or child.get("unlock") or child.get("has")))
    return 0


def yihuan_progress_total(item: Dict[str, Any]) -> int:
    for key in ("total", "target", "max", "limit"):
        value = item.get(key)
        if value not in (None, ""):
            return to_int(value, 0)
    detail = to_list(item.get("detail"))
    return len(detail)


def yihuan_progress_percent(current: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(int(round((current / total) * 100)), 100))


def percent_label(progress: Any, total: Any) -> str:
    current = float(progress or 0)
    target = float(total or 0)
    if target <= 0:
        return "0%"
    return f"{(current / target) * 100:.1f}".rstrip("0").rstrip(".") + "%"


def item_display_name(item: Dict[str, Any]) -> str:
    return safe_str(
        item.get("name")
        or item.get("showName")
        or item.get("title")
        or item.get("id")
        or item.get("ID"),
        "未命名",
    )


def normalize_role_info(item: Dict[str, Any]) -> Dict[str, str]:
    return {
        "roleId": safe_str(item.get("roleId") or item.get("id") or item.get("roleid")),
        "roleName": safe_str(item.get("roleName") or item.get("name") or item.get("rolename")),
        "serverName": safe_str(item.get("serverName") or item.get("servername")),
        "level": safe_str(item.get("lev") or item.get("level")),
    }


def pick_role(roles: List[Dict[str, Any]], selector: str = "") -> Optional[Dict[str, Any]]:
    if not roles:
        return None
    raw = clean_spaces(selector)
    if not raw:
        return roles[0]

    role_id = extract_role_id(raw)
    if role_id:
        for item in roles:
            if normalize_role_info(item)["roleId"] == role_id:
                return item

    lowered = normalize_text(raw)
    for item in roles:
        name = normalize_text(normalize_role_info(item)["roleName"])
        if name and (lowered == name or lowered in name or name in lowered):
            return item

    return roles[0]


def format_time_label(value: Any, default: str = "未记录") -> str:
    text = safe_str(value)
    if not text:
        return default
    try:
        if text.isdigit() and len(text) >= 10:
            stamp = int(text)
            if len(text) >= 13:
                stamp = stamp / 1000
            return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text


@register(
    "astrbot_plugin_TaJiDuo",
    "Codex",
    "塔吉多异环幻塔插件",
    "1.2.0",
    "https://github.com/openai/codex",
)
class TaJiDuoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.client = TaJiDuoClient(
            base_url=self.config.get("base_url", "https://tajiduo.shallow.ink"),
            api_key=self.config.get("api_key", ""),
            request_timeout_ms=self.config.get("request_timeout_ms", 15000),
            community_task_timeout_ms=self.config.get(
                "community_task_timeout_ms", 300000
            ),
        )
        self.storage = SessionStorage(str(StarTools.get_data_dir()))
        self.renderer = Renderer(
            res_path=os.path.abspath(os.path.dirname(__file__)),
            render_timeout=self.config.get("render_timeout", 30000),
        )
        self._loop = asyncio.get_running_loop()
        self.web_login_server = TaJiDuoWebLoginServer(
            on_login=self._schedule_web_login_complete,
            logger=logger,
        )
        self._configure_web_login_server()
        self.help_prefix_display = safe_str(self.config.get("help_prefix_display", ""))
        self.action_delay_ms = max(to_int(self.config.get("action_delay_ms", 3000), 3000), 0)
        self.step_delay_ms = max(to_int(self.config.get("step_delay_ms", 8000), 8000), 0)
        self.community_poll_interval_ms = max(
            to_int(self.config.get("community_poll_interval_ms", 2000), 2000), 200
        )
        self.community_task_timeout_ms = max(
            to_int(self.config.get("community_task_timeout_ms", 300000), 300000), 1000
        )
        self.auto_community_sign_enabled = bool(
            self.config.get("auto_community_sign_enabled", True)
        )
        self.auto_community_sign_time = safe_str(
            self.config.get("auto_community_sign_time", "00:20"), "00:20"
        )
        self.auto_community_sign_interval = max(
            to_int(self.config.get("auto_community_sign_interval", 3), 3), 0
        )
        self.auto_community_sign_notify_target = safe_str(
            self.config.get("auto_community_sign_notify_target", "")
        )

        self.pending_logins: Dict[str, Dict[str, Any]] = {}
        self.web_login_recalls: Dict[str, Dict[str, Any]] = {}
        self._pending_cleanup_task = asyncio.create_task(
            self._pending_login_cleanup_loop()
        )
        self._auto_community_task: Optional[asyncio.Task] = None
        if self.auto_community_sign_enabled:
            self._auto_community_task = asyncio.create_task(
                self._auto_community_sign_loop()
            )
        self._apply_command_metadata()

    def _command_desc_map(self) -> Dict[str, str]:
        return {
            "help_command": "平台账号｜查看塔吉多插件帮助与命令分组。",
            "login_command": "平台账号｜手机号验证码登录；空参时在已开启网页登录壳的情况下创建登录链接。",
            "web_login_command": "平台账号｜创建网页登录链接，支持群聊使用并艾特发起者。",
            "account_command": "平台账号｜查看当前主账号信息与登录状态。",
            "account_list_command": "平台账号｜查看当前 AstrBot 用户保存的塔吉多账号列表。",
            "switch_account_command": "平台账号｜按序号切换当前主账号。",
            "refresh_login_command": "平台账号｜刷新当前账号登录态。",
            "logout_command": "平台账号｜清空当前 AstrBot 用户本地保存的所有塔吉多账号。",
            "delete_account_command": "平台账号｜删除指定序号账号；不填序号时删除当前主账号。",
            "profile_command": "平台账号｜查询当前塔吉多账号资料卡。",
            "bindings_command": "平台账号｜查询塔吉多账号与幻塔、异环绑定概览。",
            "all_community_sign_command": "社区签到｜执行幻塔与异环社区签到。",
            "huanta_community_sign_command": "社区签到｜执行幻塔社区签到。",
            "yihuan_community_sign_command": "社区签到｜执行异环社区签到。",
            "all_community_query_command": "社区签到｜查询幻塔与异环社区任务状态。",
            "huanta_community_query_command": "社区签到｜查询幻塔社区任务状态。",
            "yihuan_community_query_command": "社区签到｜查询异环社区任务状态。",
            "huanta_record_command": "幻塔｜查询幻塔档案，可按角色与分类筛选。",
            "huanta_game_sign_command": "幻塔｜执行幻塔游戏签到，可附带角色序号、角色名或角色 ID。",
            "huanta_sign_state_command": "幻塔｜查询幻塔游戏签到状态。",
            "huanta_resign_command": "幻塔｜对指定角色执行补签。",
            "yihuan_personal_card_command": "异环｜查询异环档案卡，使用 personal_card 模板渲染。",
            "yihuan_characters_command": "异环｜查询角色列表与基础属性面板渲染。",
            "yihuan_achieve_command": "异环｜查询成就总览与各分类完成进度。",
            "yihuan_area_command": "异环｜查询区域探索度与分区进度。",
            "yihuan_real_estate_command": "异环｜查询房产收藏与家具拥有情况。",
            "yihuan_vehicles_command": "异环｜查询载具收藏与拥有进度。",
            "yihuan_game_sign_command": "异环｜执行异环游戏签到，可附带角色序号、角色名或角色 ID。",
            "yihuan_sign_state_command": "异环｜查询异环游戏签到状态。",
            "yihuan_resign_command": "异环｜对指定角色执行补签。",
            "redeem_codes_command": "商城查询｜查询塔吉多兑换码。",
            "shop_goods_command": "商城查询｜查询塔吉多商城商品列表。",
            "shop_coin_state_command": "商城查询｜查询塔吉多币状态。",
        }

    def _apply_command_metadata(self) -> None:
        desc_map = self._command_desc_map()
        module_path = self.__class__.__module__
        for handler in star_handlers_registry.get_handlers_by_module_name(module_path):
            desc = desc_map.get(handler.handler_name)
            if desc:
                handler.desc = desc

    def _is_plugin_command_message(self, text: str) -> bool:
        message = re.sub(r"\s+", " ", safe_str(text)).strip()
        if not message:
            return False
        for handler in star_handlers_registry.get_handlers_by_module_name(self.__class__.__module__):
            for filter_ in handler.event_filters:
                if not isinstance(filter_, CommandFilter):
                    continue
                for full_cmd in filter_.get_complete_command_names():
                    if message == full_cmd or message.startswith(f"{full_cmd} "):
                        return True
        return False

    def _identity_key(self, event: AstrMessageEvent) -> str:
        return SessionStorage.build_identity_key(
            event.get_platform_id(),
            event.get_self_id(),
            event.get_sender_id(),
        )

    async def _pending_login_cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                now_ms = int(dt.datetime.now().timestamp() * 1000)
                expired = [
                    key
                    for key, item in self.pending_logins.items()
                    if to_int(item.get("expires_at"), 0) <= now_ms
                ]
                for key in expired:
                    self.pending_logins.pop(key, None)
                self.web_login_server.purge_expired(max_age_seconds=600)
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    @filter.on_llm_request(priority=100000)
    async def suppress_llm_for_plugin_commands(self, event: AstrMessageEvent, req):
        if self._is_plugin_command_message(event.get_message_str()):
            event.should_call_llm(False)

    async def _auto_community_sign_loop(self) -> None:
        last_minute = ""
        startup_checked = False
        while True:
            try:
                now = dt.datetime.now()
                current_label = now.strftime("%H:%M")
                current_minute = now.strftime("%Y-%m-%d %H:%M")
                if not startup_checked:
                    startup_checked = True
                    if current_label >= self.auto_community_sign_time:
                        await self._run_auto_community_sign(now.date())
                if (
                    current_label == self.auto_community_sign_time
                    and last_minute != current_minute
                ):
                    await self._run_auto_community_sign(now.date())
                    last_minute = current_minute
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[TaJiDuo] auto community sign loop error: {exc}")
                await asyncio.sleep(30)

    async def _run_auto_community_sign(self, today: dt.date) -> None:
        today_str = today.isoformat()
        last_date = await self.storage.get_state("last_auto_community_sign_date", "")
        if last_date == today_str:
            return

        sessions = await self.storage.list_sessions()
        if not sessions:
            await self.storage.set_state("last_auto_community_sign_date", today_str)
            return

        lines = [f"塔吉多每日社区签到完成", f"执行账号数：{len(sessions)}"]
        success_count = 0
        for session in sessions:
            try:
                records = await self._run_all_community_sign_for_session(session)
                if all(item["success"] for item in records):
                    success_count += 1
                lines.append(self._summarize_session_records(session, records))
                if self.auto_community_sign_interval:
                    await asyncio.sleep(self.auto_community_sign_interval)
            except Exception as exc:
                lines.append(
                    f"{session.get('username') or session.get('tgd_uid') or session.get('identity_key')}: 失败 | {exc}"
                )

        lines.append(f"成功账号数：{success_count}/{len(sessions)}")
        await self.storage.set_state("last_auto_community_sign_date", today_str)

        if self.auto_community_sign_notify_target:
            chain = MessageChain()
            chain.chain.append(Plain("\n".join(lines)))
            try:
                await self.context.send_message(
                    self.auto_community_sign_notify_target,
                    chain,
                )
            except Exception as exc:
                logger.error(f"[TaJiDuo] auto notify failed: {exc}")

    async def _get_session(self, event: AstrMessageEvent) -> Dict[str, Any]:
        return await self.storage.get_primary_account(self._identity_key(event))

    async def _get_accounts(self, event: AstrMessageEvent) -> List[Dict[str, Any]]:
        return await self.storage.get_accounts(self._identity_key(event))

    def _session_metadata(self, event: AstrMessageEvent) -> Dict[str, Any]:
        return {
            "identity_key": self._identity_key(event),
            "platform_id": event.get_platform_id(),
            "self_id": event.get_self_id(),
            "user_id": event.get_sender_id(),
            "last_origin": event.unified_msg_origin,
            "updated_at": now_iso(),
        }

    def _web_login_config(self) -> Dict[str, Any]:
        port = max(to_int(self.config.get("login_server_port", 25188), 25188), 1)
        public_link = self._normalize_public_link(
            safe_str(self.config.get("login_server_public_link", "")),
            port,
        )
        return {
            "enabled": bool(self.config.get("login_server_enabled", False)),
            "port": port,
            "public_link": public_link,
        }

    def _normalize_public_link(self, public_link: str, port: int) -> str:
        raw = safe_str(public_link)
        if not raw:
            return f"http://127.0.0.1:{port}"

        candidate = raw if "://" in raw else f"http://{raw}"
        parsed = urlsplit(candidate)
        hostname = parsed.hostname or "127.0.0.1"
        scheme = parsed.scheme or "http"
        netloc = hostname
        if parsed.port:
            netloc = f"{hostname}:{parsed.port}"
        else:
            netloc = f"{hostname}:{port}"

        path = parsed.path.rstrip("/")
        return urlunsplit((scheme, netloc, path, "", ""))

    def _configure_web_login_server(self) -> None:
        web_config = self._web_login_config()
        self.web_login_server.configure(
            enabled=web_config["enabled"],
            port=web_config["port"],
            public_link=web_config["public_link"],
            base_url=self.config.get("base_url", "https://tajiduo.shallow.ink"),
            api_key=self.config.get("api_key", ""),
            timeout_ms=self.config.get("request_timeout_ms", 15000),
        )

    def _schedule_web_login_complete(self, payload: Dict[str, Any]) -> None:
        def _runner() -> None:
            asyncio.create_task(self._handle_web_login_complete(payload))

        self._loop.call_soon_threadsafe(_runner)

    def _account_uid(self, account: Dict[str, Any]) -> str:
        return safe_str(
            account.get("tjd_uid") or account.get("tgd_uid") or account.get("uid"),
            "未返回",
        )

    def _account_display_name(self, account: Dict[str, Any]) -> str:
        return safe_str(
            account.get("nickname")
            or account.get("username")
            or self._account_uid(account)
            or mask_token(account.get("fwt") or account.get("framework_token")),
            "未返回",
        )

    def _create_web_login_session(self, event: AstrMessageEvent) -> Dict[str, str]:
        return self.web_login_server.create_login_session(
            {
                "identity_key": self._identity_key(event),
                "platform_id": event.get_platform_id(),
                "self_id": event.get_self_id(),
                "user_id": event.get_sender_id(),
                "platform_user_id": event.get_sender_id(),
                "notify_target": event.unified_msg_origin,
            }
        )

    def _build_web_login_prompt(self, url: str) -> str:
        return "\n".join(
            [
                "塔吉多网页登录已创建",
                f"登录链接：{url}",
                "请在网页中发送验证码并完成登录。",
            ]
        )

    def _build_web_login_chain(self, event: AstrMessageEvent, text: str) -> List[Any]:
        chain = MessageChain()
        sender_id = safe_str(event.get_sender_id())
        if not event.is_private_chat() and sender_id:
            chain.chain.append(At(qq=sender_id))
            chain.chain.append(Plain(f"\n{text}"))
        else:
            chain.chain.append(Plain(text))
        return chain.chain

    def _merge_profile(self, account: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(account)
        uid = safe_str(profile.get("uid"))
        nickname = safe_str(profile.get("nickname"))
        avatar = safe_str(profile.get("avatar"))
        introduce = safe_str(profile.get("introduce"))
        if uid:
            merged["tjd_uid"] = uid
            merged["tgd_uid"] = safe_str(merged.get("tgd_uid") or uid)
        if nickname:
            merged["nickname"] = nickname
            merged["username"] = safe_str(merged.get("username") or nickname)
        if avatar:
            merged["avatar"] = avatar
        if introduce:
            merged["introduce"] = introduce
        return merged

    def _build_account_from_session(
        self,
        event: AstrMessageEvent,
        session_data: Dict[str, Any],
        *,
        phone: str = "",
        device_id: str = "",
    ) -> Dict[str, Any]:
        updated_at = now_iso()
        username = safe_str(session_data.get("username"))
        tjd_uid = safe_str(session_data.get("tjdUid") or session_data.get("uid"))
        tgd_uid = safe_str(session_data.get("tgdUid") or tjd_uid)
        return {
            **self._session_metadata(event),
            "framework_token": safe_str(session_data.get("fwt")),
            "fwt": safe_str(session_data.get("fwt")),
            "username": username,
            "nickname": safe_str(session_data.get("nickname") or username),
            "tjd_uid": tjd_uid,
            "tgd_uid": tgd_uid,
            "device_id": safe_str(session_data.get("deviceId") or device_id),
            "platform_id": safe_str(session_data.get("platformId") or event.get_platform_id()),
            "platform_user_id": safe_str(
                session_data.get("platformUserId") or event.get_sender_id()
            ),
            "phone": safe_str(phone),
            "created_at": updated_at,
            "updated_at": updated_at,
            "bind_time": updated_at,
            "last_sync": updated_at,
            "is_primary": True,
        }

    async def _save_session(self, event: AstrMessageEvent, session: Dict[str, Any]) -> None:
        merged = dict(session)
        merged.update(self._session_metadata(event))
        if not merged.get("created_at"):
            merged["created_at"] = merged["updated_at"]
        await self.storage.add_or_update_account(
            self._identity_key(event),
            merged,
            set_primary=bool(merged.get("is_primary", True)),
            metadata=merged,
        )

    async def _set_primary_session(self, event: AstrMessageEvent, fwt: str) -> Dict[str, Any]:
        return await self.storage.set_primary_account(self._identity_key(event), fwt)

    async def _remove_session_account(self, event: AstrMessageEvent, fwt: str) -> Dict[str, Any]:
        return await self.storage.remove_account(self._identity_key(event), fwt)

    async def _delete_session(self, event: AstrMessageEvent) -> None:
        await self.storage.clear_accounts(self._identity_key(event))
        self.pending_logins.pop(self._identity_key(event), None)

    async def _sync_remote_accounts(
        self,
        event: AstrMessageEvent,
        session: Dict[str, Any],
        *,
        fetch_profile: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        data = await self.client.list_accounts(session["fwt"])
        local_accounts = {
            safe_str(item.get("fwt") or item.get("framework_token")): item
            for item in await self._get_accounts(event)
        }

        merged_accounts: List[Dict[str, Any]] = []
        items = data.get("items") or []
        primary = data.get("primary") or {}
        primary_fwt = safe_str(primary.get("fwt"))
        sync_time = now_iso()
        for item in items:
            token = safe_str(item.get("fwt"))
            merged = dict(local_accounts.get(token) or {})
            merged.update(self._session_metadata(event))
            merged.update(
                {
                    "framework_token": token,
                    "fwt": token,
                    "tgd_uid": safe_str(item.get("tgdUid") or merged.get("tgd_uid")),
                    "tjd_uid": safe_str(item.get("tjdUid") or merged.get("tjd_uid")),
                    "device_id": safe_str(item.get("deviceId") or merged.get("device_id")),
                    "platform_id": safe_str(item.get("platformId") or merged.get("platform_id")),
                    "platform_user_id": safe_str(
                        item.get("platformUserId") or merged.get("platform_user_id")
                    ),
                    "created_at": safe_str(item.get("createdAt") or merged.get("created_at")),
                    "updated_at": safe_str(item.get("updatedAt") or sync_time),
                    "last_refresh_at": safe_str(
                        item.get("lastRefreshAt") or merged.get("last_refresh_at")
                    ),
                    "last_sync": sync_time,
                    "is_primary": token == primary_fwt,
                }
            )
            if not merged.get("username") and merged.get("nickname"):
                merged["username"] = merged["nickname"]
            merged_accounts.append(merged)

        if not merged_accounts:
            merged_accounts = await self._get_accounts(event)

        selected = next(
            (
                item
                for item in merged_accounts
                if safe_str(item.get("fwt")) == primary_fwt and primary_fwt
            ),
            merged_accounts[0] if merged_accounts else {},
        )

        profile: Dict[str, Any] = {}
        if fetch_profile and selected.get("fwt"):
            profile = await self.client.get_profile(selected["fwt"])
            selected = self._merge_profile(selected, profile)
            merged_accounts = [
                selected if safe_str(item.get("fwt")) == safe_str(selected.get("fwt")) else item
                for item in merged_accounts
            ]

        if merged_accounts:
            await self.storage.save_accounts(
                self._identity_key(event),
                merged_accounts,
                metadata=self._session_metadata(event),
            )
            selected = await self._get_session(event)

        return merged_accounts, selected, profile

    async def _send_origin_text(self, target: str, text: str) -> None:
        if not target:
            return
        chain = MessageChain()
        chain.chain.append(Plain(text))
        await self.context.send_message(target, chain)

    async def _send_and_get_msg_id(
        self, event: AstrMessageEvent, text: str, *, mention_sender: bool = False
    ) -> Tuple[Optional[Any], Optional[int]]:
        try:
            if event.get_platform_name() == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    group_id = event.get_group_id()
                    sender_id = safe_str(event.get_sender_id())
                    message: Any = text
                    if mention_sender and group_id and sender_id:
                        message = [
                            {"type": "at", "data": {"qq": sender_id}},
                            {"type": "text", "data": {"text": f"\n{text}"}},
                        ]
                    if group_id:
                        result = await client.send_group_msg(
                            group_id=int(group_id),
                            message=message,
                        )
                    else:
                        result = await client.send_private_msg(
                            user_id=int(event.get_sender_id()),
                            message=message,
                        )
                    if result:
                        return client, int(result.get("message_id"))
        except Exception as exc:
            logger.warning(f"[TaJiDuo] 获取消息 ID 失败: {exc}")
        return None, None

    def _schedule_recall(self, client: Any, message_id: int, delay: float) -> asyncio.Task:
        async def _do_recall() -> None:
            await asyncio.sleep(delay)
            try:
                await client.delete_msg(message_id=message_id)
            except Exception as exc:
                logger.warning(f"[TaJiDuo] 撤回消息失败: {exc}")

        return asyncio.create_task(_do_recall())

    async def _recall_web_login_message(self, session_id: str) -> None:
        tracked = self.web_login_recalls.pop(session_id, None)
        if not tracked:
            return
        recall_task = tracked.get("task")
        if recall_task and not recall_task.done():
            recall_task.cancel()
        client = tracked.get("client")
        message_id = tracked.get("message_id")
        if client and message_id:
            try:
                await client.delete_msg(message_id=int(message_id))
            except Exception as exc:
                logger.warning(f"[TaJiDuo] 登录完成后撤回消息失败: {exc}")

    async def _handle_web_login_complete(self, payload: Dict[str, Any]) -> None:
        account = dict(payload.get("account") or {})
        identity_key = safe_str(payload.get("identity_key"))
        session_id = safe_str(payload.get("id"))
        if not account or not identity_key:
            return

        if session_id:
            await self._recall_web_login_message(session_id)

        sync_time = now_iso()
        account.update(
            {
                "identity_key": identity_key,
                "platform_id": safe_str(payload.get("platform_id")),
                "self_id": safe_str(payload.get("self_id")),
                "user_id": safe_str(payload.get("user_id")),
                "platform_user_id": safe_str(
                    account.get("platform_user_id") or payload.get("platform_user_id")
                ),
                "last_origin": safe_str(payload.get("notify_target")),
                "created_at": safe_str(account.get("created_at") or sync_time),
                "updated_at": sync_time,
                "bind_time": safe_str(account.get("bind_time") or sync_time),
                "last_sync": sync_time,
                "is_primary": True,
            }
        )
        await self.storage.add_or_update_account(
            identity_key,
            account,
            set_primary=True,
            metadata={
                "identity_key": identity_key,
                "platform_id": safe_str(payload.get("platform_id")),
                "self_id": safe_str(payload.get("self_id")),
                "user_id": safe_str(payload.get("user_id")),
                "last_origin": safe_str(payload.get("notify_target")),
                "updated_at": sync_time,
            },
        )

        try:
            await self._send_origin_text(
                safe_str(payload.get("notify_target")),
                "\n".join(
                    [
                        "塔吉多网页登录成功",
                        f"昵称：{self._account_display_name(account)}",
                        f"塔吉多UID：{self._account_uid(account)}",
                    ]
                ),
            )
        except Exception as exc:
            logger.error(f"[TaJiDuo] web login notify failed: {exc}")

    def _auth_hint(self) -> str:
        if self.web_login_server.is_enabled():
            return "当前登录态已失效，请重新私聊发送：tjd登录 【手机号】；如需在群聊重新登录，请使用 tjd网页登录"
        return "当前登录态已失效，请重新私聊发送：tjd登录 【手机号】"

    def _login_hint(self) -> str:
        if self.web_login_server.is_enabled():
            return "当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】；如需在群聊登录，请使用 tjd网页登录"
        return "当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】"

    def _check_admin(self, event: AstrMessageEvent) -> bool:
        return event.is_admin()

    def _build_help_sections(self) -> List[Dict[str, Any]]:
        prefix = self.help_prefix_display

        def cmd(text: str) -> str:
            return f"{prefix}{text}"

        return [
            {
                "title": "平台账号",
                "items": [
                    {"command": cmd("tjd帮助"), "description": "查看插件帮助"},
                    {
                        "command": cmd("tjd登录 【手机号】"),
                        "description": "发送验证码并等待下一条 6 位验证码",
                    },
                    {"command": cmd("tjd网页登录"), "description": "创建网页登录链接"},
                    {"command": cmd("tjd账号"), "description": "查看当前登录账号"},
                    {"command": cmd("tjd账号列表"), "description": "查看当前已保存账号列表"},
                    {"command": cmd("tjd切换账号 [序号]"), "description": "切换当前主账号"},
                    {"command": cmd("tjd刷新登录"), "description": "刷新当前登录账号"},
                    {"command": cmd("tjd删除账号 [序号]"), "description": "删除指定账号，默认删除当前账号"},
                    {"command": cmd("tjd退出登录"), "description": "清空本地已保存账号"},
                    {"command": cmd("tjd绑定列表"), "description": "查看账号绑定概览"},
                    {"command": cmd("tjd资料"), "description": "查看当前塔吉多资料卡"},
                ],
            },
            {
                "title": "社区签到",
                "items": [
                    {"command": cmd("tjd社区签到"), "description": "依次执行幻塔与异环社区签到"},
                    {"command": cmd("幻塔社区签到"), "description": "执行幻塔社区签到"},
                    {"command": cmd("异环社区签到"), "description": "执行异环社区签到"},
                    {"command": cmd("tjd签到查询"), "description": "查看全部社区签到任务状态"},
                    {"command": cmd("幻塔签到查询"), "description": "查看幻塔社区签到任务状态"},
                    {"command": cmd("异环签到查询"), "description": "查看异环社区签到任务状态"},
                ],
            },
            {
                "title": "幻塔",
                "items": [
                    {"command": cmd("幻塔档案 [角色] [分类]"), "description": "查看幻塔角色档案，可切换武器/拟态/时装/载具"},
                    {"command": cmd("幻塔签到 [角色]"), "description": "执行幻塔游戏签到"},
                    {"command": cmd("幻塔签到状态"), "description": "查看幻塔游戏签到状态"},
                    {"command": cmd("幻塔补签 [角色ID]"), "description": "执行幻塔单角色补签"},
                ],
            },
            {
                "title": "异环",
                "items": [
                    {"command": cmd("异环档案 [角色]"), "description": "查看异环档案卡，使用 personal_card 模板"},
                    {"command": cmd("异环角色 [角色]"), "description": "查看异环角色列表与基础属性"},
                    {"command": cmd("异环成就 [角色]"), "description": "查看异环成就总览与分类进度"},
                    {"command": cmd("异环探索 [角色]"), "description": "查看异环区域探索度"},
                    {"command": cmd("异环房产 [角色]"), "description": "查看异环房产收藏"},
                    {"command": cmd("异环载具 [角色]"), "description": "查看异环载具收藏"},
                    {"command": cmd("异环签到 [角色]"), "description": "执行异环游戏签到"},
                    {"command": cmd("异环签到状态"), "description": "查看异环游戏签到状态"},
                    {"command": cmd("异环补签 [角色ID]"), "description": "执行异环单角色补签"},
                ],
            },
            {
                "title": "商城查询",
                "items": [
                    {"command": cmd("tjd兑换码"), "description": "查看全部兑换码"},
                    {"command": cmd("tjd商城 [分区] [数量]"), "description": "查看商城商品"},
                    {"command": cmd("tjd币"), "description": "查看塔吉多币状态"},
                ],
            },
        ]

    def _build_help_notes(self) -> List[str]:
        return [
            "塔吉多是平台层；幻塔与异环是游戏层。当前只对外注册核心命令，长尾接口能力暂不直接挂到插件指令面。",
            "手机号验证码登录仅支持私聊；网页登录和非隐私查询命令可以在群聊使用。",
            "已保存账号会在每天 00:20 自动执行社区签到。",
            "异环档案当前固定使用 render/personal_card 模板。",
        ]

    async def _render_menu(self) -> Optional[str]:
        return await self.renderer.render_html(
            "render/menu/index.html",
            {
                "pageTitle": "TaJiDuo 插件帮助",
                "pageSubtitle": "AstrBot 渲染版命令菜单",
                "notesTitle": "说明",
                "notes": self._build_help_notes(),
                "menuSections": self._build_help_sections(),
                "pluResPath": self.renderer.get_res_path(""),
            },
            {"viewport_width": 1100, "viewport_height": 1800},
        )

    def _help_text(self) -> str:
        lines = ["塔吉多插件帮助"]
        for group in self._build_help_sections():
            lines.append("")
            lines.append(f"{group['title']}：")
            for item in group["items"]:
                lines.append(f"{item['command']} {item['description']}")
        lines.append("")
        lines.append("说明：")
        for index, note in enumerate(self._build_help_notes(), start=1):
            lines.append(f"{index}. {note}")
        return "\n".join(lines)

    def _session_summary_lines(self, session: Dict[str, Any]) -> List[str]:
        return [
            f"昵称：{self._account_display_name(session)}",
            f"塔吉多UID：{self._account_uid(session)}",
            f"更新时间：{safe_str(session.get('last_refresh_at') or session.get('updated_at'), '未记录')}",
        ]

    async def _fetch_role_binding(self, session: Dict[str, Any], game_key: str) -> Dict[str, Any]:
        game_meta = COMMUNITY_GAME_META[game_key]
        placeholder_avatar = self.renderer.get_res_path("img/ui/logo.png")
        binding = {
            "gameName": game_meta["label"],
            "logo": self.renderer.get_res_path(game_meta["logo"]),
            "hasAccount": False,
            "avatar": placeholder_avatar,
            "name": "",
            "level": "-",
            "server": "未绑定",
            "statusText": "当前塔吉多账号下暂无该游戏角色数据",
            "stats": [],
            "emptyDescription": "当前塔吉多账号下暂无该游戏角色绑定",
        }
        try:
            data = await self.client.roles(game_key, session["fwt"])
        except TaJiDuoApiError:
            return binding

        roles = data.get("roles") or []
        if not roles:
            return binding

        first_role = roles[0]
        role_info = normalize_role_info(first_role)
        role_name = safe_str(role_info["roleName"], "未命名角色")
        level = safe_str(role_info["level"], "-")
        server = safe_str(role_info["serverName"], "已绑定")
        role_id = safe_str(role_info["roleId"], "未返回")
        avatar = placeholder_avatar
        stats = [
            {"label": "角色ID", "value": role_id},
            {"label": "等级", "value": level},
            {"label": "服务器", "value": server},
            {"label": "角色数", "value": str(len(roles))},
        ]
        status_text = f"共 {len(roles)} 个角色"

        if game_key == "yihuan" and role_id:
            try:
                role_home = await self.client.yihuan_role_home(session["fwt"], role_id=role_id)
            except TaJiDuoApiError:
                role_home = {}
            home_data = data_body(role_home) or {}
            avatar = self._yihuan_primary_avatar_url(home_data)
            role_name = safe_str(home_data.get("rolename"), role_name)
            level = safe_str(home_data.get("lev"), level)
            server = safe_str(home_data.get("servername"), server)
            achieve = home_data.get("achieveProgress") or {}
            area_total = sum(yihuan_progress_total(item) for item in to_list(home_data.get("areaProgress")))
            area_current = sum(yihuan_progress_current(item) for item in to_list(home_data.get("areaProgress")))
            stats = [
                {"label": "角色ID", "value": safe_str(home_data.get("roleid"), role_id)},
                {"label": "登录天数", "value": safe_str(home_data.get("roleloginDays"), "-")},
                {
                    "label": "成就",
                    "value": f"{safe_str(achieve.get('achievementCnt'), '0')}/{safe_str(achieve.get('total'), '0')}",
                },
                {"label": "探索度", "value": percent_label(area_current, area_total)},
            ]
            status_text = "已绑定账号，可查看角色档案与探索信息"
        elif game_key == "huanta" and role_id:
            try:
                record_card = await self.client.huanta_role_record(
                    session["fwt"], role_id=role_id, record_type="0"
                )
            except TaJiDuoApiError:
                record_card = {}
            record = data_body(record_card) or {}
            record = record.get("record") if isinstance(record.get("record"), dict) else record
            role_name = safe_str(record.get("rolename"), role_name)
            level = safe_str(record.get("lev"), level)
            server = safe_str(record.get("groupname"), server)
            avatar = safe_str(record.get("avatar"), avatar)
            stats = [
                {"label": "角色ID", "value": safe_str(record.get("roleid"), role_id)},
                {"label": "等级", "value": level},
                {"label": "服务器", "value": server},
                {"label": "最高GS", "value": safe_str(record.get("maxgs"), "-")},
            ]
            status_text = "已绑定账号，可直接执行社区与游戏签到"

        binding.update(
            {
                "hasAccount": True,
                "avatar": avatar,
                "name": role_name,
                "level": level,
                "server": server,
                "statusText": status_text,
                "stats": stats,
            }
        )
        return binding

    async def _build_bindings_render_data(
        self, event: AstrMessageEvent, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        accounts = await self.client.list_accounts(session["fwt"])
        try:
            profile = await self.client.get_profile(session["fwt"])
        except TaJiDuoApiError:
            profile = {}
        primary = accounts.get("primary") or {}
        items = accounts.get("items") or []
        tgd_uid = safe_str(primary.get("tgdUid") or session.get("tgd_uid"), "未返回")
        game_bindings = [
            await self._fetch_role_binding(session, "huanta"),
            await self._fetch_role_binding(session, "yihuan"),
        ]
        bound_games = sum(1 for item in game_bindings if item.get("hasAccount"))
        account_fallback_avatar = next(
            (
                safe_str(item.get("avatar"))
                for item in game_bindings
                if item.get("hasAccount") and safe_str(item.get("avatar"))
            ),
            self.renderer.get_res_path("img/ui/logo.png"),
        )
        account_profile = {
            "avatar": self._account_avatar_url(session, profile),
            "fallbackAvatar": account_fallback_avatar,
            "name": self._account_display_name(profile or session),
            "uid": safe_str(profile.get("uid") or tgd_uid, tgd_uid),
            "introduce": self._profile_signature(session, profile),
            "accountCount": str(len(items) or 1),
            "boundGames": str(bound_games),
            "phone": format_phone(
                safe_str(primary.get("phone") or session.get("phone"), "未公开")
            ),
            "lastRefreshAt": safe_str(
                primary.get("lastRefreshAt")
                or session.get("last_refresh_at")
                or session.get("updated_at"),
                "未记录",
            ),
        }
        summary_fields = [
            {"label": "账号名称", "value": account_profile["name"]},
            {"label": "塔吉多 UID", "value": account_profile["uid"]},
            {"label": "绑定手机", "value": account_profile["phone"]},
            {"label": "已绑定游戏", "value": account_profile["boundGames"]},
            {"label": "已保存账号", "value": account_profile["accountCount"]},
            {"label": "最近刷新", "value": account_profile["lastRefreshAt"]},
        ]
        return {
            "pageTitle": "塔吉多账号绑定",
            "accountProfile": account_profile,
            "accountFields": summary_fields,
            "gameBindings": game_bindings,
            "pluResPath": self.renderer.get_res_path(""),
            "userName": event.get_sender_name() or self._account_display_name(session),
        }

    async def _render_bindings(
        self, event: AstrMessageEvent, session: Dict[str, Any]
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/bindings/index.html",
            await self._build_bindings_render_data(event, session),
            {"viewport_width": 1100, "viewport_height": 1600},
        )

    async def _poll_community_task(
        self, game_key: str, fwt: str, task_id: str
    ) -> Dict[str, Any]:
        deadline = dt.datetime.now().timestamp() + (
            max(self.community_task_timeout_ms, 1000) / 1000
        )
        while dt.datetime.now().timestamp() < deadline:
            data = await self.client.community_sign_task(game_key, fwt, task_id)
            status = safe_str(data.get("status")).lower()
            if data.get("completed") or status in {"finished", "failed"}:
                return data
            await asyncio.sleep(self.community_poll_interval_ms / 1000)
        raise TaJiDuoApiError("社区任务执行超时")

    async def _execute_community_sign_for_game(
        self, session: Dict[str, Any], game_key: str
    ) -> Dict[str, Any]:
        game_meta = COMMUNITY_GAME_META[game_key]
        before_raw = await self.client.community_tasks(game_key, session["fwt"], gid=2)
        before_tasks = flatten_tasks(before_raw)
        submit_data = await self.client.community_sign_submit(
            game_key,
            session["fwt"],
            action_delay_ms=self.action_delay_ms,
            step_delay_ms=self.step_delay_ms,
        )
        if submit_data.get("completed") or safe_str(submit_data.get("status")).lower() in {
            "finished",
            "failed",
        }:
            final_data = submit_data
        else:
            task_id = safe_str(submit_data.get("taskId"))
            if not task_id:
                raise TaJiDuoApiError("社区任务未返回 taskId")
            final_data = await self._poll_community_task(game_key, session["fwt"], task_id)

        result_item = (final_data.get("result") or {}).get("item") or {}
        after_tasks = result_item.get("tasksAfter") or flatten_tasks(
            await self.client.community_tasks(game_key, session["fwt"], gid=2)
        )
        before_tasks = result_item.get("tasksBefore") or before_tasks
        success = bool(final_data.get("success"))
        message = safe_str(
            result_item.get("message") or final_data.get("message"), "执行完成"
        )
        return {
            "gameKey": game_key,
            "gameName": game_meta["label"],
            "success": success,
            "status": "success" if success else "failed",
            "resultText": f"{'成功' if success else '失败'} | {message}",
            "beforeTitle": "执行前任务",
            "afterTitle": "执行后任务",
            "beforeTasks": build_render_tasks(before_tasks),
            "afterTasks": build_render_tasks(after_tasks),
        }

    async def _query_community_for_game(
        self, session: Dict[str, Any], game_key: str
    ) -> Dict[str, Any]:
        game_meta = COMMUNITY_GAME_META[game_key]
        tasks_raw = await self.client.community_tasks(game_key, session["fwt"], gid=2)
        tasks = flatten_tasks(tasks_raw)
        state_raw = await self.client.community_sign_state(game_key, session["fwt"])
        signed = bool(state_raw.get("signed"))
        result_text = "成功 | 今日已签到" if signed else "成功 | 今日未签到"
        render_tasks = build_render_tasks(tasks)
        return {
            "gameKey": game_key,
            "gameName": game_meta["label"],
            "success": True,
            "status": "success",
            "resultText": result_text,
            "beforeTitle": "当前任务",
            "afterTitle": "当前任务",
            "beforeTasks": render_tasks,
            "afterTasks": render_tasks,
        }

    async def _render_signin(
        self,
        session: Dict[str, Any],
        records: List[Dict[str, Any]],
        *,
        page_title: str,
    ) -> Optional[str]:
        success_count = sum(1 for item in records if item["success"])
        if success_count == len(records):
            status_text = "执行成功"
            description = "任务处理完成"
        elif success_count > 0:
            status_text = "部分成功"
            description = "部分任务处理成功，请查看分项结果"
        else:
            status_text = "执行失败"
            description = "任务处理失败，请检查登录状态或稍后重试"
        return await self.renderer.render_html(
            "render/signin/index.html",
            {
                "pageTitle": page_title,
                "pageSubtitle": f"{session.get('username') or 'TaJiDuo'} | UID {session.get('tgd_uid') or '未返回'}",
                "summary": {
                    "statusText": status_text,
                    "description": description,
                },
                "records": records,
                "pluResPath": self.renderer.get_res_path(""),
            },
            {"viewport_width": 1300, "viewport_height": 1800},
        )

    def _sign_text(
        self, session: Dict[str, Any], page_title: str, records: List[Dict[str, Any]]
    ) -> str:
        lines = [
            page_title,
            f"账号：{session.get('username') or '未返回'}",
            f"UID：{session.get('tgd_uid') or '未返回'}",
        ]
        for record in records:
            lines.append("")
            lines.append(f"{record['gameName']}执行完成")
            lines.append(f"结果：{record['resultText']}")
            lines.append(f"{record['beforeTitle']}：")
            for task in record["beforeTasks"]:
                lines.append(f"{task['index']}. {task['name']}：{task['value']}")
            lines.append(f"{record['afterTitle']}：")
            for task in record["afterTasks"]:
                lines.append(f"{task['index']}. {task['name']}：{task['value']}")
        return "\n".join(lines)

    async def _community_sign(
        self, event: AstrMessageEvent, game_keys: List[str], page_title: str
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】")
            return
        yield event.plain_result(f"{page_title}任务已开始，社区任务耗时较长，请等待结果。")
        records: List[Dict[str, Any]] = []
        for game_key in game_keys:
            try:
                records.append(await self._execute_community_sign_for_game(session, game_key))
            except TaJiDuoApiError as exc:
                if exc.is_auth_error:
                    yield event.plain_result(self._auth_hint())
                    return
                records.append(
                    {
                        "gameKey": game_key,
                        "gameName": COMMUNITY_GAME_META[game_key]["label"],
                        "success": False,
                        "status": "failed",
                        "resultText": f"失败 | {exc}",
                        "beforeTitle": "执行前任务",
                        "afterTitle": "执行后任务",
                        "beforeTasks": build_render_tasks([]),
                        "afterTasks": build_render_tasks([]),
                    }
                )
        image_path = await self._render_signin(session, records, page_title=page_title)
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._sign_text(session, page_title, records))

    async def _community_query(
        self, event: AstrMessageEvent, game_keys: List[str], page_title: str
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】")
            return
        records: List[Dict[str, Any]] = []
        for game_key in game_keys:
            try:
                records.append(await self._query_community_for_game(session, game_key))
            except TaJiDuoApiError as exc:
                if exc.is_auth_error:
                    yield event.plain_result(self._auth_hint())
                    return
                records.append(
                    {
                        "gameKey": game_key,
                        "gameName": COMMUNITY_GAME_META[game_key]["label"],
                        "success": False,
                        "status": "failed",
                        "resultText": f"失败 | {exc}",
                        "beforeTitle": "当前任务",
                        "afterTitle": "当前任务",
                        "beforeTasks": build_render_tasks([]),
                        "afterTasks": build_render_tasks([]),
                    }
                )
        image_path = await self._render_signin(session, records, page_title=page_title)
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._sign_text(session, page_title, records))

    def _resolve_role(
        self, roles_data: Dict[str, Any], selector: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        roles = roles_data.get("roles") or []
        bind_role = safe_str(roles_data.get("bindRole"))
        selector = safe_str(selector)
        if not roles:
            return None, "当前账号下未查询到角色"

        if selector:
            if selector.isdigit():
                index = int(selector)
                if 1 <= index <= len(roles):
                    return roles[index - 1], "按序号选择"
            for role in roles:
                if safe_str(role.get("roleId")) == selector:
                    return role, "按角色ID选择"
            for role in roles:
                role_name = safe_str(role.get("roleName") or role.get("name"))
                if selector.lower() in role_name.lower():
                    return role, "按角色名选择"
            return None, "未匹配到指定角色"

        if bind_role:
            for role in roles:
                if safe_str(role.get("roleId")) == bind_role:
                    return role, "按绑定角色选择"
        if len(roles) == 1:
            return roles[0], "唯一角色"
        return None, "当前账号下存在多个角色，请追加 角色序号 / 角色ID / 角色名"

    async def _execute_game_sign_for_game(
        self, session: Dict[str, Any], game_key: str, selector: str = ""
    ) -> Dict[str, Any]:
        game_meta = COMMUNITY_GAME_META[game_key]
        state_before = await self.client.sign_state(game_key, session["fwt"])
        if state_before.get("todaySign") is True:
            return {
                "gameKey": game_key,
                "gameName": game_meta["label"],
                "success": True,
                "skipped": True,
                "message": "今天已经签到过了",
                "role": None,
                "stateAfter": state_before,
            }

        roles_data = await self.client.roles(game_key, session["fwt"])
        role, reason = self._resolve_role(roles_data, selector)
        if not role:
            return {
                "gameKey": game_key,
                "gameName": game_meta["label"],
                "success": False,
                "skipped": False,
                "message": reason,
                "role": None,
                "stateAfter": state_before,
                "roles": roles_data.get("roles") or [],
            }

        sign_data = await self.client.sign_game(
            game_key, session["fwt"], safe_str(role.get("roleId"))
        )
        state_after = await self.client.sign_state(game_key, session["fwt"])
        success = sign_data.get("success", True) is not False
        message = safe_str(sign_data.get("message"), "签到完成")
        return {
            "gameKey": game_key,
            "gameName": game_meta["label"],
            "success": success,
            "skipped": False,
            "message": message,
            "role": role,
            "stateAfter": state_after,
        }

    def _format_role_lines(self, roles: List[Dict[str, Any]]) -> List[str]:
        lines = ["可选角色："]
        for index, role in enumerate(roles, start=1):
            lines.append(
                f"{index}. {safe_str(role.get('roleName') or role.get('name'), '未命名角色')} | "
                f"Lv.{safe_str(role.get('lev') or role.get('level'), '-')} | "
                f"{safe_str(role.get('serverName'), '未知服务器')} | "
                f"ID {safe_str(role.get('roleId'), '未返回')}"
            )
        return lines

    def _build_game_sign_text(self, session: Dict[str, Any], result: Dict[str, Any]) -> str:
        lines = [
            f"{result['gameName']}游戏签到",
            f"账号：{session.get('username') or '未返回'}",
            f"UID：{session.get('tgd_uid') or '未返回'}",
            f"结果：{'成功' if result.get('success') else '失败'} | {result.get('message') or '未返回'}",
        ]
        role = result.get("role")
        if role:
            lines.append(
                f"角色：{safe_str(role.get('roleName') or role.get('name'), '未命名角色')} | "
                f"Lv.{safe_str(role.get('lev') or role.get('level'), '-')} | "
                f"{safe_str(role.get('serverName'), '未知服务器')}"
            )
        state = result.get("stateAfter") or {}
        if state:
            lines.append(
                f"状态：{'今日已签到' if state.get('todaySign') else '今日未签到'} | "
                f"累计天数 {safe_str(state.get('days'), '未返回')}"
            )
        if result.get("roles"):
            lines.extend(self._format_role_lines(result["roles"]))
        return "\n".join(lines)

    async def _run_all_community_sign_for_session(
        self, session: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        records = []
        for game_key in ("huanta", "yihuan"):
            records.append(await self._execute_community_sign_for_game(session, game_key))
        return records

    async def _run_all_game_sign_for_session(
        self, session: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        results = []
        for game_key in ("huanta", "yihuan"):
            try:
                results.append(await self._execute_game_sign_for_game(session, game_key))
            except TaJiDuoApiError as exc:
                results.append(
                    {
                        "gameKey": game_key,
                        "gameName": COMMUNITY_GAME_META[game_key]["label"],
                        "success": False,
                        "skipped": False,
                        "message": str(exc),
                        "role": None,
                        "stateAfter": {},
                    }
                )
        return results

    def _summarize_session_records(
        self, session: Dict[str, Any], records: List[Dict[str, Any]]
    ) -> str:
        parts = []
        for item in records:
            if item.get("success"):
                parts.append(f"{item['gameName']}:成功")
            else:
                parts.append(f"{item['gameName']}:失败")
        label = session.get("username") or session.get("tgd_uid") or session.get("identity_key")
        return f"{label} | {' | '.join(parts)}"

    def _build_redeem_message(
        self, title: str, items: List[Dict[str, Any]], game_code: str = ""
    ) -> str:
        lines = [title]
        if not items:
            lines.append("当前暂无可用兑换码")
            return "\n".join(lines)

        lines.append(f"数量：{len(items)}")
        if not game_code:
            counts = {"huanta": 0, "yihuan": 0, "unknown": 0}
            for item in items:
                code = normalize_redeem_game_code(item.get("gameCode"))
                counts[code or "unknown"] += 1
            if counts["huanta"]:
                lines.append(f"幻塔：{counts['huanta']}")
            if counts["yihuan"]:
                lines.append(f"异环：{counts['yihuan']}")
        lines.append("状态：默认仅展示未过期兑换码")
        lines.append("")

        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {safe_str(item.get('code'), '未返回兑换码')}")
            if item.get("description"):
                lines.append(f"描述：{item['description']}")
            if item.get("exchangeRewards"):
                lines.append(f"奖励：{item['exchangeRewards']}")
            expires_at = safe_str(item.get("expiresAt") or item.get("endAt") or item.get("endsAt"))
            if expires_at:
                lines.append(f"结束时间：{expires_at}")
            lines.append(f"状态：{'已过期' if is_redeem_expired(item) else '可用'}")
            if index < len(items):
                lines.append("===")
        return "\n".join(lines)

    def _parse_shop_goods_args(
        self, arg1: str = "", arg2: str = "", arg3: str = ""
    ) -> Dict[str, Any]:
        args = [safe_str(arg1), safe_str(arg2), safe_str(arg3)]
        args = [item for item in args if item]
        tab = "all"
        count = 20
        version = 0
        if args:
            if not args[0].isdigit():
                tab = normalize_shop_tab(args[0])
                args = args[1:]
        if args and args[0].isdigit():
            count = max(int(args[0]), 1)
            args = args[1:]
        if args and args[0].isdigit():
            version = max(int(args[0]), 0)
        return {"tab": tab, "count": count, "version": version}

    async def _resolve_shop_goods(
        self, fwt: str, keyword: str
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        keyword = safe_str(keyword)
        if not keyword:
            raise TaJiDuoApiError("请提供商品 ID 或关键词")

        catalog = await self.client.list_shop_goods(fwt, tab="all", count=80, version=0)
        goods = catalog.get("goods") or []

        if keyword.isdigit():
            target = next((item for item in goods if safe_str(item.get("id")) == keyword), None)
            return keyword, target, catalog

        lowered = keyword.lower()
        target = next(
            (
                item
                for item in goods
                if lowered in safe_str(item.get("name")).lower()
            ),
            None,
        )
        if not target:
            raise TaJiDuoApiError("未找到匹配的商品")
        return safe_str(target.get("id")), target, catalog

    def _build_shop_goods_message(self, data: Dict[str, Any], payload: Dict[str, Any]) -> str:
        goods = data.get("goods") or []
        lines = ["塔吉多商城"]
        lines.append(f"分区：{payload.get('tab') or 'all'}")
        lines.append(f"数量：{len(goods)}")
        lines.append(f"版本：{safe_str(data.get('version'), '未返回')}")
        if not goods:
            lines.append("当前没有商品数据")
            return "\n".join(lines)

        for index, item in enumerate(goods, start=1):
            lines.append(
                f"{index}. [{safe_str(item.get('id'), '-')}] {safe_str(item.get('name'), '未命名商品')} | "
                f"价格 {safe_str(item.get('price'), '-')} | 库存 {safe_str(item.get('stock'), '-')}"
            )
        return "\n".join(lines)

    def _build_shop_detail_message(
        self, data: Dict[str, Any], goods_id: str, catalog_item: Optional[Dict[str, Any]]
    ) -> str:
        item = data.get("item") or {}
        lines = [f"商品详情 [{goods_id}]"]
        lines.append(f"名称：{safe_str(item.get('name'), catalog_item.get('name') if catalog_item else '未返回')}")
        lines.append(f"价格：{safe_str(item.get('price'), catalog_item.get('price') if catalog_item else '-')}")
        lines.append(f"库存：{safe_str(item.get('stock'), catalog_item.get('stock') if catalog_item else '-')}")
        lines.append(f"兑换次数：{safe_str(item.get('exchangeNum'), catalog_item.get('exchangeNum') if catalog_item else '-')}")
        lines.append(f"限购：{safe_str(item.get('cycleLimit'), catalog_item.get('cycleLimit') if catalog_item else '-')}")
        if item.get("detail"):
            lines.append(f"详情：{safe_str(item.get('detail'))}")
        rules = item.get("rules") or {}
        if isinstance(rules, dict) and rules:
            lines.append("规则：")
            for key, value in rules.items():
                lines.append(f"{key}: {value}")
        return "\n".join(lines)

    def _build_coin_state_message(self, data: Dict[str, Any]) -> str:
        return "\n".join(
            [
                "塔吉多币状态",
                f"今日获取：{safe_str(data.get('todayGet'), '未返回')}",
                f"今日上限：{safe_str(data.get('todayTotal'), '未返回')}",
                f"当前总数：{safe_str(data.get('total'), '未返回')}",
            ]
        )

    def _build_shop_roles_message(self, game_meta: Dict[str, str], data: Dict[str, Any]) -> str:
        roles = data.get("roles") or []
        bind_role = safe_str(data.get("bindRole"))
        lines = [f"{game_meta['label']}商城角色列表"]
        if not roles:
            lines.append("当前账号未返回该游戏商城角色")
            return "\n".join(lines)
        for index, role in enumerate(roles, start=1):
            mark = " | 已绑定角色" if bind_role and bind_role == safe_str(role.get("roleId")) else ""
            lines.append(
                f"{index}. {safe_str(role.get('roleName'), '未命名角色')} | "
                f"Lv.{safe_str(role.get('lev'), '-')} | {safe_str(role.get('serverName'), '未知服务器')} | "
                f"ID {safe_str(role.get('roleId'), '未返回')}{mark}"
            )
        return "\n".join(lines)

    def _pick_shop_role(self, roles_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        roles = roles_data.get("roles") or []
        bind_role = safe_str(roles_data.get("bindRole"))
        if bind_role:
            for role in roles:
                if safe_str(role.get("roleId")) == bind_role:
                    return role
        if len(roles) == 1:
            return roles[0]
        return None

    async def _resolve_game_role(
        self, session: Dict[str, Any], game_key: str, selector: str = ""
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        roles_data = await self.client.roles(game_key, session["fwt"])
        roles = roles_data.get("roles") or []
        role = pick_role(roles, selector)
        remainder = clean_spaces(selector)
        if role:
            role_info = normalize_role_info(role)
            for token in (role_info["roleId"], role_info["roleName"]):
                if token:
                    remainder = clean_spaces(re.sub(re.escape(token), " ", remainder, count=1, flags=re.I))
        return roles, role, remainder

    def _build_game_roles_summary(self, game_name: str, roles: List[Dict[str, Any]]) -> str:
        lines = [f"{game_name}角色列表"]
        if not roles:
            lines.append("当前账号未返回角色数据")
            return "\n".join(lines)
        for index, role in enumerate(roles, start=1):
            role_info = normalize_role_info(role)
            lines.append(
                f"{index}. {safe_str(role_info['roleName'], '未命名角色')} | "
                f"Lv.{safe_str(role_info['level'], '-')} | "
                f"{safe_str(role_info['serverName'], '未知服务器')} | "
                f"ID {safe_str(role_info['roleId'], '未返回')}"
            )
        return "\n".join(lines)

    def _build_community_level_message(
        self, game_name: str, session: Dict[str, Any], data: Dict[str, Any]
    ) -> str:
        return "\n".join(
            [
                f"{game_name}社区等级",
                f"账号：{self._account_display_name(session)}",
                f"等级：{safe_str(data.get('level'), '未返回')}",
                f"当前经验：{safe_str(data.get('exp'), '未返回')}",
                f"今日经验：{safe_str(data.get('todayExp'), '未返回')}",
                f"下级经验：{safe_str(data.get('nextLevelExp'), '未返回')}",
            ]
        )

    def _build_sign_state_message(
        self, game_name: str, session: Dict[str, Any], data: Dict[str, Any]
    ) -> str:
        return "\n".join(
            [
                f"{game_name}签到状态",
                f"账号：{self._account_display_name(session)}",
                f"今日状态：{'已签到' if data.get('todaySign') else '未签到'}",
                f"累计天数：{safe_str(data.get('days'), '未返回')}",
                f"剩余补签：{safe_str(data.get('reSignCnt'), '未返回')}",
            ]
        )

    def _build_reward_records_message(
        self, title: str, items: List[Dict[str, Any]]
    ) -> str:
        lines = [title]
        if not items:
            lines.append("暂无奖励记录")
            return "\n".join(lines)
        for index, item in enumerate(items[:15], start=1):
            lines.append(
                f"{index}. {safe_str(item.get('gameName') or item.get('title'), '未命名奖励')} | "
                f"{safe_str(item.get('rewardName') or item.get('name') or item.get('reward'), '未返回')} | "
                f"{format_time_label(item.get('createTime') or item.get('time'))}"
            )
        return "\n".join(lines)

    def _build_shop_coin_records_message(
        self, title: str, items: List[Dict[str, Any]]
    ) -> str:
        lines = [title]
        if not items:
            lines.append("暂无记录")
            return "\n".join(lines)
        for index, item in enumerate(items[:15], start=1):
            lines.append(
                f"{index}. {safe_str(item.get('title') or item.get('typeName'), '记录')} | "
                f"{safe_str(item.get('num'), '0')} | "
                f"{format_time_label(item.get('createTime'))}"
            )
        return "\n".join(lines)

    async def _build_profile_render_data(
        self, event: AstrMessageEvent, session: Dict[str, Any], profile: Dict[str, Any]
    ) -> Dict[str, Any]:
        games_data = await self.client.list_games(session["fwt"])
        games = games_data.get("items") or games_data.get("games") or []
        bindings = []
        for game in games[:6]:
            bindings.append(
                {
                    "label": safe_str(game.get("name") or game.get("gameName"), "未知游戏"),
                    "value": safe_str(game.get("description") or game.get("gameCode"), "已接入"),
                }
            )
        return {
            "pageTitle": "塔吉多资料",
            "pageSubtitle": event.get_sender_name() or self._account_display_name(session),
            "user": {
                "name": self._account_display_name(profile or session),
                "uid": self._account_uid(profile or session),
                "avatar": safe_str(profile.get("avatar"), self.renderer.get_res_path("img/ui/logo.png")),
                "introduce": safe_str(profile.get("introduce"), "暂无个性签名"),
                "bindTime": format_time_label(session.get("bind_time") or session.get("created_at")),
                "updatedAt": format_time_label(session.get("last_refresh_at") or session.get("updated_at")),
                "phone": format_phone(session.get("phone")),
            },
            "bindings": bindings,
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _render_profile(
        self, event: AstrMessageEvent, session: Dict[str, Any], profile: Dict[str, Any]
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/account_profile/index.html",
            await self._build_profile_render_data(event, session, profile),
            {"viewport_width": 1080, "viewport_height": 1400},
        )

    def _asset_abs_path(self, *parts: str) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), *parts))

    def _asset_url_if_exists(self, *parts: str) -> str:
        absolute = self._asset_abs_path(*parts)
        if not os.path.exists(absolute):
            return ""
        return "file:///" + absolute.replace("\\", "/")

    def _plugin_asset_url_if_exists(self, relative_path: str) -> str:
        parts = [part for part in relative_path.replace("/", "\\").split("\\") if part]
        return self._asset_url_if_exists(*parts)

    def _first_plugin_asset_url(self, folder: str, preferred_names: List[str]) -> str:
        base_dir = self._asset_abs_path(*[part for part in folder.replace("/", "\\").split("\\") if part])
        for name in preferred_names:
            url = self._plugin_asset_url_if_exists(f"{folder}/{name}")
            if url:
                return url
        if not os.path.isdir(base_dir):
            return ""
        for name in sorted(os.listdir(base_dir)):
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                url = self._plugin_asset_url_if_exists(f"{folder}/{name}")
                if url:
                    return url
        return ""

    def _match_plugin_asset_url(self, folder: str, candidates: List[str]) -> str:
        base_dir = self._asset_abs_path(*[part for part in folder.replace("/", "\\").split("\\") if part])
        if not os.path.isdir(base_dir):
            return ""
        normalized: List[str] = []
        for item in candidates:
            token = safe_str(item).replace("\\", "/").split("/")[-1]
            stem, _ext = os.path.splitext(token)
            stem = stem.strip().lower()
            if stem:
                normalized.append(stem)
        ordered = list(dict.fromkeys(normalized))
        if not ordered:
            return ""
        for name in sorted(os.listdir(base_dir)):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            lowered = stem.lower()
            for candidate in ordered:
                if lowered == candidate or lowered.startswith(f"{candidate}_"):
                    return self._plugin_asset_url_if_exists(f"{folder}/{name}")
        return ""

    def _avatar_ref_candidates(self, value: Any) -> List[str]:
        text = safe_str(value)
        if not text:
            return []
        token = text.replace("\\", "/").split("/")[-1]
        stem, _ext = os.path.splitext(token)
        stem = stem.strip()
        if not stem:
            return []
        candidates = [stem]
        match = re.match(r"^(player_\d+)(?:_\d+)?$", stem, re.IGNORECASE)
        if match:
            candidates.append(match.group(1))
        elif "_" in stem:
            candidates.append(stem.rsplit("_", 1)[0])
        return list(dict.fromkeys(candidates))

    def _yihuan_avatar_asset_candidates(self, value: Any) -> List[str]:
        token = safe_str(value)
        if not token:
            return []
        results = self._avatar_ref_candidates(token)
        if token.isdigit():
            number = int(token)
            if len(token) <= 3:
                results.append(f"player_{number:03d}")
            else:
                results.append(f"player_{token[:3]}")
                results.append(f"player_{token[-3:]}")
        return list(dict.fromkeys(results))

    def _yihuan_avatar_url(self, item: Dict[str, Any], size: str = "200") -> str:
        avatar = safe_str(
            item.get("avatar")
            or item.get("icon")
            or item.get("img")
            or item.get("image")
            or item.get("head")
        )
        char_id = safe_str(item.get("id") or item.get("charId") or item.get("charid"))
        if avatar.startswith(("http://", "https://", "file:///")):
            return avatar
        local = self._match_plugin_asset_url(
            f"img/yihuan_avatar/{size}",
            self._yihuan_avatar_asset_candidates(avatar),
        )
        if not local:
            local = self._match_plugin_asset_url(
                f"img/yihuan_avatar/{size}",
                self._yihuan_avatar_asset_candidates(char_id),
            )
        if local:
            return local
        return self.renderer.get_res_path("img/ui/Character.png")

    def _yihuan_primary_avatar_url(self, role_home_data: Optional[Dict[str, Any]] = None) -> str:
        data = role_home_data or {}
        first_character = {}
        characters = to_list(data.get("characters"))
        if characters:
            first_character = characters[0]
        return self._yihuan_avatar_url(
            {
                "avatar": data.get("avatar"),
                "id": safe_str(first_character.get("id") or data.get("avatar")),
            }
        )

    def _yihuan_record_card_item(
        self, record_card: Optional[Dict[str, Any]] = None, role_id: str = ""
    ) -> Dict[str, Any]:
        cards = to_list(record_card or {})
        wanted_role_id = safe_str(role_id)
        if wanted_role_id:
            for card in cards:
                bind_info = card.get("bindRoleInfo") or {}
                if safe_str(bind_info.get("roleId")) == wanted_role_id:
                    return card
        return cards[0] if cards else {}

    def _yihuan_card_background_url(
        self, record_card: Optional[Dict[str, Any]] = None, role_id: str = ""
    ) -> str:
        card = self._yihuan_record_card_item(record_card, role_id)
        background = safe_str(
            card.get("backgroundImage") or card.get("background") or card.get("image")
        )
        if background.startswith(("http://", "https://", "file:///")):
            return background
        return self.renderer.get_res_path(
            "render/personal_card/img/YH_UI_personal_info_calling_caed_cover.png"
        )

    def _yihuan_house_image_url(self, show_id: str = "") -> str:
        return self._first_plugin_asset_url(
            "render/personal_card/img/runtime_house",
            [f"{show_id}.png"] if show_id else [],
        ) or self.renderer.get_res_path("render/personal_card/img/YH_UI_personal_info_ops_icon_01.png")

    def _yihuan_vehicle_image_url(self, show_id: str = "") -> str:
        return self._first_plugin_asset_url(
            "render/personal_card/img/runtime_car",
            [f"{show_id}.png"] if show_id else [],
        ) or self.renderer.get_res_path("render/personal_card/img/YH_UI_personal_info_ops_icon_02.png")

    def _account_avatar_url(
        self,
        session: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        role_home_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        candidates = [
            safe_str((profile or {}).get("avatar")),
            safe_str(session.get("avatar")),
        ]
        for value in candidates:
            if value:
                return value
        return self._yihuan_primary_avatar_url(role_home_data) or self.renderer.get_res_path(
            "img/ui/logo.png"
        )

    def _account_avatar_fallback_url(self, role_home_data: Optional[Dict[str, Any]] = None) -> str:
        if role_home_data:
            return self._yihuan_primary_avatar_url(role_home_data) or self.renderer.get_res_path(
                "img/ui/logo.png"
            )
        return self.renderer.get_res_path("img/ui/logo.png")

    def _profile_signature(
        self,
        session: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        role_home_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        for value in (
            (profile or {}).get("introduce"),
            (profile or {}).get("signature"),
            session.get("introduce"),
            (role_home_data or {}).get("introduce"),
            (role_home_data or {}).get("signature"),
            (role_home_data or {}).get("desc"),
        ):
            text = safe_str(value)
            if text:
                return text
        return "暂无个性签名"

    def _yihuan_sticker_items(self, source: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        raw_items: List[Dict[str, Any]] = []
        if isinstance(source, dict):
            for key in ("stickers", "stickerList", "pasterList", "paster", "posterList"):
                value = source.get(key)
                if isinstance(value, list):
                    raw_items = [item for item in value if isinstance(item, dict)]
                    if raw_items:
                        break

        for item in raw_items[:4]:
            image = safe_str(
                item.get("image")
                or item.get("img")
                or item.get("icon")
                or item.get("avatar")
                or item.get("url")
            )
            if not image:
                continue
            items.append({"name": safe_str(item.get("name"), "贴纸"), "image": image})
        while len(items) < 4:
            items.append({"isEmpty": True})
        return items[:4]

    def _build_yihuan_render_header(
        self, session: Dict[str, Any], role_home: Dict[str, Any]
    ) -> Dict[str, Any]:
        data = data_body(role_home) or {}
        achieve = data.get("achieveProgress") or {}
        return {
            "name": safe_str(data.get("rolename"), self._account_display_name(session)),
            "uid": safe_str(data.get("roleid") or data.get("uid"), self._account_uid(session)),
            "server": safe_str(data.get("servername"), "未返回"),
            "avatar": self._yihuan_primary_avatar_url(data),
            "level": safe_str(data.get("lev"), "-"),
            "worldLevel": safe_str(data.get("worldlevel") or data.get("tycoonLevel"), "-"),
            "loginDays": safe_str(data.get("roleloginDays"), "-"),
            "characterCount": safe_str(data.get("charidCnt"), "0"),
            "achievementCount": safe_str(achieve.get("achievementCnt"), "0"),
            "achievementTotal": safe_str(achieve.get("total"), "0"),
        }

    async def _build_personal_card_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        profile: Dict[str, Any],
        record_card: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        data = data_body(role_home) or {}
        characters = to_list(data.get("characters"))
        achieve_body = data.get("achieveProgress") or {}
        area_items: List[Dict[str, Any]] = to_list(data.get("areaProgress"))
        estate_body: Dict[str, Any] = data.get("realestate") or {}
        vehicle_body: Dict[str, Any] = data.get("vehicle") or {}
        estate_detail: List[Dict[str, Any]] = to_list(estate_body.get("detail"))
        vehicle_detail: List[Dict[str, Any]] = to_list(vehicle_body.get("detail"))
        roles = []
        for item in characters[:8]:
            roles.append(
                {
                    "isEmpty": False,
                    "name": safe_str(item.get("name"), "未命名角色"),
                    "level": safe_str(item.get("slev") or item.get("alev"), "-"),
                    "avatar": self._yihuan_avatar_url(item),
                    "fallbackAvatar": self.renderer.get_res_path("img/ui/Character.png"),
                    "ringColor": yihuan_role_ring_color(item),
                }
            )
        while len(roles) < 8:
            roles.append({"isEmpty": True})

        achievement_items = []
        for item in to_list(achieve_body.get("detail"))[:6]:
            current = yihuan_progress_current(item)
            total = yihuan_progress_total(item)
            achievement_items.append(
                {
                    "name": safe_str(item.get("name"), "未命名成就"),
                    "current": str(current),
                    "total": str(total),
                    "percent": yihuan_progress_percent(current, total),
                }
            )

        area_cards = []
        for item in area_items[:4]:
            current = yihuan_progress_current(item)
            total = yihuan_progress_total(item)
            area_cards.append(
                {
                    "name": safe_str(item.get("name"), "未命名区域"),
                    "current": str(current),
                    "total": str(total),
                    "percent": yihuan_progress_percent(current, total),
                }
            )

        total_area_current = sum(yihuan_progress_current(item) for item in area_items)
        total_area_total = sum(yihuan_progress_total(item) for item in area_items)
        owned_estates = [item for item in estate_detail if is_truthy_flag(item.get("own") or item.get("owned") or item.get("unlock") or item.get("has"))]
        owned_vehicles = [item for item in vehicle_detail if is_truthy_flag(item.get("own") or item.get("owned") or item.get("unlock") or item.get("has"))]

        return {
            "pageTitle": "异环档案",
            "user": {
                "uid": safe_str(data.get("roleid") or data.get("uid"), self._account_uid(session)),
                "avatar": self._account_avatar_url(session, profile, data),
                "fallbackAvatar": self._account_avatar_fallback_url(data),
                "name": self._account_display_name(profile or session),
                "cardSubtitle": safe_str(
                    data.get("rolename") or data.get("servername"),
                    "异环",
                ),
                "cardBackground": self._yihuan_card_background_url(
                    record_card, safe_str(data.get("roleid"))
                ),
                "adventureDays": safe_str(data.get("roleloginDays"), "-"),
                "birthday": safe_str(data.get("birthday"), "未公开"),
                "hunterLevel": safe_str(data.get("lev"), "-"),
                "identifyLevel": safe_str(data.get("worldlevel") or data.get("tycoonLevel"), "-"),
                "signature": self._profile_signature(session, profile, data),
            },
            "assets": {
                "house": {
                    "title": safe_str(estate_body.get("showName"), "暂无房产展示"),
                    "subtitle": f"拥有 {to_int(estate_body.get('ownCnt'), len(owned_estates))}/{max(to_int(estate_body.get('total'), len(estate_detail)), len(estate_detail), 1)}",
                    "image": self._yihuan_house_image_url(safe_str(estate_body.get("showId"))),
                },
                "car": {
                    "title": safe_str(vehicle_body.get("showName"), "暂无载具展示"),
                    "subtitle": f"拥有 {to_int(vehicle_body.get('ownCnt'), len(owned_vehicles))}/{max(to_int(vehicle_body.get('total'), len(vehicle_detail)), len(vehicle_detail), 1)}",
                    "image": self._yihuan_vehicle_image_url(safe_str(vehicle_body.get("showId"))),
                },
            },
            "roles": roles,
            "stickers": self._yihuan_sticker_items(data),
            "ops": [
                {
                    "label": "角色",
                    "count": safe_str(data.get("charidCnt"), "0"),
                    "icon": self.renderer.get_res_path(
                        "render/personal_card/img/YH_UI_personal_info_ops_icon_01.png"
                    ),
                },
                {
                    "label": "载具",
                    "count": safe_str((data.get("vehicle") or {}).get("ownCnt"), "0"),
                    "icon": self.renderer.get_res_path(
                        "render/personal_card/img/YH_UI_personal_info_ops_icon_02.png"
                    ),
                },
                {
                    "label": "成就",
                    "count": safe_str((data.get("achieveProgress") or {}).get("achievementCnt"), "0"),
                    "icon": self.renderer.get_res_path(
                        "render/personal_card/img/YH_UI_personal_info_ops_icon_03.png"
                    ),
                },
                {
                    "label": "探索",
                    "count": safe_str(len(to_list(data.get("areaProgress"))), "0"),
                    "icon": self.renderer.get_res_path(
                        "render/personal_card/img/YH_UI_personal_info_ops_icon_04.png"
                    ),
                },
            ],
            "insight": {
                "achievement": {
                    "total": safe_str(achieve_body.get("achievementCnt"), "0"),
                    "target": safe_str(achieve_body.get("total"), "0"),
                    "bronze": safe_str(achieve_body.get("bronzeUmdCnt"), "0"),
                    "silver": safe_str(achieve_body.get("silverUmdCnt"), "0"),
                    "gold": safe_str(achieve_body.get("goldUmdCnt"), "0"),
                    "percent": yihuan_progress_percent(
                        to_int(achieve_body.get("achievementCnt"), 0),
                        to_int(achieve_body.get("total"), 0),
                    ),
                    "items": achievement_items,
                },
                "area": {
                    "count": str(len(area_items)),
                    "current": str(total_area_current),
                    "total": str(total_area_total),
                    "percent": yihuan_progress_percent(total_area_current, total_area_total),
                    "items": area_cards,
                },
            },
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _render_personal_card(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        profile: Dict[str, Any],
        record_card: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/personal_card/index.html",
            await self._build_personal_card_render_data(
                session,
                role_home,
                profile,
                record_card,
            ),
            {"viewport_width": 1600, "viewport_height": 1000},
        )

    async def _build_yihuan_characters_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        characters_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        header = self._build_yihuan_render_header(session, role_home)
        items = []
        for item in to_list(data_body(characters_data))[:12]:
            props = []
            for prop in to_list(item.get("properties"))[:3]:
                props.append(
                    {
                        "name": safe_str(prop.get("name"), "属性"),
                        "value": safe_str(prop.get("value"), "-"),
                    }
                )
            items.append(
                {
                    "name": safe_str(item.get("name"), "未命名角色"),
                    "avatar": self._yihuan_avatar_url(item),
                    "quality": yihuan_enum_label(item.get("quality")) or "SSR",
                    "element": yihuan_enum_label(item.get("elementType")) or "-",
                    "group": yihuan_enum_label(item.get("groupType")) or "-",
                    "level": safe_str(item.get("slev") or item.get("alev"), "-"),
                    "awaken": safe_str(item.get("awakenLev"), "0"),
                    "ringColor": yihuan_role_ring_color(item),
                    "props": props,
                }
            )
        return {
            "pageTitle": "异环角色",
            "pageTag": "YIHUAN CHARACTER BOARD",
            "sectionTitle": "角色速览",
            "summary": [
                {"label": "角色数量", "value": header["characterCount"]},
                {"label": "猎人等级", "value": header["level"]},
                {"label": "鉴别等级", "value": header["worldLevel"]},
                {"label": "登录天数", "value": header["loginDays"]},
            ],
            "user": header,
            "items": items,
            "emptyText": "暂无角色数据",
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _build_yihuan_achievements_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        achieve_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        header = self._build_yihuan_render_header(session, role_home)
        data = data_body(achieve_data) or {}
        current = to_int(data.get("achievementCnt"), 0)
        total = to_int(data.get("total"), 0)
        items = []
        for item in to_list(data.get("detail"))[:12]:
            item_current = yihuan_progress_current(item)
            item_total = yihuan_progress_total(item)
            items.append(
                {
                    "name": safe_str(item.get("name"), "未命名成就"),
                    "current": str(item_current),
                    "total": str(item_total),
                    "percent": yihuan_progress_percent(item_current, item_total),
                }
            )
        return {
            "pageTitle": "异环成就",
            "pageTag": "YIHUAN ACHIEVEMENT BOARD",
            "sectionTitle": "成就进度",
            "summary": [
                {"label": "总进度", "value": f"{current}/{max(total, 0)}"},
                {"label": "铜", "value": safe_str(data.get("bronzeUmdCnt"), "0")},
                {"label": "银", "value": safe_str(data.get("silverUmdCnt"), "0")},
                {"label": "金", "value": safe_str(data.get("goldUmdCnt"), "0")},
            ],
            "highlight": {
                "label": "总完成度",
                "value": f"{current}/{total}",
                "percent": yihuan_progress_percent(current, total),
                "description": f"已完成 {current} 项成就，共 {total} 项",
            },
            "user": header,
            "items": items,
            "emptyText": "暂无成就数据",
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _build_yihuan_exploration_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        area_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        header = self._build_yihuan_render_header(session, role_home)
        area_items = to_list(data_body(area_data))
        items = []
        total_current = 0
        total_total = 0
        for item in area_items[:12]:
            current = yihuan_progress_current(item)
            total = yihuan_progress_total(item)
            total_current += current
            total_total += total
            details = []
            for child in to_list(item.get("detail"))[:3]:
                details.append(
                    f"{safe_str(child.get('name'), '子区域')} {yihuan_progress_current(child)}/{yihuan_progress_total(child)}"
                )
            items.append(
                {
                    "name": safe_str(item.get("name"), "未命名区域"),
                    "current": str(current),
                    "total": str(total),
                    "percent": yihuan_progress_percent(current, total),
                    "details": details,
                }
            )
        return {
            "pageTitle": "异环探索",
            "pageTag": "YIHUAN EXPLORATION BOARD",
            "sectionTitle": "区域探索",
            "summary": [
                {"label": "区域数量", "value": str(len(area_items))},
                {"label": "总探索", "value": f"{total_current}/{total_total}"},
                {"label": "猎人等级", "value": header["level"]},
                {"label": "登录天数", "value": header["loginDays"]},
            ],
            "highlight": {
                "label": "总探索度",
                "value": f"{total_current}/{total_total}",
                "percent": yihuan_progress_percent(total_current, total_total),
                "description": f"已统计 {len(area_items)} 个区域的探索进度",
            },
            "user": header,
            "items": items,
            "emptyText": "暂无探索数据",
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _build_yihuan_real_estate_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        estate_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        header = self._build_yihuan_render_header(session, role_home)
        data = data_body(estate_data) or {}
        details = to_list(data.get("detail"))
        owned = [
            item
            for item in details
            if is_truthy_flag(item.get("own") or item.get("owned") or item.get("unlock") or item.get("has"))
        ]
        items = []
        for item in details[:12]:
            furniture = to_list(item.get("fdetail"))
            owned_furniture = sum(
                1
                for child in furniture
                if is_truthy_flag(child.get("own") or child.get("owned") or child.get("unlock") or child.get("has"))
            )
            items.append(
                {
                    "name": item_display_name(item),
                    "state": "已拥有" if item in owned else "未拥有",
                    "subtext": f"家具 {owned_furniture}/{len(furniture)}" if furniture else "无家具数据",
                    "isOwned": item in owned,
                }
            )
        total = max(len(details), to_int(data.get("total"), 0))
        return {
            "pageTitle": "异环房产",
            "pageTag": "YIHUAN REAL ESTATE BOARD",
            "sectionTitle": "房产收藏",
            "summary": [
                {"label": "已拥有", "value": str(len(owned))},
                {"label": "总数", "value": str(total)},
                {"label": "登录天数", "value": header["loginDays"]},
                {"label": "角色数量", "value": header["characterCount"]},
            ],
            "highlight": {
                "label": "展示房产",
                "value": safe_str(data.get("showName"), "暂无展示"),
                "percent": yihuan_progress_percent(len(owned), total),
                "description": "按当前账号的房产收集进度展示",
                "image": self._yihuan_house_image_url(safe_str(data.get("showId"))),
            },
            "user": header,
            "items": items,
            "emptyText": "暂无房产数据",
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _build_yihuan_vehicles_render_data(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        vehicle_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        header = self._build_yihuan_render_header(session, role_home)
        data = data_body(vehicle_data) or {}
        details = to_list(data.get("detail"))
        owned = [
            item
            for item in details
            if is_truthy_flag(item.get("own") or item.get("owned") or item.get("unlock") or item.get("has"))
        ]
        total = max(to_int(data.get("total"), 0), len(details))
        own_cnt = max(to_int(data.get("ownCnt"), 0), len(owned))
        items = []
        for item in details[:12]:
            items.append(
                {
                    "name": item_display_name(item),
                    "state": "已拥有" if item in owned else "未拥有",
                    "subtext": f"ID {safe_str(item.get('id'), '-')}",
                    "isOwned": item in owned,
                }
            )
        return {
            "pageTitle": "异环载具",
            "pageTag": "YIHUAN VEHICLE BOARD",
            "sectionTitle": "载具收藏",
            "summary": [
                {"label": "已拥有", "value": str(own_cnt)},
                {"label": "总数", "value": str(total)},
                {"label": "猎人等级", "value": header["level"]},
                {"label": "登录天数", "value": header["loginDays"]},
            ],
            "highlight": {
                "label": "展示载具",
                "value": safe_str(data.get("showName"), "暂无展示"),
                "percent": yihuan_progress_percent(own_cnt, total),
                "description": "按当前账号的载具收集进度展示",
                "image": self._yihuan_vehicle_image_url(safe_str(data.get("showId"))),
            },
            "user": header,
            "items": items,
            "emptyText": "暂无载具数据",
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _render_yihuan_characters(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        characters_data: Dict[str, Any],
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/yihuan_characters/index.html",
            await self._build_yihuan_characters_render_data(session, role_home, characters_data),
            {"viewport_width": 1260, "viewport_height": 1800},
        )

    async def _render_yihuan_achievements(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        achieve_data: Dict[str, Any],
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/yihuan_achievements/index.html",
            await self._build_yihuan_achievements_render_data(session, role_home, achieve_data),
            {"viewport_width": 1260, "viewport_height": 1800},
        )

    async def _render_yihuan_exploration(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        area_data: Dict[str, Any],
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/yihuan_exploration/index.html",
            await self._build_yihuan_exploration_render_data(session, role_home, area_data),
            {"viewport_width": 1260, "viewport_height": 1800},
        )

    async def _render_yihuan_real_estate(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        estate_data: Dict[str, Any],
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/yihuan_real_estate/index.html",
            await self._build_yihuan_real_estate_render_data(session, role_home, estate_data),
            {"viewport_width": 1260, "viewport_height": 1800},
        )

    async def _render_yihuan_vehicles(
        self,
        session: Dict[str, Any],
        role_home: Dict[str, Any],
        vehicle_data: Dict[str, Any],
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/yihuan_vehicles/index.html",
            await self._build_yihuan_vehicles_render_data(session, role_home, vehicle_data),
            {"viewport_width": 1260, "viewport_height": 1800},
        )

    async def _build_huanta_record_render_data(
        self,
        session: Dict[str, Any],
        role: Dict[str, Any],
        record_data: Dict[str, Any],
        record_type: str,
    ) -> Dict[str, Any]:
        record = record_data.get("record") or {}
        record_type_label = {
            "0": "总览",
            "1": "武器",
            "2": "拟态",
            "3": "时装",
            "4": "载具",
        }.get(record_type, "总览")
        sections = [
            ("武器", to_list(record.get("weaponinfo") or record.get("weaponInfo"))),
            ("拟态", to_list(record.get("imitationlist") or record.get("imitationList"))),
            ("时装", to_list(record.get("dressfashionlist") or record.get("dressFashionList"))),
            ("载具", to_list(record.get("mountlist") or record.get("mountList"))),
        ]
        panel_sections = []
        for label, items in sections:
            panel_sections.append(
                {
                    "title": label,
                    "count": len(items),
                    "items": [
                        {
                            "name": item_display_name(item),
                            "id": safe_str(item.get("ID") or item.get("id")),
                        }
                        for item in items[:8]
                    ],
                }
            )
        selected = safe_str(record_data.get("selected") or record.get("selected"), "未设置")
        return {
            "pageTitle": "幻塔档案",
            "pageSubtitle": f"{safe_str(role.get('roleName') or record.get('rolename'), '未命名角色')} · {record_type_label}",
            "summary": {
                "name": safe_str(record.get("rolename") or role.get("roleName"), "未命名角色"),
                "roleId": safe_str(record_data.get("roleId") or role.get("roleId"), "未返回"),
                "server": safe_str(record.get("groupname") or role.get("serverName"), "未返回"),
                "level": safe_str(record.get("lev") or role.get("lev"), "-"),
                "gs": safe_str(record.get("maxgs"), "未返回"),
                "selected": selected,
                "uid": self._account_uid(session),
            },
            "sections": panel_sections,
            "pluResPath": self.renderer.get_res_path(""),
        }

    async def _render_huanta_record(
        self,
        session: Dict[str, Any],
        role: Dict[str, Any],
        record_data: Dict[str, Any],
        record_type: str,
    ) -> Optional[str]:
        return await self.renderer.render_html(
            "render/huanta_record/index.html",
            await self._build_huanta_record_render_data(session, role, record_data, record_type),
            {"viewport_width": 1420, "viewport_height": 1600},
        )

    def _parse_huanta_record_args(self, arg1: str = "", arg2: str = "") -> Tuple[str, str]:
        type_map = {
            "武器": "1",
            "1": "1",
            "拟态": "2",
            "2": "2",
            "时装": "3",
            "3": "3",
            "载具": "4",
            "4": "4",
        }
        raw1 = clean_spaces(arg1)
        raw2 = clean_spaces(arg2)
        if raw1 in type_map and not raw2:
            return "", type_map[raw1]
        if raw2 in type_map:
            return raw1, type_map[raw2]
        return raw1 or raw2, "0"

    def _build_profile_text(self, session: Dict[str, Any], profile: Dict[str, Any]) -> str:
        lines = [
            "塔吉多资料",
            f"昵称：{self._account_display_name(profile or session)}",
            f"UID：{self._account_uid(profile or session)}",
            f"绑定时间：{format_time_label(session.get('bind_time') or session.get('created_at'))}",
            f"最近刷新：{format_time_label(session.get('last_refresh_at') or session.get('updated_at'))}",
        ]
        if safe_str(profile.get("introduce")):
            lines.append(f"个性签名：{safe_str(profile.get('introduce'))}")
        return "\n".join(lines)

    def _build_huanta_record_text(
        self, session: Dict[str, Any], role: Dict[str, Any], record_data: Dict[str, Any], record_type: str
    ) -> str:
        record = record_data.get("record") or {}
        type_label = {"0": "总览", "1": "武器", "2": "拟态", "3": "时装", "4": "载具"}.get(record_type, "总览")
        lines = [
            f"幻塔档案 - {type_label}",
            f"角色：{safe_str(record.get('rolename') or role.get('roleName'), '未命名角色')}",
            f"角色ID：{safe_str(record_data.get('roleId') or role.get('roleId'), '未返回')}",
            f"服务器：{safe_str(record.get('groupname') or role.get('serverName'), '未返回')}",
            f"等级：{safe_str(record.get('lev') or role.get('lev'), '-')}",
            f"GS：{safe_str(record.get('maxgs'), '未返回')}",
            f"当前展示：{safe_str(record_data.get('selected') or record.get('selected'), '未设置')}",
        ]
        sections = [
            ("武器", to_list(record.get("weaponinfo") or record.get("weaponInfo"))),
            ("拟态", to_list(record.get("imitationlist") or record.get("imitationList"))),
            ("时装", to_list(record.get("dressfashionlist") or record.get("dressFashionList"))),
            ("载具", to_list(record.get("mountlist") or record.get("mountList"))),
        ]
        for label, items in sections:
            if items:
                lines.append(f"{label}：{', '.join(item_display_name(item) for item in items[:6])}")
        return "\n".join(lines)

    def _build_yihuan_home_text(self, session: Dict[str, Any], role_home: Dict[str, Any]) -> str:
        data = data_body(role_home) or {}
        achieve = data.get("achieveProgress") or {}
        vehicle = data.get("vehicle") or {}
        estate = data.get("realestate") or {}
        return "\n".join(
            [
                "异环档案",
                f"角色：{safe_str(data.get('rolename'), self._account_display_name(session))}",
                f"UID：{safe_str(data.get('roleid') or data.get('uid'), self._account_uid(session))}",
                f"服务器：{safe_str(data.get('servername'), '未返回')}",
                f"等级：{safe_str(data.get('lev'), '-')}",
                f"世界等级：{safe_str(data.get('worldlevel') or data.get('tycoonLevel'), '-')}",
                f"登录天数：{safe_str(data.get('roleloginDays'), '-')}",
                f"角色数量：{safe_str(data.get('charidCnt'), '-')}",
                f"成就：{safe_str(achieve.get('achievementCnt'), '0')}/{safe_str(achieve.get('total'), '0')}",
                f"房产展示：{safe_str(estate.get('showName'), '暂无')}",
                f"载具展示：{safe_str(vehicle.get('showName'), '暂无')}",
            ]
        )

    def _build_yihuan_characters_text(
        self, role: Dict[str, Any], characters_data: Dict[str, Any]
    ) -> str:
        items = to_list(data_body(characters_data))
        lines = [f"异环角色列表 - {safe_str(role.get('roleName'), role.get('roleId'))}"]
        if not items:
            lines.append("暂无角色数据")
            return "\n".join(lines)
        for index, item in enumerate(items[:20], start=1):
            lines.append(
                f"{index}. {safe_str(item.get('name'), '未命名角色')} | "
                f"{yihuan_enum_label(item.get('quality'))} | "
                f"{yihuan_enum_label(item.get('elementType'))} | "
                f"等级 {safe_str(item.get('alev'), '-')} | 阶段 {safe_str(item.get('slev'), '-')}"
            )
        return "\n".join(lines)

    def _build_yihuan_achieve_text(
        self, role: Dict[str, Any], achieve_data: Dict[str, Any]
    ) -> str:
        data = data_body(achieve_data) or {}
        detail = to_list(data.get("detail"))
        lines = [
            f"异环成就 - {safe_str(role.get('roleName'), role.get('roleId'))}",
            f"总进度：{safe_str(data.get('achievementCnt'), '0')}/{safe_str(data.get('total'), '0')}",
            f"铜：{safe_str(data.get('bronzeUmdCnt'), '0')}",
            f"银：{safe_str(data.get('silverUmdCnt'), '0')}",
            f"金：{safe_str(data.get('goldUmdCnt'), '0')}",
        ]
        for item in detail[:10]:
            lines.append(
                f"{safe_str(item.get('name'), '未命名成就')} | "
                f"{safe_str(item.get('progress'), '0')}/{safe_str(item.get('total'), '0')} | "
                f"{percent_label(item.get('progress'), item.get('total'))}"
            )
        return "\n".join(lines)

    def _build_yihuan_area_text(self, role: Dict[str, Any], area_data: Dict[str, Any]) -> str:
        items = to_list(data_body(area_data))
        lines = [f"异环探索 - {safe_str(role.get('roleName'), role.get('roleId'))}"]
        if not items:
            lines.append("暂无区域探索数据")
            return "\n".join(lines)
        for item in items[:12]:
            lines.append(
                f"{safe_str(item.get('name'), '未命名区域')} | "
                f"{safe_str(item.get('progress'), '0')}/{safe_str(item.get('total'), '0')} | "
                f"{percent_label(item.get('progress'), item.get('total'))}"
            )
        return "\n".join(lines)

    def _build_yihuan_real_estate_text(
        self, role: Dict[str, Any], estate_data: Dict[str, Any]
    ) -> str:
        data = data_body(estate_data) or {}
        detail = to_list(data.get("detail"))
        lines = [f"异环房产 - {safe_str(role.get('roleName'), role.get('roleId'))}"]
        lines.append(f"展示：{safe_str(data.get('showName'), '暂无')}")
        lines.append(f"总数：{safe_str(data.get('total'), len(detail))}")
        for item in detail[:12]:
            lines.append(f"- {item_display_name(item)}")
        return "\n".join(lines)

    def _build_yihuan_vehicles_text(
        self, role: Dict[str, Any], vehicle_data: Dict[str, Any]
    ) -> str:
        data = data_body(vehicle_data) or {}
        detail = to_list(data.get("detail"))
        lines = [f"异环载具 - {safe_str(role.get('roleName'), role.get('roleId'))}"]
        lines.append(f"展示：{safe_str(data.get('showName'), '暂无')}")
        lines.append(f"拥有：{safe_str(data.get('ownCnt'), '0')}/{safe_str(data.get('total'), len(detail))}")
        for item in detail[:12]:
            lines.append(f"- {item_display_name(item)}")
        return "\n".join(lines)

    @filter.command(
        "tjd帮助",
        alias={"塔吉多帮助", "塔吉多菜单", "tof帮助", "ht帮助", "nte帮助", "yh帮助"},
    )
    async def help_command(self, event: AstrMessageEvent):
        image_path = await self._render_menu()
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._help_text())

    @filter.command("tjd登录", alias={"塔吉多登录"})
    async def login_command(self, event: AstrMessageEvent, phone: str = ""):
        if not event.is_private_chat():
            yield event.plain_result("tjd登录 【手机号】仅支持私聊使用，群聊发起网页登录请使用 tjd网页登录")
            return
        phone = safe_str(phone)
        if not phone:
            if self.web_login_server.is_enabled():
                session = self._create_web_login_session(event)
                prompt = self._build_web_login_prompt(session["url"])
                client, message_id = await self._send_and_get_msg_id(event, prompt)
                if client and message_id:
                    self.web_login_recalls[session["id"]] = {
                        "client": client,
                        "message_id": message_id,
                        "task": self._schedule_recall(client, message_id, 110),
                    }
                    return
                yield event.plain_result(prompt)
                return
            yield event.plain_result("格式：tjd登录 【手机号】\n如需网页登录，请先在配置中开启 login_server_enabled")
            return
        if not PHONE_RE.fullmatch(phone):
            yield event.plain_result("格式：tjd登录 【手机号】")
            return
        device_id = uuid.uuid4().hex
        try:
            data = await self.client.send_captcha(phone, device_id)
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"验证码发送失败：{exc}")
            return
        self.pending_logins[self._identity_key(event)] = {
            "phone": phone,
            "device_id": safe_str(data.get("deviceId"), device_id),
            "expires_at": int(dt.datetime.now().timestamp() * 1000) + CAPTCHA_WAIT_TIMEOUT_MS,
        }
        yield event.plain_result(
            f"验证码已发送到 {format_phone(phone)}，请直接发送下一条 6 位验证码完成登录。"
        )

    @filter.command("tjd网页登录", alias={"塔吉多网页登录", "tjdweb登录", "tjdweb"})
    async def web_login_command(self, event: AstrMessageEvent):
        if not self.web_login_server.is_enabled():
            yield event.plain_result("当前未开启网页登录服务，请先在配置中开启 login_server_enabled")
            return
        session = self._create_web_login_session(event)
        prompt = self._build_web_login_prompt(session["url"])
        client, message_id = await self._send_and_get_msg_id(
            event,
            prompt,
            mention_sender=not event.is_private_chat(),
        )
        if client and message_id:
            self.web_login_recalls[session["id"]] = {
                "client": client,
                "message_id": message_id,
                "task": self._schedule_recall(client, message_id, 110),
            }
            return
        yield event.chain_result(self._build_web_login_chain(event, prompt))

    @filter.regex(r"^\d{6}$")
    async def consume_captcha(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not event.is_private_chat():
            return
        pending = self.pending_logins.get(self._identity_key(event))
        if not pending:
            return
        captcha = safe_str(event.get_message_str())
        if not CAPTCHA_RE.fullmatch(captcha):
            return
        if to_int(pending.get("expires_at"), 0) <= int(dt.datetime.now().timestamp() * 1000):
            self.pending_logins.pop(self._identity_key(event), None)
            yield event.plain_result("验证码等待已超时，请重新发送：tjd登录 【手机号】")
            return
        try:
            session_data = await self.client.create_session(
                phone=pending["phone"],
                captcha=captcha,
                device_id=pending["device_id"],
                platform_id=event.get_platform_id(),
                platform_user_id=event.get_sender_id(),
            )
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"塔吉多登录失败：{exc}")
            return
        session = self._build_account_from_session(
            event,
            session_data,
            phone=safe_str(pending.get("phone")),
            device_id=safe_str(pending.get("device_id")),
        )
        try:
            profile = await self.client.get_profile(session["fwt"])
        except TaJiDuoApiError:
            profile = {}
        if profile:
            session = self._merge_profile(session, profile)
        await self._save_session(event, session)
        try:
            accounts, session, _profile = await self._sync_remote_accounts(
                event,
                session,
                fetch_profile=not bool(profile),
            )
        except TaJiDuoApiError:
            accounts = [session]
        self.pending_logins.pop(self._identity_key(event), None)
        yield event.plain_result(
            "\n".join(
                [
                    "塔吉多登录成功",
                    f"昵称：{self._account_display_name(session)}",
                    f"塔吉多UID：{self._account_uid(session)}",
                    f"已保存账号数：{len(accounts)}",
                    "后续可直接使用：tjd社区签到 / 幻塔签到 / 异环签到",
                ]
            )
        )

    @filter.command(
        "tjd账号",
        alias={"塔吉多账号"},
    )
    async def account_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            accounts, session, profile = await self._sync_remote_accounts(event, session)
            image_path = await self._render_bindings(event, session)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"查询账号失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        lines = [
            "当前塔吉多账号",
            *self._session_summary_lines(session),
            f"已登录账号数：{len(accounts) or 1}",
        ]
        if event.is_private_chat():
            lines.append(f"当前令牌：{mask_token(session.get('fwt'))}")
        if safe_str(profile.get("introduce")):
            lines.append(f"个性签名：{safe_str(profile.get('introduce'))}")
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "tjd账号列表",
        alias={"塔吉多账号列表"},
    )
    async def account_list_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return

        try:
            accounts, session, _profile = await self._sync_remote_accounts(
                event,
                session,
                fetch_profile=False,
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"查询账号列表失败：{exc}")
            return

        if not accounts:
            yield event.plain_result("当前没有已保存的塔吉多账号")
            return

        lines = ["塔吉多账号列表"]
        for index, account in enumerate(accounts, start=1):
            mark = "当前" if account.get("is_primary") else "备用"
            line = f"{index}. [{mark}] {self._account_display_name(account)} | UID {self._account_uid(account)}"
            if event.is_private_chat():
                line += f" | {mask_token(account.get('fwt'))}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "tjd切换账号",
        alias={"塔吉多切换账号", "tjd切换登录", "塔吉多切换登录"},
    )
    async def switch_account_command(self, event: AstrMessageEvent, index: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return

        raw_index = safe_str(index)
        if not raw_index.isdigit():
            yield event.plain_result("格式：tjd切换账号 1")
            return

        try:
            accounts, session, _profile = await self._sync_remote_accounts(
                event,
                session,
                fetch_profile=False,
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"读取账号列表失败：{exc}")
            return

        target_index = int(raw_index) - 1
        if target_index < 0 or target_index >= len(accounts):
            yield event.plain_result(f"未找到序号为 {raw_index} 的账号")
            return

        target = accounts[target_index]
        try:
            await self.client.set_primary_account(target["fwt"])
            await self._set_primary_session(event, target["fwt"])
            _accounts, session, _profile = await self._sync_remote_accounts(event, target)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"切换账号失败：{exc}")
            return

        yield event.plain_result(
            "\n".join(
                [
                    "塔吉多主账号切换完成",
                    f"当前账号：{self._account_display_name(session)}",
                    f"塔吉多UID：{self._account_uid(session)}",
                ]
            )
        )

    @filter.command(
        "tjd刷新登录",
        alias={"塔吉多刷新登录", "塔吉多刷新会话", "塔吉多刷新", "tjd刷新", "tjd刷新账号", "塔吉多刷新账号"},
    )
    async def refresh_login_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.refresh_session(session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"刷新登录失败：{exc}")
            return
        session["framework_token"] = safe_str(data.get("fwt"), session["fwt"])
        session["fwt"] = safe_str(data.get("fwt"), session["fwt"])
        session["tjd_uid"] = safe_str(
            data.get("tjdUid") or data.get("tgdUid"), session.get("tjd_uid", "未返回")
        )
        session["tgd_uid"] = safe_str(
            data.get("tgdUid") or data.get("tjdUid"), session.get("tgd_uid", "未返回")
        )
        session["device_id"] = safe_str(data.get("deviceId"), session.get("device_id"))
        session["last_refresh_at"] = safe_str(
            data.get("lastRefreshAt") or data.get("updatedAt"),
            now_iso(),
        )
        await self._save_session(event, session)
        try:
            _accounts, session, _profile = await self._sync_remote_accounts(event, session)
        except TaJiDuoApiError:
            pass
        yield event.plain_result(
            "\n".join(["塔吉多账号刷新完成", *self._session_summary_lines(session)])
        )

    @filter.command(
        "tjd退出登录",
        alias={"塔吉多退出登录", "塔吉多退登", "塔吉多登出", "塔吉多退出"},
    )
    async def logout_command(self, event: AstrMessageEvent):
        accounts = await self._get_accounts(event)
        if not accounts:
            yield event.plain_result("当前没有需要退出的登录账号")
            return
        await self._delete_session(event)
        yield event.plain_result(f"本地已清空 {len(accounts)} 个塔吉多已保存账号")

    @filter.command("tjd删除账号", alias={"塔吉多删除账号"})
    async def delete_account_command(self, event: AstrMessageEvent, index: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前没有可删除的登录账号")
            return

        try:
            accounts, session, _profile = await self._sync_remote_accounts(
                event,
                session,
                fetch_profile=False,
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"读取账号列表失败：{exc}")
            return

        target = session
        raw_index = safe_str(index)
        if raw_index:
            target_index = to_int(raw_index, 0) - 1
            if target_index < 0 or target_index >= len(accounts):
                yield event.plain_result(f"未找到序号为 {raw_index} 的账号")
                return
            target = accounts[target_index]

        try:
            data = await self.client.delete_account(target["fwt"], target["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"删除账号失败：{exc}")
            return

        await self._remove_session_account(event, target["fwt"])
        next_session = await self._get_session(event)
        if next_session.get("fwt"):
            try:
                await self._sync_remote_accounts(event, next_session)
            except TaJiDuoApiError:
                pass

        yield event.plain_result(
            "\n".join(
                [
                    "塔吉多账号已删除",
                    f"删除账号：{self._account_display_name(target)}",
                    f"结果：{safe_str(data.get('message'), '删除成功')}",
                ]
            )
        )

    @filter.command("tjd资料", alias={"塔吉多资料", "tjd信息", "塔吉多信息", "个人资料"})
    async def profile_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            profile = await self.client.get_profile(session["fwt"])
            image_path = await self._render_profile(event, session, profile)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多资料获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_profile_text(session, profile))

    @filter.command(
        "幻塔档案",
        alias={"ht档案", "HT档案", "tof档案", "TOF档案", "幻塔角色数据", "幻塔战绩详情"},
    )
    async def huanta_record_command(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        selector, record_type = self._parse_huanta_record_args(arg1, arg2)
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "huanta", selector)
            if not role:
                yield event.plain_result("当前账号未返回幻塔角色数据")
                return
            data = await self.client.huanta_role_record(
                session["fwt"],
                role_id=safe_str(role.get("roleId")),
                record_type=record_type,
            )
            image_path = await self._render_huanta_record(session, role, data, record_type)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔档案获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_huanta_record_text(session, role, data, record_type))

    @filter.command(
        "异环档案",
        alias={"异环主页", "yh档案", "YH档案", "nte档案", "NTE档案", "yh主页", "异环角色主页"},
    )
    async def yihuan_personal_card_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            role_id = safe_str((role or {}).get("roleId"))
            role_home, profile, record_card = await asyncio.gather(
                self.client.yihuan_role_home(
                    session["fwt"],
                    role_id=role_id,
                ),
                self.client.get_profile(session["fwt"]),
                self.client.yihuan_record_card(session["fwt"]),
            )
            image_path = await self._render_personal_card(
                session, role_home, profile, record_card
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环档案获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_home_text(session, role_home))

    @filter.command(
        "异环角色",
        alias={"yh角色", "YH角色", "nte角色", "NTE角色", "异环角色列表"},
    )
    async def yihuan_characters_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            if not role:
                yield event.plain_result("当前账号未返回异环角色数据")
                return
            role_id = safe_str(role.get("roleId"))
            role_home, data = await asyncio.gather(
                self.client.yihuan_role_home(session["fwt"], role_id=role_id),
                self.client.yihuan_characters(session["fwt"], role_id=role_id),
            )
            image_path = await self._render_yihuan_characters(session, role_home, data)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环角色获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_characters_text(role, data))

    @filter.command(
        "异环成就",
        alias={"yh成就", "YH成就", "nte成就", "NTE成就"},
    )
    async def yihuan_achieve_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            if not role:
                yield event.plain_result("当前账号未返回异环角色数据")
                return
            role_id = safe_str(role.get("roleId"))
            role_home, data = await asyncio.gather(
                self.client.yihuan_role_home(session["fwt"], role_id=role_id),
                self.client.yihuan_achieve_progress(session["fwt"], role_id=role_id),
            )
            image_path = await self._render_yihuan_achievements(session, role_home, data)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环成就获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_achieve_text(role, data))

    @filter.command(
        "异环探索",
        alias={"yh探索", "YH探索", "nte探索", "NTE探索"},
    )
    async def yihuan_area_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            if not role:
                yield event.plain_result("当前账号未返回异环角色数据")
                return
            role_id = safe_str(role.get("roleId"))
            role_home, data = await asyncio.gather(
                self.client.yihuan_role_home(session["fwt"], role_id=role_id),
                self.client.yihuan_area_progress(session["fwt"], role_id=role_id),
            )
            image_path = await self._render_yihuan_exploration(session, role_home, data)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环探索获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_area_text(role, data))

    @filter.command(
        "异环房产",
        alias={"yh房产", "YH房产", "nte房产", "NTE房产"},
    )
    async def yihuan_real_estate_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            if not role:
                yield event.plain_result("当前账号未返回异环角色数据")
                return
            role_id = safe_str(role.get("roleId"))
            role_home, data = await asyncio.gather(
                self.client.yihuan_role_home(session["fwt"], role_id=role_id),
                self.client.yihuan_real_estate(session["fwt"], role_id=role_id),
            )
            image_path = await self._render_yihuan_real_estate(session, role_home, data)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环房产获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_real_estate_text(role, data))

    @filter.command(
        "异环载具",
        alias={"yh载具", "YH载具", "nte载具", "NTE载具"},
    )
    async def yihuan_vehicles_command(self, event: AstrMessageEvent, selector: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            _roles, role, _remainder = await self._resolve_game_role(session, "yihuan", selector)
            if not role:
                yield event.plain_result("当前账号未返回异环角色数据")
                return
            role_id = safe_str(role.get("roleId"))
            role_home, data = await asyncio.gather(
                self.client.yihuan_role_home(session["fwt"], role_id=role_id),
                self.client.yihuan_vehicles(session["fwt"], role_id=role_id),
            )
            image_path = await self._render_yihuan_vehicles(session, role_home, data)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环载具获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._build_yihuan_vehicles_text(role, data))

    @filter.command("幻塔签到状态", alias={"ht签到状态", "HT签到状态", "tof签到状态", "TOF签到状态"})
    async def huanta_sign_state_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.sign_state("huanta", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔签到状态获取失败：{exc}")
            return
        yield event.plain_result(self._build_sign_state_message("幻塔", session, data))

    @filter.command("异环签到状态", alias={"yh签到状态", "YH签到状态", "nte签到状态", "NTE签到状态"})
    async def yihuan_sign_state_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.sign_state("yihuan", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环签到状态获取失败：{exc}")
            return
        yield event.plain_result(self._build_sign_state_message("异环", session, data))

    @filter.command("幻塔补签", alias={"ht补签", "HT补签", "tof补签", "TOF补签"})
    async def huanta_resign_command(self, event: AstrMessageEvent, role_id: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        target_role_id = extract_role_id(role_id)
        if not target_role_id:
            yield event.plain_result("格式：幻塔补签 <角色ID>")
            return
        try:
            data = await self.client.sign_resign("huanta", session["fwt"], target_role_id)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔补签失败：{exc}")
            return
        yield event.plain_result(
            f"幻塔补签\n角色ID：{target_role_id}\n结果：{safe_str(data.get('message'), '处理完成')}"
        )

    @filter.command("异环补签", alias={"yh补签", "YH补签", "nte补签", "NTE补签"})
    async def yihuan_resign_command(self, event: AstrMessageEvent, role_id: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        target_role_id = extract_role_id(role_id)
        if not target_role_id:
            yield event.plain_result("格式：异环补签 <角色ID>")
            return
        try:
            data = await self.client.sign_resign("yihuan", session["fwt"], target_role_id)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环补签失败：{exc}")
            return
        yield event.plain_result(
            f"异环补签\n角色ID：{target_role_id}\n结果：{safe_str(data.get('message'), '处理完成')}"
        )

    async def huanta_community_level_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.community_exp_level("huanta", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔社区等级获取失败：{exc}")
            return
        yield event.plain_result(self._build_community_level_message("幻塔", session, data))

    async def yihuan_community_level_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.community_exp_level("yihuan", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环社区等级获取失败：{exc}")
            return
        yield event.plain_result(self._build_community_level_message("异环", session, data))

    async def huanta_tasks_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.community_tasks("huanta", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔任务获取失败：{exc}")
            return
        lines = ["幻塔任务"]
        for item in flatten_tasks(data)[:12]:
            lines.append(f"{safe_str(item.get('title') or item.get('taskKey'), '任务')}：{format_task_value(item)}")
        yield event.plain_result("\n".join(lines))

    async def yihuan_tasks_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.community_tasks("yihuan", session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环任务获取失败：{exc}")
            return
        lines = ["异环任务"]
        for item in flatten_tasks(data)[:12]:
            lines.append(f"{safe_str(item.get('title') or item.get('taskKey'), '任务')}：{format_task_value(item)}")
        yield event.plain_result("\n".join(lines))

    async def huanta_bind_role_command(self, event: AstrMessageEvent, role_id: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        target_role_id = extract_role_id(role_id)
        if not target_role_id:
            yield event.plain_result("格式：幻塔绑定角色 <角色ID>")
            return
        try:
            data = await self.client.bind_game_role(
                session["fwt"], game_code="huanta", role_id=target_role_id
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"幻塔绑定角色失败：{exc}")
            return
        yield event.plain_result(
            f"幻塔绑定角色\n角色ID：{target_role_id}\n结果：{safe_str(data.get('message'), '绑定成功')}"
        )

    async def yihuan_bind_role_command(self, event: AstrMessageEvent, role_id: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        target_role_id = extract_role_id(role_id)
        if not target_role_id:
            yield event.plain_result("格式：异环绑定角色 <角色ID>")
            return
        try:
            data = await self.client.bind_game_role(
                session["fwt"], game_code="yihuan", role_id=target_role_id
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"异环绑定角色失败：{exc}")
            return
        yield event.plain_result(
            f"异环绑定角色\n角色ID：{target_role_id}\n结果：{safe_str(data.get('message'), '绑定成功')}"
        )

    async def sign_reward_records_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.sign_reward_records(session["fwt"], count=15)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"奖励记录获取失败：{exc}")
            return
        yield event.plain_result(
            self._build_reward_records_message("塔吉多奖励记录", to_list(data))
        )

    async def shop_income_records_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.shop_coin_income_records(session["fwt"], size=15)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多收入记录获取失败：{exc}")
            return
        yield event.plain_result(
            self._build_shop_coin_records_message("塔吉多收入记录", to_list(data))
        )

    async def shop_consume_records_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result(self._login_hint())
            return
        try:
            data = await self.client.shop_coin_consume_records(session["fwt"], size=15)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多消费记录获取失败：{exc}")
            return
        yield event.plain_result(
            self._build_shop_coin_records_message("塔吉多消费记录", to_list(data))
        )

    @filter.command(
        "tjd绑定列表",
        alias={"塔吉多账号绑定"},
    )
    async def bindings_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】")
            return
        try:
            _accounts, session, _profile = await self._sync_remote_accounts(event, session)
            image_path = await self._render_bindings(event, session)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"绑定列表获取失败：{exc}")
            return
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result("\n".join(["塔吉多账号绑定", *self._session_summary_lines(session)]))

    @filter.command(
        "tjd社区签到",
        alias={"塔吉多社区签到", "社区签到"},
    )
    async def all_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(
            event, ["huanta", "yihuan"], "tjd社区签到"
        ):
            yield item

    @filter.command(
        "幻塔社区签到",
        alias={"塔吉多幻塔社区签到", "HT社区签到", "TOF社区签到", "ht社区签到", "tof社区签到"},
    )
    async def huanta_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(event, ["huanta"], "幻塔社区签到"):
            yield item

    @filter.command(
        "异环社区签到",
        alias={"塔吉多异环社区签到", "YH社区签到", "NTE社区签到", "yh社区签到", "nte社区签到"},
    )
    async def yihuan_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(event, ["yihuan"], "异环社区签到"):
            yield item

    @filter.command(
        "tjd签到查询",
        alias={"塔吉多签到查询"},
    )
    async def all_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(
            event, ["huanta", "yihuan"], "tjd签到查询"
        ):
            yield item

    @filter.command(
        "幻塔签到查询",
        alias={
            "塔吉多幻塔签到查询",
            "HT签到查询",
            "TOF签到查询",
            "ht签到查询",
            "tof签到查询",
        },
    )
    async def huanta_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(event, ["huanta"], "幻塔签到查询"):
            yield item

    @filter.command(
        "异环签到查询",
        alias={
            "塔吉多异环签到查询",
            "YH签到查询",
            "NTE签到查询",
            "yh签到查询",
            "nte签到查询",
        },
    )
    async def yihuan_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(event, ["yihuan"], "异环签到查询"):
            yield item

    async def manual_all_community_sign_command(self, event: AstrMessageEvent):
        if not self._check_admin(event):
            yield event.plain_result("暂无权限，只有 bot 管理员才能执行全部社区签到")
            return
        sessions = await self.storage.list_sessions()
        if not sessions:
            yield event.plain_result("当前没有已保存账号，无法执行全部社区签到")
            return
        lines = [f"塔吉多全部社区签到结果", f"执行账号数：{len(sessions)}"]
        for session in sessions:
            try:
                records = await self._run_all_community_sign_for_session(session)
                lines.append(self._summarize_session_records(session, records))
            except Exception as exc:
                lines.append(
                    f"{session.get('username') or session.get('tgd_uid') or session.get('identity_key')}: 失败 | {exc}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "幻塔签到",
        alias={"幻塔游戏签到", "塔吉多幻塔签到", "HT签到", "TOF签到", "ht签到", "tof签到"},
    )
    async def huanta_game_sign_command(
        self, event: AstrMessageEvent, selector: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】")
            return
        try:
            result = await self._execute_game_sign_for_game(session, "huanta", selector)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多幻塔游戏签到失败：{exc}")
            return
        yield event.plain_result(self._build_game_sign_text(session, result))

    @filter.command(
        "异环签到",
        alias={"异环游戏签到", "塔吉多异环签到", "YH签到", "NTE签到", "yh签到", "nte签到"},
    )
    async def yihuan_game_sign_command(
        self, event: AstrMessageEvent, selector: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：tjd登录 【手机号】")
            return
        try:
            result = await self._execute_game_sign_for_game(session, "yihuan", selector)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多异环游戏签到失败：{exc}")
            return
        yield event.plain_result(self._build_game_sign_text(session, result))

    async def manual_all_game_sign_command(self, event: AstrMessageEvent):
        if not self._check_admin(event):
            yield event.plain_result("暂无权限，只有 bot 管理员才能执行全部游戏签到")
            return
        sessions = await self.storage.list_sessions()
        if not sessions:
            yield event.plain_result("当前没有已保存账号，无法执行全部游戏签到")
            return
        lines = [f"塔吉多全部游戏签到结果", f"执行账号数：{len(sessions)}"]
        for session in sessions:
            try:
                records = await self._run_all_game_sign_for_session(session)
                parts = []
                for item in records:
                    if item.get("success"):
                        parts.append(f"{item['gameName']}:成功")
                    else:
                        parts.append(f"{item['gameName']}:失败")
                label = session.get("username") or session.get("tgd_uid") or session.get("identity_key")
                lines.append(f"{label} | {' | '.join(parts)}")
            except Exception as exc:
                lines.append(
                    f"{session.get('username') or session.get('identity_key')}: 失败 | {exc}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("tjd兑换码", alias={"塔吉多兑换码", "兑换码"})
    async def redeem_codes_command(self, event: AstrMessageEvent):
        try:
            data = await self.client.list_redeem_codes()
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"塔吉多兑换码失败：{exc}")
            return
        items = data.get("items") or []
        yield event.plain_result(self._build_redeem_message("塔吉多兑换码", items))

    async def redeem_huanta_codes_command(self, event: AstrMessageEvent):
        try:
            data = await self.client.list_redeem_codes(game_code="huanta")
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"塔吉多幻塔兑换码失败：{exc}")
            return
        items = data.get("items") or []
        yield event.plain_result(
            self._build_redeem_message("塔吉多幻塔兑换码", items, "huanta")
        )

    async def redeem_yihuan_codes_command(self, event: AstrMessageEvent):
        try:
            data = await self.client.list_redeem_codes(game_code="yihuan")
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"塔吉多异环兑换码失败：{exc}")
            return
        items = data.get("items") or []
        yield event.plain_result(
            self._build_redeem_message("塔吉多异环兑换码", items, "yihuan")
        )

    @filter.command("tjd商城", alias={"塔吉多商城", "商城"})
    async def shop_goods_command(
        self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：tjd登录 【手机号】 完成登录")
            return
        payload = self._parse_shop_goods_args(arg1, arg2, arg3)
        try:
            data = await self.client.list_shop_goods(
                session["fwt"],
                tab=payload["tab"],
                count=payload["count"],
                version=payload["version"],
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多商城失败：{exc}")
            return
        yield event.plain_result(self._build_shop_goods_message(data, payload))

    async def shop_detail_command(self, event: AstrMessageEvent, keyword: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：tjd登录 【手机号】 完成登录")
            return
        try:
            goods_id, catalog_item, _catalog = await self._resolve_shop_goods(
                session["fwt"], keyword
            )
            data = await self.client.get_shop_goods_detail(goods_id, session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多商品详情失败：{exc}")
            return
        yield event.plain_result(
            self._build_shop_detail_message(data, goods_id, catalog_item)
        )

    @filter.command(
        "tjd币",
        alias={"塔吉多币", "塔吉多币查询", "塔币", "塔币查询", "币状态"},
    )
    async def shop_coin_state_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：tjd登录 【手机号】 完成登录")
            return
        try:
            data = await self.client.get_shop_coin_state(session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多币状态失败：{exc}")
            return
        yield event.plain_result(self._build_coin_state_message(data))

    async def shop_roles_command(self, event: AstrMessageEvent, game_name: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：tjd登录 【手机号】 完成登录")
            return
        requested = normalize_shop_tab(game_name)
        metas = []
        if requested == "all":
            metas = list(SHOP_GAME_META.values())
        else:
            selected = resolve_shop_game_from_tab(requested)
            if not selected:
                yield event.plain_result("格式：tjd商城角色列表 [幻塔/异环]")
                return
            metas = [selected]

        messages = []
        for meta in metas:
            try:
                data = await self.client.get_shop_game_roles(session["fwt"], meta["game_id"])
                messages.append(self._build_shop_roles_message(meta, data))
            except TaJiDuoApiError as exc:
                messages.append(f"{meta['label']}商城角色列表\n查询失败：{exc}")
        yield event.plain_result("\n\n".join(messages))

    async def shop_exchange_command(
        self, event: AstrMessageEvent, keyword: str = "", count: str = "1"
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：tjd登录 【手机号】 完成登录")
            return
        try:
            goods_id, catalog_item, _catalog = await self._resolve_shop_goods(
                session["fwt"], keyword
            )
            tab = safe_str((catalog_item or {}).get("tab"))
            game_meta = resolve_shop_game_from_tab(tab)
            if not game_meta:
                yield event.plain_result("未能识别该商品所属游戏，暂时无法自动兑换")
                return
            roles_data = await self.client.get_shop_game_roles(
                session["fwt"], game_meta["game_id"]
            )
            role = self._pick_shop_role(roles_data)
            if not role:
                yield event.plain_result(
                    f"当前账号未返回 {game_meta['label']} 的已绑定商城角色，暂时无法自动兑换。\n"
                    f"可先发送：tjd商城角色列表 {game_meta['label']}"
                )
                return
            data = await self.client.shop_exchange(
                session["fwt"],
                goods_id=goods_id,
                game_id=game_meta["game_id"],
                role_id=safe_str(role.get("roleId")),
                count=max(to_int(count, 1), 1),
            )
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多商品兑换失败：{exc}")
            return

        yield event.plain_result(
            "\n".join(
                [
                    "塔吉多商品兑换",
                    f"商品：{safe_str((catalog_item or {}).get('name'), goods_id)}",
                    f"游戏：{game_meta['label']}",
                    f"角色：{safe_str(role.get('roleName'), '未命名角色')}",
                    f"数量：{max(to_int(count, 1), 1)}",
                    f"结果：{safe_str(data.get('message'), '兑换成功')}",
                ]
            )
        )

    async def update_command(self, event: AstrMessageEvent):
        if not self._check_admin(event):
            yield event.plain_result("暂无权限，只有 bot 管理员才能执行更新命令")
            return
        yield event.plain_result(
            "AstrBot 版暂不支持直接复用 Yunzai 的热更新逻辑。\n"
            "如需更新插件，请直接更新插件目录文件后重载插件。"
        )

    async def terminate(self):
        if self._pending_cleanup_task and not self._pending_cleanup_task.done():
            self._pending_cleanup_task.cancel()
            try:
                await self._pending_cleanup_task
            except asyncio.CancelledError:
                pass
        if self._auto_community_task and not self._auto_community_task.done():
            self._auto_community_task.cancel()
            try:
                await self._auto_community_task
            except asyncio.CancelledError:
                pass
        for tracked in self.web_login_recalls.values():
            recall_task = tracked.get("task")
            if recall_task and not recall_task.done():
                recall_task.cancel()
        self.web_login_recalls.clear()
        self.web_login_server.close()
        await self.client.close()
        await self.renderer.close()
