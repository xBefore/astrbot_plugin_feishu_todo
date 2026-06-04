import time
from typing import Any

import httpx

from astrbot.api import logger


class FeishuTaskClient:
    """飞书 Task v2 API 异步客户端"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._tenant_access_token: str = ""
        self._token_expires_at: float = 0.0
        self._client = httpx.AsyncClient(timeout=15.0)

    async def _ensure_token(self) -> None:
        """获取或刷新 tenant_access_token，提前 120 秒刷新"""
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 120:
            return

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        resp = await self._client.post(
            url,
            json={
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取 tenant_access_token 失败: code={data.get('code')}, msg={data.get('msg')}"
            )
        self._tenant_access_token = data["tenant_access_token"]
        self._token_expires_at = now + data.get("expire", 7200)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送请求并校验返回码"""
        await self._ensure_token()
        url = f"{self.BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        resp = await self._client.request(
            method, url, headers=headers, json=json_data, params=params
        )
        data = resp.json()
        code = data.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"飞书 API 错误: code={code}, msg={data.get('msg')}, path={path}"
            )
        return data

    async def create_task(
        self,
        summary: str,
        due_timestamp_ms: int,
        assignee_open_id: str,
        description: str = "",
        reminder_minutes: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        创建飞书待办任务

        Args:
            summary: 任务标题
            due_timestamp_ms: 截止时间（UTC 毫秒时间戳）
            assignee_open_id: 负责人的 open_id
            description: 任务备注
            reminder_minutes: 相对截止时间的提醒分钟数列表（如 [120] 表示截止前2小时提醒）

        Returns:
            飞书 API 返回的 task 字典
        """
        members = [
            {
                "type": "user",
                "id": assignee_open_id,
                "role": "assignee",
            }
        ]

        due = {"timestamp": str(due_timestamp_ms), "is_all_day": False}

        reminders = []
        if reminder_minutes:
            for i, minutes in enumerate(reminder_minutes):
                reminders.append({"id": str(i), "relative_fire_minute": minutes})

        body: dict[str, Any] = {
            "summary": summary,
            "due": due,
            "members": members,
        }
        if description:
            body["description"] = description
        if reminders:
            body["reminders"] = reminders

        data = await self._request(
            "POST", "/task/v2/tasks?user_id_type=open_id", json_data=body
        )
        task = data.get("data", {}).get("task", {})
        logger.info(f"创建飞书任务成功: guid={task.get('guid')}, summary={summary}")
        return task

    async def list_tasks(
        self, page_size: int = 50, user_id_type: str = "open_id"
    ) -> list[dict[str, Any]]:
        """获取当前应用的任务列表"""
        data = await self._request(
            "GET",
            "/task/v2/tasks",
            params={
                "page_size": page_size,
                "user_id_type": user_id_type,
            },
        )
        items = data.get("data", {}).get("items", [])
        logger.info(f"获取飞书任务列表: {len(items)} 条")
        return items

    async def delete_task(self, task_guid: str) -> None:
        """删除飞书待办任务"""
        await self._request("DELETE", f"/task/v2/tasks/{task_guid}")
        logger.info(f"删除飞书任务成功: guid={task_guid}")

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        await self._client.aclose()
