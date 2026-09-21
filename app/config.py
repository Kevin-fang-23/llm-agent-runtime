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
    # checkpoint 写盘档位：sync | async | exit
    #   必须默认 sync。LangGraph 1.x 的 async 档位只是把写盘**排队**而非即时落盘，
    #   进程被硬杀（kill -9 / TerminateProcess）时会丢掉最后几个 superstep 的 checkpoint
    #   —— 实测 12 次硬杀里 5 次丢到只剩初始状态，导致"崩了能恢复"实际不成立。
    #   sync 的代价是每个 superstep 多等一次写盘（吞吐会下降），这正是可恢复性的真实成本。
    checkpoint_durability: str = "sync"

    # 队列
    queue_mode: str = "local"  # local | celery
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_tasks: int = 4
    # 优雅停机：先等在跑任务自然结束（排空），超时才取消。0 = 不排空直接取消
    shutdown_drain_timeout_s: float = 10.0

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

    # 模式自适应（P2-DAG）：plan_execute 连续 plan_defect 重规划达到该次数后
    # 回退 react（重规划也救不回来，说明任务更适合让模型凭执行历史自由决策），
    # 丢弃剩余计划并广播 mode_downgraded；反向地，react 遇 plan_defect 重规划
    # 出计划后升级 plan_execute（修复"react 重规划出的计划没有执行轨道"的缺口）。
    # 0 = 关闭降级（重规划永不放弃）。
    adaptive_downgrade_after_replans: int = 2

    # 工具瞬时错误的原样重试（仅对声明 retry_transient 的工具生效）
    # 0 = 关闭自动重试，退回「交给 critic / 模型层决策」的旧行为
    retry_max_attempts: int = 2
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0

    # 鉴权与多租户（P2-1）
    # auth_enabled 默认开启（默认安全）：关闭它等于把「任何人可提交任务烧 token」敞开，
    # 仅限本机开发，与 ALLOW_UNSAFE_LOCAL_EXEC 同一性质的选择。
    auth_enabled: bool = True
    # 管理员密钥：留空则首次启动生成并写入 credentials_file（env 优先于文件）
    admin_api_key: str = ""
    credentials_file: str = str(PROJECT_ROOT / "data/api_credentials.json")
    # 新租户默认每日 token 配额（0 = 不限）
    tenant_default_daily_token_quota: int = 200_000

    # 限流：分钟级用限流器（memory = 进程内滑动窗口 / db = 业务库固定窗口），
    # 日级额度以 tasks 表实数聚合（判定依据即事实来源，重启/多 worker 都不会漂，
    # 见 routes_tasks.py）。单进程部署用 memory（精确且零开销）；多 worker
    # （uvicorn --workers N / 多副本）应设 db —— 否则突发额度 = 单实例 × worker 数。
    # 0 = 关闭该层
    rate_limit_store: str = "memory"    # L1/L2 计数存储：memory | db
    ip_rate_limit_per_min: int = 120       # L1 每 IP 每分钟（/api/* 全部端点）
    tenant_submit_per_min: int = 10        # L2 每租户每分钟提交任务数
    tenant_daily_task_limit: int = 200     # L2b 每租户每日提交任务数
    global_daily_task_limit: int = 1000    # L3 全局每日提交任务数（资金护栏）

    # 可观测性（P2-5）
    # 日志：json = 单行结构化（采集器直接解析字段）；text = 人类可读旧格式
    log_level: str = "INFO"
    log_format: str = "json"
    # Prometheus 抓取端点。刻意**不放在 /api 前缀下**：抓取器高频、无法携带租户
    # 凭据，而 L1 每 IP 限流只作用于 /api/*（见 app/api/ratelimit.py）。
    # 该端点只暴露进程级低基数聚合，不含任何 task_id / tenant_id（防基数爆炸 +
    # 防跨租户成本泄漏，两者的理由见 app/observability/metrics.py 模块头）。
    prometheus_enabled: bool = True
    prometheus_path: str = "/metrics"

    # 可观测性（P2-6）：日志脱敏 / 采样
    # 脱敏是**写入前**做的（Filter 层），因为日志一旦落进采集器就难以召回。
    # 默认开启（默认安全）：工具参数与 LLM 响应都可能带用户输入。
    log_redact_enabled: bool = True
    # 自定义脱敏正则（逗号分隔）。留空用 logging.DEFAULT_REDACT_PATTERNS 的内置集合
    # （sk- 密钥 / Bearer token / 赋值式 secret / 手机号 / 身份证 / 邮箱 / DB URL 密码）。
    # 传了就**完全替换**内置集合而不是追加：运维有时需要临时关掉某条误伤规则，
    # 追加语义下无法关闭。
    log_redact_patterns: str = ""
    # 正常日志采样：每 N 条留 1 条（1 = 全留）。**WARNING 及以上永不采样**
    # （错误日志是排查起点，采样掉它等于销毁现场），见 logging.SamplingFilter。
    log_sample_rate: int = 1

    # 可观测性（P2-6）：直方图分桶覆盖（逗号分隔的秒值，空 = 用默认 13 档）
    # 分开配置而不共用一个：LLM 调用的量级（0.2~30s）与工具执行（10ms~10s）
    # 差一个数量级，共用一套桶会让短耗时全挤在第一档（分位数失去分辨率）。
    metrics_buckets_llm: str = ""
    metrics_buckets_tool: str = ""
    metrics_buckets_task: str = ""

    # span 树（P2-6）：是否把 span 落库。关闭后 span 只进进程内缓冲与日志，
    # 不影响主流程（span 是旁路观测，不得成为任务成败的因素）。
    spans_enabled: bool = True
    # 单任务最多保留的 span 数：超限的 span 仍会闭合与进日志，但不落库。
    # 存在的理由与基数纪律同源 —— 无上限的表增长迟早压垮 DB，
    # 而 agent 的步数上限（default_max_steps=24）本身就限定了正常量级在数十条。
    spans_max_per_task: int = 500

    # 工具
    tool_db_path: str = str(PROJECT_ROOT / "data/demo.sqlite")
    workspace_dir: str = str(PROJECT_ROOT / "data/workspace")
    # 默认 mock 是刻意选择：不配 .env 的调用方（含全部测试与离线脚本）必须不出网。
    # auto 会按 sogou → bing 依次尝试，用相关性校验挑第一个可用的源。
    search_provider: str = "mock"  # mock | auto | sogou | bing | ddgs
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
