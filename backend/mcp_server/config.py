import os


class Settings:
    def __init__(self) -> None:
        self.backend_base_url: str = os.getenv("BACKEND_BASE_URL", "http://localhost:8002")
        self.mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
        self.mcp_port: int = int(os.getenv("MCP_PORT", "8765"))


settings = Settings()
