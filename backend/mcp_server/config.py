import os


class Settings:
    def __init__(self) -> None:
        self.backend_base_url: str = os.getenv("BACKEND_BASE_URL", "http://localhost:8002")
        # 机器凭据（FR-5）。MCP 没有浏览器、不存 cookie、无法交互登录，所以
        # 必须走独立的静态令牌通道。来自 secret，绝不写进 config.yml。
        # 留空时不带 Authorization 头 —— 只有 AUTH_ENFORCED=false 时才走得通。
        self.machine_token: str = os.getenv("MACHINE_TOKEN", "")
        self.mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
        self.mcp_port: int = int(os.getenv("MCP_PORT", "8765"))


settings = Settings()
