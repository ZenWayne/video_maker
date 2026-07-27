"""Unit tests for resolve_tail_frame — COS-object-presence-only decision.
target_last_frame_path is a COS key, not a local path; object_store is mocked
so this stays a pure unit test (no real COS calls)."""
from unittest.mock import AsyncMock

import pytest

import worker.tasks as tasks
from app.services import object_store


@pytest.mark.asyncio
async def test_tail_used_when_object_exists(monkeypatch):
    monkeypatch.setattr(object_store, "exists", AsyncMock(return_value=True))
    key = "projects/p1/shots/shot_1/target_last_frame.png"
    assert await tasks.resolve_tail_frame(key) == key


@pytest.mark.asyncio
async def test_tail_none_when_path_empty():
    assert await tasks.resolve_tail_frame(None) is None


@pytest.mark.asyncio
async def test_tail_none_when_object_missing(monkeypatch):
    monkeypatch.setattr(object_store, "exists", AsyncMock(return_value=False))
    key = "projects/p1/shots/shot_1/missing.png"
    assert await tasks.resolve_tail_frame(key) is None
