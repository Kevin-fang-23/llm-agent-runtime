# LLM Agent Runtime

[![CI](https://github.com/Kevin-fang-23/llm-agent-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/Kevin-fang-23/llm-agent-runtime/actions/workflows/ci.yml)

> 面向真实业务的可托管 Agent 运行时——用户用自然语言下目标，Agent 自主规划、调用工具（搜索 / 代码执行 / 数据库 / 文件操作）、多步执行并交付结果。类似开源版 Dify 的 mini-Agent 平台。

**姊妹项目定位**：校园多模态助手证明「RAG 流水线」能力（确定性流水线，人是架构设计者）；本项目证明「动态执行系统」能力（模型实时规划执行，我给模型造安全护栏）。工程挑战从"检索准不准"变成"规划对不对、执行安不安全、崩了能不能恢复、成本可不可控"。

---

## 一、核心功能（对照需求）

| 需求 | 实现 | 代码入口 |
|---|---|---|
| 任务分解与规划（双模式可切换） | ReAct / Plan-and-Execute 两个入口路由进同一状态机，critic 判定"计划缺陷"自动回 planner 重规划 | `app/graph/nodes.py` `route_entry` / `critic_node` |
| 工具注册与沙箱执行 | MCP 风格描述符注册表（name/description/inputSchema）+ JSON Schema 校验；代码执行走 Docker 沙箱（断网/限内存 CPU/只读 FS/非 root） | `app/tools/registry.py`、`app/executor/sandbox.py` |
| 失败自动重试与反思 | **结构化错误码**（timeout / network / rate_limited / upstream_5xx / auth / permission / not_found / invalid_args）驱动分类，不再依赖中文字符串嗅探；参数校验失败走自愈循环（配额按**单次调用**计）；运行期**瞬时**错误对声明 `retry_transient` 的只读工具做**指数退避原样重试**，上游给了 `Retry-After` 就**优先听它**；其余由 critic 按码分流：retryable→回决策 / plan_defect→重规划 / fatal→终止 | `app/core/errors.py` / `app/core/retry.py` / `_retry_transient` / `_classify_failure` |
| 执行轨迹可视化 | 每个节点广播事件流落库；Web 时间线经 **SSE 推送**实时渲染（无轮询），工具参数/结果可折叠，计划进度 chip，token/步数进度条；轨迹可导出 JSON / Markdown | `web/index.html`、`GET /api/tasks/{id}/stream`、`/export` |
| 任务中断恢复（checkpoint） | LangGraph checkpointer 落 SQLite/PostgreSQL，进程崩溃后 `ainvoke(None)` 从断点续跑。**工具执行流水以 `(task_id, call_id)` 为幂等键**：checkpoint 重跑节点时回放已提交结果而非再执行一次（覆盖"工具已返回、流水已提交，但 checkpoint 未提交"的崩溃窗口） | `app/graph/engine.py` `resume_task`、`app/storage/models.py` `ToolExecution` |
| Function Calling | 模型侧 OpenAI function calling，`tool_calls` 回填 `tool_call_id` 关联 | `app/core/llm.py`、`nodes.py` `_assistant_message` |
| MCP 服务端 | `app/mcp_server.py` 以 **stdio** 传输实现 `tools/list` + `tools/call`，可被任意 MCP 客户端接入（Claude Desktop / `mcp` CLI 等）；工具的 `inputSchema` 直接复用 registry 的 JSON Schema，调用走 `registry.execute()`，沙箱与结构化错误码全部复用 | `python -m app.mcp_server` |
| LangGraph 状态机持久化 | StateGraph 六节点 + 条件边，checkpointer 可插拔（SQLite/PG） | `app/graph/engine.py` `_build` |
| Docker 沙箱隔离 | network_disabled + mem_limit + nano_cpus + pids_limit + read_only + tmpfs + uid 65534 | `DockerSandbox` |
| 异步任务队列 | 默认进程内 asyncio 队列（零依赖）；生产切 Celery+Redis（`QUEUE_MODE=celery`） | `app/worker/local_queue.py`、`celery_app.py` |
| PostgreSQL 存储执行图 | 业务库 SQLAlchemy 异步（tasks/events 表），checkpoint 走 `langgraph-checkpoint-postgres` | `app/storage/` |
| 上下文压缩 | 超阈值时滑动窗口 + LLM 摘要；工具关键输出实时写入 `key_outputs` 标记为不可压缩，每步注入 system | `app/core/compressor.py` |
| 结构化校验与自愈循环 | jsonschema Draft 2020-12 校验 → 失败回喂 REPAIR 提示词 → 重校验 → 循环 | `app/tools/registry.py` `validate` |
| 并发子 Agent 资源调度 | 任务级信号量（`MAX_CONCURRENT_TASKS`）+ 工具级信号量（`MAX_CONCURRENT_TOOLS`），一轮多工具 asyncio.gather 并行 | `local_queue.py` / `tool_executor_node` |
| token/步数双维度预算 | 步数或 token 超限→降级便宜模型续跑一次→再超限则带已完成数据优雅终止 | `app/core/budget.py` |

## 二、量化指标（`python scripts/metrics.py` 实测）

| 指标 | 数值 | 口径（引用时必须一并带上） |
|---|---|---|
| 断点恢复成功率 | **100%** (n=10) | 磁盘 checkpoint（SQLite），在 `tool_executor` **前**打断 → **关闭连接后换全新连接**恢复至完成 |
| 崩溃恢复成功率 | **100%** (n=5) | 子进程在**工具执行中途**被硬杀（POSIX=SIGKILL / Windows=TerminateProcess），换进程用同一 checkpoint 库恢复至完成 |
| 自愈挽救率 | **100%** (n=10) | 注入非法工具参数，自愈循环修复后完成 |
| 并发吞吐 | **≈21 tasks/s** | m=20 个双步任务、并发 4、墙钟 0.94s，**含 SQLite checkpoint 落盘**（本机 Python 3.11 / Windows，随机器变化） |

> **口径披露**（这几条容易被读歪，所以写清楚）：
>
> 1. **测量协议在脚本内冻结**：模型 = 脚本化假模型（隔离 LLM 波动），搜索 = `mock`（不出网）。
>    不冻结工具层会让同一命令差 **5.9 倍**——`SEARCH_PROVIDER=bing` 实测只有 **7.47 tasks/s**，
>    因为那时测的是必应 RTT，不是运行时本身。
> 2. 吞吐**已开启 checkpointer**，数字包含持久化开销。先前版本的「≈46 tasks/s」是绕过落盘的
>    纯内存图测出来的，**不可与当前数字直接比较**：SQLite 会把并发写串行化，这是真实瓶颈，
>    把瓶颈测出来才是这个指标的意义。
> 3. 「崩溃恢复」是真的硬杀进程，而不是"跑到断点后干净退出"——后者不给 flush 压力，测不出真实召回能力。
> 4. 复测：`python scripts/metrics.py [--n 10] [--m 20] [--k 5]`（`--k` 会起子进程，略慢）；
>    接真实模型后可换 `scripts/mock_llm_server.py` 同法复测。
>
> 更完整的差距评估与后续计划见 [`docs/Agent运行时-差距评估与完善建议.md`](docs/Agent运行时-差距评估与完善建议.md)。

## 三、快速开始

```bash
# 1) 环境（Python 3.11+）
conda create -n agent-runtime python=3.11 -y
conda activate agent-runtime
pip install -r requirements.txt

# 2) 配置 LLM（任意 OpenAI 兼容接口：GLM / vLLM / Ollama）
cp .env.example .env   # 填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 3) 初始化演示业务库
python scripts/seed_demo_db.py
```

**三种跑法：**

```bash
# A. CLI 离线演示（无需 API Key，脚本化模型跑通全链路）
python scripts/demo_cli.py --offline

# B. CLI 真模型
python scripts/demo_cli.py --goal "查北京和上海今天的天气，并计算两地温差" --mode plan_execute

# C. Web 服务 + 轨迹可视化
python -m uvicorn app.main:app --port 8000
# 打开 http://127.0.0.1:8000 —— 左侧提交任务，右侧时间线实时看执行轨迹
```

没有 API Key 也能完整体验 Web 链路（本地 Mock 模型）：

```bash
python scripts/mock_llm_server.py                      # 终端 1：假模型 :9100
LLM_BASE_URL=http://127.0.0.1:9100/v1 LLM_API_KEY=mock \
  LLM_MODEL=mock-model python -m uvicorn app.main:app --port 8000   # 终端 2
```

### 关键演示脚本

```bash
python scripts/demo_crash_recovery.py  # M2：子进程到断点退出 → 新进程从 checkpoint 恢复至完成
                                      #     注：该演示把断点设在工具执行**之前**，所以它验证的是"恢复续跑"，
                                      #     不是幂等去重；幂等去重（工具已执行但 checkpoint 未提交，
                                      #     恢复时不得重复执行）由 tests/test_tool_journal.py 覆盖。
python scripts/metrics.py              # 四项：断点恢复 / 崩溃恢复（硬杀）/ 自愈挽救 / 并发吞吐（协议已在脚本内冻结）
python scripts/demo_cli.py --offline   # 全链路：ReAct → 并行工具 → 沙箱 code_run → 交付（模型与工具均不出网）
```

### 质量门禁（CI）

`.github/workflows/ci.yml` 四道门禁，push / PR 均触发：

| Job | 内容 |
|---|---|
| `static` | `ruff --select E9,F63,F7,F82`（**F821 未定义名**，专拦"缺 import 导致导入期崩溃"）+ `compileall` + `import app.main` / `import app.worker.celery_app` 冒烟 |
| `test` | 43 条离线用例，结果与机器无关 |
| `integration` | Docker 沙箱 4 例（无挂载执行 / 出网被拦 / uid=65534 / 超时被杀）+ PostgreSQL checkpoint 2 例 |
| `smoke` | CLI 全链路 / 崩溃恢复 / **指标门禁**（恢复率与自愈率断言 100%）；三步均注入敌对 `SEARCH_PROVIDER=bing`，断言脚本仍自报 `mock` —— 防止"离线脚本偷偷联网"复发 |

## 四、架构

```
┌────────────┐   ┌──────────────────────────────────────┐
│  Web / CLI  │──▶│  API 层 (FastAPI)                    │
└────────────┘   │  任务提交 / 轨迹查询 / 断点恢复 / 指标  │
                 └──────────┬───────────────────────────┘
                            ▼
                 ┌──────────────────────────────────────┐
                 │  调度层：asyncio 队列(默认) / Celery+Redis │
                 │  任务信号量(并发控制) / 协作式取消        │
                 └──────────┬───────────────────────────┘
                            ▼
                 ┌──────────────────────────────────────┐
                 │  执行引擎 (LangGraph StateGraph)       │
                 │  planner → react_step ⇄ tool_executor │
                 │      ↘ critic(重试/重规划/终止) ↗      │
                 │  compressor(上下文压缩) → budget 双维预算│
                 │  checkpoint 持久化 (SQLite/PostgreSQL) │
                 └──────────┬───────────────────────────┘
                            ▼
                 ┌──────────────────────────────────────┐
                 │  工具层 (MCP 风格注册表 + JSON Schema)   │
                 │  web_search / code_run(Docker沙箱) /   │
                 │  db_query(只读) / file_ops(路径越狱防护) │
                 └──────────────────────────────────────┘
```

状态机流转：`START → (planner | react_step) → tool_executor → critic → {compressor → react_step | planner | finisher} → END`。每个 superstep 结束自动落 checkpoint。

## 五、API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 提交任务 `{goal, mode, max_steps, max_tokens}`，返回 task_id |
| GET | `/api/tasks` / `/api/tasks/{id}` | 列表 / 详情（含断点位置 `checkpoint_next`） |
| GET | `/api/tasks/{id}/trace` | 全量轨迹事件 |
| GET | `/api/tasks/{id}/events?after=N` | 增量轮询（保留给不支持 SSE 的环境；前端已改走 `/stream`） |
| GET | `/api/tasks/{id}/stream` | **SSE 推送**轨迹事件 + 任务快照，终态后推 `stream_end` 并关闭 |
| GET | `/api/tasks/{id}/export?format=json\|md` | 导出轨迹（结构化 JSON / 可贴进报告的 Markdown） |
| POST | `/api/tasks/{id}/resume` | 从 checkpoint 恢复 |
| POST | `/api/tasks/{id}/cancel` | 协作式取消（节点边界优雅收尾） |
| GET | `/api/tools` | MCP `tools/list` 风格工具清单 |
| GET | `/api/metrics` | token/步数/自愈/耗时聚合 |

## 六、两种部署形态

**本地模式（默认，零外部服务）**：SQLite 应用库 + SQLite checkpoint + 进程内 asyncio 队列 + 本地受限子进程沙箱（`ALLOW_UNSAFE_LOCAL_EXEC=true` 仅限开发）。

**完整模式（docker compose）**：

```bash
cp .env.example .env   # 填 LLM 配置
docker build -t agent-sandbox:latest ./sandbox   # 构建沙箱镜像（worker 拉起沙箱容器时使用）
docker compose up --build                        # PG + Redis + API + Celery worker
```

> 说明：worker 容器通过挂载 docker.sock 调用宿主机 daemon 拉起沙箱容器；模型代码经
> `python -I -c` argv 直接传入沙箱，**不做文件挂载**——bind-mount 路径由 daemon 按宿主机
> 文件系统解析，引擎自身在容器内时该路径不存在，这正是"can't open file /srv/code.py"
> 类报错的根因。Windows/Mac 上 docker.sock 挂载不可用时，建议 compose 只跑 PG+Redis，
> worker 在宿主机运行（`QUEUE_MODE=celery REDIS_URL=redis://localhost:6379/0`）。

沙箱安全模型（防三件事）：**数据外泄**（network_disabled 无出网能力）、**资源耗尽**（内存/CPU/进程数/时长四重限制）、**宿主污染**（只读根文件系统 + tmpfs /tmp + 非 root uid 65534 + 工作目录只读挂载）。以上四项均有自动化集成测试（`tests/test_docker_sandbox.py`，daemon 不可用自动跳过）。

## 七、测试

```bash
python -m pytest tests/ -q
# 179 个用例：173 条完全离线、可确定复现；6 条需 Docker daemon / PostgreSQL（不可用时自动 skip，CI 上会真跑）
```

覆盖：ReAct 循环与并行工具、Plan-Execute 与重规划、自愈循环（成功 / 耗尽降级 / **配额按调用计** / **并发不互相挤占**）、步数与 token 预算（含模型降级）、上下文压缩、checkpoint 跨引擎恢复、**工具执行流水幂等（真实崩溃窗口 + 对照组）**、**瞬时错误退避重试（闸门 / 上限 / 取消 / 真实等待）**、工具 Schema / 路径越狱 / SQL 只读、子 Agent 委托与递归防护、API 全生命周期、Docker 沙箱隔离、PostgreSQL checkpoint。

## 八、目录结构

```
app/
  config.py            全局配置（.env 覆盖）
  core/llm.py          LLM 抽象：OpenAI 兼容客户端 + 脚本化假模型
  core/budget.py       token/步数双维度预算（含降级策略）
  core/compressor.py   上下文压缩 + key_outputs 不可压缩注入
  core/retry.py        瞬时错误判别与指数退避（critic 与自动重试共用的单一判定点）
  graph/state.py       AgentState（状态机单一事实来源）
  graph/nodes.py       六节点：planner/react_step/tool_executor/critic/compressor/finisher
  graph/engine.py      StateGraph 装配 + run/resume/cancel + 事件广播 + ToolJournal 注入点
  tools/registry.py    MCP 风格注册表（JSON Schema 校验 + retry_transient 声明）
  tools/*.py           web_search / get_weather / code_run / db_query / file_ops / subagent
  executor/sandbox.py  Docker 沙箱（本地受限子进程回退）
  storage/             任务表 + 事件表 + **工具执行流水表（幂等去重）** + 仓储
  worker/              本地 asyncio 队列 + Celery worker
  api/                 FastAPI 路由
  main.py              应用入口（lifespan 组装）
web/index.html         轨迹可视化（零依赖单页）
sandbox/Dockerfile     代码执行沙箱镜像（python:3.11-slim 最小化）
scripts/               CLI 演示 / 崩溃恢复演示 / 指标脚本 / Mock LLM / 种子库
tests/                 179 个测试（173 离线 + 6 需 Docker/PG）
docs/                  目标差距评估与 P0/P1 修复记录
.github/workflows/     CI 四道门禁
```

## 九、面试深挖点（对应设计决策）

1. **为什么图/状态机而不是自由循环**：可控（路由显式）、可持久化（superstep 边界即 checkpoint 点）、可恢复（`ainvoke(None)` 续跑）、可观测（节点边界即事件边界）。
2. **ReAct vs Plan-and-Execute 选型**：同一状态机两条入口；确定性任务（步骤清晰）用 Plan 省 token，探索型任务用 ReAct 鲁棒。critic 统一兜底：Plan 模式跑偏了也能重规划。
3. **自愈循环的边界**：错误回喂只能修"参数格式错"，修不了"工具没这能力"——所以自愈 3 次耗尽后 critic 判定为 plan_defect 回 planner，而不是无限重试。
4. **上下文压缩不丢关键状态**：摘要必然有损，所以关键工具输出在产生时即复制进 `key_outputs`（截断快照），每步注入 system——压缩只作用于"过程消息"，事实数据不走摘要。
5. **取消为什么是协作式**：强杀线程/任务会丢状态；在节点边界检查取消标志，状态照常落 checkpoint，取消本身也可追溯。
6. **"恢复不重复执行"到底保证什么**：checkpoint 落在 superstep 边界，节点内动作是 at-least-once。at-most-once 需要以 `(task_id, call_id)` 为幂等键的**工具执行流水表**——覆盖的是"工具已返回、流水已提交，但 checkpoint 尚未提交"这个崩溃窗口。工具执行**中途**崩溃（流水还没写）拦不住，那需要工具侧提供幂等键，属远程服务的责任。
7. **为什么重试要按工具声明开关**：超时不等于失败，工具可能**已经产生了副作用**，盲目重试会把它做两遍。所以只有只读/天然幂等的工具声明 `retry_transient`（web_search / get_weather / db_query），写类与有副作用类（file_ops 的 write / code_run / subagent）一律关闭。
8. **自愈与重试的分工**：自愈修"参数错"（改参数后重跑），退避重试处理"运行错"（参数一字不改）。两者都有限次，用尽后的升级阶梯是"观测值带错误 → critic 分类 → 回 `react_step` 交模型决策"，**不在工具层无限重试**。
9. **上下文压缩踩过的坑**：早期把每轮现构造的 system / 任务提示也写回了历史，于是上一轮拼进去的"输入"在下一轮变成了"历史"，上下文随步数近似 **O(n²)** 膨胀（实测第 5 次 LLM 调用收到的 token 是第 1 次的 26 倍），并连带打穿压缩器（它的切片假设 system 只出现在头部）。修法是把"每轮重建的输入"与"执行历史"拆成两条通道，只把 assistant 决策追加进历史。
10. **错误分类为什么必须结构化**：最初用中文关键词嗅探（`"超时" in errors`）。它有两个硬伤——改一句文案就失效；而且 `httpx` 的超时文案是 `timed out`，与标记 `timeout` **并不匹配**，这整类错误被静默判成"不可重试"。改成错误码后，"该不该重试"变成可枚举、可表驱动测试的问题，并顺带补齐了 `Retry-After` 优先、HTTP 5xx/429/404 分流这些文本嗅探覆盖不到的场景。
11. **指标口径要能自证**：同一个 `metrics.py`，搜索源没冻结时实测 **7.47 tasks/s**，冻结后 **43.7**（差 5.9 倍）；吞吐绕过 checkpointer 时是 **46**，开启落盘后 **21.3**。结论是**数字必须自带协议与口径**，否则它测的可能是别的东西（必应 RTT、纯内存图）。
12. **LLM 没有时间感知**：问"今天是几号"，模型答 **2024-06-19**（训练知识残留），比真实日期早两年多。时间属于**环境事实**而非模型知识，必须由运行时注入——而且要注入到**每轮现构造的 system** 里，**绝不能写进对话历史**（历史里的时间下一刻就是错的，还会重蹈深挖点 9 的上下文膨胀）。对照实验（同模型同问题，只改这一个变量）：不注入答 `2024年6月19日`，注入后答 `2026-09-20`。
13. **换模型不如换数据源**：同一个查询「2025年NBA总冠军」，必应国内版返回「2025年_百度百科」「国民经济统计公报」（连 `site:nba.com` 限定都被无视），搜狗则返回真正讨论该话题的页面。于是新增搜狗源 + `auto` 多源兜底（用相关性校验挑第一个可用的源），同一任务从 **5 步 / 11832 token 仍答不出**，变成 **2 步 / 3920 token 拿到正确答案**（模型还自己做了三源交叉验证）。教训：**再强的模型也无法从无关噪声里推出答案**——工具召回质量才是 Agent 的能力上限。
14. **来源可信度必须成为模型可见的信号**：查「2025年NBA的FMVP是谁」时，搜索返回的 5 条**全部**是 UGC/内容农场（bilibili、今日头条、网易号），其中一条还是假设性标题（原文"如果今年勇士夺冠"被截成"勇士夺冠2025"）——模型把"雷霆夺冠""库里""勇士夺冠"三条**互不相干**的结果拼凑成"勇士逆转热火、库里获 FMVP"这种完全错误的结论，并自称"关键信息具有一致性"。
    修法有三层：① 工具给每条结果标注**来源可信度**（权威/门户媒体/UGC/未知名）与**内容标记**（推测性/标题党/引流），并在"没有权威来源"时显式警告；② 提示词写死**冲突判定顺序**（权威 > 门户媒体 > UGC）与**严禁拼凑**（某个人名/比分若在任何一条结果里都没被写出，就等于没有该证据）；③ 明确**不许以"预算不足"为由带着疑问给出确定答案**。
    另一处必须知道的分层：**"被反爬限流"与"页面结构变更"是完全不同的两件事**，前者等一会儿重试即可（报 `RATE_LIMITED` 可重试），后者才需要改解析器——混为一谈会让人改错地方。免 key 的网页抓取源天然脆弱（必应召回质量差、搜狗会限流），**生产环境应接正式搜索 API**。

## 十、与需求文档的模块对照

M1 执行内核（`demo_cli.py`）→ M2 状态持久化（`demo_crash_recovery.py`）→ M3 沙箱与自愈（`sandbox.py`）→ M4 异步并发（双队列）→ M5 成本预算（双维度 + 降级）→ M6 观测产品化（事件流 + Web 时间线）。**M1–M6 已全部落地**。

在 M1–M6 之上又补了三层"负面路径"能力：**工具执行流水幂等**（恢复不重复执行）、**瞬时错误指数退避重试**（只对声明 `retry_transient` 的只读工具）、**自愈配额按调用计 + 并发安全**。

各模块的差距评估、优先级路线图与逐项修复记录见 [`docs/Agent运行时-差距评估与完善建议.md`](docs/Agent运行时-差距评估与完善建议.md)。
