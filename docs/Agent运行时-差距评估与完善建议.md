# LLM Agent Runtime — 目标差距评估与完善建议

> 评估对象：`llm-agent-runtime`（本仓库）
> 评估基准：面向真实业务的可托管 Agent 运行时（自然语言下目标 → 自主规划 → 工具调用 → 多步执行 → 交付结果）
> 评估方式：静态代码审查 + 实机执行取证（非纸面评估）
> 评估环境：Windows / conda env `agent-runtime`（Python 3.11.16）
> 代码规模（实测 2026-09-21）：`app/` 44 个 .py + `tests/` 25 个 + `scripts/` 5 个，合计 74 个 .py / 12,659 行
> （口径：`app/` 6,835 行、`tests/` 5,189 行、`scripts/` 635 行；不含 `__pycache__`、`web/`、`sandbox/`。
> 审计当时为 67 个 .py / 6,710 行 —— 增长来自 P2-1 鉴权（security/routes_admin）、
> P2-5/P2-6 可观测性（`app/observability/` 5 文件 1,401 行）与 P2-2 限流多 worker，见各附录）

---

## 0. 结论摘要

**总体判断：架构完成度远高于一般学生项目，7 个评估方向全部有可运行实现，无一是空壳。当前首要问题不是"缺功能"，而是"三个 P0 缺陷让亮点失效、指标口径虚高"，以及"工程护栏（CI/鉴权/可观测）缺失"。**

| 维度 | 实现完成度 | 可信度 | 关键结论 |
|---|---|---|---|
| 1. 任务分解与规划（双模式） | ★★★★☆ | 高 | 双入口 + critic 驱动重规划已闭环，缺运行期自适应切换与计划 DAG |
| 2. 工具注册与 Docker 沙箱 | ★★★★☆ | 中 | 沙箱加固项齐全，但**默认配置静默降级为不安全本地执行** |
| 3. 失败重试与反思 | ★★★☆☆ | 中 | 自愈 + critic 三分类已有；**自愈配额并发不安全**、错误分类靠字符串嗅探、缺 reflection |
| 4. 执行轨迹可视化 | ★★★☆☆ | 中 | 事件流落库完整，前端仍是 JSON 文本流 + 1.5s 轮询，无结构化渲染 |
| 5. 任务中断恢复（checkpoint） | ★★★★☆ | **中低** | 机制正确，但**"已完成动作不重复执行"与实现不符**（superstep 级 at-least-once） |
| 6. 关键技术栈 | ★★★★☆ | 中 | Function Calling / LangGraph / Celery / PG 全齐；**MCP 只做到描述符格式**、PG 与 Celery 路径零实测 |
| 7. 技术亮点 | ★★☆☆☆ | **低** | **上下文压缩在真实消息结构下基本失效**（实测压缩率仅 81%）；子 Agent 调度与双维预算扎实 |

**三个 P0（阻断在线演示 / 让亮点失效）**

1. `app/runtime.py:42` 缺少 `get_settings` 导入 → Windows 上 `import app.main` 直接 `NameError`，**Web 服务与 Celery worker 双双无法启动**，`tests/test_api.py` 4 条端到端用例无法收集。
2. `app/graph/nodes.py` 的 `react_step_node` 每轮把 system + user 再次追加进 `state.messages` → 上下文里堆叠 N 份重复提示，**实测第 5 轮调用 token 已是第 1 轮的 26 倍（202 → 5184）**，增长呈 O(n²)。
3. 承 2，`app/core/compressor.py` 的摘要切片假设"system 都在列表头部"被打破 → **实测压缩后仍保留 7 份 system 消息，token 仅从 8971 降到 7255（压缩率 81%）**，即"上下文压缩"这个核心亮点当前几乎不工作。

这三点都是 1 天内的修复量级，但决定了简历上能不能写"上下文压缩"和"可托管"。

---

## 1. 取证记录（可复核）

所有结论均来自实机执行，命令与输出可复现。

### 1.1 测试套件

```
环境：conda env `agent-runtime`（Python 3.11.16）
命令：python -m pytest tests/ -q --no-header
结果：exit=2
  ERROR collecting tests/test_api.py
  app/main.py:24 → app/runtime.py:42
  NameError: name 'get_settings' is not defined
  Interrupted: 1 error during collection
```

排除 `test_api.py` 后：

```
命令：python -m pytest tests/ -q --no-header --ignore=tests/test_api.py
结果：21 passed, 6 skipped in 6.73s
```

- **21 passed** = README 所写的"21 个用例"（口径无误）。
- **6 skipped** = `test_docker_sandbox.py` 4 例（Docker daemon 不可用）+ `test_postgres_checkpoint.py` 2 例（无 PostgreSQL）。
- 全仓 `def test_` 合计 **31 个**，其中 4 个（`test_api.py`）因 P0-1 完全无法收集。
- README 第 148 行"21 个用例"实际是"21 通过 + 6 跳过 + 4 收集失败"，建议同步口径。

### 1.2 P0-2 上下文重复堆叠（探针脚本）

4 轮工具调用 + 1 轮最终答案，打印每次 LLM 调用实际收到的消息：

```
call#1 条数= 2 tokens≈  202  ['system','user']
call#2 条数= 6 tokens≈ 1124  ['system','user','system','user','assistant','tool']
call#3 条数=10 tokens≈ 2262  ['system','user','system','user','system','user','assistant','tool','assistant','tool']
call#4 条数=14 tokens≈ 3615  ['system','user','system','user','system','user','system','user','assistant','tool','assistant','tool','assistant','tool']
call#5 条数=18 tokens≈ 5184  ['system','user','system','user','system','user','system','user','system','user','assistant','tool','assistant','tool','assistant','tool','assistant','tool']

最终 state.messages：19 条，system × 5，user × 5，assistant × 5，tool × 4
```

同时验证了 `app/graph/nodes.py:158-159` 的注释**与实现矛盾**：

> 注释写「state.messages 只存执行历史（assistant/tool/摘要），不含 system 与任务提示」

实际 `return {"messages": messages}` 中 `messages = [*base, assistant]`，而 `base` 就包含 system 与任务提示——注释描述的是**设计意图**，代码没有实现该意图。

### 1.3 P0-3 压缩器在真实消息结构下失效（探针脚本）

6 轮工具调用后，用 `compress_messages(msgs, threshold=3000)`：

```
压缩前: 条数=27 tokens≈8971  needs=True
压缩后: 条数=14 tokens≈7255  压缩率=81%
压缩后 role: ['system'×7, 'user'(摘要), 'tool','assistant','tool','assistant','tool','assistant']
被摘要掉的分片: system,user 交替 ×7 组 + 部分 assistant/tool
```

两个后果：
- 保留的 7 份 system 中有 6 份是**过期重复**的旧提示，永远压不掉；
- 19% 的降幅对"长轨迹控制成本"这个目标没有意义，且摘要生成本身还要**额外花一次 LLM 调用**。

### 1.4 仓库完整性

| 检查项 | 结果 |
|---|---|
| `.github/`（CI） | **不存在** |
| 依赖锁定文件 | **不存在**（无 lock / uv.lock / poetry.lock） |
| `docs/` | **空目录**（`.gitignore` 已预留 `docs/metrics.generated.md`） |
| 鉴权 / 限流代码 | **不存在**（无 `Depends`、无 auth、无 rate limit） |
| `.env` 是否被 git 忽略 | 是（`.gitignore:3`）✅ |
| Docker daemon 可用性 | 不可用 → 沙箱静默走 LocalSandbox |

### 1.5 指标口径复核（`scripts/metrics.py`）

| 指标 | 脚本实际口径 | 口径问题 |
|---|---|---|
| 断点恢复成功率 100% (n=10) | `make_engine(..., saver=MemorySaver(), interrupt_before=["tool_executor"])`，两个引擎实例**共享同一个内存 saver 对象** | 测的是"resume 逻辑正确"，**不含进程崩溃与磁盘持久化**。MemorySaver 进程重启即丢，不能代表断点恢复 |
| 自愈挽救率 100% (n=10) | 注入非法参数 → 修复 → 完成 | 口径基本可信 ✅ |
| 并发吞吐 40.3 tasks/s | `make_engine(...)` **不传 saver** → `compile(checkpointer=None)` | **完全绕过 checkpoint 写盘**。真实 SQLite saver 下 `AsyncSqliteSaver` 单连接会串行化，该数字不可外推 |

**这三条数字目前都不宜直接写进简历**（详见第 5 节）。

---

## 2. 逐方向差距矩阵

### 方向 1：任务分解与规划（ReAct / Plan-and-Execute 双模式切换）

**已实现**
- 双入口 `route_entry`（`nodes.py:84`）按 `mode` 路由进同一状态机，Plan 模式跑偏可由 `critic` 判 `plan_defect` 回 `planner` 重规划（`nodes.py:437`），且重规划只补剩余步骤（`nodes.py:111-117`）。
- 验收证据：`tests/test_plan_execute.py` 两条用例覆盖 happy path（3 步全 done）与未知工具触发 replan。

**差距**

| # | 差距 | 落点 | 影响 |
|---|---|---|---|
| 1.1 | **模式是任务级静态选择，非运行期切换**。没有"ReAct 跑了 N 步发现需要计划 → 自动升级 Plan"，也没有反向降级 | `nodes.py:84` `route_entry` | 目标写的是"双模式切换"，现状是"双入口选一"，面试会被追问 |
| 1.2 | **计划是扁平字符串列表，无 DAG**。`steps: [str]`，无依赖关系、无并行分组、无每步预期工具 | `nodes.py:107`、`prompts.PLAN_SYSTEM` | 无法真正并行执行计划步骤，也无法判断"步骤是否真的完成" |
| 1.3 | **"一次工具成功 = 该计划步骤完成"**。`critic_node:323-336` 只要本轮无失败就把 `plan[idx].status="done"` 并 `current_step+1` | `nodes.py:327-336` | 若某步骤需要两轮工具（如"先搜再算再写"），第一轮就被标记 done，**计划进度失真**。现有测试每步恰好 1 次工具，覆盖不到 |
| 1.4 | **规划失败静默降级**：JSON 解析不出 steps 时直接 `steps=[goal]`，不报错、无事件 | `nodes.py:107-109` | 规划能力退化不可观测 |
| 1.5 | **重规划无收敛控制**：可反复 `critic→planner`，仅靠 `iterations >= max_steps` 兜底 | `nodes.py:356-360` | plan thrash 会烧完预算才停 |
| 1.6 | ReAct 模式下 `current_step` 被无意义累加（plan 为空仍 +1） | `nodes.py:335-336` | 状态语义混淆 |

---

### 方向 2：工具注册与 Docker 沙箱执行

**已实现**
- MCP 风格注册表 + `jsonschema Draft202012Validator` 前置校验 + 超时折叠（`registry.py:60-110`）。
- Docker 沙箱 7 项加固：`network_disabled` / `mem_limit` / `nano_cpus` / `pids_limit=64` / `read_only` / `tmpfs 32m` / `user=65534`（`sandbox.py:63-76`），且用 `python -I -c` 传码规避 bind-mount 路径问题（这个坑解得漂亮，`sandbox.py:58-61` 的注释说明到位）。
- 6 个工具：`web_search`（含 bing/ddgs/mock 三 provider）/ `weather` / `code_run` / `db_query`（只读）/ `file_ops`（路径越狱防护）/ `subagent`。
- 4 条沙箱集成测试断言了「无挂载可执行」「出网被拦」「uid=65534」「超时被杀」。

**差距**

| # | 差距 | 落点 | 影响 |
|---|---|---|---|
| 2.1 | **默认配置不安全**：`allow_unsafe_local_exec: bool = True`（`config.py:37`），且 `build_sandbox` 在 auto 模式下 Docker 失败后**静默**回退本地子进程（`sandbox.py:150-162`），无告警、无事件 | `config.py:37` / `sandbox.py:148-163` | 对"可托管平台"是**默认不安全**：本地回退无网络隔离、可读宿主 FS。本次实测环境就是这种状态 |
| 2.2 | **沙箱后端不可观测**：`/health` 只返回 `{"status":"ok"}`，`/api/metrics` 不含 sandbox backend | `main.py:74-76` | 无法自证"沙箱真的生效了"，演示时也无法给招聘官看 |
| 2.3 | **无 MCP 服务端实现**。`registry.py:5` 注释称"可被 mcp_server.py 直接暴露"，但仓库里**没有 `mcp_server.py`**；README 第 18 行"可被任意 MCP 客户端消费"目前不成立，只有描述符**形状**是 MCP 的 | 缺失文件 | 这是"宣称 vs 事实"的落差，简历措辞需降级为"兼容 MCP 工具描述格式" |
| 2.4 | **`db_query` 只读校验是黑名单词边界匹配**（`db_query.py:30-33`）：`f" {kw} "` 会被 `/**/`、换行、`\t` 绕过；反向还会**误杀**合法查询（如 `select 'insert' as k`） | `db_query.py:21-34` | 安全性实际由 `mode=ro` 连接兜底（这点是对的），但"拒绝解析"这层不可靠；且存在假阳性 |
| 2.5 | **沙箱无容器池/预热**：每次执行 `containers.run` 冷启，30s 超时里镜像启动占大头 | `sandbox.py:62-93` | 高并发代码执行会打满；`code_run` 的 P95 延迟被容器冷启主导 |
| 2.6 | **stdout / stderr 合并**：`logs(stdout=True, stderr=True)` 且 `stderr=""` | `sandbox.py:79-87` | 模型无法区分"程序正常输出"与"异常堆栈"，降低自愈质量 |
| 2.7 | **沙箱内产物无法回收**：只回传 stdout，tmpfs 里生成的文件丢失 | `code_run.py:17-24` | "数据处理 → 交付文件"这条真实业务链路走不通 |
| 2.8 | **仅支持 Python**，无 node/deno/shell | `code_run.py:36-43` | 与"可托管平台"的通用性有距离 |
| 2.9 | 工具无权限分级 / 无 per-tool 配额 / 无版本化 | `registry.py` | 无法表达"某用户只能用只读工具" |

---

### 方向 3：失败自动重试与反思

**已实现**
- 参数校验失败走自愈循环：错误回喂 `REPAIR_SYSTEM` → 重新校验 → 重试，上限 `max_selfheal_retries=3`（`nodes.py:224-253`、`nodes.py:296-316`）。
- `critic` 三分类：`retryable` / `plan_defect` / `fatal`，`plan_defect` 回 planner，`fatal` 直接终止（`nodes.py:340-361`）。
- 验收证据：`tests/test_budget_selfheal.py` 4 例覆盖自愈成功与耗尽降级。

**差距**

| # | 差距 | 落点 | 影响 |
|---|---|---|---|
| 3.1 | **自愈配额是任务级全局计数**：`selfheal_total` 累计全任务所有工具的自愈次数 | `nodes.py:55`、`nodes.py:227` | 前几次失败耗尽 3 次配额后，**后续其他工具的校验失败将无法自愈**，直接判 `plan_defect`。应按 `(tool, call_id)` 计数 |
| 3.2 | **并发不安全**：`run_one` 内用 `nonlocal selfheal_total` 在 `asyncio.gather` 中共享累加；且每次自愈都要**串行等一次 LLM** | `nodes.py:218-253` | 并行工具时自愈配额互相挤占；声称的"并行工具"在失败路径退化为串行竞争 |
| 3.3 | **错误分类靠字符串嗅探**：`"越界"`、`"Permission"`、`"timeout"`、`"connection"` 等中文/英文关键字匹配，未命中则兜底 `retryable` | `nodes.py:340-349` | 脆弱（改一句文案就失效）；永久性错误会被当瞬时错误**重试到预算耗尽** |
| 3.4 | **retryable 无退避、无原样重试**：只是回 `compressor → react_step` 让模型重新决策 | `nodes.py:356-360` | 瞬时故障没有指数退避；同一调用不会被自动原样重试 |
| 3.5 | **缺"反思"层**：critic 只做分类，没有 reflection loop（自评失败原因 → 生成改进提示 → 写入可复用经验） | 全仓无 reflection 模块 | 目标里的"反思"目前只到"分类"这一步 |
| 3.6 | 错误信息被降维成字符串塞进 tool message（`{"error": "..."}`），`error_type` 未进入模型可见上下文 | `nodes.py:272-280` | 模型无法基于结构化错误类型做决策 |

---

### 方向 4：执行轨迹可视化

**已实现**
- 每个节点 `emit` 事件 → `Repository.append_event` 落库；事件类型覆盖规划/思考/工具/自愈/预算/压缩/critic/子 Agent 转发（`web/index.html:53-61` 有完整配色映射）。
- `GET /api/tasks/{id}/trace` 全量 + `/events?after=N` 增量轮询；Web 单页零依赖，可实时看到执行轨迹。
- 验收证据：`test_api.py::test_full_task_lifecycle` 断言 trace 含 `tool_result` / `task_done`，且增量游标 `last_seq` 对齐。

**差距**

| # | 差距 | 落点 | 影响 |
|---|---|---|---|
| 4.1 | **前端是 JSON 文本流**：`esc(evPayload(e)).slice(0,600)` 直接把 payload 字符串化 | `index.html:209` | 招聘官看到的是"一堆 JSON"，不是"Agent 在思考"。工具参数/结果无可折叠面板、无耗时条、无 token 曲线 |
| 4.2 | **计划进度没渲染**：CSS 定义了 `.plan-chip.done/.pending`（`index.html:63-65`）但从未生成 plan chip，`d-plan` 只显示断点位置 | `index.html:187` | **死 CSS**；计划可视化这一卖点在 UI 上不存在 |
| 4.3 | **1.5s 轮询 + 每轮 3 个请求**：`setInterval(1500)` 内先 `refreshDetail()`（内又调 `refreshList()` → `/api/tasks` + `/api/metrics`）再 `pollEvents()` | `index.html:171`、`index.html:180` | 打开一个详情页 ≈ 2 req/s；无 SSE/WebSocket、无断线重连、无背压 |
| 4.4 | **events 表缺复合索引**：只有 `Event.task_id` 单列索引（`models.py:47`），查询却是 `WHERE task_id=? AND seq>? ORDER BY seq` | `models.py:43-51` | 事件量上去后轨迹查询退化 |
| 4.5 | **轨迹查询无分页**：`get_events` 全量返回 | `repository.py:66-71` | 长任务几千条事件一次性返回，前端 DOM 全量渲染 |
| 4.6 | **`_seq` 是引擎实例级计数器**（`self._seq = 0`），不是任务级 | `engine.py:42` | 多任务共用引擎时 seq 跨任务递增，`after=N` 语义不干净（功能上因按 task_id 过滤仍正确，但 seq 不可当"事件总数"用） |
| 4.7 | 无轨迹导出/分享/回放；无耗时瀑布图；无节点级 Gantt | — | 作品集缺"可截图的高信息密度图"，最影响招聘官第一印象 |

---

### 方向 5：任务中断恢复（checkpoint）

**已实现**
- `checkpointer` 可插拔：`AsyncSqliteSaver` / `AsyncPostgresSaver`（`runtime.py:46-77`），`AsyncPostgresSaver` 基于 psycopg 连接池并处理了 asyncpg URL 转换。
- `resume_task` 用 `ainvoke(None)` 从待执行节点续跑（`engine.py:114-125`）；已到 END 的任务 resume 是幂等 no-op。
- 跨进程演示脚本 `scripts/demo_crash_recovery.py`（子进程 A 到断点退出 → 父进程查快照 → 子进程 B 恢复）。
- `GET /api/tasks/{id}` 暴露断点位置 `checkpoint_next`。

**差距**

| # | 差距 | 落点 | 影响 |
|---|---|---|---|
| 5.1 | **"已完成动作不重复执行"与实现不符**。checkpoint 落在 superstep 边界，节点内已执行的工具调用在恢复时会**重放**。`tests/test_checkpoint_resume.py:64-66` 自己就断言断点前的 `web_search` 被重新执行（`tool_msgs` 共 2 条，第一条是重放的"北京"） | `engine.py:109/122`、测试自身为证 | README 第 17 行的措辞（"已完成动作不重复执行"）**过强**。对 `web_search` 无副作用，但 `file_ops.write` / 外部 API 扣费 / DB 写就是**重复副作用**。这是简历表述的高风险点 |
| 5.2 | **演示脚本的"不重复"是条件选择的结果**：`demo_crash_recovery.py` 把断点设在 `tool_executor`**之前**，工具根本没执行过，所以"断点前 0 次" | `demo_crash_recovery.py:36-39,88` | 演示结论正确但与"已完成动作不重复"是两件事，混在一起会被面试官拆穿 |
| 5.3 | **无幂等键 / 副作用去重表**：恢复时不跳过已完成的 `tool_call_id` | 全仓无 | 无法达成真 at-most-once |
| 5.4 | **恢复接口无并发保护**：`submit_resume` 不检查该 task 是否已在 `_running`（`local_queue.py:44-46`），`POST /resume` 可重复调用 | `local_queue.py:44-46`、`routes_tasks.py:79-95` | 两个引擎实例同时用同一 `thread_id` 写 checkpoint，状态可能互相覆盖 |
| 5.5 | **无 HITL（人工审批暂停）**：`interrupt_before` 只用于测试与演示 | `engine.py:33` | 生产里"执行前等审批"这条能力缺失 |
| 5.6 | **无 checkpoint 保留/清理策略** | — | SQLite 文件无限增长 |
| 5.7 | **单引擎单 saver 连接**：`EngineHolder` 持有一个 saver | `runtime.py:116-119` | SQLite 下并发写串行化，是真实吞吐瓶颈（也解释了 5.8 的口径问题） |
| 5.8 | **PG 路径零实测**：`test_postgres_checkpoint.py` 2 例在无 PG 环境被 skip，本次运行即如此 | `tests/test_postgres_checkpoint.py` | README 把 PG 列为"已实现"，但没有一次真实通过记录 |

---

### 方向 6：关键技术（Function Calling / MCP / LangGraph / Celery / PG）

| 技术 | 状态 | 说明 |
|---|---|---|
| Function Calling | ✅ 完整 | OpenAI `tools` 协议，`tool_calls` 正确回填 assistant 消息并带 `tool_call_id`（`nodes.py:446-455`），有测试断言 |
| MCP | ⚠️ **只有一半** | 有 `mcp_descriptor()` 输出 `{name, description, inputSchema}` 的 `tools/list` 形状；**无 MCP server 进程、无 stdio/SSE 传输、无 MCP client（不能消费外部 MCP 工具）**。措辞应为"工具描述兼容 MCP 格式" |
| LangGraph 持久化 | ✅ 完整 | 6 节点 + 条件边；saver 可插拔；`recursion_limit` 按 `max_steps*4+24` 动态计算（`engine.py:100`，这个细节很到位） |
| Celery / ARQ 异步队列 | ⚠️ 双轨但只测一条 | LocalTaskQueue（asyncio，有 API 端到端测试）+ Celery（**零测试覆盖**）。`celery_app.py:4` 文档说 Windows 用 `--pool=solo`，而 `docker-compose.yml:40` 用 `--concurrency=4`，两处口径不一致 |
| PostgreSQL 执行图存储 | ⚠️ 代码在，实测无 | 业务库 asyncpg + checkpoint psycopg 双连接设计正确，但无 PG 可用环境验证；无 Alembic 迁移（`create_all` 无法演进表结构） |

**其他横切差距**

| # | 差距 | 影响 |
|---|---|---|
| 6.1 | **无任何鉴权 / 多租户 / 限流**：`/api/tasks` 完全开放，任何人都能提交任务烧 token | 对"可托管平台"是最大产品缺口。可复用 campus-assistant 的三层限流（每 IP 每分钟 / 每日 / 全局每日）经验 |
| 6.2 | ~~**无可观测性标准接入**：无 OpenTelemetry / Prometheus，无 trace_id 贯穿，无 LLM 调用级 span~~ | ~~`/api/metrics` 只有聚合 SQL~~ **→ 已补，见附录 F**（Prometheus `/metrics` + traceparent 贯穿 + 结构化日志） |
| 6.3 | **无 CI**：`.github/` 不存在 | **本次 P0-1 的 NameError 就是没有 CI 的直接后果**——有 CI 的话 push 时即暴露。姊妹项目 campus-assistant 已有 CI 双门禁，两个项目护栏水平不对称 |
| 6.4 | **无依赖锁定** | 复现性弱，CI/部署会漂 → 见文末附录 E：已补 `requirements.lock.txt` + CI 一致性门禁 |
| 6.5 | **无 Alembic** | 表结构变更不可演进 |

---

### 方向 7：技术亮点

**上下文压缩 —— 当前失效（P0-3）**

设计思路本身是教科书级的：摘要必然有损，所以关键工具输出在**产生时**即复制进 `key_outputs`（截断快照）、每步注入 system，压缩只作用于"过程消息"。这套"事实数据不走摘要"的分离是正确的。

但实现被 P0-2 打穿：`compress_messages` 的 `system = [m for m in messages if role=="system"]` + `mid = messages[len(system): len(messages)-6]`（`compressor.py:43-45`）假设 system 全在头部且只有一份。实测结果（第 1.3 节）：保留 7 份 system、压缩率仅 81%。**修复 P0-2 后该机制即可正常工作**，无需重写。

另一个隐患：多份 system 且位于对话**中段**，部分 OpenAI 兼容服务会拒绝非首位 system 消息或行为异常。

**工具结果结构化校验与自愈 —— 扎实**

`jsonschema Draft202012Validator` + `check_schema` 自检 + 错误路径拼成人可读消息（`registry.py:60-67`），回喂给 REPAIR 提示词。方向对、边界清楚（自愈耗尽 → 判 plan_defect 而非无限重试）。扣分项是 3.1 / 3.2 的计数与并发问题。

**并发子 Agent 资源调度 —— 本项目最亮的一块**

`app/tools/subagent.py` 的四点设计都成立且有测试：
1. **全局信号量** `max_concurrent_subagents=2` 限制同时运行的子 Agent 数；
2. **预算上卷**：子 Agent 消耗经 `_budget_tokens` 返回并由父引擎计入父预算（`nodes.py:263-266`，且 `pop` 掉不进模型上下文），使**父预算天然约束整棵执行树**；
3. **递归防护**：子 Agent 的工具注册表不含 `subagent`，深度固定为 1（`factory.py:40-51`）；
4. **隔离与可观测**：独立 `thread_id` + MemorySaver，事件以 `subagent_event` 转父轨迹。
   验收：`tests/test_subagent.py` 4 例覆盖委托+预算上卷、递归拦截、并行双委托、失败传播。

可补强：子 Agent 用 MemorySaver → 父任务恢复时子任务中间态丢失（只能整体重放）；子 Agent 返回**自由文本**、父无法校验其结论；两个信号量（tasks / tools / subagents）独立，无总资源池。

**token / 步数双维度预算 —— 设计对，口径需澄清**

`check_budget` 两维判定 + 降级一次 + 带 `key_outputs` 优雅终止（`budget.py`、`nodes.py:391-400`）。预算超限后**只走模板、不再调 LLM**（这点是对的，我已核对 `finisher_node` 提前 return）。

待补：
- `steps_used` 只在 `react_step` 递增，**planner / finisher 的 LLM 调用不计步**（`nodes.py:183`）→ "步数预算"≠"LLM 调用次数"；
- 降级只有一级（`downgraded` 单布尔），无模型阶梯；
- **两套 token 口径混用**：预算用真实 `usage.total_tokens`，压缩阈值用 `estimate_tokens` 启发式估算（`llm.py:16-25`）→ 可能"预算没超但压缩已触发"或反之；
- 无成本（¥）维度、无 per-task 硬超时、无跨任务全局配额。

---

## 3. 优先级路线图

### P0 — 1 天内，阻断发布（必须最先做）

| # | 动作 | 落点 | 验收方法 |
|---|---|---|---|
| P0-1 | 补 `get_settings` 导入：`from app.config import Settings, get_settings` | `app/runtime.py:15` | `python -m pytest tests/ -q` → 0 collection error；`uvicorn app.main:app` 能起；`curl /health` 200 |
| P0-2 | **停止把 system + user 追加进 `state.messages`**：`react_step_node` 中只把 `[assistant, *tool_msgs]` 写回 `messages`，system/任务提示每轮现构造 | `app/graph/nodes.py:174-199` | 重跑 4 轮探针：call#5 的 role 序列应为 `[system,user,assistant,tool,assistant,tool,assistant,tool,assistant,tool]`，token 约 900 而非 5184 |
| P0-3 | P0-2 修完后微调压缩器：`mid` 切片不再依赖"system 全在头部"，改为按 role 白名单切片 | `app/core/compressor.py:43-49` | 压缩率应 < 40%（8971 → < 3600），且 `key_outputs` 完整 |
| P0-4 | **沙箱默认安全**：`allow_unsafe_local_exec` 默认改 `False`；`build_sandbox` 回退时打 `WARNING` 并 emit 事件；`/health` 暴露 `sandbox_backend` 与 `isolated: true/false` | `app/config.py:37`、`app/executor/sandbox.py:148-163`、`app/main.py:74-76` | `/health` 返回 backend；无 Docker 时启动即警告 |
| P0-5 | **加最小 CI**：`.github/workflows/ci.yml` = 装依赖 + `pytest -q` + `ruff check` | 新文件 | push 后 Actions 绿；故意引入一个 NameError 应 fail |

### P1 — 1 周，把亮点做实（简历敢写的前提）

| # | 动作 | 落点 | 验收方法 |
|---|---|---|---|
| P1-1 | **checkpoint 幂等**：新增 `tool_calls` 执行记录表（`task_id + tool_call_id` 唯一），`tool_executor` 执行前查表跳过 | `app/storage/models.py`、`app/graph/nodes.py:202-269` | 新测试：断点设在 `file_ops.write` **之后**的 superstep，恢复后文件只被写 1 次 |
| P1-2 | 自愈计数改 `(tool, call_id)` 维度 + 并发安全（局部计数 + 锁） | `app/graph/nodes.py:218-253` | 新测试：并行 3 个工具全部参数非法，三个都应各自获得 3 次自愈机会 |
| P1-3 | **critic 结构化错误分类**：`ToolExecutionError` 增加 `error_code` / `retryable` / `backoff_s` 字段，字符串嗅探降为兜底 | `app/tools/registry.py`、`app/graph/nodes.py:340-361` | 新测试：注入永久性错误不应被无限重试 |
| P1-4 | retryable 路径加指数退避 + 原样重试上限 | `app/graph/nodes.py` | 新测试：连续瞬时失败 N 次后原样重试，成功即恢复 |
| P1-5 | **指标口径修正**：`metrics.py` 三处——恢复率用 `AsyncSqliteSaver`（磁盘）、吞吐开启 checkpointer、新增真实崩溃（`kill -9` 子进程）恢复率 | `scripts/metrics.py:31,38` | 重跑输出新数字；与 README 表同步 |
| P1-6 | **MCP 服务端落地**：`app/mcp_server.py` 暴露 `tools/list` + `tools/call`（stdio），并支持 MCP client 挂载外部工具 | 新文件 | 用任一 MCP 客户端（如 `mcp` CLI）能列出 6 个工具并成功调用 `web_search` |
| P1-7 | **轨迹可视化升级**：SSE 推送替代 1.5s 轮询；结构化渲染工具调用（参数/结果可折叠）、计划进度 chip、token/耗时柱；轨迹导出 JSON/Markdown | `app/api/routes_tasks.py`、`web/index.html`、`app/storage/models.py:43-51` | 浏览器实测：无轮询也能实时刷新；计划 chip 随步骤变绿 |
| P1-8 | events 表加 `(task_id, seq)` 复合索引 + `get_events` 分页 + checkpoint 保留策略 | `models.py:43-51`、`repository.py:66-71` | 5,000 事件下轨迹查询 < 100ms |
| P1-9 | 恢复接口并发保护：`submit_resume` 前检查 `task_id in self._running` | `app/worker/local_queue.py:44-46` | 新测试：并发 2 次 resume 只生效 1 次 |
| P1-10 | **PG + Celery 路径实测**：compose 只跑 PG+Redis，worker 跑宿主机；给 Celery 路径补 1 条集成测试 | `docker-compose.yml`、`tests/` | `pytest` 中 PG 用例不再 skip；Celery 任务端到端成功 |
| P1-11 | README 与实现对齐：删掉"已完成动作不重复执行"的强表述、修正"21 个用例"、MCP 措辞降级、标注三张指标表的口径 | `README.md:17,18,148,30-36` | 逐条核对，无夸大 |

### P2 — 2–4 周，产品化（拉开与同类项目差距）

| # | 动作 | 落点 |
|---|---|---|
| P2-1 | 鉴权 + 多租户 + per-user 配额 / 三层限流（复用 campus-assistant 经验） | `app/api/`、`app/storage/models.py` |
| P2-2 | HITL：`interrupt_before` 暴露为"待审批"状态 + `/approve` `/reject` 接口 + UI 审批按钮 | `app/graph/engine.py`、`routes_tasks.py` |
| P2-3 | 计划 DAG 化（依赖 + 并行分组）+ 运行期 ReAct↔Plan 自适应升级 | `app/graph/nodes.py`、`prompts.py` |
| P2-4 | 沙箱容器池 / 预热；多语言沙箱（node/deno）；沙箱内产物回收（tar 出 tmpfs） | `app/executor/sandbox.py` |
| P2-5 | ~~OpenTelemetry + Prometheus；结构化日志带 `task_id/trace_id`~~ **已做**（Prometheus `/metrics` + W3C traceparent 贯穿 + JSON 结构化日志；**零新依赖**） | `app/observability/`、`app/main.py`、`app/graph/engine.py`、`app/core/llm.py`、`app/graph/nodes.py`、`app/worker/local_queue.py`、`app/storage/models.py` |
| P2-6 | Alembic 迁移 + 依赖锁定（`uv.lock` / `requirements.lock`） | 新文件 |
| — | ~~依赖锁定~~ **已做**（`requirements.lock.txt`，见附录 E）；~~可观测性~~ **已做**（见附录 F）；Alembic 仍未做 | 2026-09-20 / 2026-09-21 |
| P2-7 | 反思记忆：失败模式库（跨任务可复用），retryable 失败沉淀为提示词片段 | 新 `app/core/reflection.py` |
| P2-8 | 成本维度预算（¥）+ per-task 硬超时 + 全局 token 配额 | `app/core/budget.py` |

---

## 4. 与姊妹项目的定位协同（作品集叙事）

两个项目不是重复，而是**互补的一对**，这是面试里最好用的叙事：

| | campus-assistant | llm-agent-runtime |
|---|---|---|
| 系统形态 | 确定性 RAG 流水线（LangGraph 固定五阶段） | 动态执行系统（模型实时规划） |
| 人是 | 架构设计者 | 护栏设计者 |
| 核心难题 | 检索准不准 | 规划对不对 / 执行安不安全 / 崩了能不能恢复 / 成本可不可控 |
| 关键指标 | MRR@5 0.972、Recall@1 0.961、缓存阈值 0.86、366 pytest + 61 vitest、三层限流 | 6 节点状态机、沙箱 7 项加固、自愈/恢复/吞吐、子 Agent 预算上卷 |
| 已验证护栏 | CI 双质量门禁（检索 + 阈值） | **待补：CI 目前缺失** |

**一句话定位**：`campus-assistant` 证明我能把 AI 应用**做对**；`llm-agent-runtime` 证明我能把 AI 应用**做稳**——把崩溃、失败、超预算、并发冲突这些负面路径当成一等公民来设计。

---

## 5. 简历亮点表述（按"现在能写 / 修复后能写"分级）

### 5.1 现在就能写的（有代码 + 有测试支撑）

> **可托管 Agent 运行时 · 独立开发**　　`Python / FastAPI / LangGraph / Docker / Celery / PostgreSQL`
>
> **项目与职责**：面向真实业务的可托管 Agent 运行时——用户用自然语言下达目标，Agent 自主规划、调用工具（搜索 / 沙箱代码执行 / 数据库 / 文件操作）、多步执行并交付结果。独立完成状态机内核、工具层、沙箱、调度与观测全链路。
>
> **执行内核**：基于 LangGraph 构建 6 节点状态机（planner / react_step / tool_executor / critic / compressor / finisher）+ 条件边，实现 ReAct 与 Plan-and-Execute 双入口路由同一状态机；critic 节点对工具失败三分类（retryable 重试 / plan_defect 回退重规划 / fatal 终止），重规划只补剩余步骤。
>
> **工具与沙箱**：MCP 风格工具注册表（name / description / inputSchema），jsonschema Draft 2020-12 前置校验 + 超时折叠；`code_run` 走 Docker 沙箱，7 项隔离加固（network_disabled / mem_limit / nano_cpus / pids_limit / read_only 根 FS / tmpfs / uid 65534），代码经 `python -I -c` argv 传入而**不依赖 bind-mount**——解掉了一个「引擎自身在容器内时挂载路径不存在、报 can't open file」的真实坑；4 条沙箱集成测试断言无网络 / 非 root / 超时被杀。
>
> **失败自愈**：JSON Schema 校验失败 → 把校验错误与原始参数回喂模型修参 → 重校验，上限 3 次；耗尽后由 critic 判为能力缺陷回 planner，而非无限重试。10 次离线注入非法参数的挽救率 100%。
>
> **断点恢复**：LangGraph checkpointer 落 SQLite / PostgreSQL，状态在 superstep 边界落盘，进程崩溃后以同 `thread_id` + `ainvoke(None)` 从断点续跑；提供跨进程崩溃恢复演示脚本（子进程 A 到断点退出 → 子进程 B 恢复至完成）。
>
> **并发子 Agent（本项目最亮）**：把带独立预算的迷你 Agent 封装为工具，实现四点资源调度——全局信号量限制并发子 Agent 数、**子 Agent token 消耗上卷计入父预算使父预算约束整棵执行树**、子注册表剔除 subagent 使递归深度固定为 1、独立 thread_id + 事件转发进父轨迹；4 条用例覆盖委托+预算上卷 / 递归拦截 / 并行双委托 / 失败传播。
>
> **成本控制**：token 与步数双维度预算，任一超限先降级便宜模型续跑一次，再超限则带已确认关键数据优雅终止；关键工具输出在产生时即写入不可压缩的 `key_outputs`，使摘要压缩不丢事实数据。
>
> **工程化**：FastAPI + SQLAlchemy 2.0 异步 + 8 个 RESTful 接口（提交 / 轨迹增量轮询 / 断点恢复 / 协作式取消 / 工具清单 / 指标）；双队列形态（进程内 asyncio 队列零依赖 / Celery+Redis 生产）共享同一引擎；Web 单页零依赖实时轨迹时间线。
>
> **技术栈**：Python · FastAPI · LangGraph · Docker · Celery · Redis · PostgreSQL / SQLite · SQLAlchemy asyncio · jsonschema

### 5.2 修复后能改进简历的（P0/P1 完成即可升级）

| 当前表述 | 修复后升级为 | 前置 |
|---|---|---|
| （不写）上下文压缩 | "长轨迹上下文压缩：工具事实输出实时快照进不可压缩 `key_outputs`，过程消息按窗口+LLM 摘要压缩，实测长轨迹 token 降幅 XX%" | P0-2 + P0-3 修完，用真实数字填 XX |
| "已完成动作不重复执行" | "新增 `(task_id, tool_call_id)` 执行记录表，崩溃恢复时跳过已完成调用，实现副作用 at-most-once（`file_ops.write` 恢复后仅写 1 次，有测试）" | P1-1 |
| "可被任意 MCP 客户端消费" | "提供 MCP 服务端（stdio），暴露 `tools/list` + `tools/call`；同一注册表同时输出 OpenAI function calling 与 MCP 描述符，已用 MCP 客户端实测挂载" | P1-6 |
| 断点恢复成功率 100%（MemorySaver） | "以 `kill -9` 真实杀进程、`AsyncSqliteSaver` 磁盘 checkpoint 实测恢复成功率 100% (n=N)" | P1-5 |
| 并发吞吐 40.3 tasks/s | "开启 checkpointer 后端到端吞吐 X tasks/s（并发 N）" | P1-5 |
| （不写）CI | "GitHub Actions 覆盖 31 条 pytest 用例，push 即拦截启动期 NameError 类缺陷" | P0-5 |

### 5.3 面试深挖预案（现有 README 第九节已很好，补 3 条）

1. **"你说 checkpoint 恢复不重复执行，那有副作用的工具怎么办？"**
   答：区分两件事——checkpoint 落在 superstep 边界，节点内动作是 at-least-once；真正的 at-most-once 需要 `(task_id, tool_call_id)` 幂等表，这是我 P1 的第一项。`web_search` 这类幂等工具靠重放无害，`file_ops.write` 必须走幂等表。
2. **"上下文压缩会不会丢关键数据？"**
   答：设计上分两层——事实数据（工具查到的数字）在产生时即复制进 `key_outputs` 并按步注入 system，压缩只作用于过程消息，所以摘要的有损性不触及事实。但我实测发现了实现层缺陷：节点把 system/任务提示重复追加进历史，导致压缩器切片假设被打破、压缩率仅 81%，修复后 < 40%。
3. **"多工具并行失败了，自愈还并行吗？"**
   答：不并行，这是我发现的一个真实缺陷——`selfheal_total` 是任务级全局计数且在 `gather` 中 `nonlocal` 累加，并行失败时配额互相挤占、自愈退化为串行等 LLM。修法是改 `(tool, call_id)` 维度计数并加锁。

---

## 6. 已知限制与未验证项（强制披露）

| 项 | 状态 |
|---|---|
| Docker 沙箱 4 条集成测试 | **本次未执行**（本机 Docker daemon 不可用，pytest 自动 skip）。沙箱加固有效性未经我实测，仅有代码与测试代码为证 |
| PostgreSQL checkpoint 2 条用例 | **本次未执行**（无 PG 环境，skip）。PG 路径无任何一次真实通过记录 |
| Celery 队列路径 | **全仓零测试覆盖**，本次未验证 |
| `scripts/metrics.py` 三个指标 | **本次未重跑**（脚本无参数依赖外，但结论基于代码口径审查：`make_engine` 不传 saver / 用 MemorySaver） |
| 前端交互 | 未在浏览器实测（本次为只读审计，未启动服务） |
| 真实 LLM 链路 | 未验证（依赖 API Key；本次全部基于离线脚本化模型） |
| P0-1 的实际启动影响 | 依据 `app/main.py:24` 与 `app/worker/celery_app.py:16` 的**模块级调用**判定为硬失败；未实机启动 uvicorn 复现（未修改任何文件） |

**本次审计未修改仓库任何文件**（仅新增本报告）。P0 修复建议均给出精确落点，可直接执行。

---

*报告生成：2026-09-19 · 评估环境 Windows / conda `agent-runtime` / Python 3.11.16*

---

# 附：P0 修复记录（2026-09-19 同日）

## A. 修复清单

| # | 缺陷 | 状态 | 改动落点 |
|---|---|---|---|
| P0-1 | `app/runtime.py` 缺 `get_settings` 导入 → Windows 上 `import app.main` NameError | **已在磁盘上被修复（非本轮改动）** | `app/runtime.py:16` → `from app.config import Settings, get_settings` |
| P0-2 | `react_step_node` 每轮把 system+user 写回 `state.messages` → 上下文 O(n²) 膨胀 | ✅ 本轮修复 | `app/graph/nodes.py:157-189` |
| P0-3 | 压缩器按下标切片，残留 system 且压缩无效 | ✅ 本轮修复 | `app/core/compressor.py:34-69` |
| — | 新增 CI 门禁（4 个 job） | ✅ 本轮新增 | `.github/workflows/ci.yml` |

**关于 P0-1 的说明（避免误记功）**：审计完成时该缺陷确实存在（`pytest` 以 exit=2 中断，`NameError` 可复现）。本轮开始前复核发现 `app/runtime.py` 的 mtime 已变为 `13:23:46`，第 16 行已含 `get_settings`，`import app.main` 冒烟通过——即**该修复不是我做的**。本轮对它的贡献是：把它变成 CI 可拦截的对象（见 C 节反向验证）。

## B. 逐项修复内容

### P0-2 `app/graph/nodes.py`

**改了什么**：`react_step_node` 组装 LLM 上下文时，把每轮现构造的 `system` + `user` 从 `state.messages` 中剥离；只把本轮的 assistant 决策追加进历史。

```python
history = state.get("messages", [])          # 纯执行历史：assistant / tool
base = [
    {"role": "system", "content": system},   # 每轮现构造
    {"role": "user", "content": user},       # 每轮现构造（携带最新计划进度）
    *history,
]
resp = await self.engine.llm.chat(base, tools=..., model=model)
messages = [*history, self._assistant_message(resp)]   # 只追加 assistant
```

同时把误导性注释改成显式警告，说明为什么不能写回（防止后人改回去）。

**为什么这样修**：`state.messages` 是「执行历史」通道，`system`/`user` 是「每轮重建的输入」通道。原实现把两者混在一个通道里，历史每轮被重新拼装一次，于是上一轮拼进去的输入变成下一轮的「历史」，形成自引用累积。

**实测前后对比**（4 轮工具调用 + 1 轮收尾，同一个探针脚本）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 第 5 次 LLM 调用消息条数 | 18 | 10 |
| 第 5 次 LLM 调用 tokens | **5184** | **3033** |
| role 序列 | `system,user` ×5 + assistant/tool ×4 | `system,user` ×1 + assistant/tool ×4 |
| `state.messages` 残留 system 份数 | 5 | **0** |
| `state.messages` 残留 user 份数 | 5 | **0** |
| 增长形态 | O(n²) | O(n) |

### P0-3 `app/core/compressor.py`

**改了什么**：放弃「system 一定在列表头部、且只有一份」的下标假设，改为按 role 划分；并加了窗口边界保护。

```python
pinned  = [m for m in messages if m.get("role") == "system"]      # 原样保留
history = [m for m in messages if m.get("role") != "system"]
recent  = history[-KEEP_RECENT_WINDOW:]
mid     = history[: len(history) - len(recent)]
while recent and recent[0].get("role") == "tool":   # 防止孤儿 tool 消息
    mid.append(recent.pop(0))
...
compressed = [*pinned, summary_msg, *recent]
```

**顺带修掉一个自伤**：改完 role 划分后，第 68 行 `[*system, ...]` 的旧变量名会直接 `NameError`——正是本轮 P0-1 的同类错误。已在提交前改为 `[*pinned, ...]`。

**为什么加 `while` 那段**：摘要会吃掉窗口左侧的消息，若窗口首条是 `tool`，它的 `tool_calls` 父 assistant 消息已被摘要掉，形成「孤儿 tool 消息」。部分 OpenAI 兼容接口会直接拒绝这种序列。把孤儿 tool 退回被摘要段即可。

**实测前后对比**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 输入规模 | 27 条 / 8971 tokens（6 轮） | 29 条 / 6838 tokens（14 轮） |
| 压缩后规模 | 14 条 / 7255 tokens | **6 条 / 1014 tokens** |
| **压缩率** | **81%**（几乎压不动） | **15%** |
| 残留 system 份数 | 7（其中 6 份是过期重复） | **0** |
| 窗口首条 role | 不确定（按下标切） | 断言必须是 `assistant` ✅ |

另加一个回归场景：构造「历史里穿插 8 份 system + 8 份 user」的脏列表（模拟旧实现会产生的结构），验证 8 份 system 全部原样保留、窗口不以孤儿 tool 开头 → 通过。

### CI `.github/workflows/ci.yml`

**触发条件**：`push` 到 `main`/`master` · 指向 `main`/`master` 的 PR · `workflow_dispatch` 手动触发。同分支新推送取消上一次未完成运行（`concurrency.cancel-in-progress`）。

**四个 job 与检查项**：

| job | 检查项 | 说明 |
|---|---|---|
| `static`（~20s，快速失败） | `ruff check --select E9,F63,F7,F82 app tests scripts` | **F82 = undefined name**，专门拦 P0-1 这类缺陷；只开关键规则，不启用风格规则，避免存量代码把 CI 长期挂红 |
| | `python -m compileall -q app tests scripts` | 语法错误 |
| | `import app.main` / `import app.worker.celery_app` | **导入期崩溃**——单元测试里没人 import 这两个入口，所以 P0-1 直到 `test_api` 收集失败才暴露；这两个冒烟语句是直接补上的缺口 |
| `test` | `pytest tests/ --ignore=test_docker_sandbox.py --ignore=test_postgres_checkpoint.py` | 25 条离线用例，结果与机器无关，期望固定 `25 passed` |
| `integration` | `pytest tests/test_docker_sandbox.py tests/test_postgres_checkpoint.py -rs` | 需要 Docker daemon。GitHub ubuntu runner **自带** Docker，所以这 6 条会真正执行（本地无 Docker 时它们只会 skip）。这是「沙箱隔离」与「PostgreSQL checkpoint」两个卖点目前唯一的实测来源 |
| `smoke` | `seed_demo_db.py` → `demo_cli.py --offline` → `grep "状态: done"` | 全链路冒烟：ReAct → 并行工具 → 沙箱 code_run → 交付 |
| | `demo_crash_recovery.py` → `grep "status=done"` | 跨进程 checkpoint 恢复 |
| | `metrics.py --n 5 --m 10` → `grep "断点恢复成功率 100%"` + `grep "自愈挽救率 100%"` | **量化指标门禁**：把基线写进 CI，防止静默退化（沿用 campus-assistant 的双门禁做法） |

**封闭性设计**：全局 env 把 `SEARCH_PROVIDER` 钉成 `mock`。实测发现 `demo_cli.py --offline` 里的「offline」**只管模型不管工具**——假模型确定、但 `web_search` 仍按 `.env` 的 `SEARCH_PROVIDER=bing` 真的访问 `cn.bing.com`。不钉 mock 的话，CI 会依赖外网与必应页面结构，属于隐性 flake。

## C. 验证修复生效的方式

| 验证 | 命令 | 结果 |
|---|---|---|
| 测试套件从「收集失败」恢复到全绿 | `python -m pytest tests/ -q` | **25 passed, 6 skipped**（原：21 passed / 6 skipped / **1 error during collection**） |
| P0-2 上下文不再膨胀 | 4 轮工具调用探针，打印每次 LLM 调用实收消息 | 第 5 次调用 **18 条/5184 tokens → 10 条/3033 tokens**；`state.messages` 中 system/user 各 **5 → 0** |
| P0-3 压缩真的生效 | 14 轮工具调用 → `compress_messages(threshold=2000)` | 压缩率 **81% → 15%**；残留 system **7 → 0**；窗口首条断言 `assistant` 通过 |
| P0-3 抗脏历史 | 合成「穿插 8 份 system」列表 | 8 份 system 全部 pinned 保留，无孤儿 tool 开头 |
| **CI 能拦住 P0-1（反向验证）** | 把 `app/runtime.py` 复制到临时目录、去掉 `get_settings` 导入，用 CI 同款命令跑 ruff | `F821 Undefined name 'get_settings'` → **exit 1**，CI 会 fail ✅ |
| CI 各步骤本机可跑通 | 逐条复刻 `ci.yml` 里的命令（数据目录指向临时目录） | **10/10 通过** |
| workflow 结构有效 | 解析 YAML | 4 个 job、3 类触发条件均正确 |

## D. 已知限制与未验证项（本轮）

| 项 | 状态 |
|---|---|
| **CI 尚未在 GitHub 上真实运行过** | 本轮只做了本机复刻验证（10/10 通过）。真实 runner 上的首跑结果需 push 后确认 |
| `integration` job 的 6 条用例 | 本机无 Docker daemon，本轮仍为 skip。CI 上会真正执行（`test_docker_sandbox` 会构建 `agent-sandbox:latest`、`test_postgres_checkpoint` 会拉 `postgres:16-alpine`）。**这是唯一存在首次运行不确定性的 job**；若 runner 上因拉镜像/构建失败而红，可先给它加 `continue-on-error` 观察 |
| `ruff==0.16.7` 版本锁定 | 取自本机受管 venv 实测通过的版本；未在 PyPI 侧二次确认该版本号长期可用 |
| 未动 README | README 的「21 个用例」「已完成动作不重复执行」「可被任意 MCP 客户端消费」三处表述仍与实现不符（对应 P1-11），本轮刻意未改，保持 diff 聚焦。**后已于附四补做** |
| 附带发现（未修） | ① `metrics.py` 本地实测吞吐 **7.51 tasks/s**（m=10/并发4），与 README 的 **40.3** 差距悬殊，再次印证该数字不可外推；② `demo_cli.py --offline` 的「离线」只覆盖模型不覆盖工具 |

---

# 附二：附带问题 + P1-1 / P1-2 修复记录（2026-09-19 第二轮）

## E. 两个附带问题：同一个根因

**根因（已实测确认）：离线/基准脚本只冻结了「模型层」，没有冻结「工具层」。**

`FakeScriptedLLM` 只保证 LLM 不出网；而 `build_default_registry(settings)` 用的是
`settings.search_provider`（`.env` 里是 `bing`），于是这些声称"离线"的脚本照样访问
`cn.bing.com`。同一个根因有三个表现：

| # | 表现 | 影响 |
|---|---|---|
| 1 | `demo_cli.py --offline` 声称离线，实际 web_search 真出网 | "离线演示"名不副实；CI 会隐性依赖外网 + 必应页面结构 |
| 2 | `metrics.py` 吞吐被网络 RTT 主导 | **同一命令在不同 `.env` 下差 5.9 倍**（见下表），README 的 40.3 与本地 7.51 的分歧由此而来 |
| 3 | `demo_crash_recovery.py` docstring 写"全部离线，无外部依赖"，`ENV` 却没有 `SEARCH_PROVIDER` | 同根因的第 3 处（本轮自查发现，一并修） |

**实测证据（m=10，同一台机器，仅改 `SEARCH_PROVIDER`）**：

| 条件 | 吞吐 | 墙钟 |
|---|---|---|
| `SEARCH_PROVIDER=bing`（.env 原值） | **7.47 tasks/s** | 1.34 s |
| `SEARCH_PROVIDER=mock` | **43.72 tasks/s** | 0.23 s |
| `mock` + m=20 | 46.01 tasks/s | 0.43 s |

README 记的是「20 个双步任务、墙钟 0.5s」= 40.3 tasks/s —— 与 mock 条件吻合。
**结论：README 的数字本身没错，错在脚本没有把协议固化下来，导致数字随 `.env` 漂移。**

**修复内容**：

| 文件 | 改动 |
|---|---|
| `scripts/demo_cli.py` | `--offline` 时 `settings.model_copy(update={"search_provider": "mock"})`；新增执行环境横幅（离线/在线 + 实际模型与搜索源）；docstring 与 `--help` 明确"模型与工具都不出网" |
| `scripts/metrics.py` | 冻结测量协议为 `search_provider="mock"` 并贯穿三个指标函数（原先是各自 `get_settings()`，协议散落）；新增协议行自述（模型/搜索/沙箱/python/平台）；显式声明"抽样吞吐未启用 checkpointer，不代表落盘后吞吐" |
| `scripts/demo_crash_recovery.py` | `ENV` 补 `SEARCH_PROVIDER: "mock"`，docstring 说明为何必须钉住 |
| `.github/workflows/ci.yml` | smoke job 三步均**注入敌对值 `SEARCH_PROVIDER=bing`**，再断言脚本自报 mock —— 把这条约束变成对抗性回归护栏，防止下次再漂 |

**验证方式（对抗性）**：故意把 `SEARCH_PROVIDER=bing` 塞进环境。

```
✅ demo_cli.py --offline    自报「离线（假模型 + mock 搜索，不出网）」+ 状态 done（3.2s）
✅ metrics.py               47.52 tasks/s（原 7.47）+ 协议行「搜索=mock」（2.4s）
✅ demo_crash_recovery.py   status=done（7.1s）
✅ ci.yml                   YAML 可解析；3 个门禁步骤均带敌对注入；本机复刻 grep 全命中
```

**副作用检查**：`ruff`(E9,F63,F7,F82) clean · `compileall` exit 0 · pytest 结果与修复前一致（当时基线 25 passed / 6 skipped）。
唯一行为变化是 `--offline` 语义变严（现在真的不出网）；需要"假模型 + 真实搜索"的组合已不再提供——这是一个刻意的取舍，已写进 docstring。

---

## F. P1-1 工具执行幂等（checkpoint 重跑不重复执行）

**为什么需要**：checkpoint 落在 superstep 边界。若进程在 `tool_executor` **执行中**被杀
（工具已产生副作用、节点输出尚未提交），恢复会从上一个 checkpoint 重跑该节点 ——
同一次工具调用被执行第二次。旧 README 的"已完成动作不重复执行"并不成立。

**改动范围（10 个文件）**：

| 文件 | 改动 |
|---|---|
| `app/storage/models.py` | 新增 `ToolExecution` 表，`UniqueConstraint(task_id, call_id)` 作为幂等键 |
| `app/storage/repository.py` | `get_tool_execution` / `record_tool_execution`（`IntegrityError` → 回滚，**以首次为准**，重复记录不报错） |
| `app/graph/engine.py` | 新增 `ToolJournal` Protocol + `journal` 注入参数。**刻意用 Protocol**：引擎与 storage 层保持解耦，测试可注入内存实现 |
| `app/graph/nodes.py` | `tool_executor_node`：执行前查表 → 命中则发 `tool_replay` 事件并直接回放；未命中则执行、落流水再返回 |
| `app/runtime.py` / `app/main.py` / `app/worker/celery_app.py` | 接线：`EngineHolder`、`build_engine*`、Celery 任务路径全部传 `journal=repo` |
| `app/tools/subagent.py` | **显式不注入**并写明理由：子 Agent 用 MemorySaver、无跨进程恢复语义，记流水只会堆无人回放的行；整个 subagent 调用已由父侧 call_id 覆盖 |
| `tests/conftest.py` | `make_engine` 增加 `journal` 参数（默认 None → 旧行为完全不变） |
| `web/index.html` · `scripts/demo_cli.py` | `tool_replay` 事件配色（轨迹时间线可见） |

**关键实现细节**：把执行逻辑抽成 `_execute_call`，使**执行与回放两条路径共用同一套后处理**——
否则回放会绕过 `key_outputs` 提取与子 Agent 预算上卷，形成"恢复后关键数据丢失 / 预算少算"的隐蔽缺陷。

**验证方式**：原有 `test_checkpoint_resume` 把断点设在 `tool_executor` **之前**（工具压根没执行过），
它证明不了幂等。新建 `tests/test_tool_journal.py` 构造**真实崩溃窗口**：

```
工具 A 执行完 → 流水已提交
工具 B 抛 BaseException → 节点未返回 → checkpoint 未提交
恢复 → tool_executor 整节点重跑
```

（`SimulatedCrash` 刻意继承 `BaseException`：`registry.execute` 用 `except Exception` 折叠工具异常，
用 Exception 子类会被吞掉、节点正常返回、checkpoint 照常提交 —— 那就构造不出要测的窗口。
另用 `max_concurrent_tools=1` 让两个调用串行，消除 `gather` 竞态。）

| 用例 | 断言 | 结果 |
|---|---|---|
| `test_replay_skips_already_executed_tool` | A 执行 **1** 次（回放）、B 重试 **2** 次、`tool_replay` 事件存在、回放结果真的进了模型可见的 tool 消息 | ✅ |
| `test_without_journal_replays_are_re_executed`（对照组） | 不注入 journal 时 A 执行 **2** 次 → 证明去重来自本机制 | ✅ |
| `test_repository_journal_end_to_end` | 真实 `Repository` + SQLite checkpoint 端到端；幂等契约三态（重复记录取首次 / 未命中返回 None / 失败结果原样回放） | ✅ |
| `test_engine_wired_with_tool_journal`（test_api） | 主应用 lifespan 真的把 `repo` 注入引擎 —— 否则只是"库里有能力、线上没接上" | ✅ |
| 迁移路径 | `create_all` 在**已存在的** `data/agent.db` 上补建 `tool_executions`（含唯一约束）成功，重复 `create_tables` 幂等 | ✅ |

**能力边界（据实披露）**：只覆盖「工具已返回、流水已提交，但 checkpoint 未提交」这一窗口。
工具执行**中途**崩溃（流水尚未写入）无法去重 —— 那需要工具侧提供幂等键，属远程服务责任。
因此准确表述是「**在流水已提交的前提下**，恢复不会重复执行已完成调用」，而非"绝对不重复执行"。

---

## G. P1-2 自愈配额按调用计 + 并发安全

**两个缺陷**（同一处代码）：

1. **配额是任务级共享池**：`while selfheal_total < max_selfheal_retries` 用的是任务累计值。
   一个工具耗尽 3 次后，**后续其他工具的校验失败直接判 `plan_defect`，拿不到任何修复机会**。
2. **并发不安全**：该计数器在 `asyncio.gather` 的多个协程间共享读写，跨 `await` 的
   「读-判-加」使并行调用互相挤占 —— 3 次配额被 3 个并行调用瓜分，合计最多只能修 3 次。

**改动**：`_execute_call` 改为**协程内局部计数**，签名由 `(state, call, name, args, selfheal_total)`
变为 `(state, call, name, args) -> (obs, attempts, repair_tokens)`；节点侧只累计本轮增量
（`selfheal_used`，单条 `+=` 语句内无 await，asyncio 单线程下天然安全），写回 state 时
`state["selfheal_total"] + selfheal_used` —— 任务级累计量仍如实汇总，`/api/metrics` 与任务详情不受影响。
自愈事件补 `call_id` 字段，便于按调用追溯。

**验证方式**：把"配额按调用计"变成**可测量的关系**，而不是靠读代码推断。

| 用例 | 断言 | 结果 |
|---|---|---|
| `test_selfheal_quota_is_per_call_not_per_task` | 连续两轮非法参数各拿满 3 次 → 共 **6** 条 `tool_validation_failed`；attempt 序列恰为 `[1,2,3,1,2,3]`；`selfheal_total == 6` | ✅ |
| `test_selfheal_quota_is_concurrency_safe` | 一轮内 3 个并行非法调用 → 共 **9** 条；按 `call_id` 分组恰好 3 组、每组 3 次 | ✅ |
| `test_selfheal_events_scale_as_quota_times_calls`（参数化 1/2/3） | 事件数 == 调用数(2) × 单调用配额 → **2 / 4 / 6** | ✅ |

**判别性说明（为什么这些断言能证明旧实现是错的）**：
- 任务级共享计数器**在数学上不可能**产出 9 条事件（上界就是 `max_selfheal_retries`）；
- 参数化用例把 `事件数 = 调用数 × 配额` 这个等式钉死，而共享配额只能产出与调用数无关的固定值。
- 原有 `test_selfheal_gives_up_after_max_retries`（断言 3 条）与 `test_selfheal_repairs_invalid_args`
  （断言 `selfheal_total == 1`）在改动后**仍然通过**，说明单调用场景语义未变。

**需要披露的权衡**：最坏情况下自愈产生的 LLM 调用数从 `O(1)` 升到 `O(并行调用数 × max_selfheal_retries)`
（3 并行 × 3 次 = 9 次额外调用）。总轮次仍受 `max_steps` / `iterations` 约束，不会无限增长，
但预算敏感场景需要调小 `max_selfheal_retries`。

---

## H. 本轮验证总账

| 项 | 结果 |
|---|---|
| 测试套件 | **34 passed, 6 skipped**（本轮 25 → 34，新增 3 条 journal + 5 条 selfheal + 1 条装配检查） |
| 静态检查 | `ruff --select E9,F63,F7,F82` All checks passed；`compileall` exit 0 |
| CI 步骤本机复刻 | **10/10 通过**（static 4 + test 1 + integration 1 + smoke 4） |
| 对抗性验证 | 注入 `SEARCH_PROVIDER=bing` → 三个离线脚本均自报 mock；注入 P0-1 缺陷 → ruff 报 F821 exit 1 |
| 迁移路径 | 已有库补建 `tool_executions` 成功且重复执行幂等 |
| 附带问题副作用 | pytest / lint / compile 与修复前基线一致，无新增失败 |

**未验证项**：

| 项 | 状态 |
|---|---|
| CI 仍未在 GitHub 上真实运行过 | 本轮仍只做本机复刻；`integration` job（Docker/PG 6 条）首跑结果待确认 |
| P1-1 幂等表在 PostgreSQL 上未实测 | 用 SQLite 验证；`UniqueConstraint` 在 PG 语义相同，但无 PG 环境实测 |
| `metrics.py` 抽样吞吐仍未启用 checkpointer | 属 P1-5（口径修正），本轮未做；已在脚本输出中显式声明 |
| README 三处表述 | **已于附四修正（P1-11 完成）**。P1-1 完成后「已完成动作不重复执行」已条件成立，改写为"流水已提交前提下不重复执行"并给出能力边界 |

**未阻塞后续任务**：P1-3（critic 结构化错误分类）、P1-4（退避重试）、P1-5（指标口径）均可独立推进；
其中 P1-4 与本轮 P1-2 有耦合——退避重试若按 `call_id` 计数，可直接复用本轮引入的 `call_id` 字段。

---

# 附三：P1-4 瞬时错误退避重试（2026-09-19 第三轮）

## I. 缺陷

critic 判 `retryable` 后只回 `compressor → react_step`，由模型重新决策。缺三样东西：

1. **没有原样重试**：一次瞬时超时不会被自动重试，而是把决策成本推给模型；
2. **没有退避**：若模型立刻重发同一调用，等于在故障服务上连续打点；
3. **没有重试上限**：唯一的兜底是 `iterations >= max_steps`，同一调用可能被反复重试。

## J. 三个设计判断（这轮的核心不在代码量，在这三处取舍）

**判断 1：重试放在工具层，不在图拓扑上加重试节点。**
在图里表达"重试"需要保留 `pending_tool_calls` 并新增节点/条件边，改动状态机拓扑与 checkpoint 语义；
而「原样重试同一调用」本质是**调用级**行为。放 `_execute_call` 里最小侵入，且天然复用既有机制：
重试发生在**落流水之前**（P1-1 的 journal 只记录最终结果）、计数是**协程局部**（P1-2 的按调用配额）。

**判断 2：超时不等于失败 —— 新增 `ToolSpec.retry_transient`，默认关闭。**
一个超时的工具**可能已经产生了副作用**，盲目重试会把它做第二遍。所以只有只读/天然幂等的工具开：

| 工具 | retry_transient | 理由 |
|---|---|---|
| `web_search` | ✅ | 只读检索 |
| `get_weather` | ✅ | 只读查询 |
| `db_query` | ✅ | 连接以 `mode=ro` 打开，只读 |
| `file_ops` | ❌ | 有 `write`，重试可能重复写 |
| `code_run` | ❌ | 30s 量级操作，重试代价高且语义不明（沙箱内产物不可回收） |
| `subagent` | ❌ | 重试等于重跑整棵子执行树，代价远超收益 |

**判断 3：判别与退避收口到单一模块，且刻意不扩充标记表。**
`looks_transient` / `TRANSIENT_MARKERS` / `backoff_delay` 集中到 `app/core/retry.py`，
critic 与自动重试**共用同一份**关键词表（原先是内联在 critic 里），消除两处漂移。
**未扩充**标记表（HTTP 5xx / 429 等仍不在内）——那属于错误分类工作，顺带扩充会改变 critic 的分类行为；
把它留给专门的结构化错误码改造，`retry.py` 就是那次替换的唯一落点。

**三重闸门**（缺一不可）：错误文本命中瞬时标记 → 工具声明 `retry_transient` → 任务未取消。
**升级阶梯**：自动重试用尽 → 观测值带 runtime 错误 → critic 判 `retryable` → `react_step` 交模型决策。
即：工具层只做**有限次盲重试**，不试图在那里"解决"问题。

## K. 改动范围（11 个文件）

| 文件 | 改动 |
|---|---|
| `app/core/retry.py` | **新增**：`TRANSIENT_MARKERS` / `looks_transient` / `backoff_delay`（指数 + 封顶，不做抖动并说明理由） |
| `app/config.py` | 三个配置：`retry_max_attempts=2` / `retry_base_delay_s=0.5` / `retry_max_delay_s=8.0`（`0` = 关闭自动重试） |
| `app/tools/registry.py` | `ToolSpec.retry_transient: bool = False`，注释写明默认关闭的理由 |
| `app/tools/web_search.py` · `weather.py` · `db_query.py` | 声明 `retry_transient=True` |
| `app/graph/nodes.py` | `_retry_transient()` + `_execute_call` 接入；critic 改用共用的 `looks_transient`；`import asyncio` 提到模块级 |
| `tests/test_retry_backoff.py` | **新增** 9 条用例（三层） |
| `web/index.html` · `scripts/demo_cli.py` | `tool_retry_scheduled` / `_success` / `_exhausted` 三个事件的配色 |
| `.env.example` | 三个重试配置项 |

**实现中自查修掉的一个缺陷**：`_retry_transient` 开头若直接 `registry.get(name)` 取 spec，
而错误恰好来自 `registry.get` 本身（"未知工具"）时会**二次抛错把节点打崩**。
改为先判瞬时性再查 spec，并对查表加保护——顺带也让"未知工具"这类高频错误不再白查一次。

## L. 验证方式与结果

**三层共 9 条新用例**（`tests/test_retry_backoff.py`，0.58s）：

| 层 | 用例 | 断言 | 结果 |
|---|---|---|---|
| 纯函数 | `test_looks_transient_only_matches_explicit_markers` | 命中超时/连接/网络/temporarily 且大小写不敏感；未知工具、路径越界、空串**不**命中 | ✅ |
| 纯函数 | `test_backoff_delay_is_exponential_and_capped` | 0.5→1→2→4→8 封顶不溢出；提前触顶；`attempt<1` 抛 `ValueError` | ✅ |
| 节点 | `test_transient_error_is_retried_and_recovers` | 调用 **2** 次、attempt=1 的 scheduled、成功事件、结果进模型可见 tool 消息 | ✅ |
| 节点 | `test_retry_budget_is_capped_and_reported` | 调用 **3** 次（1+2，不无限）、2 条 scheduled、1 条 exhausted(`attempts=2`)、终态 done | ✅ |
| 节点 | `test_backoff_actually_waits_exponentially` | 真实等待 ≥ 0.15s（base=0.05 → 0.05+0.10），证明**真的在睡**而非只发事件 | ✅ |
| 闸门 | `test_permanent_error_is_not_retried` | 永远抛非瞬时错 → 调用 **1** 次、无任何 retry 事件 | ✅ |
| 闸门 | `test_tool_without_optin_is_not_retried` | 抛超时错但未声明 opt-in → 调用 **1** 次（副作用保护） | ✅ |
| 闸门 | `test_retry_disabled_when_budget_zero` | `retry_max_attempts=0` → 调用 **1** 次、无事件（逃生舱） | ✅ |
| 闸门 | `test_retry_stops_immediately_when_canceled` | 首次调用时取消 → 调用 **1** 次且**无** scheduled 事件（取消判定在 sleep 之前）、终态 canceled | ✅ |

**生产默认配置下的端到端验证**（非测试覆写，用出厂默认 `base=0.5s`）：

```
✅ 真实注册表 flags：code_run=False · db_query=True · file_ops=False · get_weather=True · web_search=True
✅ 真实超时文案「工具 slow_probe 执行超时（>0.05s）」→ looks_transient=True
   （这一步很关键：若标记表与 registry 的错误文案脱钩，重试永远不会触发）
✅ 默认延迟下重试：事件 [(attempt=1, delay=0.5)]；工具调用 2 次；墙钟 0.52s；终态 done
```

**回归基线**：`pytest` **25 → 34 → 43 passed, 6 skipped**；`ruff`(E9/F63/F7/F82) clean；`compileall` 0；
CI 全部步骤本机复刻 **10/10**，对抗性 smoke 门禁全过。套件总耗时 13.75s → 12.59s（真实退避只加了 0.15s）。

## M. 已知限制与未验证项（本轮）

| 项 | 状态 |
|---|---|
| HTTP 5xx / 429 不在瞬时标记内 | **明确不在本轮范围**。当前自动重试只覆盖文案含"超时/连接/网络/temporarily"的错误；补充状态码判定属错误分类改造（P1-3），顺带扩充会改变 critic 行为 |
| 自愈循环内的"重跑时抛运行错"未接自动重试 | `_execute_call` 里自愈重跑抛 `ToolExecutionError` 时按 `error_type="validation"` 上报，**不走退避重试**。这是自愈与重试两套记账交界处的一个不一致点，**本轮刻意未顺手改**（避免把两条恢复路径的计数混在一起），列为独立小项 |
| 重试次数未进 state / Task 表 | 只有事件可观测（轨迹时间线可见），未做指标聚合；要进 `/api/metrics` 需加 state 字段 + 表列 |
| PostgreSQL 未实测 | 本轮不涉及存储变更，沿用上轮结论 |
| CI 仍未在 GitHub 真实运行过 | 仍是本机复刻；`integration` job 首跑待确认 |

**未阻塞后续任务**：P1-3（结构化错误分类）可直接替换 `app/core/retry.py` 的内部实现而**不动调用方**——
这正是本轮把标记表收口到单一模块的目的。P1-5（指标口径）与 P1-4 无耦合，可并行。

---

# 附四：P1-11 README 口径修正（公开仓库前置，2026-09-19 第四轮）

## N. 为什么要先修再公开

仓库改为**公开**后，招聘官会**同时**看到 `README.md` 与本审计报告。
README 若继续写「21 个用例」「已完成动作不重复执行」「可被任意 MCP 客户端消费」，
而本报告白纸黑字写明这三条不成立 —— 那不是"诚实记录"，是**自相矛盾**。
所以公开前必须先让 README 与实现对齐。

## O. 逐条修正

| 位置 | 原表述 | 现表述 | 依据 |
|---|---|---|---|
| 核心功能·中断恢复 | 已完成动作**不重复执行** | 工具执行流水以 `(task_id, call_id)` 为幂等键，重跑节点时**回放已提交结果而非再执行**；并写明覆盖窗口是"工具已返回、流水已提交，但 checkpoint 未提交" | P1-1 的实现边界（附二 F 节） |
| 核心功能·MCP | 可被**任意 MCP 客户端消费** | 降级为「**描述符格式兼容，非完整接入**」：只有 `tools/list` 形状，**无 MCP 服务端进程（无 stdio/SSE）、无 MCP 客户端** | `mcp_server.py` 至今不存在 |
| 核心功能·失败重试 | 参数自愈 + critic 三分类 | 补上**退避重试**（`retry_transient` 只读工具）与**自愈配额按单次调用计** | P1-2 / P1-4 |
| Function Calling / MCP | 合并成一行 | **拆成两行**：Function Calling 标为完整；MCP 单独成行并标注缺口 | 避免用一个词掩盖半成品 |
| 量化指标 | 吞吐 **40.3 tasks/s**，只写"20 个双步任务、并发 4、墙钟 0.5s" | **≈46 tasks/s**，并把**测量协议**写进表格口径栏；另加四条口径披露：① 协议已在脚本内冻结（`SEARCH_PROVIDER=bing` 时只有 7.47，差 5.9 倍）② 抽样吞吐**未启用 checkpointer**，不代表落盘吞吐 ③ 恢复率用的是**进程内 MemorySaver**，跨进程由 SQLite 用例覆盖 ④ 复测命令 | 附一节 1.5 的指标口径复核 |
| 演示脚本 | `demo_crash_recovery.py`（**无重复执行**） | 注明该演示断点设在工具执行**之前**，它验证的是"恢复续跑"，**不是幂等去重**；幂等去重由 `tests/test_tool_journal.py` 覆盖 | 附一节 5.2 |
| 测试 | **21 个用例，全部离线** | **49 个用例：43 条完全离线 + 6 条需 Docker/PG**，并补齐新增覆盖项 | 实测 `43 passed, 6 skipped` |
| 目录结构 | `tests/ 21 个离线测试` | 49 个（43 离线 + 6 需 Docker/PG）；补 `core/retry.py`、工具执行流水表、`docs/`、`.github/workflows/` | 同上 |
| 模块对照 | M1–M6「**全部完成**」 | 改为「M1–M6 已落地」+ 列明在 M1–M6 之上补的三层负面路径能力，并**链接本审计文档** | 让两份文档互相印证而非互相打脸 |
| 新增 | — | **CI 徽章**（`actions/workflows/ci.yml`）+ **质量门禁小节**（四道 job 的内容，含对抗性 `SEARCH_PROVIDER=bing` 注入） | 公开仓库需要可验证信号 |
| 新增 | — | 面试深挖点从 5 条扩到 9 条，补：恢复到底保证什么（at-least-once vs at-most-once）、重试为何按工具声明开关、自愈与重试的分工、上下文压缩踩过的 O(n²) 坑 | 这四条都是本轮真踩出来的 |
| 隐私 | `docs/…md:42` 含 `C:\Users\<用户名>\anaconda3\envs\agent-runtime\python.exe` | 改为 `conda env agent-runtime（Python 3.11.16）` | 公开后不应暴露本机用户名 |

## P. 公开发布前的泄露扫描（63 个已跟踪文件）

| 检查项 | 结果 |
|---|---|
| 密钥前缀（`sk-` / `ghp_` / `gho_` / `github_pat_` / `AKIA` / `xox*`） | ✅ 0 处 |
| 私钥头（`BEGIN … PRIVATE KEY`） | ✅ 0 处 |
| 非空密钥赋值 / 硬编码 Bearer | ✅ 0 处 |
| 手机号 / 邮箱 | ✅ 0 处 |
| 本机绝对路径 | 修正前 1 处 → **修正后 0 处** ✅ |
| 演示凭据（知情项） | `.env.example` 的 `sk-xxx` 是注释占位符；`docker-compose.yml` 的 `agent:agent` 是本地 compose 默认口令，公开属常规 |

## Q. 仍然存在、但已如实写进 README 的限制

- MCP 只有描述符格式（无服务端/客户端）
- 抽样吞吐未启用 checkpointer；恢复率口径为进程内 MemorySaver
- PostgreSQL 路径在 CI 上才首次真跑；本机无 Docker daemon，集成用例长期 skip
- `retry.py` 仍是字符串嗅探的过渡实现（HTTP 5xx / 429 等状态码仍未纳入），P1-3 负责替换
- 自愈循环内"重跑时抛运行错"未接退避重试

---

# 附五：测试套件封闭性缺陷——公开前复核时发现并修复（2026-09-19 第五轮）

## R. 发现过程

改完 README/文档后复跑全量测试，结果 **8 failed, 35 passed，耗时 88s**（此前 12.5s 全绿）。
文档改动不可能影响测试，所以先看失败详情——**测试输出里 `settings` 明晃晃写着 `search_provider='bing'`**：

```
RuntimeError: 必应搜索未解析到结果（页面结构可能已变更）
  app\tools\web_search.py:52
```

## S. 根因

`tests/conftest.py` 冻结了模型层（`LLM_MODEL` / `LLM_BASE_URL`）与沙箱/队列，**却没有冻结 `SEARCH_PROVIDER`**。
它回落到 `.env` 的 `bing`，于是所有用到 `web_search` 的用例都真的去访问 `cn.bing.com`：
必应当下返回了无法解析的页面 → `_parse_bing` 返回空列表 → 工具（按设计）明确抛错，而不是静默给空结果 →
8 个用例集体失败。

**这与我在脚本层修的是同一个根因**（"只冻结模型层、不冻结工具层"），只是测试套件里还留着一处，
前几轮的脚本修复没有覆盖到它。

## T. 必须写下的一处更正

此前几轮报告中"43 passed / 25 passed"的**绿色结果是部分靠运气**——那几次必应恰好可用。
这不是本轮新引入的回归，而是**一直存在的封闭性缺陷**，被外部服务的偶然可用性掩盖了。
把它记在这里，比事后让别人发现更好。

## U. 修复与验证

| 改动 | 内容 |
|---|---|
| `tests/conftest.py` | 增加 `os.environ["SEARCH_PROVIDER"] = "mock"`（无条件覆写，外部环境变量也压不过它）与退避延迟归零；文件头写明"测试必须封闭"这条铁律及本次事故 |
| `.github/workflows/ci.yml` | `test` job 增加**对抗性步骤**：注入 `SEARCH_PROVIDER=bing` 再跑一遍离线套件，必须同样全绿 |

**对抗性验证（三次运行，每次约 6.6s）**：

| 场景 | 结果 |
|---|---|
| 正常环境（不设该变量 → 会回落 `.env` 的 bing） | ✅ 43 passed, 6 skipped |
| 注入 `SEARCH_PROVIDER=bing` | ✅ 43 passed, 6 skipped |
| 注入 `SEARCH_PROVIDER=ddgs`（另一个会出网的 provider） | ✅ 43 passed, 6 skipped |

耗时也从 88s 回到 6.6s —— 那 80 秒全是等必应超时。

**为什么这条对公开仓库尤其重要**：招聘官或任何人 clone 下来跑 `python -m pytest tests/`，
结果必须只取决于代码，而不是取决于"此刻必应是否可解析"。

## V. 顺带的安全提醒

pytest 失败时会把 `settings` 整个 repr 出来，其中包含 `.env` 里的真实 `LLM_API_KEY`。
已确认该密钥**只存在于 `.env`**（`.gitignore` 第 3 行已排除），**未进入任何已跟踪文件**（附四 P 节的泄露扫描为 0 命中）。
但由此得出一条纪律：**不要把测试失败输出直接贴到公开场合**（Issue、聊天、截图）。

---

# 附六：首轮真实 CI 运行结果与修复（2026-09-19 第六轮）

## W. 首次真实运行：4 个 job 里 3 个通过

仓库 `Kevin-fang-23/llm-agent-runtime` 公开后第一次推送（HEAD `1c0f763`，run #1）触发：

| Job | 结果 | 用时 | 说明 |
|---|---|---|---|
| `static` 静态检查 | ✅ success | 35s | ruff F821 门禁 / compileall / 双入口导入冒烟 全过 |
| **`integration` Docker 沙箱 + PostgreSQL checkpoint** | ✅ **success** | 41s | **本机无 Docker daemon，这 6 条用例从项目诞生起一直是被 skip 的**；首次真跑即通过 |
| `smoke` 端到端冒烟 | ✅ success | 28s | CLI 全链路 / 崩溃恢复 / 指标门禁（恢复率与自愈率 100%）全过 |
| `test` 单元测试 | ❌ failure | 8s | 见下 |

**这条结论的价值**：沙箱隔离（无挂载执行 / 出网被拦 / uid=65534 / 超时被杀）与 PostgreSQL
checkpoint（asyncpg + psycopg 双通道、重连落盘）**从"代码里写了"变成"CI 可验证"**。
附一报告中把这两项列为"未验证项"，现在可以销账。

## X. `test` job 失败的根因（已复现、已修）

**现象**：仅该 job 失败、耗时 8 秒、失败集中在 `tests/test_api.py` 一组；本地 Windows 全绿。

**为什么本地绿、CI 红**——两层叠加：

1. `tests/conftest.py` 的 `settings` fixture 把 `TOOL_DB_PATH` / `WORKSPACE_DIR` /
   `CHECKPOINT_SQLITE_PATH` 三条路径重定向到临时目录，**却漏了 `DATABASE_URL`**，它仍指向 `./data/…`。
2. `app/main.py` 模块级调用的 `ensure_windows_selector_loop()` 写的是
   `if sys.platform == "win32" and get_settings()...` —— **在 Windows 上会短路求值并调用
   `get_settings()`，顺带创建出 `./data/`；在 Linux 上第一个条件即为假，`get_settings()` 根本不被调用**。
   于是 Windows 上 `./data/` 因这个副作用而存在，Linux 上不存在；而 CI 是干净检出、`data/` 又被
   `.gitignore` 排除 → SQLite 无处落库。

**复现**（把 `DATABASE_URL` 指向不存在的目录，复刻 CI 条件）：

```
FAILED tests/test_api.py::test_engine_wired_with_tool_journal
ERROR  tests/test_api.py::test_full_task_lifecycle / test_tools_manifest_is_mcp_shape
ERROR  tests/test_api.py::test_metrics_endpoint / test_validation_error
→ sqlite3.OperationalError: unable to open database file
→ app/main.py:34 lifespan → repo.create_tables()
1 failed, 38 passed, 4 errors
```

与 CI 现象完全吻合。`smoke` job 之所以过，是因为它先跑 `seed_demo_db.py`，那个脚本会
`Path(db_path).parent.mkdir(parents=True, exist_ok=True)` 创建出 `./data/`。

## Y. 修复（两处，都是真缺陷）

| 文件 | 改动 | 为什么这算真缺陷而非"让测试变绿" |
|---|---|---|
| `tests/conftest.py` | fixture 内追加 `DATABASE_URL` 重定向（用 `as_posix()`，URL 里不能出现 Windows 反斜杠） | 符合 fixture 既有设计意图：测试应完全自包含，不该隐式依赖"工作目录下恰好有 `data/`" |
| `app/main.py` | lifespan 退出时 `await engine.dispose()`；原先 `_, session_factory = make_engine_and_session(...)` 把引擎丢弃 | **真实的连接池/文件句柄泄漏**：每次 lifespan 都漏一个连接池，一直占着 SQLite 文件句柄。Linux 上因为"删除已打开的文件不报错"而被长期掩盖，Windows 上表现为临时目录无法清理 |

> 第二处是被第一处**顺手暴露**出来的：把库文件移进每测试独立的临时目录后，fixture 拆除时
> `TemporaryDirectory.cleanup()` 报 `PermissionError: [WinError 32] 另一个程序正在使用此文件` ——
> 顺着回溯才发现引擎从未被 dispose。

## Z. 验证

| 场景 | 结果 |
|---|---|
| 条件1 正常（`data/` 存在） | ✅ 43 passed |
| 条件2 CI 同款：`DATABASE_URL` 指向不存在的目录 | ✅ 43 passed |
| 条件3 敌意：条件2 + `SEARCH_PROVIDER=bing` | ✅ 43 passed |
| 全量套件（含 Docker/PG 两组，本机应 skip 6 条） | ✅ 43 passed, 6 skipped |

耗时从修复前的 88s / 10.5s 回落到 ~5s。

**仍未验证**：这两处修复尚未在真实 runner 上跑过（需再推一次）。`integration` job 已在真机上
通过一次，但它无法覆盖本次改动，仍需重跑确认全绿。

---

# 附七：P1-3 结构化错误码 + P1-5 指标口径修正（2026-09-19 第七轮）

## AA. 前置：CI run #2 全绿

第二轮推送（HEAD `223b539`）四个 job 全部 success：静态检查 26s / 单元测试 27s（含注入
`SEARCH_PROVIDER=bing` 的对抗性步骤）/ 端到端冒烟 27s / Docker 沙箱 + PostgreSQL 集成 38s。
CI 门禁由此成立，P1-3/P1-5 才动手。

## AB. P1-3：错误分类从"猜文本"改为"读错误码"

**问题**：原先用中文关键词嗅探（`"超时" in errors`）。两个硬伤：
1. 改一句文案就失效；
2. **`httpx` 的超时文案是 `timed out`，与标记 `timeout` 并不匹配** —— 这整类超时错误
   被静默判定为"不可重试"，而它恰恰是最典型的瞬时故障。

**改动**：

| 文件 | 内容 |
|---|---|
| `app/core/errors.py`（**新增**） | `ToolErrorCode`（StrEnum，10 个码）+ `RETRYABLE_BY_CODE` 可重试性表 + `code_from_http_status` + `UpstreamHTTPError` + `parse_retry_after` |
| `app/tools/registry.py` | `ToolExecutionError(message, code=, retryable=, retry_after_s=)`；**异常类型 → 错误码的集中映射**（PermissionError→PERMISSION、FileNotFoundError→NOT_FOUND、ConnectionError→NETWORK、TimeoutError→TIMEOUT、ValueError→INVALID_ARGS）；`UpstreamHTTPError` 折算成码并透传 Retry-After |
| `app/core/retry.py` | `is_transient_error`（解析顺序：显式 retryable → 结构化 code → 文本兜底）、`retry_delay_hint`（Retry-After 优先） |
| `app/graph/nodes.py` | 观测值 / `tool_error` 事件 / 模型可见的工具消息**都带 `error_code`**；`_classify_failure` 按码分流；重试延迟优先取 Retry-After |
| `app/tools/web_search.py` · `weather.py` | 上游 HTTP 状态码与 httpx 超时/连接异常结构化成码 |
| `app/tools/subagent.py` | 显式 `retryable=False`：重试一次等于重跑整棵子执行树 |

**兼容策略（不是破坏性迁移）**：`code` 默认 `None`，此时退回原文本启发式。
所以未标注错误码的抛错点行为**完全不变**；标注了的则精确判定。

**迁移中踩到并修掉的两个坑（都值得写下来）**：

1. **`class X(str, Enum)` 的隐蔽陷阱**：Python 3.11 下 `str(member)` 得到
   `"ToolErrorCode.TIMEOUT"`，且枚举的 `__hash__` 基于**成员名**而非值。观测值会随 checkpoint
   持久化，反序列化后退回**普通字符串**——此时 `codes & {枚举成员}` 的集合交集会**静默失配**，
   critic 会悄悄退化成文本兜底（不报错、不崩溃，只是判定变糊）。
   处置：改用 `StrEnum`（`str(x)` 即值、哈希与字符串一致），并让观测值**一律存字符串**。
2. **我漏删了一行旧 `return None, last_err`**，被 CI 的 `ruff --select E9,F63,F7,F82` 里的
   **F821 未定义名**当场拦下 —— 这条门禁的价值第二次被验证（上一次是它拦住了缺 import 的 P0）。

**验证**：新增 `tests/test_error_codes.py` **59 条**（参数化后），四层覆盖：
① 枚举完整性**全枚举**（含"每个码都必须有 critic 判定"，禁抽样）、HTTP 状态码 12 组表驱动、
Retry-After 解析 8 组；② 判定顺序 11 组（含"显式 retryable 双向覆盖结构化默认值"）+ 文本兜底 7 组；
③ 真实 registry 折叠各类异常（含超时、5xx/429/404/401、未知工具）；④ 端到端：错误码进事件与工具消息、
PERMISSION→fatal 终止、Retry-After 压过 base=5s 的退避（实测 elapsed < 1s）。

全量：43 → **102 passed, 6 skipped**；敌对条件（`DATABASE_URL` 指向不存在目录 + `SEARCH_PROVIDER=bing`）同样 102 passed。

## AC. P1-5：指标口径修正 —— 让数字测的是它声称的东西

**问题**：三项指标都"虚高"，因为它们绕过了被声称要测的对象：

| 指标 | 旧口径的问题 |
|---|---|
| 断点恢复成功率 | 用**进程内 MemorySaver**，两个引擎共享同一个内存对象 —— 完全没有覆盖"状态落盘" |
| 并发吞吐 | `make_engine` 不传 saver → `checkpointer=None` → **完全绕过写盘** |
| 崩溃恢复 | 只有 `demo_crash_recovery.py`，而它是"跑到断点后**干净退出**"，不给 flush 压力 |

**改动**：

| 指标 | 新口径 |
|---|---|
| 断点恢复成功率 | 每个样本独立 SQLite 文件；打断后**关闭连接**，再用**全新连接**恢复至完成 |
| **崩溃恢复成功率（新增）** | 子进程进入工具执行后长睡并打标记，父进程**硬杀**它（POSIX=SIGKILL / Windows=TerminateProcess），再用同一 checkpoint 换进程恢复至完成 |
| 并发吞吐 | **开启 SQLite checkpoint**，共享一个 saver（与生产 `EngineHolder` 同形态） |

**实测（协议：模型=假模型 搜索=mock checkpointer=sqlite / Python 3.11 / Windows）**：

| 指标 | 数值 |
|---|---|
| 断点恢复成功率（磁盘 checkpoint，换连接） | **100%** (n=10) |
| 崩溃恢复成功率（工具执行中被硬杀） | **100%** (n=5) |
| 自愈挽救率 | **100%** (n=10) |
| 并发吞吐（含 SQLite checkpoint 落盘） | **≈21 tasks/s** (m=20, 并发 4, 墙钟 0.94s) |

**最重要的结论**：吞吐从「46（绕过落盘）」降到「21（含落盘）」——
**旧数字不是"更快"，而是测了别的东西**。SQLite 会把并发写串行化，这是端到端真实瓶颈；
把瓶颈如实测出来，才是指标的意义所在。README 已同步，并明确标注两个数字**不可直接比较**。

**同步改动**：CI 指标门禁从 2 项扩到**四项 + 协议断言**（`搜索=mock`、`checkpointer=sqlite`）；
grep 由固定空格数改为 `.*100%`，避免标签格式变化成为脆点。

## AD. 本轮总账与未验证项

| 项 | 结果 |
|---|---|
| 测试套件 | **102 passed, 6 skipped**（43 → 102，新增 59 条错误码用例） |
| 静态检查 | `ruff`(E9/F63/F7/F82) clean（并当场拦下我的一处 F821 残码） |
| CI 步骤本机复刻 | **11/11 通过**（含四项指标门禁） |
| 敌对条件 | 无 `data/` 目录 + `SEARCH_PROVIDER=bing` → 102 passed |

**未验证**：本轮改动**尚未在真实 runner 上跑过**，需再推一次触发 CI #3。
`integration` job 会重跑（Docker 沙箱 + PostgreSQL），预计仍应通过。

---

# 附八：CI #3 核验 + 指标门禁稳定性事故 + P1-6 / P1-7（2026-09-19 第八轮）

## AE. CI #3 全绿

run #3（HEAD `2c0f2f1`）四个 job 全部 success：静态检查 28s / 单元测试 26s（含注入 `bing`
的对抗性步骤）/ 冒烟 28s / 集成 42s。其中「指标门禁（四项指标 + 协议锁定）」step **success**。

## AF. 门禁稳定性事故：崩溃恢复指标**曾经是假的稳定**

按用户要求对四项指标做稳定性抽样，结果发现问题：

| 指标 | 6 次连跑序列 |
|---|---|
| 断点恢复成功率 | 100% × 6 ✅ |
| **崩溃恢复成功率** | 100, 100, **33**, 100, **67**, 100 ❌ |
| 自愈挽救率 | 100% × 6 ✅ |
| 并发吞吐 | 21.1 ~ 23.7（无阈值，只观测波动） |

CI #3 恰好抽到 100% 通过 —— **一个不稳定门禁被一次绿灯掩盖了**。

**根因（两层，都已修）**：

1. **统计口径 bug**：子进程若没跑到工具，我 `continue` 跳过时**只漏掉了分子、仍用 `k` 做分母**，
   把"harness 失败"静默算成"恢复失败"；同时子进程 stdout/stderr 被丢进 `DEVNULL`，等于销毁证据。
2. **更深的一层：checkpoint 的持久性只在 superstep 边界成立**。放大到 12 次硬杀后，
   `next` 分布为 `tool_executor` 7 / `__start__` 5 —— 也就是**工具明明跑过了，checkpoint 却丢回初始状态**。
   进一步定位到 LangGraph 1.x 的 **`durability` 档位**：默认 `async` 只是把写盘**排队**，
   进程被硬杀时队列里的 checkpoint 会丢。设 `sync` 后仍只是缓解（2/12 仍丢），
   说明"在节点内部任意时刻硬杀"这个测法本身不可控。

**修复（一次性，不补丁叠加）**：

| 改动 | 内容 |
|---|---|
| `app/config.py` | 新增 `checkpoint_durability`（**默认 `sync`**）。<br>⚠️ 这是**生产级缺陷**：项目主打"崩了能恢复"，而默认 `async` 档位下硬杀会丢 checkpoint，承诺实际不成立。sync 的代价是每个 superstep 多等一次写盘。 |
| `app/graph/engine.py` | `_config()` 在 `saver is not None` 时带上 `durability` |
| `scripts/metrics.py` | ① 硬杀点固定在 **checkpoint 断点**（`interrupt_before=["tool_executor"]`，checkpoint 已提交后再真杀进程）；② 区分「harness 无效样本」与「恢复失败」，无效样本不计入分母并**显式告警**；③ 子进程输出落盘而非丢弃 |

**复验**：12 次硬杀 → `next=['tool_executor']` **12/12**、恢复到 done **12/12**；
6 次门禁连跑 → 三项指标**全部 100% × 6**，吞吐 18.6 ~ 22.6 tasks/s。门禁不再抖动。

> 一句实话：这一轮最值钱的不是"指标变好看了"，而是**发现默认档位下崩溃恢复的承诺不成立**。
> 若不是用户要求做稳定性抽样，它会以"CI 是绿的"一直潜伏下去。

## AG. P1-6：MCP 服务端落地

新增 `app/mcp_server.py`（stdio 传输，基于官方 `mcp` SDK）：`tools/list` + `tools/call`。
依赖新增 `mcp>=2.0`。

设计要点：
- **工具的 `inputSchema` 直接沿用 registry 的 JSON Schema**，不另写一份 —— 只有一份事实来源。
  （踩到并修掉：SDK 是**从函数签名重新生成** schema 的，只给裸类型会把 `description` 与
  `minimum/maximum` 全丢掉；改用 `Annotated[T, Field(...)]` 才带过去。测试用**语义字段归一化比较**
  做护栏。）
- `tools/call` 走 `registry.execute()`：沙箱隔离、JSON Schema 校验、结构化错误码全部复用。
- 执行期失败**直接返回 `CallToolResult(is_error=True)`** 而不是抛异常 —— 抛异常会被 SDK 包成
  `"Error executing tool X"`，把错误码吞掉。
- 明确分层：SDK 会先按 inputSchema 做**协议层**类型校验，类型错走不到业务层。

测试 `tests/test_mcp_server.py` **5 条**，含**真实子进程 + 真实 MCP 客户端 stdio 握手**
（initialize → list_tools → 调用成功 → 越界调用返回 isError 且错误码可见）。

## AH. P1-7：轨迹可视化升级

| 改动 | 内容 |
|---|---|
| `app/api/routes_tasks.py` | 新增 `GET /tasks/{id}/stream`（**SSE**）：推事件 + 任务快照（只在有变化时推），终态推 `stream_end` 后关闭；新增 `GET /tasks/{id}/export?format=json\|md` |
| `app/storage/models.py` | `Event` 增加 `(task_id, seq)` **复合索引**（增量推送走的是这个范围扫描）+ 索引随 `create_all` 补建（无 Alembic 时的迁移路径，已实测） |
| `web/index.html` | 轮询（1.5s）→ **EventSource**；事件参数/结果 `<details>` 可折叠；计划/降级/自愈 chip；token 与步数**双维进度条**；导出按钮 |
| `tests/conftest.py` | `client` 夹具提到 conftest（多模块共用），并补上 `journal=repo`，与生产接线一致 |

为什么 SSE 内部仍是"查库"：事件可能由**另一个进程**（Celery worker）产生，内存队列跨进程不可见，
业务库是跨进程唯一可见的事件源。

测试 `tests/test_trace_stream.py` **9 条**：SSE 事件序列与终止、data 段可解析、终态状态回传、
404、导出 JSON/Markdown 形状、未知 format 400、旧增量接口仍可用、复合索引存在。

## AI. 本轮总账

| 项 | 结果 |
|---|---|
| 测试套件 | **116 passed, 6 skipped**（107 → 116，新增 MCP 5 + 轨迹 9，剔除重复计数） |
| 静态检查 | `ruff`(E9/F63/F7/F82) clean（**第三次**拦下我的 F821：移夹具时漏改的引用） |
| 敌对条件 | 无 `data/` 目录 + `SEARCH_PROVIDER=bing` → 116 passed |
| 指标门禁 | 6 次连跑三项 100% + 12/12 硬杀恢复 |

**未验证**：P1-6/P1-7 尚未在真实 runner 跑过（需再推一次）。MCP 测试依赖新增的 `mcp` 包，
CI 会按 `requirements.txt` 安装 —— 若该包在 runner 上下载失败，`test` job 会红。

---

# 附九：P2-1 鉴权 + 多租户 + 限流 + 每租户配额（2026-09-20）

对应对账表 6.1「无任何鉴权 / 多租户 / 限流：`/api/tasks` 完全开放，任何人都能提交任务烧 token」——
这是审计点名的最大产品缺口，补完后「可托管」三个字才成立。

## AJ. 设计判断（五条）

1. **明文 key 不落库**：只存 SHA-256 哈希，明文仅在创建 / 轮换时返回一次（GitHub PAT 模式）；
   `key_prefix` 存前 12 位供管理页识别。用 SHA-256 而非 bcrypt/argon2 是刻意的：
   key 是 128 bit 随机数而非人类口令，没有弱熵可爆破，慢哈希只增加每请求验证延迟。
2. **401 / 403 / 404 三分**：401 = 缺 key 或 key 无效；403 = 已认证但租户被禁用或非管理员；
   跨租户读任务一律 **404** —— 与「任务不存在」同响应，不向其他租户泄漏任务存在性。
   租户与管理员两套凭据完全独立（租户 key 打不开管理端点，反之亦然），管理员比较用
   `secrets.compare_digest` 防时序侧信道。
3. **日级额度用事实来源聚合，不建计数器表**：每日提交数按 `tasks.created_at` 计数，
   每日 token 配额 = 已完成任务实耗 + **在途任务按 `max_tokens` 预占** + 本次请求需求。
   判定依据即事实来源，重启 / 多 worker / 重复提交都不会漂。预占是刻意保守：
   在途实耗要等完成才落库，只看实耗的话租户可以在配额耗尽前并发挤进任意多任务 ——
   资金护栏宁可少放行。这条直接复用 campus-assistant 限流计数器两次实测超发的教训
   （内存判定 + 异步记账超发 50%；先读后写偶发超发 1 次）。分钟级突发控制仍用进程内
   滑动窗口（按实例算，多 worker 时额度 = 单实例 × worker 数，显式披露）。
4. **零配置引导**：`AUTH_ENABLED` 默认开启（默认安全，与 `ALLOW_UNSAFE_LOCAL_EXEC` 的教训一致）；
   `ADMIN_API_KEY` 留空则首次启动生成，与 `default` 租户 key 一起写入
   `data/api_credentials.json`（env > 文件 > 现场生成；文件 key 与库中哈希失配时轮换并回写）。
   本地开发零配置可用，生产用 env 注入。
5. **key 传递通道最小化**：`X-API-Key` → `Authorization: Bearer` → `?api_key=` 查询串仅对
   `/stream` 端点放开（EventSource 无法设置请求头），key 进 URL 有被代理 / 访问日志记录的
   风险，只对必须的端点开放。

## AK. 改动范围（13 个文件）

| 文件 | 改动 |
|---|---|
| `app/storage/models.py` | `Tenant` 表（api_key_hash 唯一索引）；`Task.tenant_id` + `(tenant_id, created_at)` 复合索引；`migrate_schema`：create_all 不给已存在的表补列，旧库按方言探测后 `ALTER TABLE` 补列 + `CREATE INDEX IF NOT EXISTS`（幂等，历史任务归属为 ""） |
| `app/storage/repository.py` | 租户 CRUD；`get_task/list_tasks/metrics/create_task` 带 `tenant_id` 过滤（None = 内部调用不过滤，API 层永远传租户 id）；`count_tasks_since` / `tenant_token_usage`（实耗 + 在途预占聚合） |
| `app/api/security.py`（新） | 哈希 / 生成 / `TenantRegistry`（正负缓存 + 管理变更失效）/ `require_tenant` / `require_admin` / `bootstrap_auth` |
| `app/api/ratelimit.py`（新） | `SlidingWindowLimiter`（无 await 原子性说明）+ `PerIpRateLimitMiddleware`（纯 ASGI，挂在鉴权**之前**，撞库请求同样计数）+ 日界时间辅助 |
| `app/api/routes_admin.py`（新） | 租户创建 / 列表 / PATCH / 轮换 / 用量 / 全局指标，全部 `require_admin` 守卫 |
| `app/api/routes_tasks.py` | 全部端点接 `require_tenant`；`create_task` 前置 `_enforce_submit_limits`（L2/L2b/L3/L4 四层，便宜的内存判定在前、DB 聚合在后，429 带 `Retry-After`） |
| `app/main.py` | lifespan 装配 registry / limiter / 引导；挂管理路由与 L1 中间件 |
| `app/config.py` | 鉴权 5 项 + 限流 4 项 + 停机排空 1 项（见 AL） |
| `app/worker/local_queue.py` | **stop() 先排空后取消**（AL 节，本轮最值钱的修复）；resume 任务纳入 `_running` 跟踪 |
| `web/index.html` | 右上角 API Key 输入框（localStorage 持久化）；全部请求带 `X-API-Key`；SSE 走 `?api_key=`；401/403/429 的可读报错 |
| `tests/conftest.py` | `TenantClient` 薄包装（自动带租户 key，存量 30+ 处 API 调用零改动）；`ADMIN_API_KEY` / `CREDENTIALS_FILE` 测试隔离 |
| `tests/test_auth_multitenant.py`（新） | 20 条用例：认证 401/403、Bearer 与查询串提取、禁用即时生效、密钥不泄漏、租户隔离、历史任务不可见、租户级指标、L1–L4 各层（含 Retry-After）、配额预占口径、零配置引导（文件生成 / 丢失后轮换）、旧库迁移幂等、key 轮换 |
| `.env.example` / `.github/workflows/ci.yml` / `README.md` | 配置段与用例数同步 |

## AL. 附带发现并修复的真缺陷：优雅停机必须「先排空、后取消」

新增测试全绿后，全量套件在 Windows 上**随机**（约 30–50% 概率）出现一个夹具 teardown 错误：
临时目录清理报 `WinError 32`（agent.db 被占用）。取证过程：

1. **隔离**：进程内复现器三变量对照 —— 空启动不泄漏；仅建租户不泄漏；**提交任务必泄漏**。
2. **二分**：桩引擎（无 LangGraph、无事件、无 LLM、只有队列读写库）同样泄漏 → 排除
   我的配额查询与引擎；关键对照：**提交后立即停机 100% 泄漏，等任务到终态再停机 0% 泄漏**。
3. **排除法**：泄漏时刻 Python 侧无任何打开的 sqlite3 连接（gc 全扫 + 先扫后 collect）、
   无存活线程（含 aiosqlite runner）、`asyncio.all_tasks` 无悬挂任务 —— 句柄在 C 层孤儿化。
4. **结论**：停机时任务被 cancel，`CancelledError` 恰好打断 aiosqlite 连接的关闭流程，
   SQLite 文件句柄从此无人能释放（进程存活期间永久占用）。老测试都等任务到终态才结束，
   所以从未暴露；「提交即退」的新测试放大了它。

**修法**：`queue.stop()` 从「发出 cancel 就返回」改为**先等在跑任务自然结束**
（`SHUTDOWN_DRAIN_TIMEOUT_S`，默认 10s，0 = 关闭排空），超时才取消兜底。
这本来就是优雅停机应有的语义：让任务在节点边界把状态落完，而不是半路掐断。
顺带修复两个同源小问题：resume 任务此前不被 `_running` 跟踪（停机时既不等也不取消）；
`stop()` 原先不等被取消任务结束就返回。

## AM. 验证总账

| 项 | 结果 |
|---|---|
| 测试套件 | **199 条**（193 离线 + 6 需 Docker/PG）；**5 连跑全绿 0 error**（修复前约 30–50% 概率 teardown error） |
| 静态检查 | `ruff`(E9/F63/F7/F82) clean —— 第四次拦下 F821（conftest 注解引用）；`compileall` exit 0 |
| 敌对条件 | `SEARCH_PROVIDER=bing` → 193 passed（套件封闭性不受鉴权改动影响） |
| smoke 三脚本 | demo_cli / demo_crash_recovery / metrics 在 `SEARCH_PROVIDER=bing` 下全过，指标 100% |
| 复现器回归 | 排空修复后，原 100% 泄漏的变体 30/30 全部 ok |

## AN. 已知限制与未验证项（本轮）

| 项 | 状态 |
|---|---|
| L1/L2 分钟级滑动窗口在进程内存 | 多 worker 时额度 = 单实例 × worker 数（与 campus-assistant L1 同一取舍，已在 docstring 披露）；日级额度不受此限 |
| `migrate_schema` 的 PostgreSQL 分支 | 本机无 PG 未实测（information_schema 探测 + `CREATE INDEX IF NOT EXISTS` 均为标准 SQL）；CI integration job 的 PG 用例若覆盖建表路径会真跑 |
| 管理端点无分页 / 租户名可重复 | 演示规模够用；`default` 引导按 name 定位，其他同名租户不影响引导 |
| 未提交 | 本轮改动留待用户审阅后提交 |

---

# 附十：P1-10 Celery + PG 路径实测（2026-09-20）

对应对账表 6：「Celery（零测试覆盖）」「PG 路径零实测」；以及 P1-10 的验收标准
「给 Celery 路径补 1 条集成测试 / Celery 任务端到端成功」。

## AO. 验证分层设计（由离线到真实，共 7 条新用例）

| 层 | 验证什么 | 依赖 | 落点 |
|----|----------|------|------|
| eager 任务体 | `run_task` / `resume_task` 的真实任务体：状态迁移、结果/错误写回 DB、真实 SQLite saver 建连与关闭、失败先落 DB 再向调度器抛出 | 无（离线） | `test_celery_path.py` |
| API 分发 | `QUEUE_MODE=celery` 时 API 走 `.delay()` 且不入本地队列（任务保持 queued） | 无（分发双打） | 同上 |
| 真实 broker 往返 | API → Redis → **独立 worker 子进程** → DB 写回 done（compose 生产形态，LLM 换脚本化假模型） | Redis | 同上 |
| Celery + PG | 任务体在 PG 业务库 + PG checkpoint 上跑通（worker 容器的存储形态） | PostgreSQL | 同上 |
| PG 迁移分支 | `migrate_schema` 在 PG 上按 information_schema 探测补 tenant_id 列（此前零实测），幂等 | PostgreSQL | `test_postgres_checkpoint.py` |

关键设计：worker 是独立进程，测试进程的 monkeypatch 够不着它 —— 新增
`tests/_celery_worker_entry.py` 子进程入口，在导入 celery app **之前**替换
`app.runtime.build_engine_with_saver`（`_execute` 是函数内延迟导入，会取到替换后的实现）；
工具层靠环境变量冻结。broker、DB 写回、saver 生命周期都是真的，唯独 LLM 冻结 ——
与测试封闭性铁律一致。pg_url / redis_url 夹具用 Docker SDK 拉一次性容器、无 Docker 自动
skip（沿用 postgres 用例的既有模式），CI runner 自带 Docker 必跑。

## AP. 附带发现并修复：Celery 任务体泄漏业务库引擎（每任务一个）

eager 用例跑通后，套件在 Windows 上又开始报临时目录清理 WinError 32。定位：
`celery_app._execute` 用 `make_engine_and_session` 创建业务库引擎后**从未 dispose** ——
每个任务泄漏一个 aiosqlite 连接（连接线程持有 SQLite 文件句柄直到 GC）。这与附六修过的
lifespan 引擎泄漏、附九修过的停机取消句柄孤儿化是**同一类问题的第三种形态**：
「谁建引擎，谁负责释放」在这条异步链路上一共出现了三处。修复：`_execute` 加
`finally: await db_engine.dispose()`。修的过程中还踩了一个变量遮蔽：内层
`engine, closer = build_engine_with_saver(...)` 把业务库引擎变量覆盖成 AgentEngine，
`finally: await engine.dispose()` 就 dispose 到了错误的对象上（`AttributeError`，
被 CI 同款 ruff 门禁之外的 pytest 当场拦下）—— 业务库引擎改名为 `db_engine`。

## AQ. 一个值得记下的 Python 坑：finally 里的 return 会吞掉 body 异常

给 conftest 写临时目录夹具（清理失败重试后容忍）时，第一版在 @contextmanager 的
finally 里用 `return` 表示"清理完成"。结果全量套件 92 个用例集体报
`ValueError: settings did not yield a value`，且 `yield` 前的调试打印全部跳过。

机制：body 抛出 TypeError（helper `yield td` 给的是 TemporaryDirectory **对象**，
而原夹具 `as td` 拿到的是 `__enter__` 返回的**字符串**，`Path(对象)` 不成立）→
with 语句把异常 `throw` 进 contextmanager 生成器 → finally 里 cleanup 成功后
`return` —— **finally 中的 return 会把正在传播的异常替换成 StopIteration** →
@contextmanager 的 `__exit__` 收到 StopIteration 且 `exc is not value` 为真 →
判定"异常已抑制"返回 True → body "正常"结束 → 夹具生成器没 yield 就结束。

教训两条：① **finally 里永远不要 return/break 出异常传播路径**（清理失败要用
ok 标志记录，而不是 return）；② wrapper 类 helper 的 yield 值必须与被包装对象的
语义对齐（`__enter__` 返回 name 字符串是有原因的）。

## AR. 验证总账

| 项 | 结果 |
|---|---|
| 测试套件 | **206 条**（197 离线 + 9 需 Docker/PG/Redis）；本地 **8 连跑全绿 0 error** |
| 静态检查 | `ruff`(E9/F63/F7/F82) clean；`compileall` exit 0 |
| 敌对条件 | `SEARCH_PROVIDER=bing` → 193 passed |
| smoke 三脚本 | bing 注入下全过，指标 100% |
| ci.yml | integration job 增补 test_celery_path + redis:7-alpine 预拉取，用例数注释同步（206/197） |

## AS. 已知限制与未验证项（本轮）

| 项 | 状态 |
|---|---|
| Redis/PG 三条新集成用例本地未真跑 | 本机无 Docker daemon（fixture 正确 skip）；CI integration job 首跑待确认 |
| 本地套件耗时 +8s（18s → 26s） | 无 daemon 时 docker SDK 连接探测超时 ×3（pg_url/redis_url 每模块一次）；CI 上 Docker 可用无此开销 |
| worker 子进程 `--pool=solo` | 与 README 的 Windows 指引一致；Linux 生产可用 prefork 提并发，测试取两平台一致的最小形态 |

## E. 依赖锁定（2026-09-20）

> 对应 1.4「依赖锁定文件：不存在」与 6.4「无依赖锁定」两项。本附录记录补齐后的实际形态。

**问题不是"没用 lock 文件"这么抽象 —— 是已经真的漂了。** 用 `pip install --dry-run --report`
对现网 `requirements.txt` 做了一次解析取证：

| 声明 | 本机实装 | `pip` 当前解析 | 风险 |
|---|---|---|---|
| `openai>=1.50` | **3.14.1** | **3.16.2** | 声明写 1.50，实际已跨 2 个大版本；同一份文件今天装和下周装结果不同 |
| `langgraph>=0.2.60` | **1.2.11** | 1.2.11 | 0.x → 1.x 跨大版本 |
| `langgraph-checkpoint-sqlite>=2.0.0` | **3.1.1** | 3.1.1 | 2.x → 3.x |
| `langgraph-checkpoint-postgres>=2.0` | **3.1.2** | 3.1.2 | 2.x → 3.x |
| `pytest>=8.0` | 9.1.1 | 9.1.1 | 跨大版本 |

其中 `langgraph` 与两个 checkpoint 包是关键：它们一旦变更 **checkpoint 序列化格式**，
「断点恢复 100%」这个核心指标会**静默失效**（不是报错，是恢复到错误的状态）。
这正是必须锁定的理由 —— 不是洁癖，是这条能力链条的存档格式由它决定。

**落地方案（两个文件分工，不做替换）**

- `requirements.txt` **保留**，语义明确为「兼容范围声明」（`>=`），表达意图与升级空间；
- 新增 `requirements.lock.txt` = 「已知可用组合」（`==`，100 条含传递依赖），
  取自「205 条离线用例 + 四项指标全部实测通过」的那套版本；
- 文件头注释写清用法、生成方式、更新流程（升级后必须重跑测试与 `metrics.py` 再导出）。

**接入点**

| 位置 | 改动 |
|---|---|
| `.github/workflows/ci.yml` | 四个 job（static / test / integration / smoke）全部改用 lock 安装 |
| `Dockerfile` | `COPY requirements.lock.txt` + `pip install -r requirements.lock.txt` |
| `README.md` | 快速开始改为主推 lock、注释说明另一种；目录树补两文件职责 |
| `static` job | 新增**依赖锁定一致性门禁** |

**门禁逻辑**（防"锁了但锁脱钩"—— 那比不锁更危险，因为 CI 全绿却装的是没验证过的版本）：

1. 锁定文件必须覆盖 `requirements.txt` 的**全部直接依赖**；
2. 每条锁定版本必须**落在 `>=` 声明的兼容范围内**（用 `packaging.Requirement.specifier.contains` 判，不手写比较）。

**验证结果**

| 检查 | 方式 | 结果 |
|---|---|---|
| 门禁脚本可执行 | 从 `ci.yml` 抽出内嵌脚本本地实跑 | exit 0，「锁定自洽：20 个直接依赖 / 100 条锁定」 |
| 锁定可解析且自洽 | `pip install --dry-run --report -r requirements.lock.txt` | 解析出 101 个包，与锁定声明**逐条一致** |
| 版本未越界 | 逐条比对 `>=` 范围 | 全部通过 |
| 无回归 | 离线套件 | **205 passed / 2 skipped / 0 failed（24.82s）** |

**遗留**：lock 未做哈希校验（`--require-hashes`）。本项目锁的是版本组合而非供应链完整性，
哈希能防的（Artifactory 上的同版本号投毒）不在当前威胁模型内；若将来要上生产，这是下一步。

---

# 附十一：P2-5 可观测性（2026-09-21）

对应对账表 **6.2「无可观测性标准接入：无 OpenTelemetry / Prometheus，无 trace_id 贯穿，
无 LLM 调用级 span」** 与路线图 **P2-5**。原文承诺"`app/main.py`、新 `app/observability/`"，
实际落点比承诺多四处接线（引擎 / LLM / 工具节点 / 队列），原因见下。

## AT. 一个先要交代的判断：为什么**不**引 `prometheus-client` / 完整 OTel SDK

| 方案 | 代价 | 取舍 |
|---|---|---|
| 引 `prometheus-client` | 同步改 `requirements.lock.txt`（100 条锁定要重生成）、CI **依赖锁定一致性门禁**、`Dockerfile` | 只为"计数 + 文本序列化"这点薄能力 |
| 引完整 OTel SDK + exporter | 上面全部，再加 collector 进程才能落地数据 | 演示形态下没有 collector |
| **零依赖手写 exposition** | 约 100 行 | ✅ 采用 |

关键事实：**exposition 文本格式（`text/plain; version=0.0.4`）是稳定的公开协议**，
不是某家 SDK 的私产。自己序列化出来的文本，`prometheus` 抓取器一视同仁地能读 ——
所以"不引 SDK"丢掉的是 SDK 的便利，不是协议兼容性。

关于 OTel：本机装的是 `opentelemetry-api==1.44.0`（纯 API 门面，无 SDK/exporter），
**采用它定义的 `traceparent` 语义与字段格式**，但不依赖它跑 export —— trace 落进自己的
事件表与日志。这让 trace 在**本地零依赖形态**下就能用（项目的离线默认形态）。

## AU. 三块能力与各自的设计判断

### ① Prometheus 指标（`app/observability/metrics.py`）

生产指标 11 个：`agent_tasks_total`(status) / `agent_task_duration_seconds`(status) /
`agent_tasks_inflight` / `agent_queue_depth` / `agent_llm_calls_total`(model,outcome) /
`agent_llm_call_duration_seconds`(model) / `agent_llm_tokens_total`(model) /
`agent_tool_calls_total`(tool,outcome) / `agent_tool_execution_duration_seconds`(tool) /
`agent_events_total`(type) / `agent_selfheal_total`(tool)。

**基数纪律**（最重要的一条约束）：标签值只允许**低基数枚举**（status / event_type /
tool / model / outcome）。**任何 ID 都不许做标签** —— `task_id` / `tenant_id` / `trace_id`
是 UUID 级取值，做标签会让时间序列数量无界增长，抓取器内存先于业务先崩。
所以 `/api/metrics`（按租户聚合的业务洞察）与 `/metrics`（进程级低基数聚合）**定位不同、
各自不可替代**：前者要按租户切分，后者要能长期留存。ID 只进事件表与日志。

另两个必须知道的细节：

- **抓取端点有意不放在 `/api` 前缀下**。`PerIpRateLimitMiddleware` 只拦 `/api/*`，
  而抓取器每 15s 拉一次、无法携带租户凭据 —— 放在 `/api/metrics` 会被限流中间件拦掉。
  公开性是有意为之：只暴露进程级低基数聚合，不含任何租户数据。
- **无样本的指标不输出**，而不是补一行假 0。Prometheus 里"没数据"与"数据为 0"是两回事，
  补假 0 会污染 `rate()` / `sum()` 的计算。

### ② trace 贯穿（`app/observability/context.py`）

- **入站复用**：带标准 W3C `traceparent: 00-<32hex trace-id>-<16hex span-id>-<2hex flags>`
  提交任务 → 原样复用上游 trace（接得上调用方链路）；不带或格式非法 → 自生成 32 位 hex。
- **严格解析**（非宽松）：段数、十六进制、长度逐项校验，且**全零 trace-id/parent-id 视为缺
  失** —— 规范把全零保留为非法值。宽松解析会把脏 id 传播到整条链路，比拒绝更难排查。
- **用 `contextvars` 而不是 `AgentState` 字段**：trace 是**请求级上下文**，不是**任务状态**。
  写进 state 会让存量 checkpoint 反序列化后缺字段（checkpoint 是单一事实来源，改形态等于
  破坏向后兼容）。这与既有的 `_current_task_id`（`app/graph/engine.py`）同机制。
- **必须在 `run_task` / `resume_task` 内部显式绑定**，而不是写成 HTTP 中间件：任务由后台
  队列协程拉起（`asyncio.create_task` 之后的上下文**不继承**请求上下文），Celery 路径更是
  另一个进程 —— 显式绑定是唯一在两种队列形态下都成立的做法。

### ③ 结构化日志（`app/observability/logging.py`）

`logging.Filter` 从 `contextvars` 读取并注入 `task_id` / `trace_id`。选 Filter 而不是
"每个调用点手动传 extra"的理由：**Filter 在每个 handler 上执行**，
第三方库（uvicorn / sqlalchemy / httpx）的日志会自动补齐字段 —— 手工传 extra 只能覆盖
自己写的代码，而排查问题时恰恰最需要框架侧的日志。

`setup_logging` 的两个要点：`force=True` 清空既有 handler（否则 uvicorn 已经配过 root，
日志会打两遍）；输出走 **stdout**（12-factor：日志作为事件流交给平台收集，落文件是平台的事）。
`LOG_FORMAT=text|json` 可切，本地开发用 text、生产用 json。

## AV. 落点（比原计划多 4 处接线）

| 文件 | 改动 |
|---|---|
| `app/observability/`（新，4 文件） | `__init__` 门面 / `context.py` trace 上下文 / `metrics.py` 指标与序列化 / `logging.py` Filter 与 Formatter |
| `app/main.py` | 模块级 `setup_logging`（创建 app 前）+ `_prometheus_route()` 注册 `GET /metrics` |
| `app/graph/engine.py` | `run_task` / `resume_task` 绑定与还原 trace、任务耗时与在飞计数、`emit()` 事件带 `trace_id` 并计数 |
| `app/core/llm.py` | **LLM 调用级 span 落在这里**（见下）；`OpenAIChatLLM` 与 `FakeScriptedLLM` 同等埋点 |
| `app/graph/nodes.py` | 工具调用计数/耗时（含自愈与退避的全部时间）、自愈循环计数 |
| `app/worker/local_queue.py` | `submit(task_id, traceparent=...)`、`QUEUE_DEPTH` 维护、`_execute` 取出并传入 traceparent |
| `app/api/routes_tasks.py` | `create_task` 读取入站 `traceparent` 头并透传给队列 |
| `app/storage/models.py` | `Event.trace_id` 列 + 幂等迁移（抽出 `_existing_columns()` 辅助，SQLite `PRAGMA` / PG `information_schema`） |
| `app/storage/repository.py` | `append_event` 写入 / `_event_dict` 读出 `trace_id` |
| `app/config.py` | `log_level` / `log_format` / `prometheus_enabled` / `prometheus_path` |
| `tests/test_observability.py`（新） | **63 条** |

**为什么 LLM span 落在 `app/core/llm.py` 而不是 `react_step` 节点**：LLM 调用是全项目**唯一
真实出网点**，而它由多个节点触发（planner / react_step / compressor 摘要 / critic）。
埋在 `chat()` 里则"调用即被观测"，埋在每个节点里则要重复 N 处、且新增节点会漏。
顺带一个真实发现：**`FakeScriptedLLM` 也必须计数** —— 它是项目的离线默认形态，
只给 `OpenAIChatLLM` 埋点会让默认运行方式下 LLM 指标恒为 0（这是先写测试才发现的）。

## AW. 过程中发现并修掉的真缺陷：直方图 `+Inf` 桶漏计

**症状**：`observe(9999.0)` 后 `_count` 与 `le="+Inf"` 都是 **0**。

**根因**：第一版拿"末桶计数"当总数（`counts[-1]`），而 `counts[-1]` 只在**末桶（60s）
也命中**时才等于总观测数 —— 观测值超过所有桶边界时一个桶都不进，`counts[-1]` 恒为 0。
而 Prometheus 规范要求 `+Inf` 桶**恒等于总观测数**、各桶**单调不减**。

**影响面**：`histogram_quantile()` 会算错；而且这类缺陷**单跑正常流量看不出来**
（正常耗时都落在桶内），只有超时重试后的长尾（> 60s 的 LLM 调用）能逼出来 ——
恰恰是最需要被观测到的那批请求会凭空消失。

**修法**：单独维护总观测数 `self._totals`（不再从桶反推），并补两条回归用例：
边界值必须进 `+Inf`、各桶单调不减。这就是"写测试比写实现更值钱"的典型一例。

## AX. 测试设计（63 条）与踩到的三个坑

三条坑都写进了测试模块 docstring：

1. **指标是进程级单例，且被其他测试文件共享**。`test_api.py` / `test_hitl.py` 会真实跑任务
   并写全局注册表 —— 所以"断言计数等于 N / 断言注册表为空"这类写法必然失败。两类断言严格分开：
   **纯单元断言**（格式 / 累积 / 转义 / 清空语义）用 `unit` fixture 造**独立注册表**；
   **端到端断言**只做**存在性**断言（存在性对污染免疫）。
2. **trace 是 `contextvars`**，多次 `bind_trace` 必须各自 reset，否则向后泄漏。
3. **标签按字典序输出**。标签顺序在 Prometheus 里无语义，但会让
   `'name{tool=...,outcome=...}' in body` 这类整段子串断言误报 —— 断言一律经
   `_sample_line` / `_label` / `_bucket_value` 取值。这条坑当场咬了一次：把
   `_sample_line(body, m)` 的"第一条"当成目标序列，读到的却是别的测试文件留下的假工具名。

覆盖分布：traceparent 解析 **16**（含全零 id 两个独立用例）、指标格式 **16**、结构化日志
**9**、LLM span **6**、端到端 **8**（含 `/metrics` 无高基数标签、指标存在性）、trace 贯穿 **5**。

其中一条刻意做成**负向断言**：`test_metrics_endpoint_has_no_high_cardinality_labels`
断言 `task_id` / 租户 key **不出现在** `/metrics` 响应体里 —— 基数纪律靠代码审查守不住，
必须由测试守。

## AY. 端到端取证（本机实跑）

审批路径的 trace 传播（探针脚本输出，脱敏）：

```
--- events BEFORE approve ---
  seq=1 type=llm_step            trace='39536f5ad5ad8af525bbf3be7b044fb8'
--- events AFTER approve ---
  seq=1 type=llm_step            trace='39536f5ad5ad8af525bbf3be7b044fb8'
  seq=2 type=approval_granted    trace='a0751986e2bd927488660bd126856f48'
  seq=3 type=tool_result         trace='a0751986e2bd927488660bd126856f48'
  seq=4 type=llm_final_draft     trace='a0751986e2bd927488660bd126856f48'
  seq=5 type=task_done           trace='a0751986e2bd927488660bd126856f48'
```

即：提交阶段一条 trace，审批恢复**另起**一条 —— 与设计一致（审批是一次独立触发，
要能回答"谁在何时把任务救回来"）。

## AZ. 验证总账

> 下表是 **P2-5 当时（277 条）的快照**，append-only 保留。P2-6 之后为 **362 条**，
> 见附录十二 G 节。

| 项 | 结果 |
|---|---|
| 测试套件 | **277 条**（268 离线 + 9 需 Docker/PG/Redis）；`268 passed, 9 skipped, 0 failed（31.10s）` |
| 新增用例 | `tests/test_observability.py` **63 条**（214 → 277） |
| 静态检查 | `ruff --select E9,F63,F7,F82` → **All checks passed**；`compileall` exit 0；`import app.main` OK |
| 依赖锁定门禁 | 本地复跑 → 「锁定自洽：20 个直接依赖 / 100 条锁定」（**零新依赖**，lock 无需重生成） |
| 基线不回归 | 原 205 条离线用例全部保持通过 |

## BA. 已知限制与未验证项（本轮）

> ⚠️ **本节四项已被附录十二（P2-6）全部解决**，此处保留原样以示前后对照。

| 项 | 状态 |
|---|---|
| trace 未接 OTel collector / 无 span 层级 | 只有 `trace_id` 与 `parent-id` 的解析复用，**没有真正的 span 树**（无父子耗时分解）。原因是演示形态无 collector；落点已就位（`context.py` 的解析是 span 化的前提） |
| 指标是**进程内**计数 | 多 worker 部署时 `/metrics` 是各进程各自的值，需靠 Prometheus 端按 `instance` 标签聚合 —— 这是 exposition 模型的正常用法，但 README 未展开说明 |
| 日志无采样 / 无脱敏 | 目标是离线演示；生产需补 PII 脱敏（工具参数可能含用户输入） |
| 直方图分桶是硬编码常量 | `DEFAULT_BUCKETS` 13 档覆盖 LLM(0.2~30s) 与工具(10ms~10s)，未做按指标可配 |
| 未提交 | 本轮改动留待用户审阅后提交 |

---

# 附录十二：P2-6 —— 把"能观测"补成"够用"（span 树 / 多 worker / 脱敏采样 / 分桶）

> 本附录是 **P2-5（附录十一）留下的三个已知限制的落地记录**，同时解决分桶硬编码。
> 附录十一的「已知限制」表里如实交代了四项，本轮把其中三项补齐，第四项（分桶）一并做掉。

## A. 本轮解决的四个问题（与附录十一「已知限制」逐条对应）

| 附录十一的限制原文 | 本轮做法 | 落地位置 |
|---|---|---|
| 「只有 `trace_id` 与 `parent-id` 的解析复用，**没有真正的 span 树**（无父子耗时分解）」 | 保留入站 `parent-id` → 本进程 root span 继承之；四层埋点（task/step/llm/tool）；`self_ms` 分解；`spans` 表 + 查询 API | `app/observability/spans.py`、`context.py`、`models.py`、`routes_tasks.py` |
| 「指标是**进程内**计数，多 worker 时需 Prometheus 端按 `instance` 标签聚合」 | 导出**进程身份指标**（`agent_build_info{version,pid}`、`agent_process_start_time_seconds{pid}`）+ 约定 `PROCESS_INSTANCE` 作为 `instance` 标签 | `app/observability/metrics.py` |
| 「日志无采样 / 无脱敏」 | 8 条脱敏规则 + 递归 `redact_value` + 采样三条规则（WARNING 永不丢） | `app/observability/logging.py` |
| 「直方图分桶是硬编码常量」 | `parse_buckets` + 按指标 env 覆盖 + 两个 workload profile（各 13 档） | `app/observability/metrics.py`、`app/config.py` |

## B. 设计决策：为什么不接 OTel SDK

**结论：不接。互操作靠 traceparent 线格式，不靠 SDK。**

1. **零依赖铁律**：引入 `opentelemetry-sdk` + `opentelemetry-exporter-otlp` 要同步改
   `requirements.txt` / `requirements.lock.txt`（本次实测 20 直接依赖 / 100 条锁定）/
   CI 依赖锁定门禁 / Dockerfile，改动面与收益不成正比 —— 与 P2-5 拒绝
   `prometheus-client` 是同一个判断。
2. **本机无 collector 可验**：无法产出真实取证，那这一项就只能是"纸面声明"，
   与"任何写入简历的数字必须有可复现脚本或测试为证"的项目纪律冲突。
3. **互操作点其实在线格式**：上游（网关 / 前端 / 其他服务）传下来的
   `traceparent` 头是 W3C 标准，谁生成的都一样。本实现**原样复用入站 `trace_id`
   并把入站 `parent-id` 作为本进程 root span 的父节点**，于是跨进程也能拼成一棵树 ——
   这正是"接得上上游链路"的实际含义。将来真接 collector 时，
   `SpanSink` Protocol 就是现成的接入点（`record_spans(list[dict])`），
   换一个实现即可，埋点与聚合逻辑不用动。

**`context.py` 的解析确实是 span 化的前提**（如附录十一所判断）：只有拿到
`parent-id` 才能建父子关系。原实现只返回 `trace_id`（把 `parent-id` 丢了），
本轮补 `parse_traceparent_full() -> TraceParent | None`，**同时保持
`parse_traceparent() -> str` 签名不变**（委托给 full 版），使既有 16 条解析测试零改动。

## C. span 树实现要点

**四层埋点与生命周期**：

| kind | 埋点位置 | 生命周期 |
|---|---|---|
| `task` | `engine.run_task` / `resume_task` | 整个任务（**必须显式 `begin_span`/`end`**，见下"踩坑"） |
| `step` | `nodes.react_step_node` 外层 | 一轮 ReAct |
| `llm` | `core/llm.py` 的 `chat`（真实与 Fake 都埋） | 一次模型调用 |
| `tool` | `nodes._execute_call` | 一次工具调用 |

**自耗时公式**：`self_ms = duration_ms − Σ(直接子 span duration)`。

三个易错点（均有回归用例）：

1. **只扣直接子**：孙辈耗时已经计入子辈的 `duration_ms`，再扣一次就是重复扣。
   用 `test_self_time_does_not_double_subtract_grandchildren` 锁住
   （root→mid→leaf 三层，断言 `root.self == root.dur − mid.dur`）。
2. **并行子之和可能超过父**：并行工具同时跑时，两个子 span 的 wall-clock 会重叠，
   之和大于父 duration。故取 `max(0.0, ...)` 下界保护，而非允许负数。
3. **孤儿与自环**：父 span 不在查询结果集里时（例如按 `kind` 过滤后把父滤掉了），
   该节点**提升为顶层**而不是丢弃 —— 否则过滤查询会返回空树。
   自环（`parent_span_id == span_id`）单独兜底，避免构造无限递归。

**`SpanSession` 为什么必须存在（实测踩到）**：

`span()` 是 `@contextmanager`，`__enter__()` 的返回值才是 handle。长生命周期的
root span 要跨 `try/except/finally` 多段使用，用 `with` 包住整段 `run_task` 会
把 `finally` 里的清理也包进去（职责重叠）。正确形态是显式两段式：

```python
root = self.begin_span(obs_spans.KIND_TASK, "run_task", task_id=task_id)
try:
    ...
    root.set_attribute("status", "done")
except BaseException as exc:      # 只在这里连异常一起闭合
    root.end(exc)
    raise
finally:                          # 只做指标与 ContextVar 清理
    ...
root.end()                        # 正常路径闭合
await self.flush_spans(task_id)
```

初版写成 `root_span = self.open_span(...)` 再 `root_span.__enter__()`，
拿到的是**生成器对象**而非 handle，`set_attribute` 直接 `AttributeError`，
表现为**所有任务 500**（`test_full_task_lifecycle` 断言 `'failed' == 'done'`）。
这个坑的教训是：`@contextmanager` 不能"手动分段驱动"。

**另一个必踩的坑**：`ContextVar` 必须在 `SpanSession.__init__` 里显式 `set`。
外层 span 显式传了 `trace_id=tid` 但若不写进 `trace_id_var`，内层 span 调
`current_trace_id()` 会拿到空串并退化成 `"-"` —— 表现是"父子链断了但看不出为什么"。

**落库用缓冲而非在 `finally` 里 await**：span 在 `finally` 闭合，但
`finally` 里不能 `await` 落库（会覆盖原始异常）——所以先进进程级
`BUFFER`，由调用方在安全点（`run_task` 末尾）批量 `flush_spans` 落库。
span 落库失败**吞异常只告警**，与工具执行流水的严格策略**刻意相反**：
流水是正确性的一部分（漏一条会导致重复执行），span 只是旁路观测
（丢一条不影响任务结果），不该因为观测组件故障而让任务失败。

实测一次真实任务（探针输出）：

```
span 总数 = 9
by_kind = {"task":{"count":1,"total_ms":1241.454},"step":{"count":3,"total_ms":19.67},
           "llm":{"count":3,"total_ms":0.018},"tool":{"count":2,"total_ms":1170.934}}
[task] run_task    dur=1241.454ms self=  50.850ms parent=00f067aa0ba902b7
  [tool] get_weather dur=1170.787ms self=1170.787ms parent=efd9737839adc4ad
  [step] react_step  dur=    7.891ms self=   7.885ms parent=efd9737839adc4ad
    [llm] chat       dur=    0.006ms self=   0.006ms parent=c3bc60d61f99bd07
  ...
1) 与入站 trace 同源？            True
2) 有 task 根 span？             True  count=1
3) llm/tool/step span 数         3 / 2 / 3
4) root 的直接子 span 数          5
5) 断链 span                     []
6) 继承入站 parent-id 的 span 数   1
7) Σ self_ms = 1241.454ms = root dur   ← 守恒
```

**第 7 项是这次实现最有力的正确性证据**：所有 span 的自耗时之和恰好等于
根 span 的 duration。这个守恒律同时校验了"不重复扣"与"不遗漏扣"两件事。

## D. 多 worker 聚合实现要点

**问题**：`uvicorn --workers 4` 下四个进程各自导出 `/metrics`，Prometheus
按 `instance`（默认 `host:port`）去重 → 四份序列**互相覆盖**，计数器随机跳动；
进程重启后计数归零，`rate()` 又会产生假尖峰。

**做法**：导出两个进程身份指标，并给出 `instance` 标签的约定值。

```
agent_build_info{pid="26540",version="probe-2.6"} 1
agent_process_start_time_seconds{pid="26540"} 1789951773.0767982
PROCESS_INSTANCE = "hostname:26540:1789951773"
```

- `agent_build_info` 值**恒为 1**，只用于暴露 `version` / `pid` 标签
  （Grafana 里可用 `count by (version) (agent_build_info)` 看版本分布）。
- `agent_process_start_time_seconds` 让抓取侧能把 PID 换算成进程 uptime，
  也是区分"同一个 PID 的新旧两代进程"的唯一手段。
- `PROCESS_INSTANCE` 用 `host:pid:start`（而非 `host:port`）作 `instance` ——
  这正是 Prometheus 官方对多进程场景的标准建议。

**基数纪律**：`pid` 与 `version` 是**低基数**（进程数有界、版本数更少），
可以做标签；而 `task_id` / `tenant_id` / `trace_id` 是 UUID 级，**绝不能做标签**
（每加一个唯一值就多一条序列）。这条界线在 P2-5 已确立，本轮新增的两个指标
都严格落在"低基数"一侧。

## E. 日志脱敏与采样实现要点

**脱敏（`RedactionFilter`）**：8 条默认规则，按"模式特异性"排序。
挂在 **Filter 层而非 Formatter 层**：Filter 对每条 `LogRecord` 执行且早于格式化，
于是 `json` 与 `text` 两种格式档位共用同一份脱敏结果（挂在 Formatter 上要写两遍，
两边迟早不一致）。三类载体都要处理：

- `record.msg` 与 `record.args`（`log.info("url=%s", url)` 的敏感值在 args 里，
  只处理 msg 是最典型的"以为脱敏了其实没有"）
- `extra={...}` 字段（**必须递归**：工具参数以 `extra={"arguments": {...}}` 进来，
  顶层是 dict 而非 str —— 初版只处理顶层 str，被
  `test_redaction_filter_masks_extra_fields` 抓到）
- 异常栈文本（**预填 `exc_text` 并置 `exc_info=None`**，同时
  `JsonFormatter` 必须认得 `exc_text` —— 否则异常栈会整体消失，比泄漏更糟）

**两个实测抓到的真缺陷（探针取证）**：

1. **规则顺序**：宽泛的 `key[:=]value` 若排在连接串规则之前，
   `postgres://admin:hunter2secret@db:5432/app` 会被中间的 `secret` 字样触发 →
   输出 `postgres=[REDACTED]db:5432/app`。密码遮蔽了，但**方案名被吃掉**，
   日志失去"连的是哪个库"这个关键排查信息。
   修法：连接串规则提到赋值式规则之前，并新增 `_KEEP_GROUPS` 声明式保留组
   （连接串保留第 1+2 组 = scheme + user，只遮蔽第 3 组 password）。
2. **偏移基准**：替换函数若从文本 0 起切片（而非 `m.start()`），
   会把 match 之前的整段前缀再抄一遍 ——
   `"连接串 postgres://admin:pw@db"` → `"连接串 连接串 postgres://admin:[REDACTED]@db"`。
   结构被破坏，**且替换不幂等**。
   修法：切片基准统一用 `m.start()`。

**第 2 点的通用守护比逐条断言更值钱**：

```python
def test_redact_is_idempotent_for_every_default_pattern():
    """每条默认规则都必须幂等：f(f(x)) == f(x)。"""
```

理由：脱敏的失败模式大多是"结构被破坏"（前缀复制 / 组错位 / 片段重复），
这类缺陷用逐条样例很难穷尽；而**幂等是它们共同会违反的性质** ——
结构一旦破坏，再跑一次通常会继续恶化，于是"跑两遍 = 跑一遍"成了
廉价且高敏感的探测器。一条参数化用例即覆盖全部 8 条规则 × 9 个样例。

**采样（`SamplingFilter`）三条规则**：

1. `levelno >= WARNING` → **必留**。错误日志是排查起点，采样掉它等于销毁现场。
2. 每个 `(logger, levelno)` 的**首条必留** —— 否则"某模块开始报日志"这个事件
   本身不可见，看起来像模块没启动。
3. 之后每 N 条留 1 条。

用**计数器而非随机数**：随机采样在低日志量下可能连续丢弃，观测不稳定；
计数器在任意量级下行为可预期、可测试（无 flaky）。
`sample_rate <= 0` 视为**关闭采样（全留）**，与配置里"0 = 关闭"的既有约定一致
（见限流项），避免"设为 0 反而丢掉全部日志"这种事故。

**Filter 顺序**：`TraceContextFilter` → `RedactionFilter` → `SamplingFilter`。
先注入上下文（日志内容需要 id），再脱敏（在采样丢日志前完成，且万一将来
有 Filter 把日志转存别处，脱敏已在它之前生效），最后采样。

## F. 分桶可配实现要点

`parse_buckets(spec, fallback)` 接受 `"a,b"` / `"a b"` / list 三种写法，
**逐项跳过**非法值与 NaN/Inf（而非整体回退），`sorted(set(...))` 去重保序；
档数 `< 2` 或 `> 50` 退回 fallback（`_MAX_BUCKETS` 防呆：分桶过密会让
每次观测的桶遍历成为热点，且序列数爆炸）。

两个 workload profile（各 13 档）：

- `_LLM_BUCKETS`：`0.05 ~ 60.0`（模型调用跨度大，含超时重试长尾）
- `_TOOL_BUCKETS`：`0.001 ~ 60.0`（工具含本地毫秒级与外部 HTTP 秒级）

环境变量 `METRICS_BUCKETS_TASK` / `_LLM` / `_TOOL` 覆盖。
读取走 `_buckets_from_env()` 而**不 import config**：避免 observability → config
的循环导入，也避免在模块导入期就触发 config 的建目录副作用。

**一个测试写错的教训**：初版写成
`test_parse_buckets_falls_back_on_invalid_input[nan,1,2]` 断言"退回默认"，
但实现是**逐项跳过** —— 跳掉 `nan` 后剩 `(1.0, 2.0)` 仍是合法集合。
拆成两条：`..._skips_nan_and_inf_but_keeps_the_rest`（断言 `(1.0, 2.0)`）+
`..._all_invalid_falls_back`（断言默认）。**测试错了要修测试，不能改实现迎合测试**，
但改之前必须判断清"谁才是对的行为"——这里实现是对的（"部分配置生效"比
"一个字符写错就整体回默认"更符合运维直觉）。

## G. 验证总账（P2-6）

| 项 | 结果 |
|---|---|
| 全量测试 | **362 条**（353 离线 + 9 需 Docker/PG/Redis）；`353 passed, 9 skipped, 0 failed（32.70s）` |
| 可观测性单文件 | `tests/test_observability.py` **148 条**（63 → 148） |
| 本轮新增用例 | **85 条**（277 → 362） |
| 9 条 skip 明细 | Docker 沙箱 4 + PostgreSQL 3 + Celery 2（全部因本机无 Docker daemon，CI 上会真跑） |
| 离线收集数 | `355`（排除 docker/postgres 两个文件后；= 353 passed + 2 skipped） |
| CI 测试数门禁 | 阈值 `268 → 353`（**单调上调，绝不下调**） |
| 探针取证 | span 树 7 项断言全 PASS（含 Σ self_ms 守恒）；脱敏/采样 15 项断言全 PASS；分桶 4 项全 PASS |
| 基线不回归 | 附录十一的 277 条全部保持通过 |

## H. 已知限制（P2-6 之后仍存在）

| 项 | 状态 |
|---|---|
| 未提交 | 本轮改动留待用户审阅后提交 |
| 未接 OTel collector | **有意不接**（理由见 B 节：零依赖铁律 + 本机无 collector 可验）。`SpanSink` Protocol 是现成接入点 |
| 未用 `PROMETHEUS_MULTIPROC_DIR` | 该方案需 `prometheus_client` + gunicorn 生命周期钩子，且只在**多进程共享同一端口**时才有收益；当前 uvicorn 单进程部署下无收益。多 worker 场景已通过"进程身份指标 + `instance` 标签约定"给出**部署侧**的正确用法 |
| `spans` 表只对 `trace_id` 建索引 | `get_spans_by_task` 是低频排查路径，且单任务 span 数受 `SPANS_MAX_PER_TASK`（默认 500）约束，数据量天然有界。**有意不加** `task_id` 索引（写了索引反而增加每次 span 写入的成本，而写入是热路径、查询是冷路径） |
| 脱敏是"降低泄漏面"而非"保证不泄漏" | 正则只覆盖高置信度模式。业务自由文本里的敏感内容无法靠正则穷尽 —— 本模块定位是纵深防御的一层，**不是唯一防线**（鉴权 + 租户隔离才是主防线） |
| span 不采样 | 高吞吐下 span 落库会成为写入热点。当前靠 `SPANS_MAX_PER_TASK` 截断兜底；真要采样应做成"按 trace_id 哈希采样"，保证同一条 trace 要么全留要么全丢（否则树残缺） |

## I. 本轮新增/修改文件清单

| 文件 | 变更 |
|---|---|
| `app/observability/spans.py` | **新建**（~330 行）：`SpanSession` / `begin_span` / `span` / `self_time_ms` / `build_tree` / `SpanSink` / `BUFFER` |
| `app/observability/context.py` | 重写：`span_id_var` / `TraceParent` / `parse_traceparent_full` / `new_span_id` / `bind_trace` 继承入站 parent-id |
| `app/observability/logging.py` | 重写：8 条脱敏规则 + `_KEEP_GROUPS` + `redact_value` + `SamplingFilter` + `JsonFormatter` 认 `exc_text` |
| `app/observability/metrics.py` | `parse_buckets` / `_buckets_from_env` / 两个 profile / 进程身份指标 / `PROCESS_INSTANCE` |
| `app/observability/__init__.py` | 导出扩充 + 模块 docstring 改写 |
| `app/config.py` | 新增 8 个可观测性配置项 |
| `app/graph/engine.py` | `SpanSink` Protocol / `begin_span` / `open_span` / `flush_spans` + run/resume 根 span |
| `app/graph/nodes.py` | `react_step` 与 `_execute_call` 埋 span |
| `app/core/llm.py` | `chat` 埋 span（真实 + Fake 两条路径） |
| `app/storage/models.py` | 新增 `spans` 表 + 索引迁移 |
| `app/storage/repository.py` | `record_spans` / `get_spans` / `get_spans_by_task` / `_span_dict` |
| `app/api/routes_tasks.py` | 新增 `GET /api/tasks/{id}/spans` |
| `app/runtime.py` | `span_sink` 全链路穿透 |
| `app/main.py` | 日志装配参数 / 分桶 env 导出 / 进程指标初始化 / `span_sink` 接线 |
| `tests/conftest.py` | `make_engine` 加 `span_sink`（**漏传会让端到端断言永远为 0**） |
| `tests/test_observability.py` | 63 → **148 条**（新增六段：span 树 / 进程身份 / 分桶 / 脱敏 / 采样 / 端到端） |
| `README.md` | span 约定 / 多 worker 抓取配置 / 环境变量表 / 测试数 362 / 目录树 / 深挖点 20–23 |
| `.github/workflows/ci.yml` | 测试数门禁阈值 `268 → 353` + 口径注释 |
| `docs/Agent运行时-差距评估与完善建议.md` | 本附录 |
| `docs/项目逐文件分析报告.md` | 测试数快照说明更新 |


# 附录十三：P2-2 限流多 worker 额度共享（2026-09-21 第二轮）

**问题**（P2-1 落地时在 `ratelimit.py` docstring 显式披露的已知局限）：
L1（每 IP 每分钟）/ L2（每租户每分钟）的滑动窗口计数在**进程内存**里，
多 worker 部署（`uvicorn --workers 4`）下突发额度 = 单实例额度 × worker 数——
四道门限形同虚设四倍。

## A. 为什么日级层不用改，分钟级层却没有现成方案

日级额度（L2b/L3/L4）聚合 `tasks` 表——判定依据即事实来源，多 worker 天然共享。
分钟级限流**没有事实表可聚合**：L1 计的是"到达 `/api/*` 的请求"（含无效 key
撞库请求，不产生任务行），L2 计的是"提交尝试"（被拒的也不产生任务行）。
无业务事实 → 只能建专属计数存储。

## B. 设计：`RATE_LIMIT_STORE = memory | db`

| | memory（默认） | db |
|---|---|---|
| 窗口语义 | 滑动窗口（精确） | 固定窗口（边界最多 2×limit，可接受近似） |
| 多 worker | 额度 ×N（缺陷） | **共享同一份额度** |
| 每请求成本 | 零库往返 | 一次 DB 往返 |
| 故障行为 | 内存故障即进程故障 | **fail-open**（写库失败放行 + 告警） |

三个关键决策：

1. **固定窗口而不是跨进程滑动**：滑动的标准做法是每 key 存全部命中时间戳
   （Redis ZSET 模式），映射到 SQL 就是每请求一行写入——热点 key 写放大不可接受。
   固定窗口每 `(key, window)` 恒定一行 UPSERT。分钟级是突发软限制，真正的
   资金护栏在日级 tasks 聚合（那里不受影响），2× 边界突发可接受。
2. **判定与记账合并为同一条原子 UPSERT**：
   `INSERT … ON CONFLICT (scope, window_start) DO UPDATE SET hits = hits + 1
   RETURNING hits`——与日级"单事务原子占位"同一手法（campus-assistant 超发
   50% 的教训），两个 worker 并发命中不丢计数（50 并发测试取证）。
3. **窗口起点用墙钟 `time.time()`**：`math.floor(now / window_s) * window_s`。
   monotonic 各进程基准不同，跨进程不可比。

**不引 Redis**：本地模式零依赖铁律；Redis 仅 celery 形态存在，而限流需求
与队列形态正交（uvicorn 多 worker 就够触发）。

**fail-open 的论证**：L1/L2 是突发控制（软限制），DB 故障时随后的鉴权查询
同样会失败，放行不产生额外越权面；且限流器故障不该比它要保护的资源先倒下。

**表自洁**：首命中（hits==1）时顺带 `DELETE WHERE window_start < 当前窗口`，
每 key 任意时刻 ≤1 行，无需后台清理任务；删与增同事务，崩溃要么都发生要么
都不发生。已知残留：废弃 key 留 1 行（~50B），按去重 IP 线性——演示规模可忽略。

## C. 接线

- `app/storage/models.py`：`RateWindow` 表（`uq_rate_windows_scope_window` 唯一约束）
- `app/storage/repository.py`：`rate_limit_hit`（原子 UPSERT + 首命中清旧行）/
  `count_rate_windows`（调试）
- `app/api/ratelimit.py`：`RateLimiter` Protocol（**async allow**——db 实现必须
  await；memory 实现内部无 await，单事件循环天然原子）+ `DbWindowLimiter` +
  `SlidingWindowLimiter` 改 async（逻辑不变）+ 中间件 await
- `app/main.py`：lifespan 按 `settings.rate_limit_store` 条件构造
- `app/api/routes_tasks.py`：L2 判定加 `await`
- `app/config.py`：`rate_limit_store: str = "memory"`

## D. 测试（`tests/test_ratelimit_store.py`，11 条）

分三层：

- **repo 层**：递增 / 新窗口重置 / 首命中清旧行 / **50 并发 gather 序号恰为
  1..50**（原子性取证）。
  夹具必须用临时**文件** SQLite：`:memory:` 下每个池化连接是独立库，
  测不出跨连接并发合并——这正是 db 存储要解决的问题本身。
- **limiter 层**：**双实例共享预算**（两个独立 Repository 模拟两个 worker，
  limit=5 交替打 → `[True]*5+[False]`）+ **memory 反证**（两个
  SlidingWindowLimiter 全 True——修前缺陷存证，防止有人"顺手统一"把单进程
  零库往返的默认也改掉）+ limit=0 不碰存储（Boom 桩）+ Retry-After 边界
  （∈ [1, window_s+1]）+ fail-open。
- **端到端**：`RATE_LIMIT_STORE=db` 下走真实 lifespan + 中间件，L1/L2 行为与
  memory 一致（401×4→429 / 202,202→429 + Retry-After）。

**env 注入时机**：limiter 实例在 lifespan 构建**一次**，故 `RATE_LIMIT_STORE=db`
必须在 TestClient 进入**前**设置；阈值类 env（IP_RATE_LIMIT_PER_MIN 等）是每
请求实时 `get_settings()` 读的，测试中途改仍生效——两种 env 的生效时机不同，
这是本文件最容易被忽视的夹具细节。

## E. 验证总账（P2-2）

| 项 | 结果 |
|---|---|
| 新增用例 | **11 条**（tests/test_ratelimit_store.py），单文件 11 passed（6.27s） |
| 全量回归 | **364 passed, 9 skipped, 0 failed（36.10s）**（353 + 11 = 364 精确吻合） |
| 收集数取证 | 全量 **373** = 364 passed + 9 skipped；离线 **366** = 364 + 2（排除 docker/postgres 两文件，`--collect-only -q` 计 `::` 行） |
| CI 门禁 | 阈值 `353 → 364` + 口径注释更新（355→366） |
| 既有 L1/L2 测试 | 不变绿（默认 memory，语义未变） |
| `allow(` 调用点 | grep 全仓仅中间件与 routes_tasks 两处，均已 await |

## F. 已知限制（P2-2 之后）

| 项 | 状态 |
|---|---|
| db 形态固定窗口边界突发 | 最多 2×limit（有意接受，见 B 节决策 1） |
| 废弃 key 残留 1 行 | ~50B/行，按去重 IP 线性；治理加定时清理即可 |
| db 形态每请求一次库往返 | 多 worker 部署的必要代价；单进程请保持 memory |
| 未提交 | 本轮改动留待用户审阅后提交 |


# 附录十四：P2-Alembic 迁移框架化（2026-09-21 第三轮）

**问题**（路线图 P2 正式项）：schema 演进靠 `models.py` 旁的手写幂等 ALTER
（`migrate_schema`）。它对"加列"够用，但列类型变更、约束调整、漏写幂等分支
都没有防线；且迁移脚本与 `Base.metadata` 会随时间漂移——改了模型忘了同步
ALTER，本地新库与线上旧库就静默分叉。

## A. 设计：三路径自举 + 防漂移对照

`create_tables` 启动时先探测库的**形态**，再选路径：

| 库形态 | 判定 | 动作 |
|---|---|---|
| 全新库 | 无 `alembic_version` 且无 `tenants`/`tasks` | `command.upgrade head`（baseline 建六张表） |
| 存量旧库 | 无 `alembic_version` 但有业务表 | **旧行为逐字保留**：`create_all` + `migrate_schema` 补列 → commit → `command.stamp head` |
| 已版本化 | 有 `alembic_version` | `upgrade head`（幂等空操作） |

三个关键决策：

1. **baseline 不手抄**：0001 版本对空临时库跑 autogenerate 从 `Base.metadata`
   diff 生成（6688 字节，六表 + 索引 + 两个唯一约束）。手抄必漂移；防漂移由
   `test_upgraded_schema_matches_metadata` 持续把关——upgrade 后的列集合必须
   **等于** metadata 列集合，改了模型忘写迁移 → CI 当场红。
2. **旧库先补列再 stamp，而不是强制 upgrade**：存量库里的数据不能冒险走
   通用迁移路径；先按旧逻辑补齐（行为与升级前完全一致），再 `stamp` 标记为
   当前版本，从此进入 Alembic 管辖。升级零风险，回滚也不需要。
3. **async 引擎零新驱动**：`migrations/env.py` 手工实现官方 async 模板
   （`create_async_engine` + `run_sync` + `asyncio.run`），`sqlite+aiosqlite` /
   `postgresql+asyncpg` 直接可用。URL 经 `cfg.attributes["db_url"]` 从**实际
   engine** 注入（`render_as_string(hide_password=False)`），CLI 回退
   `get_settings().database_url`——避免"运行时连的库"与"迁移连的库"不一致。

同步 `command.*` 用 `asyncio.to_thread` 包裹：alembic 无原生 async API，
lifespan 只跑一次，线程成本可忽略。

## B. 接线

- `migrations/env.py` / `script.py.mako` / `alembic.ini`：**新建**三件套
  （ini 不含 `sqlalchemy.url`，URL 一律 programmatic 注入）
- `migrations/versions/0001_baseline.py`：autogenerate 生成
- `app/storage/repository.py`：`_alembic_config` + `create_tables` 三路径重构
- `app/config.py`：无新配置项（迁移不需要 env 开关）
- `requirements.txt` / `requirements.lock.txt`：`alembic>=1.13` + 锁定
  `alembic==1.20.0`、`Mako==1.4.1`、`MarkupSafe==3.0.3`（依赖锁定门禁
  「声明↔锁定自洽」无硬编码数量，加包无需改 CI）

## C. 测试（`tests/test_migrations.py`，4 条）

1. **fresh**：空库 → upgrade → 六表齐 + `alembic_version=="0001"` + 业务读写
2. **幂等**：已版本化库二次 `create_tables` → version 不变 + 数据保留
3. **legacy**：raw SQL 重建旧形态 tasks 表 + 历史行 → 补列 + stamp → 引导后
   可用 + 二次幂等
4. **防漂移**：六表列集合 == `Base.metadata` 列集合（对照断言）

另有 **PG 分支**（`tests/test_postgres_checkpoint.py` 追加 1 条，integration
job 专属）：`DROP TABLE alembic_version` + `DROP TABLE tasks CASCADE` 重建历史
现场 → legacy 补列 + stamp → 二次幂等 → 业务读写。

## D. 验证总账（P2-Alembic）

| 项 | 结果 |
|---|---|
| 新增用例 | **4 条** SQLite（单文件 4 passed, 1.39s）+ 1 条 PG（本地无 Docker skip，CI 真跑） |
| 存量回归 | 受影响四文件 43 passed（test_migrations + test_auth_multitenant + test_ratelimit_store + test_tool_journal + test_api，14.68s） |
| 全量回归 | **379 passed, 10 skipped, 0 failed（37.20s）**（364 + 4 + 11 精确吻合，见附录十五） |
| 依赖锁定 | alembic 1.20.0 / Mako 1.4.1 / MarkupSafe 3.0.3 已入 lock；「声明↔锁定自洽」通过 |

## E. 已知限制（P2-Alembic 之后）

| 项 | 状态 |
|---|---|
| 迁移只有 baseline 一个版本 | 后续 schema 变更用 `alembic revision --autogenerate` 生成新版本即可 |
| `render_as_batch=True` 仅 SQLite | SQLite ALTER 能力有限，batch 模式重建表；PG 走原生 ALTER |
| 未提交 | 本轮改动留待用户审阅后提交 |


# 附录十五：P2-DAG 计划 DAG 化 + ReAct↔Plan 自适应（2026-09-21 第三轮）

**问题**（路线图 P2-3）：计划是线性列表，独立步骤只能串行；且 react 模式
重规划产出的计划没有任何执行轨道（`plan_context` 仅 plan_execute 注入）——
重规划结果只是躺进 `state.plan` 的死数据。

## A. 依赖关系表示：`deps` 键缺省 = 线性链

`PlanStep` 新增可选 `deps: list[str]`（前置步骤 id 列表）。**键缺省 ≠ 无依赖，
= 依赖列表上一步**（线性链）：

| 形态 | 语义 |
|---|---|
| 无 `deps` 键（旧 prompt / 旧 checkpoint） | 线性链，行为与升级前完全一致，**零迁移** |
| `deps: []`（显式） | 无依赖，可与同批步骤并行 |
| `deps: ["s1","s2"]` | 依赖指定步骤，全部完成后才进本批 |

模型守新提示词给 `deps` 才并行；**模型漏写 deps 宁可串行，不做乱序全并行**
（`_parse_plan_steps` 对 dict 形态缺 deps 不写键，退回线性链兜底）。

## B. 执行顺序：Kahn 分层 + 三级兜底

`_plan_layers` 按依赖做 Kahn 分层：每轮取全部依赖已就绪的步骤为一批，
`current_step` 语义从"步骤下标"升级为"批次号"（线性计划层号 == 旧步骤下标，
行为等价）。非法计划（id 重复 / 引用不存在 / 自依赖 / 环）返回 None，
走 `_safe_layers` 三级兜底：**DAG 分层 → 删 deps 退化线性链 → 逐步分层**——
宁可串行不崩。旧 checkpoint 里 replan 重用 `s1..sn` 产生的重复 id 是真实
存量数据，第三级兜底保证它们仍可执行。

**replan id 顺延重编号**：DAG 时代 id 是依赖引用键，旧实现重用 `s1..sn` 会与
保留的 done 步骤撞 id。新 id 接续旧计划 `^s(\d+)$` 最大值顺延，rename map
同步改写新步骤间的 deps 引用；显式 `deps: []` 的新步骤挂到最后一个已完成
步骤之后（done 占据计划头部，不挂会与它们混入同批）。

## C. ReAct↔Plan 自适应切换

| 方向 | 触发 | 动作 |
|---|---|---|
| **升级** react → plan_execute | react 模式重规划**成功** | `mode=plan_execute` + `plan_defect_streak=0` + `mode_upgraded` 事件 —— 修复"重规划计划无执行轨道"缺口 |
| **降级** plan_execute → react | 连续 plan_defect ≥ `ADAPTIVE_DOWNGRADE_AFTER_REPLANS`（默认 2，0=关闭） | 保留 done 步骤、`error_kind` 改记 `retryable`（走 compressor → react_step，**不再回 planner 空转烧 token**）+ `mode_downgraded` 事件 |

经济学：计划被反复证明不可行时，重规划只是每轮烧 token 的循环；降级把
决策权交还模型自由推理。两个方向都由预算（max_steps/max_tokens）兜底，
自适应循环不会失控。

## D. 接线

- `app/graph/state.py`：`PlanStep.deps` + `AgentState.plan_defect_streak` +
  `current_step` 注释更新（批次号）
- `app/config.py`：`adaptive_downgrade_after_replans: int = 2`
- `app/graph/prompts.py`：`PLAN_SYSTEM` 改 dict 形态输出（id 唯一 + deps 引用
  已定义 id）；`PLAN_CONTEXT_LINE` 批次化（"第 X 批 / 共 Y 批" + 当前批全部
  pending 步骤列表）
- `app/graph/nodes.py`：`_parse_plan_steps` / `_plan_layers` / `_linearize_plan` /
  `_safe_layers` 四个模块级函数；planner_node（解析归一 + replan 重编号 +
  分层 + 升级分支 + 事件带 `layers`）；react_step_node（完成判定改批次数 +
  plan_context 注入当前批 pending 列表）；critic_node（成功路径**整批推进** +
  失败路径降级分支）；init_state 补 `plan_defect_streak=0`
- 图装配**零改动**（planner 出边本就固定到 react_step）

## E. 测试（`tests/test_plan_dag.py`，11 条）

- **单元 6 条**：解析双形态（dict 补 id/保留显式空 deps；str 无 deps 键）/
  diamond 分层 `[s1],[s2,s3],[s4]` / 旧格式线性链 / 非法四例（重复、幽灵引用、
  自依赖、环）/ 兜底链（环 → linearize；重复 id → 逐步）
- **端到端 5 条**：diamond 分批并行（`plan_created.layers==[2,1]` +
  两次决策步 tool_calls 数 `[2,1]`）/ critic 整批 done（直调）/
  react 升级（mode_upgraded + final mode）/ plan_execute 降级
  （consecutive_defects==2 + 降级后不再 replan）/ replan id 顺延唯一

**存量兼容**：test_plan_execute 两条零改动通过（线性 JSON → 无 deps 键 →
逐步分层，step_done==3 不变）。**一处行为变化**：react 重规划成功即升级，
finisher 随之走计划汇总分支（比旧版多一次 LLM 调用）——
`test_recursion_blocked` 子脚本补足一次汇总调用，属新语义的预期成本。

## F. 验证总账（P2-DAG）

| 项 | 结果 |
|---|---|
| 新增用例 | **11 条**（tests/test_plan_dag.py），单文件 11 passed（0.44s） |
| 全量回归 | **379 passed, 10 skipped, 0 failed（37.20s）**（364 + 4 migrations + 11 DAG = 379 精确吻合） |
| 收集数取证 | 全量 **389** = 379 + 10；离线 **381**（pytest 自报与 `grep -c '::'` 双向一致）= 379 + 2 celery skip |
| CI 门禁 | 阈值 `364 → 379` + 口径注释更新（366→381）+ job 名同步 |
| skip 明细变化 | 9 → **10**：Docker 4 + PG 4（checkpoint 2 + 租户列 1 + **Alembic 自举 1**）+ Celery 2 |

## G. 已知限制（P2-DAG 之后）

| 项 | 状态 |
|---|---|
| 升降级振荡由预算兜底 | react 升级→又缺陷→降级→再升级的循环每次都消耗 iterations/max_steps，`check_budget` 保证有界；无专门的振荡计数 |
| 批内并行度由模型决定 | plan_context 列出当前批全部 pending 步骤并提示并行调用，但一轮出几个 tool_call 仍由模型决定；分批只保证"依赖就绪"，不强制"一次出满" |
| `plan.index(p)` 序号在重复 id 场景可能取首个 | 仅第三级兜底（重复 id 旧 checkpoint）的事件展示字段受影响，执行不受影响 |
| 未提交 | 本轮改动留待用户审阅后提交 |














---

# 附录十六：一键启动崩溃修复——bootstrap_auth 的 KeyError（2026-09-21 第四轮）

## 现象

双击 `scripts\start.bat`：服务窗口弹出了，但 60 秒就绪探测超时，网页打不开。
前台直跑 uvicorn 复现到根因——lifespan 启动即崩：

```
File "app\api\security.py", line 199, in bootstrap_auth
    row["api_key_hash"] == hash_api_key(default_key):
KeyError: 'api_key_hash'
```

## 根因

`Repository._tenant_dict()` 刻意不返回 `api_key_hash`（当初为了"列表接口不泄漏哈希"
把脱敏做在了仓储层），但 `bootstrap_auth` 走"库中已有 default 租户 + 凭据文件 key
与库一致"分支时需要拿哈希比对 → `row["api_key_hash"]` KeyError → lifespan 失败 →
服务退出 → 就绪探测永远等不到。

**为什么 380 条测试没拦住**：每次测试都用全新临时库，bootstrap 只会走"首次创建"分支；
"第二次启动且文件 key 匹配"这个分支此前没有任何用例覆盖——而真实 `data/agent.db`
第二次启动（哪怕只是重启服务）必然踩中。这是典型的"测试环境自洽、真实数据路径漏测"。

## 修复（分层归位）

| 文件 | 改动 |
|---|---|
| `app/storage/repository.py` | `_tenant_dict()` 补回 `api_key_hash`——哈希是内部事实，仓储层原样返回 |
| `app/api/routes_admin.py` | 新增 `_public()`：管理 API 边界统一剥掉 `api_key_hash`（list/create/patch/rotate 四个端点）——**脱敏的正确位置是 API 边界，不是仓储层** |
| `tests/test_auth_multitenant.py` | 新增回归用例 `test_bootstrap_second_startup_is_idempotent`：同一业务库第二次启动必须不崩溃、不轮换 key、文件凭据仍可用（对修复前代码必现 KeyError） |

## 验证

| 项 | 结果 |
|---|---|
| 全量套件 | 380 passed, 10 skipped（含新回归用例） |
| start.bat 端到端 | 服务就绪，`GET / → 200`、`/health → {"status":"ok"}`、带 default 租户 key 的 `GET /api/tasks → 200` |
| 编码/环境排查 | start.bat 本体为 ANSI(GBK)+CRLF 无损；PATH 上的 `python` 是 anaconda base（3.8，无依赖），脚本按设计顺延选中 conda `agent-runtime` 解释器——脚本逻辑无罪，问题全在应用启动崩溃 |
