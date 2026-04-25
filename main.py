import asyncio
import datetime as dt
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain

from .core.client import TaJiDuoApiError, TaJiDuoClient
from .core.render import Renderer
from .core.storage import SessionStorage

PHONE_RE = re.compile(r"^1\d{10}$")
CAPTCHA_RE = re.compile(r"^\d{6}$")
CAPTCHA_WAIT_TIMEOUT_MS = 300000

COMMUNITY_GAME_META = {
    "huanta": {
        "label": "幻塔",
        "logo": "img/bind/ht_link.png",
        "community_command": "塔吉多幻塔社区签到",
        "query_command": "塔吉多幻塔社区查询",
        "game_command": "塔吉多幻塔签到",
    },
    "yihuan": {
        "label": "异环",
        "logo": "img/bind/yh_link.png",
        "community_command": "塔吉多异环社区签到",
        "query_command": "塔吉多异环社区查询",
        "game_command": "塔吉多异环签到",
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
    return f"{text[:3]}****{text[-4:]}"


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


@register(
    "astrbot_plugin_TaJiDuo",
    "Codex",
    "塔吉多异环幻塔插件",
    "0.2.0",
    "https://github.com/openai/codex",
)
class TaJiDuoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.client = TaJiDuoClient(
            base_url=self.config.get("base_url", "https://tajiduo.shallow.ink"),
            api_key=self.config.get("api_key", "tjd-8FtI7adTkMHMjZaE"),
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
        self._pending_cleanup_task = asyncio.create_task(
            self._pending_login_cleanup_loop()
        )
        self._auto_community_task: Optional[asyncio.Task] = None
        if self.auto_community_sign_enabled:
            self._auto_community_task = asyncio.create_task(
                self._auto_community_sign_loop()
            )

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
            except asyncio.CancelledError:
                break
            except Exception:
                continue

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
        return await self.storage.get_session(self._identity_key(event))

    async def _save_session(self, event: AstrMessageEvent, session: Dict[str, Any]) -> None:
        session["identity_key"] = self._identity_key(event)
        session["platform_id"] = event.get_platform_id()
        session["self_id"] = event.get_self_id()
        session["user_id"] = event.get_sender_id()
        session["last_origin"] = event.unified_msg_origin
        session["updated_at"] = now_iso()
        if not session.get("created_at"):
            session["created_at"] = session["updated_at"]
        await self.storage.save_session(session["identity_key"], session)

    async def _delete_session(self, event: AstrMessageEvent) -> None:
        await self.storage.delete_session(self._identity_key(event))
        self.pending_logins.pop(self._identity_key(event), None)

    def _auth_hint(self) -> str:
        return "当前登录态已失效，请重新私聊发送：塔吉多登录 [手机号]"

    def _check_admin(self, event: AstrMessageEvent) -> bool:
        return event.is_admin()

    def _build_help_sections(self) -> List[Dict[str, Any]]:
        prefix = self.help_prefix_display

        def cmd(text: str) -> str:
            return f"{prefix}{text}"

        return [
            {
                "title": "帮助命令",
                "items": [
                    {"command": cmd("塔吉多帮助"), "description": "查看插件帮助"},
                    {"command": cmd("tjd帮助"), "description": "帮助别名"},
                    {"command": cmd("ht帮助"), "description": "幻塔帮助别名"},
                    {"command": cmd("yh帮助"), "description": "异环帮助别名"},
                ],
            },
            {
                "title": "登录命令",
                "items": [
                    {
                        "command": cmd("塔吉多登录 [手机号]"),
                        "description": "发送验证码并等待下一条 6 位验证码",
                    },
                    {"command": cmd("塔吉多账号"), "description": "查看当前登录账号"},
                    {"command": cmd("塔吉多刷新登录"), "description": "刷新当前登录账号"},
                    {"command": cmd("塔吉多退出登录"), "description": "退出当前登录"},
                    {"command": cmd("塔吉多删除账号"), "description": "删除当前登录账号"},
                    {"command": cmd("塔吉多绑定列表"), "description": "查看账号绑定概览"},
                ],
            },
            {
                "title": "社区功能",
                "items": [
                    {"command": cmd("塔吉多社区签到"), "description": "依次执行幻塔与异环社区签到"},
                    {"command": cmd("塔吉多幻塔社区签到"), "description": "执行幻塔社区签到"},
                    {"command": cmd("塔吉多异环社区签到"), "description": "执行异环社区签到"},
                    {"command": cmd("塔吉多社区查询"), "description": "查看全部社区任务状态"},
                    {"command": cmd("塔吉多幻塔社区查询"), "description": "查看幻塔社区任务状态"},
                    {"command": cmd("塔吉多异环社区查询"), "description": "查看异环社区任务状态"},
                    {"command": cmd("塔吉多全部社区签到"), "description": "管理员批量执行全部账号社区签到"},
                ],
            },
            {
                "title": "游戏与商城",
                "items": [
                    {"command": cmd("塔吉多幻塔签到"), "description": "执行幻塔游戏签到，可附带角色名/序号/ID"},
                    {"command": cmd("塔吉多异环签到"), "description": "执行异环游戏签到，可附带角色名/序号/ID"},
                    {"command": cmd("塔吉多全部游戏签到"), "description": "管理员批量执行全部账号游戏签到"},
                    {"command": cmd("塔吉多兑换码"), "description": "查看全部兑换码"},
                    {"command": cmd("塔吉多幻塔兑换码"), "description": "查看幻塔兑换码"},
                    {"command": cmd("塔吉多异环兑换码"), "description": "查看异环兑换码"},
                    {"command": cmd("塔吉多商城 [分区] [数量]"), "description": "查看商城商品"},
                    {"command": cmd("塔吉多商品 [ID/关键词]"), "description": "查看商品详情"},
                    {"command": cmd("塔吉多币"), "description": "查看塔吉多币状态"},
                    {"command": cmd("塔吉多商城角色列表 [幻塔/异环]"), "description": "查看商城角色"},
                    {"command": cmd("塔吉多兑换商品 [ID/关键词] [数量]"), "description": "兑换商城商品"},
                    {"command": cmd("塔吉多更新"), "description": "AstrBot 版占位更新命令"},
                ],
            },
        ]

    def _build_help_notes(self) -> List[str]:
        return [
            "登录相关命令仅支持私聊使用。",
            "已保存账号会在每天 00:20 自动执行社区签到。",
            "部分商城与批量功能已接入当前后端，未开放能力会在命令中直接提示。",
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
            f"昵称：{session.get('username') or '未返回'}",
            f"塔吉多UID：{session.get('tgd_uid') or '未返回'}",
            f"更新时间：{session.get('updated_at') or '未记录'}",
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
        role_name = safe_str(
            first_role.get("roleName") or first_role.get("name"), "未命名角色"
        )
        level = safe_str(first_role.get("lev") or first_role.get("level"), "-")
        server = safe_str(first_role.get("serverName"), "已绑定")
        role_id = safe_str(first_role.get("roleId"), "未返回")
        binding.update(
            {
                "hasAccount": True,
                "name": role_name,
                "level": level,
                "server": server,
                "statusText": f"共 {len(roles)} 个角色",
                "stats": [
                    {"label": "角色ID", "value": role_id},
                    {"label": "等级", "value": level},
                    {"label": "服务器", "value": server},
                    {"label": "角色数", "value": str(len(roles))},
                ],
            }
        )
        return binding

    async def _build_bindings_render_data(
        self, event: AstrMessageEvent, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        accounts = await self.client.list_accounts(session["fwt"])
        primary = accounts.get("primary") or {}
        items = accounts.get("items") or []
        tgd_uid = safe_str(primary.get("tgdUid") or session.get("tgd_uid"), "未返回")
        summary_fields = [
            {"label": "塔吉多账号", "value": session.get("username") or "未返回"},
            {"label": "塔吉多UID", "value": tgd_uid},
            {"label": "已登录账号", "value": str(len(items) or 1)},
            {
                "label": "最近刷新",
                "value": safe_str(
                    primary.get("lastRefreshAt") or session.get("updated_at"), "未记录"
                ),
            },
        ]
        game_bindings = [
            await self._fetch_role_binding(session, "huanta"),
            await self._fetch_role_binding(session, "yihuan"),
        ]
        return {
            "pageTitle": "塔吉多账号绑定",
            "accountFields": summary_fields,
            "gameBindings": game_bindings,
            "pluResPath": self.renderer.get_res_path(""),
            "userName": event.get_sender_name() or session.get("username") or "TaJiDuo",
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
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
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
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
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

    @filter.command(
        "塔吉多帮助",
        alias={"塔吉多菜单", "tjd帮助", "tof帮助", "ht帮助", "nte帮助", "yh帮助"},
    )
    async def help_command(self, event: AstrMessageEvent):
        image_path = await self._render_menu()
        if image_path:
            yield event.image_result(image_path)
            return
        yield event.plain_result(self._help_text())

    @filter.command("塔吉多登录", alias={"tjd登录"})
    async def login_command(self, event: AstrMessageEvent, phone: str = ""):
        if not event.is_private_chat():
            yield event.plain_result("塔吉多登录命令仅支持私聊使用")
            return
        phone = safe_str(phone)
        if not PHONE_RE.fullmatch(phone):
            yield event.plain_result("格式：塔吉多登录 13800138000")
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

    @filter.regex(r"^\d{6}$")
    async def consume_captcha(self, event: AstrMessageEvent):
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
            yield event.plain_result("验证码等待已超时，请重新发送：塔吉多登录 [手机号]")
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
        session = {
            "identity_key": self._identity_key(event),
            "username": safe_str(session_data.get("username"), "未返回"),
            "tgd_uid": safe_str(
                session_data.get("tjdUid") or session_data.get("tgdUid"), "未返回"
            ),
            "fwt": safe_str(session_data.get("fwt")),
            "device_id": safe_str(session_data.get("deviceId")),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        await self._save_session(event, session)
        self.pending_logins.pop(self._identity_key(event), None)
        yield event.plain_result(
            "\n".join(
                [
                    "塔吉多登录成功",
                    f"昵称：{session['username']}",
                    f"塔吉多UID：{session['tgd_uid']}",
                    "后续可直接使用：塔吉多社区签到 / 塔吉多幻塔签到 / 塔吉多异环签到",
                ]
            )
        )

    @filter.command(
        "塔吉多账号",
        alias={"塔吉多会话", "塔吉多状态", "tjd账号", "tjd状态"},
    )
    async def account_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
        try:
            accounts = await self.client.list_accounts(session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"查询账号失败：{exc}")
            return
        primary = accounts.get("primary") or {}
        if primary.get("tgdUid"):
            session["tgd_uid"] = safe_str(primary.get("tgdUid"))
            await self._save_session(event, session)
        lines = [
            "当前塔吉多账号",
            *self._session_summary_lines(session),
            f"已登录账号数：{len(accounts.get('items') or []) or 1}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "塔吉多刷新登录",
        alias={"塔吉多刷新会话", "塔吉多刷新", "tjd刷新登录", "tjd刷新"},
    )
    async def refresh_login_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前没有可刷新的登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
        try:
            data = await self.client.refresh_session(session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"刷新登录失败：{exc}")
            return
        session["fwt"] = safe_str(data.get("fwt"), session["fwt"])
        session["tgd_uid"] = safe_str(
            data.get("tjdUid") or data.get("tgdUid"), session.get("tgd_uid", "未返回")
        )
        await self._save_session(event, session)
        yield event.plain_result(
            "\n".join(["塔吉多登录刷新完成", *self._session_summary_lines(session)])
        )

    @filter.command(
        "塔吉多退出登录",
        alias={"塔吉多退登", "塔吉多登出", "塔吉多退出", "tjd退出登录"},
    )
    async def logout_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前没有需要退出的登录账号")
            return
        await self._delete_session(event)
        yield event.plain_result("当前塔吉多登录已退出")

    @filter.command("塔吉多删除账号", alias={"tjd删除账号"})
    async def delete_account_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前没有可删除的登录账号")
            return
        try:
            data = await self.client.delete_account(session["fwt"])
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"删除账号失败：{exc}")
            return
        await self._delete_session(event)
        yield event.plain_result(
            f"塔吉多账号已删除\n结果：{safe_str(data.get('message'), '删除成功')}"
        )

    @filter.command(
        "塔吉多绑定列表",
        alias={"塔吉多绑定", "塔吉多账号绑定", "tjd绑定列表"},
    )
    async def bindings_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
        try:
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
        "塔吉多社区签到",
        alias={"社区签到", "tjd社区签到"},
    )
    async def all_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(
            event, ["huanta", "yihuan"], "塔吉多社区签到"
        ):
            yield item

    @filter.command(
        "塔吉多幻塔社区签到",
        alias={"幻塔社区签到", "HT社区签到", "TOF社区签到", "ht社区签到", "tof社区签到"},
    )
    async def huanta_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(event, ["huanta"], "塔吉多幻塔社区签到"):
            yield item

    @filter.command(
        "塔吉多异环社区签到",
        alias={"异环社区签到", "YH社区签到", "NTE社区签到", "yh社区签到", "nte社区签到"},
    )
    async def yihuan_community_sign_command(self, event: AstrMessageEvent):
        async for item in self._community_sign(event, ["yihuan"], "塔吉多异环社区签到"):
            yield item

    @filter.command(
        "塔吉多社区查询",
        alias={"社区查询", "tjd社区查询"},
    )
    async def all_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(
            event, ["huanta", "yihuan"], "塔吉多社区查询"
        ):
            yield item

    @filter.command(
        "塔吉多幻塔社区查询",
        alias={"幻塔社区查询", "HT社区查询", "TOF社区查询", "ht社区查询", "tof社区查询"},
    )
    async def huanta_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(event, ["huanta"], "塔吉多幻塔社区查询"):
            yield item

    @filter.command(
        "塔吉多异环社区查询",
        alias={"异环社区查询", "YH社区查询", "NTE社区查询", "yh社区查询", "nte社区查询"},
    )
    async def yihuan_community_query_command(self, event: AstrMessageEvent):
        async for item in self._community_query(event, ["yihuan"], "塔吉多异环社区查询"):
            yield item

    @filter.command(
        "塔吉多全部社区签到",
        alias={"塔吉多手动社区签到", "塔吉多管理员社区签到", "全部社区签到", "手动社区签到", "管理员社区签到"},
    )
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
        "塔吉多幻塔签到",
        alias={"塔吉多幻塔游戏签到", "幻塔签到", "幻塔游戏签到", "HT签到", "TOF签到", "ht签到", "tof签到"},
    )
    async def huanta_game_sign_command(
        self, event: AstrMessageEvent, selector: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
        try:
            result = await self._execute_game_sign_for_game(session, "huanta", selector)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多幻塔签到失败：{exc}")
            return
        yield event.plain_result(self._build_game_sign_text(session, result))

    @filter.command(
        "塔吉多异环签到",
        alias={"塔吉多异环游戏签到", "异环签到", "异环游戏签到", "YH签到", "NTE签到", "yh签到", "nte签到"},
    )
    async def yihuan_game_sign_command(
        self, event: AstrMessageEvent, selector: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("当前还没有已登录账号，请先私聊发送：塔吉多登录 [手机号]")
            return
        try:
            result = await self._execute_game_sign_for_game(session, "yihuan", selector)
        except TaJiDuoApiError as exc:
            if exc.is_auth_error:
                yield event.plain_result(self._auth_hint())
                return
            yield event.plain_result(f"塔吉多异环签到失败：{exc}")
            return
        yield event.plain_result(self._build_game_sign_text(session, result))

    @filter.command(
        "塔吉多全部游戏签到",
        alias={"塔吉多手动游戏签到", "塔吉多管理员游戏签到", "全部游戏签到", "手动游戏签到", "管理员游戏签到"},
    )
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

    @filter.command("塔吉多兑换码", alias={"兑换码", "tjd兑换码"})
    async def redeem_codes_command(self, event: AstrMessageEvent):
        try:
            data = await self.client.list_redeem_codes()
        except TaJiDuoApiError as exc:
            yield event.plain_result(f"塔吉多兑换码失败：{exc}")
            return
        items = data.get("items") or []
        yield event.plain_result(self._build_redeem_message("塔吉多兑换码", items))

    @filter.command(
        "塔吉多幻塔兑换码",
        alias={"幻塔兑换码", "HT兑换码", "TOF兑换码", "ht兑换码", "tof兑换码"},
    )
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

    @filter.command(
        "塔吉多异环兑换码",
        alias={"异环兑换码", "YH兑换码", "NTE兑换码", "yh兑换码", "nte兑换码"},
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

    @filter.command("塔吉多商城", alias={"商城", "tjd商城"})
    async def shop_goods_command(
        self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：塔吉多登录 [手机号] 完成登录")
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

    @filter.command("塔吉多商品", alias={"商品", "tjd商品"})
    async def shop_detail_command(self, event: AstrMessageEvent, keyword: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：塔吉多登录 [手机号] 完成登录")
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
        "塔吉多币",
        alias={"塔吉多币查询", "塔币", "塔币查询", "币状态", "tjd币"},
    )
    async def shop_coin_state_command(self, event: AstrMessageEvent):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：塔吉多登录 [手机号] 完成登录")
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

    @filter.command(
        "塔吉多商城角色列表",
        alias={"塔吉多商城角色", "商城角色列表", "商城角色", "tjd商城角色列表"},
    )
    async def shop_roles_command(self, event: AstrMessageEvent, game_name: str = ""):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：塔吉多登录 [手机号] 完成登录")
            return
        requested = normalize_shop_tab(game_name)
        metas = []
        if requested == "all":
            metas = list(SHOP_GAME_META.values())
        else:
            selected = resolve_shop_game_from_tab(requested)
            if not selected:
                yield event.plain_result("格式：塔吉多商城角色列表 [幻塔/异环]")
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

    @filter.command(
        "塔吉多兑换商品",
        alias={"塔吉多商城兑换", "兑换商品", "商城兑换", "tjd兑换商品"},
    )
    async def shop_exchange_command(
        self, event: AstrMessageEvent, keyword: str = "", count: str = "1"
    ):
        session = await self._get_session(event)
        if not session.get("fwt"):
            yield event.plain_result("请先私聊发送：塔吉多登录 [手机号] 完成登录")
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
                    f"可先发送：塔吉多商城角色列表 {game_meta['label']}"
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

    @filter.command(
        "塔吉多更新",
        alias={"更新塔吉多", "塔吉多强制更新", "强制更新塔吉多"},
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
        await self.client.close()
        await self.renderer.close()
