from typing import Any, Optional

import httpx

HEADERS = {"X-User-Name": "mcp-agent"}


class BackendError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"backend {status_code}: {detail}")


class BackendClient:
    """Async wrapper over the video_maker backend HTTP API."""

    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        client = await self._http()
        resp = await client.request(method, path, json=json, headers=HEADERS)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise BackendError(resp.status_code, str(detail))
        return resp.json()

    async def list_projects(self) -> list[dict]:
        data = await self._request("GET", "/api/projects")
        # ProjectList shape: {"items": [...]} (backend uses ProjectList.items);
        # also tolerate {"projects": [...]} and bare list for flexibility.
        if isinstance(data, dict):
            if "items" in data:
                return data["items"]
            if "projects" in data:
                return data["projects"]
        return data

    async def get_project(self, project_id: str) -> dict:
        return await self._request("GET", f"/api/projects/{project_id}")

    async def create_project(
        self, title: str, theme_text: str, aspect_ratio: str = "16:9"
    ) -> dict:
        return await self._request(
            "POST",
            "/api/projects",
            json={"title": title, "theme_text": theme_text, "aspect_ratio": aspect_ratio},
        )

    async def patch_shot(self, project_id: str, shot_id: int, body: dict) -> dict:
        return await self._request(
            "PATCH", f"/api/projects/{project_id}/shots/{shot_id}", json=body
        )

    async def replace_storyboard(
        self, project_id: str, scene_overview: str, shots: list[dict]
    ) -> dict:
        return await self._request(
            "PUT",
            f"/api/projects/{project_id}/storyboard",
            json={"scene_overview": scene_overview, "shots": shots},
        )
