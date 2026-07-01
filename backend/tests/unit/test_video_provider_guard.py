"""Unit tests for the startup video-provider guard.

The committed deploy/config.yml defaults VIDEO_PROVIDER to 'vertex', but a stale
gitignored deploy/config.env can silently override it to 'kie'. check_video_provider
must surface that at startup so the revert is visible in logs, never silent.
"""
import logging

from unittest.mock import patch

import app.main as main


def test_warns_when_provider_is_kie(caplog):
    """Non-vertex provider → a WARNING naming the actual value is logged."""
    with patch.object(main.settings, "video_provider", "kie"):
        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            main.check_video_provider()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "kie" in warnings[0].getMessage()
    assert "vertex" in warnings[0].getMessage()


def test_no_warning_when_provider_is_vertex(caplog):
    """Vertex provider → info only, no warning."""
    with patch.object(main.settings, "video_provider", "vertex"):
        with caplog.at_level(logging.INFO, logger=main.logger.name):
            main.check_video_provider()

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("vertex" in r.getMessage() for r in caplog.records)
