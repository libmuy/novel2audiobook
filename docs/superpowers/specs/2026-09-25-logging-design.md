# 日志体系设计（logging design）

日期：2026-09-25 ｜ 状态：已评审（对话中逐节确认）｜ 触发事件：Web UI 上传正文后配音工作台为空，排查时发现无任何服务端日志可查

## 1. 背景与问题

一次真实排查（上传苍玄第一章 → 工作台无分块）暴露的可观测性缺口：

1. **全项目无 logging 配置**：`logging.basicConfig` 只存在于 `gpu_arbiter` 的 `__main__`，webui/CLI 进程里所有 `logger.info/debug` 被静默丢弃（root 默认 WARNING 且无 handler）；只有 4 条 `llm_parser` 的降级告警本该可见却看不到。
2. **无日志文件**：webui 的 stdout/stderr 指向启动它的终端，关终端即失；`.cache/tasks/*.log` 里 `log_path` 是创建时固化的绝对路径，项目从 `/srv/unsafe/...` 搬家后失效。
3. **API 层无请求日志**、**关键数据动作无埋点**（重新导入删除了哪些下游产物、状态标签怎么变的，事后无从得知）。
4. **前端报错只进浏览器 console**，后端不可见。
5. `print()`（CLI 结果行等）只到终端。

## 2. 目标

一次 grep `.cache/logs/n2a.log` 能串起：HTTP 请求 → 业务动作 → 状态流转 → 任务执行 → 前端报错，用 `request_id` / `task_id` 关联；`print` 也落同一文件，与终端所见一致。

## 3. 已确认的决策（用户选择）

| 决策点 | 选择 |
|---|---|
| 覆盖场景 | API 请求与状态变化 + 任务管线 + 持久化文件轮转 + 前端 console 收集（全选） |
| 级别控制 | 配置文件可调，默认 INFO，改后重启生效；不做运行时热切换、设置页不加控件 |
| API 日志粒度 | 元数据 + 请求体摘要（文本截 200 字；multipart 只记字节数） |
| 前端收集 | 自动收集 `error` / `unhandledrejection` / `console.error`，批量上报 |
| print | 进程级 tee 到日志文件（不迁移 print、不做 fd 级重定向） |
| llm_parser | 补解析过程埋点（backend/降级原因/每段耗时与重试/成功回退计数/分块数） |
| 任务归因 | 加 task_id contextvar，与 request_id 同机制 |
| 格式 | 人读友好中文文本，非 JSON 结构化（YAGNI） |

## 4. 架构

### 4.1 日志装配（新增 `src/runtime/log_setup.py`）

幂等 `setup_logging(config=None)`，`src/cli.py main()` 开头调用（覆盖 webui + 全部子命令）。

**双路 tee，与 logging 各走各路、零重复：**

```
print()         → sys.stdout(tee) ──┬→ 原始终端
                                    └→ _FileSink（唯一轮转权威）
裸写 stderr      → sys.stderr(tee) ──┬→ 原始终端
                                    └→ _FileSink
logging 记录     → _SinkHandler ───────→ _FileSink        （文件唯一一份）
                 → console handler → 原始 stderr → 终端    （绑 tee 安装前的流）
```

要点：

- **单一写入权威**：文件侧只有一个 `_FileSink`（自带锁 + 按 `max_bytes`/`backup_count` 轮转），logging 侧用 `_SinkHandler.emit()` 写入该 sink，tee 的裸写也进同一 sink——两条路径互不重复、轮转不打架。
- **console handler 绑定 tee 安装前捕获的原始 stderr**，因此 logging 输出不经过 tee，不会被抄进文件第二遍。
- **contextvar 归因**：`request_id_var` / `task_id_var` + logging `Filter` 注入 `record.request_id` / `record.task_id`（请求/任务外为空串，不占位）。格式串：
  `%(asctime)s %(levelname)s [%(name)s]%(request_id)s%(task_id)s %(message)s`
- **tee 代理** `.buffer` / `.fileno()` / `.isatty()` / `.encoding`，不破坏 tqdm、颜色输出、pytest capsys；`write` 异常一律吞掉（日志绝不打断主流程）。
- **可还原**：记录被替换的原流，`reset_logging()` 还原 sys.stdout/stderr、清 handlers、复位幂等标志（测试 teardown 用）。
- `logging.file: null` → 不装 tee、不建文件 handler，只保留终端输出。
- `logging.level` 非法 → 回退 INFO 并打一条警告，不抛错。
- 路径用 `utils.resolve_path()`（调用时动态读 PROJECT_ROOT，测试可 monkeypatch），相对项目根。

**捕获边界（明确不做）**：发给 `DEVNULL` 的子进程输出（llama-server）、直接写 fd 1/2 的 C 层输出、`setup_logging()` 之前 import 阶段的 print、tee 类未代理的 `sys.stdout.buffer` 直写字节。

### 4.2 配置

`config/global_config.yaml` 新增段（代码带默认值，老配置缺段照常工作）：

```yaml
logging:
  level: "INFO"                  # DEBUG/INFO/WARNING/ERROR；改后重启生效
  file: ".cache/logs/n2a.log"    # 相对项目根；null = 只打终端
  max_bytes: 5242880
  backup_count: 3
```

不进 `system.py` 的 PATCH 白名单、设置页不加控件（避免"保存成功但没生效"）。`.gitignore` 补 `.cache/logs/`。

### 4.3 请求日志中间件（新增 `src/api/middleware.py`）

**纯 ASGI**（非 `BaseHTTPMiddleware`）：`events.py` 的 SSE 是流式响应，纯 ASGI 只旁路观察、不碰响应体。

- 生成 8 位 hex `request_id` → `scope["state"]` + `request_id_var`（下游路由/领域层日志自动携带）。
- 包装 `receive` 旁路计数：累计总字节 + 保留前 2KB 前缀，**不缓存整包**（角色参考音频是 MB 级）。
- 日志行（logger `n2a.access`）：

  ```
  REQ PUT /api/novels/nv_cang_xuan/chapters/ch_0001/raw?confirm=true -> 200 15ms | body(multipart/form-data, 11388B)
  REQ PATCH /api/novels/n/chapters/c/segments/s012 -> 200 6ms | body(application/json, 96B): {"speaker":"lin_dong"}
  ```

- 摘要规则：JSON/表单/文本 → 解码前缀、压换行、截 200 字；multipart → 只记类型+字节数；无 body → 省略 `|` 段。
- 级别：≥500 ERROR，≥400 WARNING，其余 INFO；**非 `/api` 路径（静态）与 `/api/events`（SSE 长连接）一律 DEBUG**（默认 INFO 下不可见）。
- 不记录请求头（含潜在敏感头）。

### 4.4 业务事件埋点（打在领域层/收口点，API 与 CLI 同样产出）

| # | 位置 | 事件 |
|---|---|---|
| 1 | `library.py import_chapter_raw` | 重导入：删除下游产物清单、保留缓存数、写入字节数（触发本次设计的关键一条） |
| 2 | `chapters.py upload_raw` 新章分支 | 首次写入正文 novel/chapter/字节数 |
| 3 | `library.py promote_draft_to_final` | 草稿转正 draft→final |
| 4 | `novels.py delete_node / delete_novel` | 删除对象、章节目录是否移入 `.trash/` |
| 5 | `segments.py update_segment / batch` | 变更字段列表 + text 前 80 字摘要（不记全文） |
| 6 | `task_queue.py submit / 终态` | 任务索引行：提交（type/scope）、完成/失败（耗时/错误） |
| 7 | `utils.py update_chapter_status` | 章节状态流转：`.status.json` 旧标签 → 新标签（状态类问题单一收口点） |
| 8 | `llm_parser.py` | backend 选中与降级原因、每段耗时与重试（DEBUG）、LLM 成功 vs 回退计数、分块数（INFO 摘要） |

### 4.5 任务执行归因与任务日志修复（`task_queue.py`）

- `_execute_task` 执行期设置 `task_id_var` → 该任务内所有 logger 输出自动带 `[tsk_...]`。
- `TaskContext.log` 除写任务自己的 `.log` 文件外，同时 `logger.info(msg)` → 统一文件成为唯一事实源。
- **路径修复**：读（`read_log`）、删、写三处一律现场派生 `os.path.join(self.tasks_dir, f"{task.id}.log")`，不再信任任务 JSON 里序列化的 `log_path`（修复搬家后旧任务日志读不出）；字段保留仅作展示。

### 4.6 前端收集

- `src/web/static/js/core.js`：IIFE 内安装（无构建工具约定）。钩 `window` 的 `error`/`unhandledrejection` + 包装 `console.error`（保留原行为；`log/info` 不收）。
- 去重合并（同消息计数，distinct ≤50）、≥10 条或 2 秒批量 flush、每批 ≤20 条、msg ≤500 字、stack ≤1000 字；**防递归**（上报端点自身的失败直接丢弃）；**熔断**（连续 5 次失败冷却 60 秒）。
- 后端新增 `src/api/routers/logs.py`：`POST /api/frontend-logs`，body ≤64KB、批 ≤20 条校验（非法 400），进程内滑动窗口限流 30 批/分钟（超限 429），以 `logger("frontend")` 写同一文件，行首 `FRONTEND`，error→ERROR 其余→INFO。

## 5. 测试与验收

**自动化（pytest，随 `./run.sh test --all` 回归）**

- 新 `tests/test_log_setup.py`：文件创建、print 落盘、logging 在文件中**恰好一份**、`file: null` 不动系统流、非法 level 回退、幂等、轮转触发、`reset_logging()` 还原。
- 新 `tests/test_request_logging.py`：元数据齐全（方法/路径/状态/耗时/request_id）、JSON 摘要、multipart 只记字节数、4xx→WARNING、静态与 SSE 在 INFO 不可见。
- 新 `tests/test_frontend_logs_api.py`：合法批次落日志（`FRONTEND` 行）、截断、超批 400、限流 429。
- 补 `tests/test_task_queue.py`：`log_path` 指向失效旧绝对路径时 `read_log` 按当前 tasks_dir 找回。
- 补 `tests/test_library.py` 等：重导入删除清单日志、状态流转日志（caplog 断言）。

**手动验收**

1. 启动 webui → `.cache/logs/n2a.log` 出现启动行与 uvicorn access 行。
2. Web UI 上传正文 → 文件出现 `REQ PUT .../raw`、`删除下游产物=[script_draft.json]`、`章节状态流转`。
3. 浏览器 console 触发错误 → 2 秒内文件出现 `FRONTEND` 行。
4. `logging.level` 改 DEBUG 重启 → 静态/SSE/body 细节可见。
5. CLI 跑 `./run.sh status` / 一次任务 → 同样进文件；print 结果行也在。

## 6. 明确不做（范围外）

- 不迁移 110 处 `print()`（tee 已覆盖文件落盘；表格/结果行本就是终端展示）。
- 设置页日志控件、运行时热切换。
- JSON 结构化日志、远程日志收集。
- 进程/fd 级 stdout 重定向（复杂度高且与 tee 方案收益重叠）。
- **不修**本次事件暴露的业务 bug（`import_chapter_raw` 不重置 `.status.json` 致状态标签过期、上传后不自动解析）——本设计保证其在日志中**可见**，修复另开任务。
