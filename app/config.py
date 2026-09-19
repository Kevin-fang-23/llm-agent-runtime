"""全局配置。所有可调参数集中在 Settings，支持 .env 覆盖。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM（OpenAI 兼容）
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"
    llm_model_cheap: str = ""

    # 存储
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data/agent.db'}"
    checkpoint_db: str = "sqlite"  # sqlite | postgres
    checkpoint_sqlite_path: str = str(PROJECT_ROOT / "data/checkpoints.sqlite")

    # 队列
    queue_mode: str = "local"  # local | celery
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_tasks: int = 4

    # 子 Agent（agent-as-tool）
    max_concurrent_subagents: int = 2
    subagent_max_tokens: int = 20000
    subagent_max_steps: int = 8
    subagent_timeout_s: float = 240.0

    # 沙箱
    sandbox_mode: str = "auto"  # auto | docker | local
    allow_unsafe_local_exec: bool = True
    sandbox_image: str = "agent-sandbox:latest"
    sandbox_timeout_s: float = 30.0
    sandbox_mem_limit: str = "256m"
    sandbox_nano_cpus: int = 1_000_000_000

    # 预算与上下文
    default_max_tokens: int = 60000
    default_max_steps: int = 24
    compress_threshold_tokens: int = 8000
    max_selfheal_retries: int = 3

    # 工具瞬时错误的原样重试（仅对声明 retry_transient 的工具生效）
    # 0 = 关闭自动重试，退回「交给 critic / 模型层决策」的旧行为
    retry_max_attempts: int = 2
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0

    # 工具
    tool_db_path: str = str(PROJECT_ROOT / "data/demo.sqlite")
    workspace_dir: str = str(PROJECT_ROOT / "data/workspace")
    search_provider: str = "mock"  # mock | ddgs
    tool_timeout_s: float = 30.0
    max_concurrent_tools: int = 4


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # 确保本地目录存在
    Path(s.workspace_dir).mkdir(parents=True, exist_ok=True)
    Path(s.tool_db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(s.checkpoint_sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return s
