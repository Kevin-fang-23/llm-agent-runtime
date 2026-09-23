"""结构化日志：把 `task_id` / `trace_id` 自动注入每条日志，并做脱敏与采样。

**为什么不用 `logging.basicConfig(format=...)` 直接拼**：`task_id` / `trace_id` 是
运行时上下文，不在 logger 调用点的参数里。要让它出现在每条日志上，正规做法是
`contextvars` + `logging.Filter` —— Filter 在**每个 handler 上**执行，因而无论
日志来自 `log.info("x")` 还是第三方库（uvicorn / sqlalchemy / httpx），都能被
自动补齐。这也是本模块存在的理由：一处装配，全进程生效。

**日志格式两种档位**：
- `json`（默认）：一行一个 JSON 对象，便于采集器（Loki / ES / CloudWatch）直接
  解析字段，不需要正则。`task_id` / `trace_id` 是**顶层字段**而非拼在 message 里。
- `text`：保留人类可读的旧格式 —— 本地 `--reload` 开发时更舒服，也保证
  "日志格式变更"不会成为既有排查流程的破坏性变更。

**脱敏（P2-6）**：工具参数、LLM 响应、错误文本都可能带上用户输入 —— 其中包括
API Key、Bearer token、手机号、身份证号。日志会落到 stdout→采集器→长期存储，
**一旦写入就难以召回**，所以脱敏必须在**写入前**做，且必须挂在 Filter 层
（而不是各调用点手工处理）：调用点会漏，Filter 不会。

**采样（P2-6）**：高吞吐下每步工具执行都打一条 INFO 会让日志量随任务数线性增长
（20 tasks/s × 每任务 ~10 条 = 200 行/秒）。采样策略刻意保守 ——
**WARNING 及以上永不采样**，且每个 `(logger, level)` 的**首条必留** ——
保证"出问题时至少有一条现场"，避免采样把唯一一条错误日志丢掉。

**脱敏是"降低泄漏面"而非"保证不泄漏"**：正则只覆盖高置信度模式（带前缀的密钥、
标准格式的 PII）。业务自由文本里的敏感内容无法靠正则穷尽，所以本模块的定位是
纵深防御的一层，不是唯一防线（README 的鉴权/租户隔离才是主防线）。
"""
from __future__ import annotations

import json
import logging
import re
import sys

from app.observability.context import current_trace_id

# 由 engine 在任务执行期间绑定（见 app/graph/engine.py），供日志 Filter 读取。
# 定义在这里而不是从 graph 层导入：observability 不应反向依赖业务包，
# 否则 `import app.observability` 会连带拉起 langgraph（CI 的 import 冒烟会变重）。
task_id_var = None  # type: ignore[var-annotated]  # 由 bind_task_id_var() 注入

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}

# 脱敏占位符：保留长度信息无用，只标记"此处已遮蔽"，避免运维反推原值长度
MASK = "[REDACTED]"

# 默认脱敏规则（高置信度模式，宁漏勿误伤）
#
# **顺序是有意义的**，且规则间的相互作用比"从具体到宽泛"更微妙 —— 实测踩到：
# 若把 `key[:=]value` 这种宽泛赋值规则放在 DB URL 规则之前，
# `postgres://admin:hunter2secret@db/app` 会被中间的 `secret` 字样触发，
# 结果变成 `postgres=[REDACTED]db/app`（方案名被吃掉、结构被破坏）。
# 因此顺序按"模式特异性 + 是否可能被别的规则误触发"排：
#   1. 带厂商前缀的密钥（最不可能误伤）
#   2. Bearer token
#   3. HTTP 头形式的 X-API-Key
#   4. 连接串（含 URL 结构，必须在裸 `key=value` 之前跑完）
#   5. 赋值式密钥（宽泛，放后面）
#   6. PII（手机号 / 身份证 / 邮箱）
DEFAULT_REDACT_PATTERNS: tuple[str, ...] = (
    # OpenAI / 兼容厂商密钥：sk-xxx（含 sk-proj- 变体），长度下限 16 防误伤短串
    r"\bsk-[A-Za-z0-9_\-]{16,}",
    # Authorization / Bearer token（HTTP 头在异常回显里很常见）
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}",
    # HTTP 头形式的 X-API-Key
    r"(?i)\bx-api-key\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{8,})[\"']?",
    # 连接串里的密码部分：**只遮蔽密码**，保留 scheme://user@host:port/db 结构，
    # 否则日志会失去"连的是哪个库"这个关键排查信息
    r"(?i)\b(postgres(?:ql)?|mysql|redis|amqp)://([^:/\s@]+):([^@\s]+)@",
    # 赋值式密钥：key/secret/token/password = value（YAML/JSON/env 三种写法都覆盖）。
    # C5：前导边界不用 \b —— `_` 是词字符，`BOCHA_API_KEY=xxx` 里 "API" 前是 "_"，
    # \b 不成立导致整条规则失配、密钥原样进日志。改用"前面不是字母/数字"的
    # lookbehind：下划线/连字符分隔的 env 式键名照常命中，`fpwd` 这类字母粘连仍挡住。
    r"(?i)(?<![A-Za-z0-9])(api[_-]?key|apikey|secret|token|password|passwd|pwd)\b\s*[:=]\s*"
    r"[\"']?([A-Za-z0-9._\-/+]{8,})[\"']?",
    # 中国大陆手机号（1 开头 11 位）
    r"\b1[3-9]\d{9}\b",
    # 中国大陆身份证号（18 位，末位可为 X）
    r"\b\d{17}[\dXx]\b",
    # 邮箱（工具参数里的联系人信息）
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
)

# 各规则替换时保留的捕获组（0 基，对应 re.Match.groups() 的第 N 组）。
# 需要"保留结构、只遮蔽值"的规则在此登记 —— 声明式表达比在正则里写死 \1\2 更可读，
# 也避免 `\2=[REDACTED]` 这类替换串在 f-string 里被转义搞错。
_KEEP_GROUPS: dict[str, tuple[int, ...]] = {
    # 赋值式：保留 key 名（第 1 组），遮蔽 value（第 2 组）。键串须与规则原文一致
    r"(?i)(?<![A-Za-z0-9])(api[_-]?key|apikey|secret|token|password|passwd|pwd)\b\s*[:=]\s*"
    r"[\"']?([A-Za-z0-9._\-/+]{8,})[\"']?": (1,),
    # X-API-Key：保留键名，遮蔽值
    r"(?i)\bx-api-key\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{8,})[\"']?": (),
    # 连接串：保留 (scheme, user)，遮蔽 (password)，保留其余原文
    r"(?i)\b(postgres(?:ql)?|mysql|redis|amqp)://([^:/\s@]+):([^@\s]+)@": (1, 2),
}

# 编译一次复用：每条日志都跑正则，重复 compile 会让日志路径成为热点
_COMPILED_CACHE: dict[tuple[str, ...], list[tuple[re.Pattern, object]]] = {}


def _make_replacement(pattern: str, groups: int):
    """为一条规则生成替换函数：保留 `_KEEP_GROUPS` 声明的组，其余组替换为 MASK。

    为什么用函数而不是替换串：`\\1=[REDACTED]\\3` 这类替换串要手写组序号，
    拆成"保留哪些组 + 遮蔽哪些组"后就很难对上（多写一个括号就错位）。
    函数里以"组序号集合"驱动，可读且不易错位。

    未被声明保留的**非空**捕获组一律替换为 MASK；未参与匹配的组（None）保持空，
    这样 `postgres://(user):(pw)@` 这类规则能保留 scheme 与 user、只吃掉密码。

    **核心不变量：以 `m.start()` 为偏移基准**。
    `m.string` 是**全部待替换文本**，而 `m.start(i)/m.end(i)` 是**相对该 match 的偏移**。
    若从 0 起切片，会把 `match` 之前的整段前缀再抄一遍 —— 实测症状：
    `"连接串 postgres://admin:pw@db"` → `"连接串 连接串 postgres://admin:[REDACTED]@db"`
    （前缀被复制，把 `admin@` 挤出了断言视野），且替换**不幂等**（跑两次抄两遍）。
    以 `m.start()` 为基准后，所有切片都落在 match 内部，结构自然守恒。
    """
    keep = set(_KEEP_GROUPS.get(pattern, ()))
    if not groups:
        return MASK  # 无捕获组：整段遮蔽（sk- / Bearer / 手机号 / 邮箱等）

    def _repl(m: "re.Match") -> str:
        out: list[str] = []
        last = m.start()
        for i in range(1, groups + 1):
            if m.start(i) < 0:  # 该组未参与匹配（可选组未命中）
                continue
            out.append(m.string[last:m.start(i)])
            out.append(m.group(i) if i in keep else MASK)
            last = m.end(i)
        out.append(m.string[last:m.end()])
        return "".join(out)

    return _repl


def _build_regexes(patterns: tuple[str, ...]) -> list[tuple[re.Pattern, object]]:
    """编译脱敏正则；命中缓存直接复用（同进程内 patterns 通常只有一个值）。"""
    cached = _COMPILED_CACHE.get(patterns)
    if cached is not None:
        return cached
    built: list[tuple[re.Pattern, object]] = []
    for raw in patterns:
        try:
            compiled = re.compile(raw)
        except re.error as exc:  # 配置写错正则不该让服务起不来
            logging.getLogger(__name__).warning(
                "脱敏正则无效已跳过 pattern=%r err=%s", raw, exc)
            continue
        built.append((compiled, _make_replacement(raw, compiled.groups)))
    _COMPILED_CACHE[patterns] = built
    return built


def redact_text(text: str, patterns: tuple[str, ...] | None = None) -> str:
    """对单段文本做脱敏；对外暴露为函数，便于测试与 `extra` 字段复用。"""
    if not text:
        return text
    pats = patterns if patterns is not None else DEFAULT_REDACT_PATTERNS
    for regex, repl in _build_regexes(pats):
        text = regex.sub(repl, text)
    return text


def bind_task_id_var(var) -> None:
    """注入 `task_id` 的 ContextVar（由 app.graph.engine 在导入时调用）。

    用注入而非直接 import：`app/observability` 保持"不依赖业务包"的方向，
    依赖关系单向（graph → observability），避免循环导入。
    """
    global task_id_var
    task_id_var = var


def _current_task_id() -> str:
    if task_id_var is None:
        return ""
    try:
        return task_id_var.get() or ""
    except LookupError:  # 极少数脱离上下文的线程（asyncio 之外的 executor）
        return ""


class TraceContextFilter(logging.Filter):
    """把 task_id / trace_id 注入每条 LogRecord（含第三方库的日志）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = _current_task_id()
        record.trace_id = current_trace_id()
        return True  # 永不拦截，只做补充


def redact_value(value, patterns: tuple[str, ...] | None = None):
    """递归脱敏任意结构（str / dict / list / tuple）。

    **必须递归**：工具参数以 `extra={"arguments": {...}}` 的形式进来，顶层是 dict
    而非 str。只处理顶层 str 会漏掉最常见的那条路径 —— 初版就是这样，
    `test_redaction_filter_masks_extra_fields` 抓到了。

    非字符串标量（int/float/bool/None）原样返回：它们不承载文本 PII，
    且替换成 MASK 会破坏 JSON 结构（调用方拿到的字段类型会变）。
    """
    if isinstance(value, str):
        return redact_text(value, patterns)
    if isinstance(value, dict):
        return {k: redact_value(v, patterns) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(v, patterns) for v in value)
    return value


class RedactionFilter(logging.Filter):
    """脱敏 Filter：遮蔽 message、`extra` 字段与异常栈里的敏感值。

    为什么挂在 Filter 而不是 Formatter：Filter 在 **每条 LogRecord** 上执行且
    早于格式化，因此两种格式档位（json / text）共用同一份脱敏结果。
    挂在 Formatter 上就得实现两遍，两边迟早不一致。

    为什么连 `record.args` 一起处理：`log.info("url=%s", url)` 的敏感值在 args 里，
    只处理 `record.msg` 会漏掉 —— 这是最典型的"以为脱敏了其实没有"。
    """

    def __init__(self, patterns: tuple[str, ...] | None = None):
        super().__init__()
        self.patterns = patterns if patterns is not None else DEFAULT_REDACT_PATTERNS

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg, self.patterns)
        if record.args:
            record.args = self._redact_args(record.args)
        # extra={...} 带进来的字段（工具参数常走这条路，且可能是嵌套 dict/list）
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED or key in ("task_id", "trace_id", "msg", "args"):
                continue
            record.__dict__[key] = redact_value(value, self.patterns)
        # 异常栈文本里常含完整 URL / 请求体：在**格式化前**替换
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact_text(
                logging.Formatter().formatException(record.exc_info), self.patterns)
            # 置 None 让 Formatter 复用 exc_text，而不是重新格式化原始异常
            record.exc_info = None
        return True  # 永不拦截

    def _redact_args(self, args):
        if isinstance(args, dict):
            return {k: (redact_text(v, self.patterns) if isinstance(v, str) else v)
                    for k, v in args.items()}
        if isinstance(args, tuple):
            return tuple(redact_text(a, self.patterns) if isinstance(a, str) else a
                         for a in args)
        if isinstance(args, str):
            return redact_text(args, self.patterns)
        return args


class SamplingFilter(logging.Filter):
    """采样 Filter：正常日志按 1/N 抽样，**异常日志永不采样**。

    规则（三条，按优先级）：
      1. `levelno >= WARNING` → **必留**。错误日志是排查起点，采样掉它等于
         把现场销毁；INFO/DEBUG 才是"量太大"的那部分。
      2. 每个 `(logger, levelno)` 的**首条必留** —— 保证"某模块开始报某类日志"
         这个事件本身可见（否则前 N 条全被丢掉，看起来像模块没启动）。
      3. 之后按 `sample_rate` 概率保留。

    为什么用计数器而非随机数：随机采样在低日志量下可能连续丢弃，观测变得不稳定；
    计数器是"每 N 条留 1 条"，在任意量级下行为可预期、可测试（无 flaky）。

    `sample_rate <= 0` 视为**关闭采样**（全留）：与配置里"0 = 关闭"的既有约定一致
    （见 app/config.py 的限流项），避免"设为 0 反而丢掉全部日志"这种事故。
    """

    def __init__(self, sample_rate: int = 1):
        super().__init__()
        # 语义是"每 N 条留 1 条"（N=1 全留）。负数按 1 处理，避免取模异常
        self.every = max(1, int(sample_rate))
        self._seen: dict[tuple[str, int], int] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if self.every <= 1:
            return True
        # 规则 1：异常级别必留
        if record.levelno >= logging.WARNING:
            return True
        key = (record.name, record.levelno)
        count = self._seen.get(key, 0)
        self._seen[key] = count + 1
        # 规则 2：首条必留（count == 0 即本次是首条）
        # 规则 3：之后每 every 条留一条
        return count % self.every == 0


class JsonFormatter(logging.Formatter):
    """单行 JSON 格式化器：把 LogRecord 的附加字段一并输出（`extra={...}` 可用）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": getattr(record, "task_id", ""),
            "trace_id": getattr(record, "trace_id", ""),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") \
                    and key not in ("task_id", "trace_id"):
                # 调用方通过 log.info("...", extra={"tool": "web_search"}) 带的字段
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif getattr(record, "exc_text", None):
            # 脱敏 Filter 把 exc_info 置 None 并预填 exc_text（见 RedactionFilter）：
            # 不认它的话，异常栈会从日志里整体消失 —— 比泄漏更糟（排查现场没了）
            payload["exception"] = record.exc_text
        # ensure_ascii=False：中文日志不转义成 \uXXXX，否则 JSON 可读性全失
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def setup_logging(level: str = "INFO", fmt: str = "json", *,
                  redact: bool = True,
                  redact_patterns: tuple[str, ...] | None = None,
                  sample_rate: int = 1) -> None:
    """装配根日志：输出到 stdout，统一挂 TraceContextFilter（+ 可选脱敏/采样）。

    为什么输出到 **stdout** 而不是 stderr：12-factor 约定日志即事件流，容器平台
    （docker logs / k8s）默认按 stdout 采集；混用一个 handler 写两个流会把同一
    请求的日志拆散，排查时要分别抓两个来源。

    为什么 `force=True`：uvicorn / 测试框架可能已经装了 handler，不接管的话
    会出现"日志打两遍"（一份旧的朴素格式 + 一份新格式）。`force=True` 先清空
    既有 handler 再装，行为确定可测。副作用是**会覆盖调用方**在 import 前做的
    `basicConfig` —— 这正是我们想要的：日志格式只能有一个来源。

    Filter 顺序（`addFilter` 是**顺序**执行，任一返回 False 即丢弃）：
      TraceContextFilter → RedactionFilter → SamplingFilter
      先注入上下文（采样判据与上下文无关但日志内容需要 id），再脱敏
      （采样丢日志前先脱敏无意义，但保持"脱敏尽量靠前"更安全 —— 万一将来
      有个 Filter 把日志转存别处，脱敏已经在它之前生效），最后采样。

    **脱敏失败不阻塞服务**：无效正则被跳过（见 `_build_regexes`），
    但 Filter 本身若抛异常会由 logging 内部处理（打印到 stderr 并继续）。
    """
    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s [task=%(task_id)s trace=%(trace_id)s] %(message)s")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(TraceContextFilter())
    if redact:
        handler.addFilter(RedactionFilter(redact_patterns))
    if sample_rate and sample_rate > 1:
        # 只在真正需要采样时挂 Filter：全留时挂上去只是多一次取模
        handler.addFilter(SamplingFilter(sample_rate))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # uvicorn 自带 logger 默认 propagate=False + 自己的 handler，不处理会绕过我们的格式。
    # 置 propagate=True 让它们汇入根 handler，实现"一份格式、一处装配"。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
