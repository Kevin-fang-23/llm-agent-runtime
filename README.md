# LLM Agent Runtime

> 面向真实业务的可托管 Agent 运行时——用户用自然语言下目标，Agent 自主规划、调用工具（搜索 / 代码执行 / 数据库 / 文件操作）、多步执行并交付结果。类似开源版 Dify 的 mini-Agent 平台。

**姊妹项目定位**：校园多模态助手证明「RAG 流水线」能力（确定性流水线，人是架构设计者）；本项目证明「动态执行系统」能力（模型实时规划执行，我给模型造安全护栏）。工程挑战从"检索准不准"变成"规划对不对、执行安不安全、崩了能不能恢复、成本可不可控"。

---

## 一、核心功能（对照需求）

| 需求 | 实现 | 代码入口 |
|---|---|---|
| 任务分解与规划（双模式可切换） | ReAct / Plan-and-Execute 两个入口路由进同一状态机，critic 判定"计划缺陷"自动回 planner 重规划 | `app/graph/nodes.py` `route_entry` / `critic_node` |
| 工具注册与沙箱执行 | MCP 风格描述符注册表（name/description/inputSchema）+ JSON Schema 校验；代码执行走 Docker 沙箱（断网/限内存 CPU/只读 FS/非 root） | `app/tools/registry.py`、`app/executor/sandbox.py` |
| 失败自动重试与反思 | 参数校验失败走自愈循环（错误回喂模型修参，上限 3 次）；运行期错误由 critic 节点分类：retryable→重试 / plan_defect→重规划 / fatal→终止 | `tool_executor_node` / `critic_node` |
| 执行轨迹可视化 | 每个节点广播事件流落库，Web 时间线实时渲染（规划/思考/工具/自愈/预算/压缩全部可见） | `web/index.html`、`GET /api/tasks/{id}/trace` |
| 任务中断恢复（checkpoint） | LangGraph checkpointer 落 SQLite/PostgreSQL，进程崩溃后 `ainvoke(None)` 从断点续跑，已完成动作不重复执行 | `app/graph/engine.py` `resume_task` |
| Function Calling / MCP | 模型侧 OpenAI function calling；工具清单即 MCP `tools/list` 格式（`GET /api/tools`），可被任意 MCP 客户端消费 | `ToolSpec.mcp_descriptor()` |
| LangGraph 状态机持久化 | StateGraph 六节点 + 条件边，checkpointer 可插拔（SQLite/PG） | `app/graph/engine.py` `_build` |
| Docker 沙箱隔离 | network_disabled + mem_limit + nano_cpus + pids_limit + read_only + tmpfs + uid 65534 | `DockerSandbox` |
| 异步任务队列 | 默认进程内 asyncio 队列（零依赖）；生产切 Celery+Redis（`QUEUE_MODE=celery`） | `app/worker/local_queue.py`、`celery_app.py` |
| PostgreSQL 存储执行图 | 业务库 SQLAlchemy 异步（tasks/events 表），checkpoint 走 `langgraph-checkpoint-postgres` | `app/storage/` |
| 上下文压缩 | 超阈值时滑动窗口 + LLM 摘要；工具关键输出实时写入 `key_outputs` 标记为不可压缩，每步注入 system | `app/core/compressor.py` |
| 结构化校验与自愈循环 | jsonschema Draft 2020-12 校验 → 失败回喂 REPAIR 提示词 → 重校验 → 循环 | `app/tools/registry.py` `validate` |
| 并发子 Agent 资源调度 | 任务级信号量（`MAX_CONCURRENT_TASKS`）+ 工具级信号量（`MAX_CONCURRENT_TOOLS`），一轮多工具 asyncio.gather 并行 | `local_queue.py` / `tool_executor_node` |
| token/步数双维度预算 | 步数或 token 超限→降级便宜模型续跑一次→再超限则带已完成数据优雅终止 | `app/core/budget.py` |

## 二、量化指标（`python scripts/metrics.py` 实测）

| 指标 | 数值 | 说明 |
|---|---|---|
| 断点恢复成功率 | **100%** (n=10) | tool_executor 前打断，新引擎实例恢复至完成 |
| 自愈挽救率 | **100%** (n=10) | 注入非法工具参数，自愈循环修复后完成 |
| 并发吞吐 | **40.3 tasks/s** | 20 个双步任务、并发 4、墙钟 0.5s（离线假模型） |

> 指标基于离线脚本化模型（隔离 LLM 波动，测的是运行时本身）；接真实模型后用 `scripts/mock_llm_server.py` 同法可复测。

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
python scripts/demo_crash_recovery.py  # M2：进程崩溃 → 跨进程 checkpoint 恢复（无重复执行）
python scripts/metrics.py              # 断点恢复率 / 自愈挽救率 / 并发吞吐
```

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
| GET | `/api/tasks/{id}/events?after=N` | 增量轮询（前端时间线用） |
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
python -m pytest tests/ -q     # 21 个用例，全部离线
```

覆盖：ReAct 循环与并行工具、Plan-Execute 与重规划、自愈循环（成功/耗尽降级）、步数与 token 预算（含模型降级）、上下文压缩、checkpoint 跨引擎恢复、工具 Schema/路径越狱/SQL 只读、API 全生命周期。

## 八、目录结构

```
app/
  config.py            全局配置（.env 覆盖）
  core/llm.py          LLM 抽象：OpenAI 兼容客户端 + 脚本化假模型
  core/budget.py       token/步数双维度预算（含降级策略）
  core/compressor.py   上下文压缩 + key_outputs 不可压缩注入
  graph/state.py       AgentState（状态机单一事实来源）
  graph/nodes.py       六节点：planner/react_step/tool_executor/critic/compressor/finisher
  graph/engine.py      StateGraph 装配 + run/resume/cancel + 事件广播
  tools/registry.py    MCP 风格注册表（JSON Schema 校验）
  tools/*.py           web_search / code_run / db_query / file_ops
  executor/sandbox.py  Docker 沙箱（本地受限子进程回退）
  storage/             SQLAlchemy 任务表 + 事件表 + 仓储
  worker/              本地 asyncio 队列 + Celery worker
  api/                 FastAPI 路由
  main.py              应用入口（lifespan 组装）
web/index.html         轨迹可视化（零依赖单页）
sandbox/Dockerfile     代码执行沙箱镜像（python:3.11-slim 最小化）
scripts/               CLI 演示 / 崩溃恢复演示 / 指标脚本 / Mock LLM / 种子库
tests/                 21 个离线测试
```

## 九、面试深挖点（对应设计决策）

1. **为什么图/状态机而不是自由循环**：可控（路由显式）、可持久化（superstep 边界即 checkpoint 点）、可恢复（`ainvoke(None)` 续跑）、可观测（节点边界即事件边界）。
2. **ReAct vs Plan-and-Execute 选型**：同一状态机两条入口；确定性任务（步骤清晰）用 Plan 省 token，探索型任务用 ReAct 鲁棒。critic 统一兜底：Plan 模式跑偏了也能重规划。
3. **自愈循环的边界**：错误回喂只能修"参数格式错"，修不了"工具没这能力"——所以自愈 3 次耗尽后 critic 判定为 plan_defect 回 planner，而不是无限重试。
4. **上下文压缩不丢关键状态**：摘要必然有损，所以关键工具输出在产生时即复制进 `key_outputs`（截断快照），每步注入 system——压缩只作用于"过程消息"，事实数据不走摘要。
5. **取消为什么是协作式**：强杀线程/任务会丢状态；在节点边界检查取消标志，状态照常落 checkpoint，取消本身也可追溯。

## 十、与需求文档的模块对照

M1 执行内核（`demo_cli.py`）→ M2 状态持久化（`demo_crash_recovery.py` + 恢复率 100%）→ M3 沙箱与自愈（`sandbox.py` + 自愈挽救率 100%）→ M4 异步并发（双队列 + 吞吐 40.3 tasks/s）→ M5 成本预算（双维度 + 降级）→ M6 观测产品化（事件流 + Web 时间线）。全部完成。
