import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api.event import MessageChain

sys.path.insert(0, os.path.dirname(__file__))
from feishu_api import FeishuTaskClient

_task_ids_key = "_task_ids"


@register(
    "astrbot_plugin_feishu_todo",
    "xBefore",
    "自动识别飞书消息中的任务并创建飞书待办，支持多级 DDL 提醒",
    "0.1.0",
)
class FeishuTodoPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._client: FeishuTaskClient | None = None
        self._poll_task: asyncio.Task | None = None

    async def initialize(self):
        """插件异步初始化：加载配置、实例化客户端、注册 Function Tool、启动定时巡检"""
        config = self.config
        app_id = config.get("feishu_app_id", "")
        app_secret = config.get("feishu_app_secret", "")

        if not app_id or not app_secret:
            logger.warning(
                "飞书待办插件: 未配置 feishu_app_id 或 feishu_app_secret，请在 WebUI 插件配置中填写"
            )
            return

        self._client = FeishuTaskClient(app_id, app_secret)
        self.context.add_llm_tools(
            CreateReminderTaskTool(plugin=self),
            DeleteReminderTaskTool(plugin=self),
        )
        logger.info("飞书待办插件: Function Tool 已注册")

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("飞书待办插件: 定时巡检已启动（间隔 30 分钟）")

    async def terminate(self):
        """插件销毁：取消定时任务，释放资源"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
        logger.info("飞书待办插件: 已停止")

    async def _poll_loop(self):
        """每 30 分钟扫描一次，匹配提醒规则后推送"""
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await self._check_and_notify()
            except Exception as e:
                logger.error(f"飞书待办插件巡检异常: {e}")

    async def _check_and_notify(self):
        """扫描所有任务，检查是否需要提醒"""
        task_ids: list[str] = await self.get_kv_data(_task_ids_key, [])
        if not task_ids:
            return

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        remaining_ids: list[str] = []

        for guid in task_ids:
            try:
                task_data: dict = await self.get_kv_data(f"task_{guid}", {})
                if not task_data:
                    logger.warning(f"飞书待办插件: task_{guid} 数据已损坏，移除追踪")
                    continue

                umo = task_data.get("umo", "")
                deadline_ms = task_data.get("deadline_ms", 0)
                rules: list[dict] = task_data.get("rules", [])

                if deadline_ms == 0:
                    remaining_ids.append(guid)
                    continue

                all_triggered = True
                for rule in rules:
                    if rule.get("triggered", False):
                        continue
                    minutes_before = rule.get("minutes_before", 0)
                    notify_ms = deadline_ms - minutes_before * 60 * 1000
                    if now_ms >= notify_ms:
                        try:
                            remaining_min = minutes_before
                            if remaining_min >= 60 * 24:
                                time_desc = f"{remaining_min // (60 * 24)} 天"
                            elif remaining_min >= 60:
                                time_desc = f"{remaining_min // 60} 小时"
                            else:
                                time_desc = f"{remaining_min} 分钟"
                            msg = (
                                f"⏰ 任务提醒\n"
                                f"📋 {task_data.get('summary', '未知任务')}\n"
                                f"⏳ 距截止还有 {time_desc}\n"
                                f"{'🔴 这是复杂任务' if task_data.get('is_complex') else ''}"
                            )
                            chain = MessageChain().message(msg)
                            await self.context.send_message(umo, chain)
                            rule["triggered"] = True
                            logger.info(
                                f"飞书待办插件: 已推送提醒 task={guid}, minutes_before={minutes_before}"
                            )
                        except Exception as e:
                            logger.error(f"飞书待办插件推送提醒失败: {e}")

                    if not rule.get("triggered", False):
                        all_triggered = False

                await self.put_kv_data(f"task_{guid}", task_data)

                if not all_triggered:
                    remaining_ids.append(guid)
                else:
                    logger.info(f"飞书待办插件: 任务 {guid} 所有提醒已完成，移除追踪")

            except Exception as e:
                logger.error(f"飞书待办插件处理任务 {guid} 异常: {e}")
                remaining_ids.append(guid)

        await self.put_kv_data(_task_ids_key, remaining_ids)


@dataclass
class CreateReminderTaskTool(FunctionTool[AstrAgentContext]):
    """创建飞书待办任务的 Function Tool"""

    plugin: Any = Field(default=None, exclude=True)

    name: str = "create_reminder_task"
    description: str = (
        "创建飞书待办提醒任务。当用户在飞书私聊中提到待办事项、任务、截止日期、DDL、deadline、"
        "需要完成某事等信息时，调用此工具自动提取任务信息并创建飞书待办。"
        "工具会根据任务复杂度自动设置多级提醒：复杂任务在截止前2天和2小时提醒，"
        "普通任务在截止前1天和2小时提醒。用户也可口头指定额外提醒时间。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "任务标题，简洁概括任务内容，不超过100字",
                },
                "description": {
                    "type": "string",
                    "description": "任务详细描述，包含任务的关键细节和要求",
                },
                "deadline": {
                    "type": "string",
                    "description": (
                        "任务截止时间。你必须直接根据用户输入和当前日期推导出 ISO 8601 格式的时间字符串，"
                        "严禁调用任何时间计算工具或 search 工具来计算时间。"
                        "\n推导规则：\n"
                        "- 具体日期时间 → 直接转换，如 '6月15日下午3点' → 当前年份的6月15日 15:00\n"
                        "- 仅日期无时间 → 默认 23:59，如 '6月15日' → '2025-06-15T23:59+08:00'\n"
                        "- 相对天数 → 当前日期+天数，如 '3天后' → 当前日期+3天的 23:59\n"
                        "- '明天/后天/大后天' → 直接算出对应日期 23:59\n"
                        "- 周几 → 当前日期之后最近的那个周几，如 '下周三'/'这周五'/'周三'\n"
                        "- 小时级 → 当天该时刻，如 '下午2点' → 当天 14:00\n"
                        "- 未指定年份 → 默认当前年份；未指定月份/日期 → 参考上下文推断\n"
                        "- 所有时区必须为 Asia/Shanghai (UTC+8)\n"
                        "\n示例（当前日期为 2025-06-04）：\n"
                        "- 用户说 '下周五下午4点前完成报告' → '2025-06-13T16:00+08:00'\n"
                        "- 用户说 '明天之前交方案' → '2025-06-05T23:59+08:00'\n"
                        "- 用户说 '3天后提醒我' → '2025-06-07T23:59+08:00'\n"
                        "- 用户说 '下午2点开会' → '2025-06-04T14:00+08:00'\n"
                        "\n输出格式：ISO 8601 字符串，如 '2025-06-15T18:00+08:00'"
                    ),
                },
                "is_complex": {
                    "type": "boolean",
                    "description": (
                        "是否为复杂任务。判断标准：涉及多人协作、需要多步骤完成、"
                        "或有较高出错风险的任务为复杂任务。简单查资料、写短文等不算复杂。"
                    ),
                },
                "custom_remind_days": {
                    "type": "number",
                    "description": (
                        "用户口头指定的额外提醒天数，在截止前 N 天额外提醒。"
                        "例如用户说'提前3天提醒我'，则填 3。没有则不填。"
                    ),
                },
            },
            "required": ["summary", "deadline", "is_complex"],
        },
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        summary = kwargs.get("summary", "")
        description = kwargs.get("description", "")
        deadline_str = kwargs.get("deadline", "")
        is_complex = kwargs.get("is_complex", False)
        custom_remind_days = kwargs.get("custom_remind_days")

        if not summary or not deadline_str:
            return "缺少必要参数：任务标题或截止时间"

        if not self.plugin._client:
            return "飞书待办插件未初始化，请检查配置"

        try:
            dt = datetime.fromisoformat(deadline_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            deadline_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return f"截止时间格式错误: {deadline_str}"

        event: AstrMessageEvent = context.context.event
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()

        rules: list[dict] = []

        if is_complex:
            rules.append({"minutes_before": 60 * 24 * 2, "triggered": False})
        else:
            rules.append({"minutes_before": 60 * 24, "triggered": False})

        rules.append({"minutes_before": 120, "triggered": False})

        if custom_remind_days and custom_remind_days > 0:
            rules.append(
                {
                    "minutes_before": int(custom_remind_days * 24 * 60),
                    "triggered": False,
                }
            )

        try:
            task = await self.plugin._client.create_task(
                summary=summary,
                due_timestamp_ms=deadline_ms,
                assignee_open_id=sender_id,
                description=description,
                reminder_minutes=[120],
            )
        except Exception as e:
            logger.error(f"创建飞书任务失败: {e}")
            return f"创建飞书任务失败: {e}"

        guid = task.get("guid", str(uuid.uuid4()))

        task_data = {
            "guid": guid,
            "summary": summary,
            "deadline_ms": deadline_ms,
            "is_complex": is_complex,
            "rules": rules,
            "umo": umo,
        }
        await self.plugin.put_kv_data(f"task_{guid}", task_data)

        task_ids: list[str] = await self.plugin.get_kv_data(_task_ids_key, [])
        task_ids.append(guid)
        await self.plugin.put_kv_data(_task_ids_key, task_ids)

        task_type = "复杂任务" if is_complex else "普通任务"
        remind_desc_parts = ["截止前 2 小时"]
        if is_complex:
            remind_desc_parts.insert(0, "截止前 2 天")
        else:
            remind_desc_parts.insert(0, "截止前 1 天")
        if custom_remind_days and custom_remind_days > 0:
            remind_desc_parts.insert(0, f"提前 {custom_remind_days} 天")
        output = (
            f"✅ 飞书待办已创建！\n"
            f"📋 任务：{summary}\n"
            f"⏰ 截止：{deadline_str}\n"
            f"📌 类型：{task_type}\n"
            f"🔔 提醒节点：{'、'.join(remind_desc_parts)}"
        )
        return output


@dataclass
class DeleteReminderTaskTool(FunctionTool[AstrAgentContext]):
    """删除飞书待办任务的 Function Tool"""

    plugin: Any = Field(default=None, exclude=True)

    name: str = "delete_reminder_task"
    description: str = (
        "删除飞书待办任务。当用户提到删除、取消、移除某个之前创建的待办或任务时，调用此工具。"
        "工具会搜索所有已记录的任务，按标题关键词匹配，找到后从飞书和本地同时删除。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "task_summary": {
                    "type": "string",
                    "description": (
                        "要删除的任务标题或关键词，用于匹配已创建的任务。"
                        "直接使用用户在对话中提到的任务名称，如 '项目报告'、'取快递'。"
                    ),
                },
            },
            "required": ["task_summary"],
        },
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        task_summary = kwargs.get("task_summary", "").strip()

        if not task_summary:
            return "缺少必要参数：请指定要删除的任务名称"

        if not self.plugin._client:
            return "飞书待办插件未初始化，请检查配置"

        task_ids: list[str] = await self.plugin.get_kv_data(_task_ids_key, [])
        if not task_ids:
            return "当前没有待办任务可删除"

        matched: list[dict] = []
        for guid in task_ids:
            task_data: dict = await self.plugin.get_kv_data(f"task_{guid}", {})
            if not task_data:
                continue
            summary = task_data.get("summary", "")
            if task_summary in summary:
                matched.append(
                    {"guid": guid, "summary": summary, "task_data": task_data}
                )

        if not matched:
            all_summaries = []
            for guid in task_ids:
                td: dict = await self.plugin.get_kv_data(f"task_{guid}", {})
                if td:
                    all_summaries.append(td.get("summary", "未知"))
            return f"未找到匹配 '{task_summary}' 的任务。当前任务列表: {', '.join(all_summaries)}"

        if len(matched) > 1:
            names = [m["summary"] for m in matched]
            return f"找到多个匹配任务，请指定更精确的名称：{', '.join(names)}"

        item = matched[0]
        guid = item["guid"]

        try:
            await self.plugin._client.delete_task(guid)
        except Exception as e:
            logger.error(f"飞书 API 删除任务失败: {e}")
            return f"删除飞书任务失败: {e}"

        new_ids = [g for g in task_ids if g != guid]
        await self.plugin.put_kv_data(_task_ids_key, new_ids)
        await self.plugin.delete_kv_data(f"task_{guid}")

        return f"✅ 已删除任务「{item['summary']}」"
