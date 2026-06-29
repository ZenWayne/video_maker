from fastmcp import FastMCP

from mcp_server.client import BackendClient
from mcp_server.config import settings
from mcp_server.context import shape_project, shape_shot, with_neighbors
from mcp_server.guidelines import AUTHORING_GUIDELINES


def create_server(backend: BackendClient) -> FastMCP:
    mcp = FastMCP("video-maker-dialogue-action")

    @mcp.tool
    async def list_projects() -> list[dict]:
        """List projects (id, title, status, shot_count)."""
        projects = await backend.list_projects()
        return [
            {
                "id": p["id"],
                "title": p.get("title"),
                "status": p.get("status"),
                "shot_count": p.get("shot_count", len(p.get("shots", []))),
            }
            for p in projects
        ]

    @mcp.tool
    async def get_project(project_id: str) -> dict:
        """Get project context: theme, status, characters, scene_overview, shot_count."""
        return shape_project(await backend.get_project(project_id))

    @mcp.tool
    async def list_shots(project_id: str) -> list[dict]:
        """List all shots of a project with dialogue, motion, and word-count info."""
        p = await backend.get_project(project_id)
        return [shape_shot(s) for s in sorted(p.get("shots", []), key=lambda s: s["shot_id"])]

    @mcp.tool
    async def get_shot(project_id: str, shot_id: int) -> dict:
        """Get one shot with prev/next dialogue context and word-count target."""
        p = await backend.get_project(project_id)
        return with_neighbors(p.get("shots", []), shot_id)

    @mcp.tool
    async def get_authoring_guidelines() -> str:
        """Return dialogue + motion authoring conventions."""
        return AUTHORING_GUIDELINES

    return mcp


def main() -> None:
    backend = BackendClient(base_url=settings.backend_base_url)
    server = create_server(backend)
    server.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
