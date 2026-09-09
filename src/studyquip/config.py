"""集中配置；环境变量使用 STUDYQUIP_ 前缀。"""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDYQUIP_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    allowed_origins: list[str] = Field(default_factory=list)
    lease_seconds: int = Field(90, ge=3)
    heartbeat_seconds: float = Field(15, gt=0)
    stream_progress_interval_seconds: float = Field(1, gt=0)
    default_subjects: list[str] = Field(default_factory=lambda: ["生物", "化学", "物理", "英语", "数学"])
    busy_timeout_ms: int = Field(5000, ge=1)
    output_tokens: int = Field(4096, ge=128)
    max_upload_mb: int = Field(1024, ge=1)
    worker_poll_seconds: float = Field(1, gt=0)
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    session_hours: int = Field(24, ge=1)
    lock_timeout_seconds: float = Field(3, ge=0)
    pdf_timeout_seconds: int = Field(120, ge=10)
    pdf_scale: float = Field(2, ge=0.5, le=6)
    max_pdf_pages: int = Field(2000, ge=1)
    max_image_pixels: int = Field(80_000_000, ge=1)
    timezone: str = "Asia/Shanghai"
    search_default_limit: int = Field(12, ge=1)
    search_max_limit: int = Field(100, ge=1)
    tool_search_max_limit: int = Field(30, ge=1)
    question_embedding_batch_size: int = Field(8, ge=1)
    question_index_debounce_seconds: float = Field(2, ge=0)
    block_relation_limit: int = Field(3, ge=0)
    concept_relation_limit: int = Field(6, ge=0)
    node_relation_limit: int = Field(12, ge=0)
    summary_batch_size: int = Field(8, ge=1)

    @model_validator(mode="after")
    def validate_timing(self) -> "Settings":
        if self.search_default_limit > min(self.search_max_limit, self.tool_search_max_limit):
            raise ValueError("默认检索条数不能超过 Web 或工具的检索上限")
        if self.heartbeat_seconds >= self.lease_seconds / 2:
            raise ValueError("heartbeat_seconds 必须小于 lease_seconds 的一半")
        self.data_dir = self.data_dir.expanduser().resolve()
        self.frontend_dir = self.frontend_dir.expanduser().resolve()
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studyquip.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def origins(self) -> set[str]:
        return {origin.rstrip("/") for origin in self.allowed_origins} | {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }
