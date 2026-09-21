"""Prometheus 指标：零依赖的注册表 + exposition 文本格式序列化。

**为什么手写而不是引入 `prometheus-client`**：
- 只需要"计数/计量 + 文本序列化"这一点薄能力，而 exposition 格式是稳定的公开协议
  （`text/plain; version=0.0.4`），实现量约 100 行；
- 仓库的既有气质是离线零依赖（见 `app/observability/__init__.py` 说明）；
- 引入它还得同步 `requirements.txt` / `requirements.lock.txt` / CI 锁定一致性门禁
  / Dockerfile，收益与改动面不成正比。

**基数纪律（最重要的一条约束）**：标签值只允许**低基数枚举**
（status / event_type / tool / model / outcome）。**任何 ID 都不许做标签** ——
`task_id` / `tenant_id` / `trace_id` 是 UUID 级取值，做标签会让时间序列数量无界增长，
抓取器内存先于业务先崩（业界叫"基数爆炸"）。所以本模块的 Counter/Histogram 都
不提供"随便打标"的口子，标签集合由调用点显式给出的小集合决定。

`agent_tasks_total{status=...}` 这类计数用**进程内自增**，而不是每次抓取去 DB 聚合：
抓取频率高（15s 一次）且抓取器可能并发，把 DB 查询挂在 `/metrics` 上等于给了
一条打库的路径。DB 的权威聚合仍由 `/api/admin/metrics` 提供，两者定位不同。
"""
from __future__ import annotations

import math
import os
import re
import socket
import threading
import time
from typing import Iterable

# 分桶档数上限：超过即视为配置错误并退回默认（理由见 parse_buckets）
_MAX_BUCKETS = 50

# 直方图默认分桶（秒）：覆盖 LLM 调用（0.2~30s）与工具执行（10ms~10s）两个量级。
# 13 档的粗粒度是刻意选择：Prometheus 每个桶都是**一条独立时间序列**，
# 桶数 × 标签组合数 = 存储与查询成本。默认档位够看分位数（P50/P90/P99 → ±10%），
# 又不会让 agent_tool_execution_duration_seconds{tool=...} 的序列数失控。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0, 60.0,
)


def parse_buckets(spec: str | object | None,
                  fallback: Iterable[float] = DEFAULT_BUCKETS) -> tuple[float, ...]:
    """把分桶规格解析为**升序去重**的边界元组（支持 env / 配置文件覆盖）。

    接受的写法：`"0.01,0.1,1,10"` / `"0.01 0.1 1 10"` / `[0.01, 0.1, 1, 10]`。
    空值、全非法、只有一个边界都退回 `fallback` —— 单边界直方图的 P99 只能落在
    "小于等于"或"大于"两格里，几乎无信息量，视为配置错误而非有效输入。

    为什么要**去重 + 排序**而不是原样保留：Prometheus 要求各 `le` 桶累计值单调
    不减。乱序或重复边界会让 exposition 里的桶不再单调，抓取器按规范会判定
    该指标无效（且报错位置在采集端，排查成本高）。在入口处规范化掉，下游
    `Histogram` 就不必再处理这种输入。

    **超过 50 档直接拒绝**：桶数失控是本项目最容易犯的存储错误，而这行配置
    改错的后果要到生产才显现。宁可退回默认，也不接受一个把序列数翻十倍的配置。
    """
    if spec is None:
        return tuple(sorted(fallback))
    if isinstance(spec, str):
        # 逗号或空白都能作分隔符：env 里写逗号，yaml 里写空格，两种都常见
        items = [p for p in re.split(r"[,\s]+", spec.strip()) if p]
    else:
        try:
            items = list(spec)
        except TypeError:
            return tuple(sorted(fallback))

    out: list[float] = []
    for item in items:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue  # 跳过非法项而不是整条配置作废
        # NaN 会让所有比较返回 False（桶统计静默归零）；+Inf 与 +Inf 桶冲突
        if math.isnan(value) or math.isinf(value):
            continue
        out.append(value)
    unique = sorted(set(out))
    if len(unique) < 2 or len(unique) > _MAX_BUCKETS:
        return tuple(sorted(fallback))
    return tuple(unique)


def _buckets_from_env(env_name: str, fallback: Iterable[float] = DEFAULT_BUCKETS
                      ) -> tuple[float, ...]:
    """读环境变量覆盖分桶；未设置或非法时退回 `fallback`。

    **为什么读 env 而不是 import settings**：`app/config.py` 的 `get_settings()`
    是 lru_cache 单例且在调用时**创建 data/ 目录**（见其实现）。指标在模块导入期
    就要确定分桶，此刻若拉 config，会让"只想 import 指标做单元测试"也产生文件
    系统副作用，并让 metrics 反向依赖 config（环）。env 是唯一在导入期可安全读取
    的配置源，且运维在 k8s 里改分桶本来就是走 env（`METRICS_BUCKETS_LLM=...`）。
    配置文件里的同名项由 `app/main.py` 在装配时回填到环境（见那里的说明）。
    """
    raw = os.environ.get(env_name, "")
    return parse_buckets(raw or None, fallback)

# 标签值合法性：Prometheus 文本格式里 `"` 与 `\` 必须转义，换行直接破坏行结构。
_ESCAPE_MAP = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}


def _escape(value: object) -> str:
    """按 Prometheus 文本格式转义标签值（工具名/错误文本可能含引号与换行）。"""
    s = str(value)
    for raw, escaped in _ESCAPE_MAP.items():
        s = s.replace(raw, escaped)
    return s


def _fmt(value: float) -> str:
    """数值格式化：整数不带小数点（Prometheus 偏好），非有限值交给规范处理。"""
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)


def _labels_repr(labels: dict[str, object]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class _Metric:
    """指标基类：统一持有 help / type / 并发锁。

    加锁的必要性：`/metrics` 抓取可能与其他请求的写入并发（uvicorn 单事件循环，
    但 `render()` 会被 `run_in_threadpool` 或测试线程调用），而 dict 迭代中改大小
    会直接抛 `RuntimeError: dictionary changed size during iteration`。
    """

    kind = "untyped"

    def __init__(self, name: str, help_: str, labelnames: tuple[str, ...] = ()):
        if labelnames:
            # 排序后比 tuple 相等性稳定：调用方按任意顺序传标签都能命中同一条序列
            raise ValueError("本实现不支持自定义标签维度，标签由指标自身定义")
        self.name = name
        self.help = help_
        self.labelnames: tuple[str, ...] = ()
        self._lock = threading.Lock()
        self._samples: dict[tuple, float] = {}

    def _key(self, labels: dict[str, object]) -> tuple:
        unknown = set(labels) - set(self.labelnames)
        if unknown:
            # 显式拒绝未知标签：拼错标签名会让指标静默消失（打成一个新的序列），
            # 与其事后对着空图排查，不如在写入点就报错
            raise KeyError(f"{self.name}: 未知标签 {sorted(unknown)}，"
                           f"允许 {list(self.labelnames)}")
        return tuple(labels.get(n, "") for n in self.labelnames)

    def samples(self) -> list[str]:
        with self._lock:
            return self._render_locked()

    def _render_locked(self) -> list[str]:
        return []

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


class Counter(_Metric):
    """单调递增计数器。同标签重复 inc 累加；负增量被拒绝（Prometheus 语义要求）。"""

    kind = "counter"

    def __init__(self, name: str, help_: str, labelnames: tuple[str, ...] = ()):
        super().__init__(name, help_)
        self.labelnames = tuple(labelnames)

    def inc(self, labels: dict[str, object] | None = None, value: float = 1.0) -> None:
        if value < 0:
            raise ValueError(f"{self.name}: Counter 不接受负增量（{value}）")
        key = self._key(labels or {})
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + value

    def value(self, labels: dict[str, object] | None = None) -> float:
        with self._lock:
            return self._samples.get(self._key(labels or {}), 0.0)

    def _render_locked(self) -> list[str]:
        lines: list[str] = []
        for key, val in sorted(self._samples.items()):
            labels = dict(zip(self.labelnames, key))
            lines.append(f"{self.name}{_labels_repr(labels)} {_fmt(val)}")
        return lines


class Gauge(_Metric):
    """可增可减的瞬时值（如当前在跑任务数）。"""

    kind = "gauge"

    def __init__(self, name: str, help_: str, labelnames: tuple[str, ...] = ()):
        super().__init__(name, help_)
        self.labelnames = tuple(labelnames)

    def set(self, value: float, labels: dict[str, object] | None = None) -> None:
        key = self._key(labels or {})
        with self._lock:
            self._samples[key] = float(value)

    def inc(self, labels: dict[str, object] | None = None, value: float = 1.0) -> None:
        key = self._key(labels or {})
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + value

    def dec(self, labels: dict[str, object] | None = None, value: float = 1.0) -> None:
        self.inc(labels, -value)

    def value(self, labels: dict[str, object] | None = None) -> float:
        with self._lock:
            return self._samples.get(self._key(labels or {}), 0.0)

    def _render_locked(self) -> list[str]:
        lines: list[str] = []
        for key, val in sorted(self._samples.items()):
            labels = dict(zip(self.labelnames, key))
            lines.append(f"{self.name}{_labels_repr(labels)} {_fmt(val)}")
        return lines


class Histogram(_Metric):
    """分桶直方图：输出累计桶（`le`）+ `_sum` + `_count`，符合 Prometheus 规范。"""

    kind = "histogram"

    def __init__(self, name: str, help_: str, labelnames: tuple[str, ...] = (),
                 buckets: Iterable[float] = DEFAULT_BUCKETS):
        super().__init__(name, help_)
        self.labelnames = tuple(labelnames)
        # 经 parse_buckets 规范化：既支持调用方传 env 覆盖的原生字符串，
        # 也保证 sorted+去重（乱序边界会让 exposition 的桶不单调，见 parse_buckets）
        self.buckets = parse_buckets(buckets)
        # counts 是**累计**桶计数（索引 i = "观测值 ≤ buckets[i] 的累计次数"），
        # totals 是独立维护的**总观测次数**。
        # 为什么必须独立维护：超过最大桶边界（如 observe(9999.0) 而末桶是 60.0）的观测
        # 一个桶都不会命中，若拿 counts[-1] 当总数，`le="+Inf"` 与 `_count` 就会漏计 ——
        # 而 Prometheus 规范要求 `+Inf` 桶恒等于总观测数，且各桶必须单调不减。
        self._counts: dict[tuple, list[int]] = {}
        self._totals: dict[tuple, int] = {}
        self._sums: dict[tuple, float] = {}

    def observe(self, value: float, labels: dict[str, object] | None = None) -> None:
        key = self._key(labels or {})
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0] * len(self.buckets)
                self._counts[key] = counts
                self._totals[key] = 0
                self._sums[key] = 0.0
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    # 累计语义：命中第 i 桶意味着也命中所有更大的桶
                    counts[i] += 1
            self._totals[key] += 1
            self._sums[key] += value

    def stats(self, labels: dict[str, object] | None = None) -> tuple[int, float]:
        """返回 (观测次数, 观测值总和)，供测试与自检使用。"""
        key = self._key(labels or {})
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                return 0, 0.0
            return self._totals.get(key, 0), self._sums.get(key, 0.0)

    def _render_locked(self) -> list[str]:
        lines: list[str] = []
        for key in sorted(self._counts):
            counts = self._counts[key]
            total = self._totals.get(key, 0)
            sums = self._sums.get(key, 0.0)
            labels = dict(zip(self.labelnames, key))
            for bound, count in zip(self.buckets, counts):
                lb = dict(labels, le=_fmt(bound))
                lines.append(f"{self.name}_bucket{_labels_repr(lb)} {_fmt(count)}")
            lb = dict(labels, le="+Inf")
            lines.append(f"{self.name}_bucket{_labels_repr(lb)} {_fmt(total)}")
            lines.append(f"{self.name}_sum{_labels_repr(labels)} {_fmt(sums)}")
            lines.append(f"{self.name}_count{_labels_repr(labels)} {_fmt(total)}")
        return lines


class Registry:
    """指标注册表：负责 `# HELP` / `# TYPE` 表头与文本序列化。"""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: _Metric) -> _Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                # 模块被重复导入（测试里 reload / 双解释器）时返回既有实例，
                # 而不是抛错：否则一次 `import` 顺序变化就能让进程起不来。
                return existing
            self._metrics[metric.name] = metric
            return metric

    def get(self, name: str) -> _Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def clear(self) -> None:
        """清空所有样本（保留指标定义）。仅供测试隔离使用。"""
        with self._lock:
            for metric in self._metrics.values():
                metric.clear()

    def render(self) -> str:
        """产出 Prometheus exposition 文本（`text/plain; version=0.0.4`）。

        格式要点：每个指标前输出 `# HELP` 与 `# TYPE`；指标之间空一行；
        数值行末尾必须换行（否则最后一个样本会被部分抓取器丢弃）。
        """
        with self._lock:
            metrics = [self._metrics[k] for k in sorted(self._metrics)]
        blocks: list[str] = []
        for metric in metrics:
            lines = metric.samples()
            if not lines:
                # 无样本的指标不输出：Prometheus 里"没数据"与"数据为 0"是两回事，
                # 补一行假 0 会污染 rate()/sum() 的正确性。
                continue
            blocks.append(f"# HELP {metric.name} {metric.help}\n"
                          f"# TYPE {metric.name} {metric.kind}\n"
                          + "\n".join(lines))
        return "\n".join(blocks) + ("\n" if blocks else "")


# 进程级注册表（模块单例）
REGISTRY = Registry()

# ---------- 指标定义（标签一律低基数枚举） ----------
#
# 两类耗时用**不同的分桶 profile**：LLM 调用是秒级（出网 + 生成），工具执行多数是
# 毫秒级（本地文件/SQL）到秒级（出网搜索）。共用一套桶会让"0.005~0.1"这一段
# 挤满工具调用（丢分辨率），而 LLM 那一段只有 2~3 档（丢分辨率）。
# 两个 profile 都保持在 13 档以内，不放大序列数。
_LLM_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 15.0, 30.0, 60.0,
)
_TOOL_BUCKETS: tuple[float, ...] = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0,
)

TASKS_TOTAL = REGISTRY.register(Counter(
    "agent_tasks_total", "任务数（按终态/进行态分桶）", ("status",)))
TASK_DURATION = REGISTRY.register(Histogram(
    "agent_task_duration_seconds", "任务从开始执行到进入终态的耗时", ("status",),
    buckets=_buckets_from_env("METRICS_BUCKETS_TASK")))
TASKS_INFLIGHT = REGISTRY.register(Gauge(
    "agent_tasks_inflight", "当前正在执行的任务数（未含排队）"))
QUEUE_DEPTH = REGISTRY.register(Gauge(
    "agent_queue_depth", "本地队列中待执行的任务数"))

LLM_CALLS = REGISTRY.register(Counter(
    "agent_llm_calls_total", "LLM 调用次数", ("model", "outcome")))
LLM_DURATION = REGISTRY.register(Histogram(
    "agent_llm_call_duration_seconds", "LLM 调用耗时（唯一真实出网点的 span）", ("model",),
    buckets=_buckets_from_env("METRICS_BUCKETS_LLM", _LLM_BUCKETS)))
LLM_TOKENS = REGISTRY.register(Counter(
    "agent_llm_tokens_total", "LLM token 消耗（按 model 分桶）", ("model",)))

TOOL_CALLS = REGISTRY.register(Counter(
    "agent_tool_calls_total", "工具调用次数", ("tool", "outcome")))
TOOL_DURATION = REGISTRY.register(Histogram(
    "agent_tool_execution_duration_seconds", "工具执行耗时", ("tool",),
    buckets=_buckets_from_env("METRICS_BUCKETS_TOOL", _TOOL_BUCKETS)))

EVENTS_TOTAL = REGISTRY.register(Counter(
    "agent_events_total", "事件流产出的事件数（按事件类型分桶）", ("type",)))

SELFHEAL_TOTAL = REGISTRY.register(Counter(
    "agent_selfheal_total", "参数自愈循环的尝试次数", ("tool",)))

# ---------- 多 worker 聚合所需的"进程身份" ----------
#
# 为什么需要这三个指标：本实现的计数是**进程内**的（见模块头"为什么手写"）。
# 多 worker（uvicorn --workers N / k8s 多副本）时，每个进程各自暴露一份 /metrics，
# 抓取端会拿到 N 条同名的 `agent_tasks_total{status="done"}` —— 它们**不能相加**
# 成一个数，而是应该按 `instance` 标签分别存放、由 PromQL 在查询时聚合：
#
#     sum by (status) (rate(agent_tasks_total[5m]))            # 全实例求和
#     max by (status) (agent_tasks_total)                      # 各实例最大值
#
# 但"按 instance 标签区分"有个前提：**抓取端得知道哪个序列属于哪个进程**。
# Prometheus 的 `instance` 标签默认来自抓取目标地址（host:port），而同一 host
# 上多进程（如 uvicorn --workers 4 共用一个端口、由 SO_REUSEPORT 分发）时，
# 抓取到的每个 `instance` 值都一样，序列会**互相覆盖**（Prometheus 按
# (metric, labels) 唯一化，同名同标签的后到者覆盖先到者）。
#
# 解法：在指标里带上**进程自证信息**，让下游能：
#   1. 用 `agent_build_info{pid=...}` 做 join，把裸序列按 pid 分组；
#   2. 用 `agent_process_start_time_seconds` 识别"进程重启"（计数器归零不是
#      业务下降，而是新进程）—— 这是多实例下**最常见的误读**：
#      `rate()` 在计数器归零处会产出一个尖峰，告警系统看到的是"任务数暴增"。
#
# 三个标签刻意都是低基数（pid / version / 语义版本号），不违反基数纪律。

BUILD_INFO = REGISTRY.register(Gauge(
    "agent_build_info", "构建信息（值恒为 1，版本经 version 标签暴露）",
    ("version", "pid")))

PROCESS_START_TIME = REGISTRY.register(Gauge(
    "agent_process_start_time_seconds",
    "进程启动时刻（Unix 秒）。重启检测用：该值变化 = 新进程，计数器已归零",
    ("pid",)))

# 进程启动时刻在**模块导入时**取一次：这才是进程真实的启动时刻。
# 若在 render() 里取 time.time()，每次都变，重启检测完全失效。
_PROCESS_START_TIME = time.time()
_PROCESS_PID = str(os.getpid())

# 进程内唯一标识：同一 PID 复用（如容器内 PID 恒定）时，用启动时刻区分
# "同一个 PID 的不同生命周期" —— 容器重启后 PID 常常还是 1
PROCESS_INSTANCE = f"{socket.gethostname()}:{_PROCESS_PID}:{int(_PROCESS_START_TIME)}"


def init_process_metrics(version: str = "dev") -> None:
    """登记进程启动信息（由 app/main.py 在装配时调用一次）。

    `version` 优先级：显式入参 > 环境变量 `AGENT_VERSION` > 默认 "dev"。
    刻意不在本模块读 `app.config`：metrics 是被 config 依赖方向的**下层**，
    反向导入会形成环（config → observability.metrics → config）。
    """
    resolved = version or os.environ.get("AGENT_VERSION", "") or "dev"
    BUILD_INFO.set(1, {"version": resolved, "pid": _PROCESS_PID})
    PROCESS_START_TIME.set(_PROCESS_START_TIME, {"pid": _PROCESS_PID})


def render() -> str:
    """渲染全部指标为 Prometheus exposition 文本。"""
    return REGISTRY.render()
