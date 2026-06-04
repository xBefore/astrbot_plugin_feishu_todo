"""飞书任务 API HTTP 客户端"""

import time
import logging
import httpx

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuTaskClient:
    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = ""
        self._token_expires = 0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 120:
            return self._token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"获取 tenant_access_token 失败: {data}")

            self._token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200)
            logger.info("已刷新飞书 tenant_access_token")
            return self._token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token = await self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Content-Type", "application/json")

        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method, f"{FEISHU_BASE_URL}{path}",
                headers=headers, timeout=kwargs.pop("timeout", 15), **kwargs,
            )
            data = resp.json()
            code = data.get("code", -1)
            if code != 0:
                raise RuntimeError(f"飞书 API 错误 [{code}]: {data.get('msg', data)}")
            return data

    async def create_task(
        self,
        summary: str,
        due_timestamp_ms: int,
        assignee_open_id: str,
        description: str = "",
        reminder_minutes: int = 120,
    ) -> dict:
        payload = {
            "summary": summary,
            "description": description,
            "due": {"timestamp": str(due_timestamp_ms), "is_all_day": False},
            "members": [
                {"id": assignee_open_id, "type": "user", "role": "assignee"}
            ],
            "reminders": [
                {"relative_fire_minute": reminder_minutes}
            ],
        }

        data = await self._request("POST", "/task/v2/tasks", json=payload)
        task = data["data"]["task"]
        logger.info(f"飞书任务已创建: {task['guid']} - {summary}")
        return task

    async def list_tasks(self, page_size: int = 100) -> list:
        all_tasks = []
        page_token = ""

        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            data = await self._request("GET", "/task/v2/tasks", params=params)
            items = data.get("data", {}).get("items", [])
            all_tasks.extend(items)

            page_token = data.get("data", {}).get("page_token", "")
            if not page_token:
                break

        return all_tasks
