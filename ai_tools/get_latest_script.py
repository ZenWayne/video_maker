#!/usr/bin/env python3
"""Fetch the most recently updated script from the video-maker API.

Usage:
    uv run --project ../backend python get_latest_script.py
    uv run --project ../backend python get_latest_script.py --id <project_id>
    uv run --project ../backend python get_latest_script.py --json
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://localhost:8002"


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def find_latest_script_project() -> dict | None:
    """Return the most recently updated project that has a script."""
    data = get("/api/projects?sort=updated_at:desc&limit=50")
    for p in data["items"]:
        if p["status"] in ("script_review", "shot_generating", "shot_review", "exporting", "exported"):
            return p
    return None


def fetch_script(project_id: str) -> dict:
    return get(f"/api/projects/{project_id}/script")


def print_human(script: dict) -> None:
    print(f"项目: {script['title']}  [{script['status']}]")
    print(f"ID:   {script['project_id']}")
    print()
    print("── 主题 " + "─" * 54)
    print(script.get("theme_text") or "(无)")
    print()
    print("── 场景概览 " + "─" * 50)
    print(script.get("scene_overview") or "(无)")
    print()
    for s in script["shots"]:
        warn = " ⚠ 字数超限" if s.get("word_count_warning") else ""
        print(f"Shot {s['shot_id']}  {s['shot_type']}  {s['shot_duration']}s{warn}")
        print(f"  台词: {s['text']}")
        print(f"  视觉: {s['visual_description']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch latest video-maker script")
    parser.add_argument("--id", help="Project ID (defaults to most recent with a script)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output raw JSON")
    parser.add_argument("--save", metavar="FILE", help="Save JSON to file")
    args = parser.parse_args()

    project_id = args.id
    if not project_id:
        project = find_latest_script_project()
        if not project:
            print("No project with a script found.", file=sys.stderr)
            sys.exit(1)
        project_id = project["id"]

    script = fetch_script(project_id)

    if args.save:
        out = Path(args.save)
        out.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved to {out}")
        return

    if args.as_json:
        print(json.dumps(script, ensure_ascii=False, indent=2))
        return

    print_human(script)


if __name__ == "__main__":
    main()
