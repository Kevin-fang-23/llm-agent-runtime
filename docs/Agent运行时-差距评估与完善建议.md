# LLM Agent Runtime — 目标差距评估与完善建议

> 评估对象：`llm-agent-runtime`（本仓库）
> 评估基准：面向真实业务的可托管 Agent 运行时（自然语言下目标 → 自主规划 → 工具调用 → 多步执行 → 交付结果）
> 评估方式：静态代码审查 + 实机执行取证（非纸面评估）
> 评估环境：Windows / conda env `agent-runtime`（Python 3.11.16）
> 代码规模：`app/` 33 个 .py + `tests/` 11 个 + `scripts/` 5 个，合计约 2,984 行

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
| 6.2 | **无可观测性标准接入**：无 OpenTelemetry / Prometheus，无 trace_id 贯穿，无 LLM 调用级 span | `/api/metrics` 只有聚合 SQL |
| 6.3 | **无 CI**：`.github/` 不存在 | **本次 P0-1 的 NameError 就是没有 CI 的直接后果**——有 CI 的话 push 时即暴露。姊妹项目 campus-assistant 已有 CI 双门禁，两个项目护栏水平不对称 |
| 6.4 | **无依赖锁定** | 复现性弱，CI/部署会漂 |
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
| P2-5 | OpenTelemetry + Prometheus；结构化日志带 `task_id/trace_id` | `app/main.py`、新 `app/observability/` |
| P2-6 | Alembic 迁移 + 依赖锁定（`uv.lock` / `requirements.lock`） | 新文件 |
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







