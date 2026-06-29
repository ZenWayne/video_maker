# MCP Server — Dialogue & Motion Authoring

An MCP (Model Context Protocol) server that lets an external LLM agent read project
state and write dialogue / motion content to shots.  The server itself makes **no LLM
calls** — all AI capability comes from the agent that connects to it.

## Quick start

```bash
# Start via Makefile (backend must already be running)
make dev-mcp

# Or start directly
BACKEND_BASE_URL=http://localhost:8002 \
uv run --project backend python -m mcp_server.server
```

The server listens on `http://0.0.0.0:8765/mcp` (FastMCP HTTP transport).

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `BACKEND_BASE_URL` | `http://localhost:8002` | Base URL of the backend API |
| `MCP_HOST` | `0.0.0.0` | Bind address for the MCP HTTP server |
| `MCP_PORT` | `8765` | Bind port for the MCP HTTP server |

No authentication is required — the server is intended for use on a trusted internal
network.  All backend calls are sent with a fixed `X-User-Name: mcp-agent` header.

## Tool catalog

### Read tools (5)

| Tool | Arguments | Description |
|---|---|---|
| `list_projects` | _(none)_ | List all projects: `id`, `title`, `status`, `shot_count`. |
| `get_project` | `project_id: str` | Get project context: theme, status, characters, `scene_overview`, `shot_count`. |
| `list_shots` | `project_id: str` | List all shots with dialogue, motion, word-count info, and `has_video` flag. |
| `get_shot` | `project_id: str`, `shot_id: int` | Get one shot with prev/next dialogue context and word-count target. |
| `get_authoring_guidelines` | _(none)_ | Return dialogue and motion authoring conventions (language, word targets, lip-sync rules). |

### Write tools (4)

| Tool | Arguments | Description |
|---|---|---|
| `update_dialogue` | `project_id: str`, `shot_id: int`, `text: str` | Set a shot's dialogue (`text` / 台词). Rejects empty text; word count is advisory. Returns updated shot + word-count report. |
| `update_motion` | `project_id: str`, `shot_id: int`, `motion_prompt: str`, `sync_lip_marker: bool = True` | Set a shot's `motion_prompt` (动作). When `sync_lip_marker=True` the lip-sync marker (`The character says: "..."`) is kept in sync with the current dialogue automatically. |
| `batch_update_shots` | `project_id: str`, `updates: list[dict]` | Apply many edits in one call. Each update: `{shot_id, text?, motion_prompt?}`. Partial success is allowed — each item reports `"ok": true/false` independently. |
| `replace_storyboard` | `project_id: str`, `scene_overview: str`, `shots: list[dict]` | Full-replace the storyboard (structure + dialogue). Requires `script_review` status (returns `{"ok": false, "status_code": 409}` otherwise). Each shot dict: `{shot_id, text, shot_type, visual_description, shot_duration, align_with_previous, reference_image_hint?}`. Does **not** accept `motion_prompt` — set motion afterward via `update_motion` or `batch_update_shots`. |

## Two-phase authoring flow

The recommended sequence for authoring a full storyboard:

```
Phase 1 — Structure + Dialogue
  replace_storyboard(project_id, scene_overview, shots=[
      {"shot_id": 1, "text": "...", "shot_type": "Close-up", ...},
      ...
  ])
  → Atomically upserts all shots (deletes shots absent from the list).

Phase 2 — Motion
  batch_update_shots(project_id, updates=[
      {"shot_id": 1, "motion_prompt": "Camera slowly pushes in..."},
      ...
  ])
  → Writes motion prompts for all shots in one call.
```

Single-shot edits can also use `update_dialogue` / `update_motion` directly.

## Word-count advisory

`list_shots`, `get_shot`, and `update_dialogue` all return a `word_count` report:

```json
{
  "actual": 9,
  "target_range": [8, 10],
  "within_range": true
}
```

Targets (English-word approximation): 4 s → 8–10 words, 6 s → 13–16 words, 8 s → 18–21 words.
These are advisory — the backend never blocks a save because of word count.

## `has_video` note

Shots returned by `list_shots` / `get_shot` carry `"has_video": true/false`.
When `has_video` is true, edits saved via `update_dialogue` / `update_motion` /
`batch_update_shots` will **not** affect the existing rendered video until the shot is
regenerated — the backend returns an explanatory `"note"` field alongside the result.

## Error handling

| Error | When | Result |
|---|---|---|
| `BackendError(status_code, detail)` | Backend returns 4xx/5xx | Raised by `BackendClient`; `replace_storyboard` returns `{"ok": false, ...}` instead of re-raising |
| `ValueError("text must not be empty")` | `update_dialogue` called with blank text | Raised immediately, no backend call |
| `ValueError("missing shot_id")` | `batch_update_shots` item lacks `shot_id` | Item marked `"ok": false` in results |
| `KeyError(f"shot {shot_id} not found")` | `get_shot` on a non-existent shot | Raised by `with_neighbors` in `context.py` |

## Module layout

```
backend/mcp_server/
├── server.py        # FastMCP server — tool definitions, create_server(backend) factory
├── client.py        # BackendClient (httpx), BackendError
├── config.py        # Settings from environment variables
├── validation.py    # word_count_report() — mirrors screenwriter rules
├── context.py       # shape_project / shape_shot / with_neighbors
└── guidelines.py    # AUTHORING_GUIDELINES constant
```
