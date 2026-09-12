# 计划 003: 常驻 TTS 服务 + 显式 GPU 换手确认

> **状态：已实现**（见 `tools/gpu_arbiter.py`、`tools/indextts_infer.py`、
> `src/tts_daemon.py`、`cli.py` 及对应测试）。本文档在开工前只以「阶段 B」
> 的形式存在于会话记录里，这里是补写的实现记录，供计划 002（Gradio 界面）
> 的 Tab 4 直接消费。

## 背景

计划 002 的 Tab 4「TTS 试听」每点一次都会走 `IndexTTSBackend.synthesize_batch`
起一个全新子进程——`IndexTTS2.__init__` 无条件加载 Wav2Vec2Bert、CAMPPlus、
semantic_codec、s2mel、BigVGAN、GPT，耗时数十秒；更麻烦的是
`LlmSuspendedForGpu` 会在每次调用前后杀掉/重启 llama-server。交互式调音色
的场景下这个代价不可接受。

同时用户明确要求：**GPU 换手不做自动仲裁，由用户当场决定**——把"现在显存
被谁占用""接下来要不要换手、换手要多久"变成显性信息，而不是像原来的
`LlmSuspendedForGpu` 那样悄悄停/悄悄起。

## 方案

### 1. 显式 owner 模型（`tools/gpu_arbiter.py`）

新增只读探测 + 描述性 API，**不做任何自动执行**：

- `get_current_owner(config=None) -> "llm" | "tts" | None`：
  `"tts"`（常驻服务）靠 pidfile 探测（`.cache/tts_daemon/state.json` 记录
  的 PID 是否还活着）；`"llm"` 靠现有的 `is_server_up()` 探测端口；
  两者都命中时优先 `"tts"`（本项目自己管的 pidfile 比探测外部
  `ai llm serve` 命令的端口更可靠）。不探测 `"assets"`——ACE-Step/TangoFlux
  是一次性子进程，没有常驻状态可查。
- `plan_swap(target_owner) -> dict`：无副作用，返回
  `{current_owner, target_owner, noop, steps: [...], estimated_seconds}`。
  `estimated_seconds` 来自真实历史记录（`.cache/gpu_arbiter/swap_history.json`），
  从不编造数字——没有历史数据就返回 `None`，调用方应展示"预计耗时未知"
  而不是拿超时上限充数。

### 2. 换手的实际执行者分散在两处（对原计划文字的修正）

草案曾设想在 `gpu_arbiter.py` 里放一个统一的 `execute_swap()`。实现时发现
这会让 `tools/` 反向依赖 `src/`（`gpu_arbiter` 需要知道怎么启动
`IndexTTSDaemon`），与项目里 `tools/` 是独立推理环境脚本、`src/` 是主 venv
业务逻辑的既有边界冲突。改为：

- **swap → tts**：`src.tts_daemon.IndexTTSDaemon.ensure_started()`
- **swap → llm**：同一个类的 `.shutdown()`

两者各自负责调用 `gpu_arbiter.stop_llama_server`/`start_llama_server`，并把
真实耗时记进 `gpu_arbiter.record_swap_seconds()`（key 形如 `"llm->tts"` /
`"none->tts"` / `"tts->llm"`）。`plan_swap()` 只读这些 key，不执行。

顺带给现有的 `LlmSuspendedForGpu`（tts 一次性批处理、assets 批处理都在用，
**行为完全不变**）加了两个独立的耗时记录点——`"llm_stop"` / `"llm_start"`，
用于 `cli.py tts` 命令换手确认时给用户一个真实数字。

### 3. 常驻子进程与 Unix socket 协议（`tools/indextts_infer.py`）

`tools/indextts_infer.py` 新增 `--serve --socket <path>` 模式，与原有的
`--jobs-file/--result-file` 一次性批量模式共用同一份合成循环
（`_run_batch()`，含 embedding 缓存加载/落盘逻辑，避免两份容易分歧的实现）。

协议：逐个接受 Unix socket 连接，一行 JSON 请求 → 一行 JSON 响应 → 关闭连接
（不并发处理——GPU 上的合成本来就该串行）：
```
{"cmd": "ping"}                        -> {"ok": true}
{"cmd": "synthesize_batch", "jobs": [...]} -> {"ok": true, "results": {...}}
{"cmd": "shutdown"}                    -> {"ok": true}（响应后进程退出）
```
`jobs` 与一次性批量模式的 job 字典结构完全相同（id/text/ref_audio/lang/
emo_vector/duration_factor/out）。

### 4. `src/tts_daemon.IndexTTSDaemon`

- `ensure_started()`：已在运行则幂等直接返回；否则记录换手前 owner、
  按需停 llama-server、`Popen` 拉起 `--serve` 子进程、轮询 socket 直到
  能应答 `ping` 或超时（`tts.index_tts.daemon_startup_timeout_sec`，
  默认 180s）。超时/加载失败会 kill 子进程、按需恢复 llama-server，
  不留下 pidfile。
- `shutdown()`：请求子进程优雅退出（连不上就轮询 PID 后 kill 兜底），
  若启动时为了腾显存停过 llama-server 就在这里恢复。**不做空闲超时
  自动调用**——必须由用户主动触发（CLI 的 `tts-serve stop` 或未来 webui
  的按钮）。
- `synthesize_batch(jobs)`：入参/返回形状与
  `src.tts_engine.IndexTTSBackend.synthesize_batch` 一致，内部复用
  `EMOTION_TO_VECTOR`/`speed_to_duration_factor`，不重复维护第二份映射表。

### 5. CLI 集成

- 新增 `cli.py tts-serve {start,stop,status}` 子命令，脱离 webui 单独调试。
  `start` 在会产生实际换手代价时（`plan_swap` 非 noop）、交互式终端下、
  且没传 `--yes` 时，打印步骤+预计耗时并等待确认。
- `cli.py parse`/`cli.py tts` 各自加了一道换手前置检查（都新增 `--yes`）：
  - `parse` 依赖 llama-server：常驻服务正占着显存时，交互式下先问一句
    是否释放，非交互式/`--yes` 直接释放（`_release_tts_daemon_if_running`）。
  - `tts` 的批量链路本身**不变**（仍是 `LlmSuspendedForGpu` 自动停/起），
    只是交互式终端下、真的会触发换手时，先告知一声大概要停多久
    （`_confirm_batch_llm_swap`），避免用户在不知情的情况下让 llama-server
    被停用一整个批次的时长（单章可能耗时 65-70 分钟）。
  - 判断是否"交互式"统一用 `sys.stdin.isatty()`：管道/脚本/CI 下沿用
    一直以来的自动化行为，不阻塞。

### 6. 运行时状态文件

- `.cache/tts_daemon/state.json`：`{pid, socket_path, llm_was_running, started_at}`
- `.cache/tts_daemon/daemon.sock`：Unix socket
- `.cache/gpu_arbiter/swap_history.json`：`{"llm_stop": 5.2, "llm_start": 12.1, "llm->tts": 47.3, "tts->llm": 41.0, ...}`

三者均加入 `.gitignore`（本机运行时状态，不随代码分发）。

## 验证

- 单元测试（全部 mock/monkeypatch，不碰真实 GPU/llama-server/socket）：
  `tests/test_gpu_arbiter.py`（19 项：owner 探测、换手历史、plan_swap 描述）、
  `tests/test_tts_daemon.py`（16 项：启动/关闭的各分支、job 转换）、
  `tests/test_cli_gpu_swap.py`（19 项：交互式/非交互式/`--yes` 的确认分流）。
- CPU-only 冒烟测试（用假的 `tts` 桩对象，不加载真实模型，安全地在
  llama-server 真实运行时执行）：验证 embedding 缓存的加载/失效/落盘
  （见计划 001）、验证 `--serve` 的 socket 协议全流程（ping/合成/未知
  命令/shutdown）。
- `python cli.py test --all`：绿；完整 `pytest tests/`：281 passed。
- **未做（需要真实 GPU 环境）**：没有针对真实 IndexTTS2 模型跑一次
  `cli.py tts-serve start` 端到端验证——实现时机器上 llama-server 正在
  运行，为避免打断线上服务未执行，需要用户在合适时机手动跑一遍并确认：
  - `python cli.py tts-serve start` 能正确停掉 llama-server、加载模型、
    落盘 pidfile
  - 期间 `python cli.py parse --chapter 0001` 能提示换手而不是直接失败
  - `python cli.py tts-serve stop` 能正确恢复 llama-server

## 文件清单

| 文件 | 操作 |
|------|------|
| `tools/gpu_arbiter.py` | 修改：owner 探测 + 换手历史 + `plan_swap()` |
| `tools/indextts_infer.py` | 修改：新增 `--serve` 常驻模式，批量循环重构为共用 `_run_batch()` |
| `src/tts_daemon.py` | 新建：`IndexTTSDaemon` |
| `cli.py` | 修改：新增 `tts-serve` 子命令；`parse`/`tts` 加换手前置确认 |
| `.gitignore` | 修改：新增 `.cache/tts_daemon/`、`.cache/gpu_arbiter/` |
| `tests/test_gpu_arbiter.py` | 新建 |
| `tests/test_tts_daemon.py` | 新建 |
| `tests/test_cli_gpu_swap.py` | 新建 |
