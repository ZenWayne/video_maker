def test_config_defaults(monkeypatch):
    monkeypatch.delenv("BACKEND_BASE_URL", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    from importlib import reload
    import mcp_server.config as cfg
    reload(cfg)
    assert cfg.Settings().backend_base_url == "http://localhost:8002"
    assert cfg.Settings().mcp_port == 8765


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("BACKEND_BASE_URL", "http://video-maker-backend-dev:8002")
    monkeypatch.setenv("MCP_PORT", "9000")
    from importlib import reload
    import mcp_server.config as cfg
    reload(cfg)
    s = cfg.Settings()
    assert s.backend_base_url == "http://video-maker-backend-dev:8002"
    assert s.mcp_port == 9000
