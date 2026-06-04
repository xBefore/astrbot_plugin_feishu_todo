"""飞书待办提醒插件 — AstrBot 插件

用户向小织发送任务详情（文本/截图），插件自动：
1. 调用飞书 Task API 创建待办，设置截止时间 + 原生 2h 提醒
2. 按复杂/普通任务分级设置额外提醒（2天前/1天前）
3. 支持用户口头指定自定义提醒时间
4. 每30分钟巡检一次，推送到期提醒到飞书私聊
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api.star import Star, Context
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger
from pydantic import Field
from pydantic.dataclasses import dataclass

from feishu_api import FeishuTaskClient

_star = None


@dataclass
class CreateReminderTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "create_reminder_task"
    description: str = (
        "当用户在对话中提及待办事项、任务、作业、截止日期(DDL)或学习任务时调用。"
        "从用户消息（文本或截图描述）中提取：任务名称、详细描述、截止时间、是否复杂任务、自定义提醒天数。"
        "复杂任务判断标准：大作业/论文/项目/需要长期准备 = true，日常作业/小事 = false。"
        "即使用户只发了截图，也请先描述截图内容，再提取任务信息调用此工具。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "任务名称或标题，简洁概括",
                },
                "description": {
                    "type": "string",
                    "description": "任务详细描述，用户提供的额外说明。如无则填空字符串",
                },
                "deadline": {
                    "type": "string",
                    "description": "截止时间。格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。仅日期时自动补 23:59",
                },
                "is_complex": {
                    "type": "boolean",
                    "description": "是否为复杂任务。大作业/论文/项目/需长期准备 = true",
                },
                "custom_remind_days": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "用户口头指定的自定义提前提醒天数，如用户说'提前3天提醒'则填[3]。无则填空数组[]",
                },
            },
            "required": ["summary", "deadline", "is_complex"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        global _star
        if _star is None:
            return "插件尚未初始化，请稍后再试"
        return await _star._handle_create_task(context, **kwargs)


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        global _star
        _star = self
        self._feishu: FeishuTaskClient | None = None
        self._scan_task: asyncio.Task | None = None
        self._config: dict = {}

    async def initialize(self):
        self._config = await self._load_config()
        app_id = self._config.get("feishu_app_id", "")
        app_secret = self._config.get("feishu_app_secret", "")

        if not app_id or not app_secret:
            logger.warning(
                "飞书应用凭证未配置！请在服务器上编辑 "
                "data/plugin_data/astrbot_plugin_task_reminder/config.json "
                "填入 feishu_app_id 和 feishu_app_secret，然后重载插件"
            )
            return

        self._feishu = FeishuTaskClient(app_id, app_secret)
        self.context.add_llm_tools(CreateReminderTaskTool())
        logger.info("[待办提醒] 已注册 create_reminder_task 工具")

        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("[待办提醒] 巡检已启动（每30分钟）")

    async def terminate(self):
        if self._scan_task:
            self._scan_task.cancel()

    # ── 配置 ──────────────────────────────────────────────

    async def _load_config(self) -> dict:
        config_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_task_reminder"
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.json"

        if not config_file.exists():
            default = {"feishu_app_id": "", "feishu_app_secret": ""}
            config_file.write_text(
                json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return default

        return json.loads(config_file.read_text(encoding="utf-8"))

    # ── 创建任务（Function Tool 回调）─────────────────────

    async def _handle_create_task(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> str:
        try:
            event: AstrMessageEvent = context.context.event
            user_id = event.get_sender_id()
            umo = event.unified_msg_origin

            summary = kwargs.get("summary", "")
            description = kwargs.get("description", "")
            deadline_str = kwargs.get("deadline", "")
            is_complex = kwargs.get("is_complex", False)
            custom_remind_days = kwargs.get("custom_remind_days", [])

            due_dt = _parse_deadline(deadline_str)
            due_ms = int(due_dt.timestamp() * 1000)

            task = await self._feishu.create_task(
                summary=summary,
                description=description,
                due_timestamp_ms=due_ms,
                assignee_open_id=user_id,
                reminder_minutes=120,
            )

            task_guid = task["guid"]
            task_url = task.get("url", "")

            rules: list[dict] = [
                {"minutes_before": 120, "triggered": False}
            ]
            if is_complex:
                rules.append({"minutes_before": 2880, "triggered": False})
            else:
                rules.append({"minutes_before": 1440, "triggered": False})
            for days in custom_remind_days:
                rules.append(
                    {"minutes_before": days * 24 * 60, "triggered": False}
                )

            task_data = {
                "summary": summary,
                "deadline_ms": due_ms,
                "deadline_str": deadline_str,
                "is_complex": is_complex,
                "rules": rules,
                "umo": umo,
            }
            await self.put_kv_data(f"task_{task_guid}", task_data)

            task_ids = await self.get_kv_data("_task_ids", [])
            task_ids.append(task_guid)
            await self.put_kv_data("_task_ids", task_ids)

            complex_str = "复杂" if is_complex else "普通"
            remind_parts = []
            if is_complex:
                remind_parts.append("截止前2天")
            else:
                remind_parts.append("截止前1天")
            remind_parts.append("截止前2小时")
            for d in custom_remind_days:
                remind_parts.append(f"提前{d}天")

            result = (
                f"✅ 已创建待办任务「{summary}」\n"
                f"⏰ 截止：{deadline_str}\n"
                f"📂 类型：{complex_str}\n"
                f"🔔 提醒：{'、'.join(remind_parts)}\n"
            )
            if task_url:
                result += f"🔗 查看：{task_url}"
            return result

        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            return f"创建任务失败: {e}"

    # ── 定时巡检 ──────────────────────────────────────────

    async def _scan_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self._scan_and_remind()
            except Exception as e:
                logger.error(f"[待办提醒] 巡检异常: {e}")
            await asyncio.sleep(1800)

    async def _scan_and_remind(self):
        if self._feishu is None:
            return

        logger.info("[待办提醒] 开始巡检...")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        task_ids: list = await self.get_kv_data("_task_ids", [])

        for task_guid in task_ids:
            try:
                task_data = await self.get_kv_data(f"task_{task_guid}")
                if not task_data:
                    continue

                deadline_ms = task_data["deadline_ms"]
                if now_ms >= deadline_ms:
                    continue

                time_left_minutes = (deadline_ms - now_ms) / (1000 * 60)
                rules = task_data["rules"]
                updated = False

                for rule in rules:
                    if rule["triggered"]:
                        continue
                    if time_left_minutes <= rule["minutes_before"]:
                        await self._send_reminder(
                            umo=task_data["umo"],
                            summary=task_data["summary"],
                            minutes_before=rule["minutes_before"],
                        )
                        rule["triggered"] = True
                        updated = True

                if updated:
                    task_data["rules"] = rules
                    await self.put_kv_data(f"task_{task_guid}", task_data)

            except Exception as e:
                logger.error(f"[待办提醒] 处理 {task_guid} 失败: {e}")

        logger.info("[待办提醒] 巡检完成")

    async def _send_reminder(
        self, umo: str, summary: str, minutes_before: int
    ):
        hours = minutes_before / 60
        if hours >= 24:
            time_desc = f"{int(hours / 24)} 天"
        else:
            time_desc = f"{int(hours)} 小时"

        msg = f"⏰ 提醒：任务「{summary}」还有 {time_desc} 截止，请及时处理!"

        chain = MessageChain().message(msg)
        try:
            await self.context.send_message(umo, chain)
            logger.info(f"[待办提醒] 已推送: {summary} ({time_desc})")
        except Exception as e:
            logger.error(f"[待办提醒] 推送失败: {e}")


# ── 工具函数 ──────────────────────────────────────────────

def _parse_deadline(deadline_str: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(deadline_str, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    raise ValueError(f"无法解析截止时间: {deadline_str}")
