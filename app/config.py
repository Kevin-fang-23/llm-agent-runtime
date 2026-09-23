"""全局配置。所有可调参数集中在 Settings，支持 .env 覆盖。

凭据字段（llm_api_key / admin_api_key / bocha_api_key）用 `SecretStr`：
任何对 Settings 的整体 repr / 异常日志 / 调试 dump 都只出现 `**********`，
明文只在显式 `get_secret_value()` 的出网比对处落地。
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM（OpenAI 兼容）
    # 默认值 = 本项目**实际使用**的组合（阿里云百炼 / 千问），与 .env.example 模板保持一致。
    # 为什么必须对齐：默认值代表"没有 .env 时指向哪家厂商"。此前默认指向智谱而 .env 用百炼，
    # 一旦 .env 缺失或未被加载（新机器 / CI / Docker 未传环境变量），请求会**静默打到智谱**
    # 且带着空 key，表现为"上游 401"—— 报错指向上游，会被误诊成"key 过期"。
    # 凭据永远只来自 .env：llm_api_key 默认为空，未配置时应当**显式报错**而不是拿空 key 去打上游。
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "qwen-plus"
    # 留空 = 不降级（超预算直接终止汇报）。这是保守兜底，不随 .env 一起改，
    # 免得"没配 .env"的环境顺带获得了它没要求的降级行为。
    llm_model_cheap: str = ""
    # LLM 出网防护：单次请求超时（秒，openai SDK 默认 600s 对交互任务形同无限等待）；
    # 可重试类故障（连接/超时/429/5xx）的同模型退避重试次数，0 = 不重试。
    # 重试仍失败时，若配了 llm_model_cheap 则自动换它再试一轮（只救瞬时故障）。
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # 存储
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data/agent.db'}"
    checkpoint_db: str = "sqlite"  # sqlite | postgres
    checkpoint_sqlite_path: str = str(PROJECT_ROOT / "data/checkpoints.sqlite")
    # checkpoint 写盘档位：sync | async | exit
    #   必须默认 sync。LangGraph 1.x 的 async 档位只是把写盘**排队**而非即时落盘，
    #   进程被硬杀（kill -9 / TerminateProcess）时会丢掉最后几个 superstep 的 checkpoint
    #   —— 实测 12 次硬杀里 5 次丢到只剩初始状态，导致"崩了能恢复"实际不成立。
    #   sync 的代价是每个 superstep 多等一次写盘（吞吐会下降），这正是可恢复性的真实成本。
    #   Literal 收紧：配错（"Sync"/"sync "）在**构造 Settings 时**即 ValidationError，
    #   不再拖到每个任务首个 checkpoint 才炸、被队列折叠成 failed。
    checkpoint_durability: Literal["sync", "async", "exit"] = "sync"

    # 队列
    queue_mode: str = "local"  # local | celery
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_tasks: int = 4
    # 优雅停机：先等在跑任务自然结束（排空），超时才取消。0 = 不排空直接取消
    shutdown_drain_timeout_s: float = 10.0
    # H8：celery 模式单任务硬时限（秒）。worker 被 SIGKILL/OOM 带走时，
    # 它跑的 running 行没人写终态、永久锁死 L4 在途配额；时限保证"活着的
    # 任务不可能比它更老"，孤儿清扫（fail_stale_active_tasks）据此放心判死。
    celery_task_time_limit_s: int = 900

    # 子 Agent（agent-as-tool）
    max_concurrent_subagents: int = 2
    subagent_max_tokens: int = 20000
    subagent_max_steps: int = 8
    subagent_timeout_s: float = 240.0

    # 沙箱
    sandbox_mode: str = "auto"  # auto | docker | local
    # H9：默认关闭**自动回退**。旧默认 True 的组合是"默认不安全"：Docker 任何
    # 异常都被静默吞掉、降级 LocalSandbox —— 模型生成代码与服务进程同 uid 执行，
    # 可直连 redis/postgres、可读 LLM_API_KEY。现在：显式 sandbox_mode=local
    # 仍然可用（那是人的决定，开发/CI 场景）；auto 回退必须显式打开本开关，
    # 且回退时打 CRITICAL 并暴露在 /health（不再静默）。
    allow_unsafe_local_exec: bool = False
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
    # H6：react↔plan_execute 互切次数上限（升级与降级各计一次）。超限后 planner
    # 不再升级到 plan_execute；critic 若还要降级则直接判 failed——反复切换本身
    # 就是"两条轨道都被证伪"的信号，继续切只是换方向烧 token。
    max_mode_switches: int = 3

    # 工具瞬时错误的原样重试（仅对声明 retry_transient 的工具生效）
    # 0 = 关闭自动重试，退回「交给 critic / 模型层决策」的旧行为
    retry_max_attempts: int = 2
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0

    # 鉴权与多租户（P2-1）
    # auth_enabled 默认开启（默认安全）：关闭它等于把「任何人可提交任务烧 token」敞开，
    # 仅限本机开发，与 ALLOW_UNSAFE_LOCAL_EXEC 同一性质的选择。
    auth_enabled: bool = True
    # 本机（回环地址）免密放行：默认开启，但带**前置代理护栏**（H12）。
    #
    # 解决的问题：一键启动脚本拉起服务后浏览器打开 127.0.0.1:8000，页面右上角却要求
    # 手填 X-API-Key（该 key 只在 data/api_credentials.json 里，且启动脚本不该把它
    # 回显到终端/写进页面）—— 演示体验因此断在第一步。
    #
    # H12 修正的旧断言：此前注释称"经 ngrok/反代暴露后 client.host 是代理或真实
    # 远端 IP"——**同机部署时这是错的**：ngrok/nginx 与本平台同机，socket 对端
    # 就是 127.0.0.1，所有外部用户都会命中免密分支共享 default 租户。
    # 现在 `_localhost_bypass_allowed` 在 trusted_proxy_hops=0 时额外检查：
    # 回环来源请求只要携带 X-Forwarded-For / X-Real-IP / Forwarded 中任意一个
    # （正规代理都会写），一律拒绝免密并记 warning —— "忘了关旁路"从静默敞开
    # 变成显式 401 + 日志。护栏拦不住"完全不写转发头的代理"，生产前置代理仍应
    # 设 AUTH_LOCALHOST_BYPASS=false；启动日志会为此专门告警一次（bootstrap_auth）。
    auth_localhost_bypass: bool = True
    # 显式声明「可信代理」数量：置 >1 时才信任 X-Forwarded-For 的右侧第 N 跳。
    # 默认 0 = **完全不信任**该头（客户端可随意伪造它，信了等于把鉴权交给攻击者）。
    # 只有在你自己控制的反向代理后面、且代理会覆写该头时才设置。
    trusted_proxy_hops: int = 0
    # 管理员密钥：留空则首次启动生成并写入 credentials_file（env 优先于文件）
    admin_api_key: SecretStr = SecretStr("")
    credentials_file: str = str(PROJECT_ROOT / "data/api_credentials.json")
    # 新租户默认每日 token 配额（0 = 不限）。
    # 2026-09-22 由 200_000 上调至 2_000_000：一次深检索任务实耗 6万~13万 token
    # （多轮搜索 + 长上下文），20 万只够 2~3 个深任务/天，正常使用半天即触顶。
    # 200 万 ≈ 25 个深任务或数百个轻任务，防滥用由 L1~L3 与日总量上限继续兜底。
    tenant_default_daily_token_quota: int = 2_000_000

    # 限流：分钟级用限流器（memory = 进程内滑动窗口 / db = 业务库固定窗口），
    # 日级额度以 tasks 表实数聚合（判定依据即事实来源，重启/多 worker 都不会漂，
    # 见 routes_tasks.py）。单进程部署用 memory（精确且零开销）；多 worker
    # （uvicorn --workers N / 多副本）应设 db —— 否则突发额度 = 单实例 × worker 数。
    # 0 = 关闭该层
    rate_limit_store: str = "memory"    # L1/L2 计数存储：memory | db
    ip_rate_limit_per_min: int = 120       # L1 每 IP 每分钟（/api/* 全部端点）—— 最外层防滥用，保持不变
    tenant_submit_per_min: int = 20        # L2 每租户每分钟提交任务数（10 → 20，提交本身不产生模型成本）
    tenant_daily_task_limit: int = 500     # L2b 每租户每日提交任务数（200 → 500）
    global_daily_task_limit: int = 3000    # L3 全局每日提交任务数（资金护栏，1000 → 3000）
    # L1 豁免全部读请求（含 SSE）后的两个补充闸（D2）：
    # SSE 长连接每条都以 0.4s 间隔轮询 DB、最长挂 300s，并发不设上限即可
    # 用几十条连接拖垮库连接池；admin key 的爆破尝试也必须计节流。
    sse_max_concurrent_per_tenant: int = 4     # 每租户并发 SSE 流上限（0=关闭）
    sse_max_concurrent_total: int = 64         # 进程级并发 SSE 流总上限（0=关闭）
    admin_auth_fail_per_min: int = 10          # 每 IP 每分钟 admin 端点尝试数（暴破节流）

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
    # auto 会按 bocha（若配了 key）→ sogou → bing → ddgs 依次尝试，
    # 用相关性校验挑第一个可用的源。
    search_provider: str = "mock"  # mock | auto | bocha | sogou | bing | ddgs；配错启动即报错，不会静默走 mock
    # 博查 Web Search API（可选）：配了 key 才会启用，auto 会优先用它 ——
    # 免 key 的网页抓取源（sogou/bing）存在反爬限流与"年份词条"退化，正式 API 更稳。
    # 申请：https://open.bocha.cn → API KEY 管理。留空则完全跳过该源（零配置开箱可用）。
    bocha_api_key: SecretStr = SecretStr("")
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
