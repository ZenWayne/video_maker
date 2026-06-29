from fastmcp import FastMCP

from app.agents.director import postprocess_motion_prompt
from mcp_server.client import BackendClient, BackendError
from mcp_server.config import settings
from mcp_server.context import shape_project, shape_shot, with_neighbors
from mcp_server.guidelines import AUTHORING_GUIDELINES
from mcp_server.validation import word_count_report


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

    @mcp.tool
    async def create_project(
        title: str, theme_text: str, aspect_ratio: str = "16:9"
    ) -> dict:
        """Create a new project (status=draft). Returns id/title/status/aspect_ratio.

        - title: project title (项目标题).
        - theme_text: theme / description the script is generated from (主题描述).
        - aspect_ratio: "16:9" or "9:16" (画面比例); defaults to "16:9".

        Character reference images (主题角色) are uploaded separately as image
        files and cannot be set through this MCP — add them via the web UI.
        """
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        if not theme_text or not theme_text.strip():
            raise ValueError("theme_text must not be empty")
        if aspect_ratio not in ("16:9", "9:16"):
            raise ValueError('aspect_ratio must be "16:9" or "9:16"')
        created = await backend.create_project(title, theme_text, aspect_ratio)
        return {
            "id": created["id"],
            "title": created.get("title"),
            "status": created.get("status"),
            "aspect_ratio": created.get("aspect_ratio"),
        }

    @mcp.tool
    async def update_dialogue(project_id: str, shot_id: int, text: str) -> dict:
        """Set a shot's dialogue (text/台词). Rejects empty text; word count is advisory."""
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        shot = await backend.patch_shot(project_id, shot_id, {"text": text})
        return {
            "shot": shot,
            "word_count": word_count_report(text, shot["shot_duration"]),
            "note": _video_note(shot),
        }

    @mcp.tool
    async def update_motion(
        project_id: str, shot_id: int, motion_prompt: str, sync_lip_marker: bool = True
    ) -> dict:
        """Set a shot's motion_prompt (动作). When sync_lip_marker, keep the lip-sync line in sync."""
        final = motion_prompt
        if sync_lip_marker:
            current = await backend.get_project(project_id)
            shot_text = next(
                (s.get("text") for s in current.get("shots", []) if s["shot_id"] == shot_id),
                None,
            )
            if shot_text:
                final = postprocess_motion_prompt(motion_prompt, shot_text)
        shot = await backend.patch_shot(project_id, shot_id, {"motion_prompt": final})
        return {"shot": shot, "note": _video_note(shot)}

    @mcp.tool
    async def batch_update_shots(project_id: str, updates: list[dict]) -> dict:
        """Apply many {shot_id, text?, motion_prompt?} edits in one call. Partial success allowed."""
        results = []
        for u in updates:
            sid = u.get("shot_id")
            try:
                if sid is None:
                    raise ValueError("missing shot_id")
                body = {k: u[k] for k in ("text", "motion_prompt") if k in u and u[k] is not None}
                if not body:
                    raise ValueError("no text or motion_prompt provided")
                shot = await backend.patch_shot(project_id, sid, body)
                results.append({"shot_id": sid, "ok": True, "shot": shot})
            except (BackendError, ValueError) as e:
                results.append({"shot_id": sid, "ok": False, "error": str(e)})
        return {"results": results}

    @mcp.tool
    async def replace_storyboard(
        project_id: str, scene_overview: str, shots: list[dict]
    ) -> dict:
        """Full-replace the storyboard (structure + dialogue). Requires script_review status.

        Each shot: {shot_id, text, shot_type, visual_description, shot_duration,
        align_with_previous, reference_image_hint?}. Set motion via update_motion afterward.
        """
        try:
            await backend.replace_storyboard(project_id, scene_overview, shots)
            return {"ok": True}
        except BackendError as e:
            return {"ok": False, "status_code": e.status_code, "error": e.detail}

    return mcp


def _video_note(shot: dict) -> str | None:
    if shot.get("video_path"):
        return "edit saved; it won't change the existing video until the shot is regenerated"
    return None


def main() -> None:
    backend = BackendClient(base_url=settings.backend_base_url)
    server = create_server(backend)
    server.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
