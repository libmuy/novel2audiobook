# 计划 005: 后台任务队列 + GPU 安全网 + FastAPI 后端

> **状态：未开始**
>
> 前置：`docs/plan/004-multi-novel-library.md` 必须已完成并验收通过。
> 后续：`docs/plan/006-web-frontend.md`。

## 目标

在 004 的多小说数据层之上，补齐三样东西：

1. **后台任务队列**：批量解析 / 批量 TTS / 批量混音可以提交到后台跑，能看进度、能取消、能查日志。
2. **GPU 安全网**：保证任何时刻只有一个任务占用显卡，并且进程被强杀后 llama-server 不会永久停摆。
3. **FastAPI 后端**：把上面这些能力和 004 的数据层暴露成 REST API + SSE 事件流。

本阶段**仍然不产出正式界面**，全部用 pytest、curl 和 httpx TestClient 验收。
`cli.py webui` 会重新出现，但打开只能看到一个占位页面。

## 背景

现在项目里**完全没有**任何线程或任务队列代码。被删掉的 Gradio 界面靠
`concurrency_id="gpu", concurrency_limit=1` 让 Gradio 自己的队列做串行化，
而且**没有任何取消机制**——一旦点下「全部 TTS」，就只能等它跑完或者杀进程。

而这个项目的批量任务动辄跑几个小时（`global_config.yaml` 里 IndexTTS 的
`timeout_sec` 是 10800，注释说单章 121 句约 65-70 分钟），没有取消能力是不可接受的。

### 为什么不引入 Celery / RQ / Huey

- **GPU 任务的真实并发度恒等于 1**（见下面「GPU 单 worker 是硬约束」），
  所谓「队列」实质就是一个 worker 线程加一个 FIFO，一百多行就写完了。
- 这些库都是为分布式多机设计的，Celery/RQ 要 Redis broker，Huey/Dramatiq 要额外的进程模型。
  这里是单机、单用户、单进程。
- 更关键的是，「取消」的本质是 kill 掉 IndexTTS 子进程组并保证 llama-server 被恢复，
  这段逻辑跟 `tools/gpu_arbiter.py` 深度耦合，任何通用任务库都帮不上忙，只会挡路。

---

## 执行顺序

- [ ] 任务 1：`tools/gpu_arbiter.py` 孤儿恢复（安全网，先做）
- [ ] 任务 2：三个流水线函数加 `progress_cb` / `should_cancel`
- [ ] 任务 3：`src/task_queue.py` 任务队列
- [ ] 任务 4：`src/monitor.py` 资源监控
- [ ] 任务 5：`src/derived_index.py` 角色引用索引
- [ ] 任务 6：`src/preflight.py` 批量预检
- [ ] 任务 7：`src/api/` FastAPI 应用
- [ ] 任务 8：`cli.py webui` 重新接上 + `global_config.yaml` 补全

---

## 任务 1：GPU 孤儿恢复

### 为什么必须先做这个

`tools/gpu_arbiter.py:112 LlmSuspendedForGpu` 是个上下文管理器：进入时停掉 llama-server 腾显存，
退出时把它拉回来。它有两个致命问题，现在都没有防护：

1. **不可重入。** 两个 TTS 任务并发时，后进入的那个探测到 llama-server 已经停了，
   `_was_running` 记成 False；先结束的那个会在另一个还在跑 IndexTTS 的时候
   把 llama-server 拉回来，两边一起抢显存，直接爆。
   → 这条靠任务 3 的「GPU lane 严格单 worker」解决。
2. **进程被强杀就泄漏。** 正常的协作式取消是在 `with` 块内返回或抛异常，`__exit__` 一定会执行，
   不会泄漏。但如果整个进程被 `kill -9`（或者机器断电），llama-server 就永久停在那里了，
   下次 parse 会莫名其妙走降级路径，用户根本不知道为什么。
   → 这条要靠下面的落盘标记解决。

### 改动

在 `tools/gpu_arbiter.py` 里新增：

```python
LLM_SUSPENDED_PATH = os.path.join(_CACHE_DIR, "gpu_arbiter", "llm_suspended.json")
# 内容：{"pid": 12345, "since": "2026-09-13 10:00:00", "model_registry_name": "qwen3.8-27b-unsloth"}


def recover_orphaned_suspension(config: dict = None) -> dict:
    """
    检查是否存在「llama-server 被某个已经死掉的进程停用后没人负责恢复」的孤儿状态。
    存在就把 llama-server 拉回来并清掉标记文件。
    返回 {"recovered": bool, "reason": str}。
    只在服务启动/关闭这类明确的时机调用，不要做成后台轮询。
    """
```

修改 `LlmSuspendedForGpu`：
- `__enter__` 里，成功停掉 llama-server 之后立刻原子写 `LLM_SUSPENDED_PATH`
  （记 `os.getpid()`、时间、`model_registry_name`）。
- `__exit__` 里，无论恢复成功与否都删掉这个文件（用 `try/except` 包住，
  删不掉不要影响主流程）。

`recover_orphaned_suspension` 的判定逻辑：
- 文件不存在 → 无事发生。
- 文件存在但 `is_pid_alive(pid)` 为 **True** → 有活着的进程正持有，不要动它，返回未恢复。
- 文件存在且 pid 已死 → 调 `start_llama_server(...)` 恢复，清文件，返回已恢复。

### 测试

`tests/test_gpu_arbiter.py` 新增用例（全部 monkeypatch 掉真实的 `subprocess` 和 HTTP 探测）：
- `__enter__` 后标记文件存在、内容含当前 pid
- `__exit__` 后标记文件被删除
- 块内抛异常时标记文件同样被删除
- `recover_orphaned_suspension`：pid 已死 → 调了 `start_llama_server` 且文件被清
- `recover_orphaned_suspension`：pid 还活着 → **没有**调 `start_llama_server`

---

## 任务 2：三个流水线函数加进度与取消

给三个函数各加两个**关键字参数，默认 None**。默认 None 时行为必须与现在逐字节一致，
这样 004 留下的全部测试不用改一行。

```python
progress_cb: callable = None     # progress_cb(done: int, total: int, message: str) -> None
should_cancel: callable = None   # should_cancel() -> bool；返回 True 表示请求取消
```

取消的语义统一是「**当前这一批跑完就停**」，不是「立刻中断」。
被取消时抛出新异常 `TaskCancelled`（定义在 `src/task_queue.py`，
但为避免循环导入，实际放在新文件 `src/pipeline_errors.py` 里，三个流水线模块都从这里导入）。

### 2.1 src/llm_parser.py

`parse_text_to_json`（`:262`）的 `for para in paragraphs`（`:283`）天然是「一段一次 LLM 请求」，
进度点和取消检查点直接放在循环开头：

```python
for idx, para in enumerate(paragraphs):
    if should_cancel and should_cancel():
        raise TaskCancelled(f"用户取消（已完成 {idx}/{len(paragraphs)} 段）")
    ...
    if progress_cb:
        progress_cb(idx + 1, len(paragraphs), f"已解析 {idx + 1}/{len(paragraphs)} 段")
```

`process_chapter_parse`（`:318`）把两个参数原样透传下去。

### 2.2 src/tts_engine.py —— 本任务最需要小心的地方

`generate_tts_incremental`（`:209`）天然分三遍：

- **第一遍**（算 md5、收集 `pending_jobs`）跑完后回调一次总数：
  `progress_cb(0, len(script_final_data), f"共 {len(script_final_data)} 句，需新合成 {len(pending_jobs)} 句")`
- **第二遍**（`backend.synthesize_batch`）是唯一耗时的地方，改动见下。
- **第三遍**（读 wave 拼时间线）很快，不需要进度。

#### ⚠️ 切批只能在常驻 daemon 后端上做

现在 `:245` 是把整批 `batch_jobs` 一次性丢给 `backend.synthesize_batch()`。
要拿到取消粒度和进度，直觉上应该切成小批循环调用——**但对
`IndexTTSBackend` 这么做会是灾难**：它是一次性子进程实现，每调一次
`synthesize_batch` 就重新拉起一个 Python 进程、重新把 IndexTTS2 模型加载进显存。
一章切成 15 批，就要重载 15 次模型，65 分钟能变成 3 小时。

所以必须按后端类型分支，**这个判断要写死并且加测试**：

```python
# 常驻 daemon 模型常驻显存，切批的额外成本只有 socket 往返，可以放心切；
# 一次性子进程后端每批都要重载一遍 IndexTTS2 模型，切批会让耗时成倍膨胀，
# 因此整批下发，只能在批与批之间（也就是整章结束时）响应取消。
supports_chunking = getattr(backend, "supports_chunking", False)
chunk_size = chunk_size or config.get("server", {}).get("gpu_chunk_size", 8)
```

在 `src/tts_daemon.py` 的 daemon 后端类上加类属性 `supports_chunking = True`；
`IndexTTSBackend` 和 `MockTTSBackend` 保持默认的 False
（Mock 很快，不需要切；测试里想验证切批逻辑就临时给 Mock 打上 True）。

切批时**必须沿用现有的排序顺序再切窗口**：`:252` 按 `role_cfg["reference_audio"]` 排序，
是为了让同一个角色的任务连续下发、命中 IndexTTS 的音色缓存（那里的注释解释了为什么）。
对**已排序**的列表切连续窗口，最多在每个批次边界多出一次角色切换，代价可以忽略；
但如果先切再排、或者按 seg_id 顺序切，这个优化就整个作废了。

```python
for i in range(0, len(batch_jobs), chunk_size):
    if should_cancel and should_cancel():
        raise TaskCancelled(f"用户取消（已合成 {i}/{len(batch_jobs)} 句）")
    chunk = batch_jobs[i:i + chunk_size]
    results = backend.synthesize_batch(chunk)
    ...
    if progress_cb:
        progress_cb(i + len(chunk), len(batch_jobs), ...)
```

**取消是安全的**：已经合成出来的 wav 留在 `audio_cache/` 里（按内容 md5 命名），
下次重跑自动命中缓存续上，不会白干。这一点要写进日志告诉用户。

#### 子进程要能被杀掉

`IndexTTSBackend.synthesize_batch`（`src/tts_engine.py:165` 附近）现在用
`subprocess.run(...)`，一跑最长 10800 秒，期间无法干预。改成：

```python
proc = subprocess.Popen([...], start_new_session=True, ...)
self._current_proc = proc          # 暴露给任务队列，取消时用
try:
    out, err = proc.communicate(timeout=timeout_sec)
finally:
    self._current_proc = None
```

配套加方法：

```python
def terminate_current(self):
    """取消任务时由 TaskQueue 调用。先 terminate 整个进程组，宽限 5 秒再 kill。
    用 os.killpg 而不是 proc.terminate()——start_new_session=True 起的是独立进程组，
    IndexTTS 内部可能还有子进程。"""
```

### 2.3 src/audio_mixer.py

`mix_chapter` **不加进度回调**（单章混音只要几十秒，加了是过度设计）。
只在任务队列层面、批量任务的章与章之间检查取消。

---

## 任务 3：src/task_queue.py

新建 `src/task_queue.py`。纯标准库（`threading` / `queue` / `dataclasses` / `json`），
不引入任何第三方任务库。

### 双 lane 模型

| lane | worker 数 | 承载的任务 | 原因 |
|---|---|---|---|
| `gpu` | **恒为 1，不可配置** | `parse` / `tts` / `precompute_embedding` | 见任务 1 的第 1 点 |
| `cpu` | `server.cpu_workers`，默认 2 | `mix` / `import` | 纯 CPU/ffmpeg，可以并行 |

这样能一边混音第 N 章、一边 TTS 第 N+1 章，系统配置页里的「后台并发任务数」才有真实含义。

**GPU worker 的启动处要写死 assert 并配注释**：

```python
assert lane != "gpu" or worker_count == 1, (
    "GPU lane 必须严格单 worker：LlmSuspendedForGpu 不可重入，"
    "两个并发 TTS 会让先结束的那个在另一个还在跑时把 llama-server 拉回来抢显存"
)
```

### 数据结构

```python
@dataclass
class Task:
    id: str                # tsk_<unix毫秒>_<6位随机>
    type: str              # parse | tts | mix | precompute_embedding | import
    lane: str              # gpu | cpu
    novel_id: str
    chapter_id: str = None
    group_id: str = None   # 批量任务的分组 ID，形如 grp_<...>
    params: dict = None
    state: str = "queued"  # queued | running | succeeded | failed | cancelled
    progress: dict = None  # {"done": 0, "total": 0, "phase": "", "message": ""}
    created_at: str = None
    started_at: str = None
    finished_at: str = None
    error: str = None
    log_path: str = None
```

`group_id` 承载「整卷解析」这类批量操作：一次请求展开成 N 个单章 task，
界面上按 group 聚合显示进度，取消 group 就是逐个取消它下面的 task。

### 类骨架

```python
class TaskQueue:
    def __init__(self, config=None, tasks_dir=None, library_dir=None)
    def start(self) -> None            # 拉起 worker 线程（daemon 线程）
    def stop(self, wait=True) -> None  # 停止 worker，等待当前任务收尾

    def submit(self, type, novel_id, chapter_id=None, group_id=None, params=None) -> Task
    def submit_batch(self, type, novel_id, chapter_ids, params=None) -> tuple  # (group_id, [Task])

    def cancel(self, task_id) -> bool
    def cancel_group(self, group_id) -> int

    def get(self, task_id) -> Task
    def list(self, state=None, group_id=None, limit=200) -> list
    def read_log(self, task_id, offset=0) -> tuple      # (文本, 新的 offset)

    def add_listener(self, fn) -> None      # fn(event: dict)，用于 SSE 推送
    def remove_listener(self, fn) -> None

    def recover_on_startup(self) -> int     # 见下面「重启残留」
```

任务处理函数用注册表分发，不要写成一长串 if/elif：

```python
TASK_HANDLERS = {
    "parse": _handle_parse,
    "tts": _handle_tts,
    "mix": _handle_mix,
    "precompute_embedding": _handle_precompute_embedding,
}
# 每个 handler 签名：_handle_xxx(task: Task, ctx: TaskContext) -> None
# ctx 提供 ctx.log(msg) / ctx.progress(done, total, msg) / ctx.should_cancel()
```

### 取消机制

协作式。每个 task 一个 `threading.Event` 作为 CancelToken，存在 TaskQueue 的内存字典里
（不落盘，进程重启后本来也没有 running 任务了）。

- `state == "queued"` 时取消：直接把状态改成 `cancelled`，worker 取到时跳过。
- `state == "running"` 时取消：置 Event。worker 注入给流水线的 `should_cancel`
  就是 `token.is_set`，流水线在段落/批次边界抛 `TaskCancelled`。
- 如果当前跑的是 `IndexTTSBackend`（一次性子进程，不支持切批），
  额外调它的 `terminate_current()` 直接杀子进程组——否则最坏要等一整章。
- daemon 后端的 `synthesize_batch` 是阻塞 socket 请求，本身无法中断，
  靠 `gpu_chunk_size`（默认 8 句）把最坏等待压到一两分钟。
  给 daemon 加 `{"cmd": "cancel"}` 协议列为后续迭代，本阶段不做。

### 持久化

- 状态：`.cache/tasks/<task_id>.json`。每次状态变更都原子写（tmp + `os.replace`）。
- 日志：`.cache/tasks/<task_id>.log`，append-only 纯文本，每行
  `[HH:MM:SS] 内容`。前端按字节 offset 增量拉取。
- 列表查询：`glob` + 内存缓存，**不要再建一个 index.json**——多写者并发更新索引文件
  是纯粹的自找麻烦，几百个任务 glob 一下毫秒级。
- 启动时清理超过 `server.task_retention_days`（默认 7 天）的任务记录。

### 重启残留

`recover_on_startup()`：扫描所有 `state == "running"` 的任务记录，
一律改写为 `failed`，`error` 记「服务重启中断」。

**不要自动续跑。** 流水线本身是幂等的（`audio_cache` 按内容 md5 命名，已合成的算力都保住了），
让用户自己决定要不要重点一次，比替他做决定安全。

### 事件推送

`add_listener` 注册的回调会在任务状态/进度变化时被调用。
API 层注册的那个回调必须用 `loop.call_soon_threadsafe` 把事件投递进 asyncio 队列
——**worker 是普通线程，直接碰 asyncio 对象会出问题**。

进度事件要节流：同一个 task 的 `progress` 变化最多 500ms 推一次，
状态变化（queued→running→succeeded 等）立即推。

### tests/test_task_queue.py 必须覆盖

全部用 Mock 后端和 `tmp_path`，**不允许碰真实 GPU**。

- 提交单个任务 → 跑完 → 状态 succeeded，日志文件有内容
- handler 抛异常 → 状态 failed，`error` 字段有信息，**不影响后续任务继续跑**
- 取消 queued 状态的任务 → 状态 cancelled，handler 从未被调用
- 取消 running 状态的任务 → handler 收到 `should_cancel() == True`，状态变 cancelled
- `submit_batch` 返回的 group 下所有 task 共享同一个 group_id
- `cancel_group` 能一次取消整组
- GPU lane 同一时刻只有一个任务在 running（用一个会 sleep 的假 handler 验证）
- CPU lane 能并发（`cpu_workers=2` 时两个任务同时 running）
- `recover_on_startup` 把残留的 running 改成 failed
- listener 能收到状态变化事件

---

## 任务 4：src/monitor.py

零依赖实现，**不要装 psutil**。

```python
def detect_drm_card() -> str
    """扫 /sys/class/drm/card*/device/，返回第一张有 gpu_busy_percent 文件的卡路径。
    找不到返回 None（比如跑在没有独显的机器上），上层要能优雅降级。"""

def read_gpu(card_path: str = None) -> dict
    """读三个 sysfs 文件，返回
    {"busy_percent": int, "vram_used_bytes": int, "vram_total_bytes": int}。
    读不到任何一个就返回 None。"""

def read_cpu_percent() -> float
    """读 /proc/stat 的第一行，与模块内保存的上一次快照做差分。
    第一次调用没有基准，返回 0.0。"""

def read_memory() -> dict
    """读 /proc/meminfo，返回 {"used_bytes": ..., "total_bytes": ...}。
    used = MemTotal - MemAvailable（不是减 MemFree，那个数字会吓人）。"""

def snapshot(config: dict = None) -> dict
    """汇总上面三个，返回给前端资源条的完整结构。"""
```

本机实测数据源（AMD RX 7900 XTX，只需支持这一张卡）：

```
/sys/class/drm/card1/device/gpu_busy_percent      -> 0-100 的整数
/sys/class/drm/card1/device/mem_info_vram_used    -> 字节数
/sys/class/drm/card1/device/mem_info_vram_total   -> 25753026560（约 24GB）
```

card 号从 `server.drm_card` 配置读，为 null 时调 `detect_drm_card()` 自动探测。
**不要 fork `rocm-smi` 子进程**——1 秒刷一次的话，读 sysfs 是微秒级，
起子进程是几十毫秒级，差三个数量级。

测试用 `tmp_path` 造假的 sysfs 目录结构注入，不依赖真实硬件。

---

## 任务 5：src/derived_index.py

角色引用统计（「该角色被多少小说、多少分块引用」）是唯一需要跨章节聚合的功能。
用一个**可随时删除、删了自动重建**的派生索引来做，不引入数据库。

```python
ROLE_REFS_PATH = ".cache/index/role_refs.json"

def compute_fingerprint(library_dir=None) -> str
    """把所有 library/*/chapters/*/script_final.json 的
    (相对路径, mtime, size) 三元组排序后拼起来算 md5。
    只 stat 不读内容——几千个文件也是毫秒级。"""

def build_role_refs(library_dir=None) -> dict
    """全量扫描，统计每个 role_id 的引用情况。只统计 script_final.json，
    script_draft.json 是未定稿的草稿不算数。返回：
    {"fingerprint": "...",
     "roles": {"su_yan": {"novels": ["nv_xianni"], "segment_count": 87,
                          "by_novel": {"nv_xianni": 87}}}}"""

def get_role_refs(library_dir=None) -> dict
    """读缓存文件，比对 fingerprint；不一致或文件不存在就调 build_role_refs 重建并落盘。"""

def invalidate() -> None
    """直接删掉缓存文件。所有写 script_final.json 的 API 路径都要调这个。"""
```

测试要覆盖：删掉缓存文件后 `get_role_refs` 能自动重建、
改动某个 `script_final.json` 后 fingerprint 变化触发重建、
未绑定（`speaker` 为 null）的分块不计入任何角色的引用数。

---

## 任务 6：src/preflight.py 批量预检

用户点「批量生成人声」之前，先扫一遍要动到哪些东西，弹一次确认。
**只做一次性汇总确认，不做逐个确认**——后台任务跑到一半挂起等用户点弹窗，
在异步队列里是个大坑，收益却很小。

```python
def preflight(task_type, novel_id, chapter_ids, library_dir=None) -> dict
    """返回：
    {"chapters": [{"chapter_id": "ch_0001", "action": "create|overwrite|skip",
                   "reason": "已有成品 MP3，将被重新生成"}],
     "summary": {"create": 8, "overwrite": 3, "skip": 1},
     "invalidated_cache_count": 0,
     "estimated_gpu_minutes": None}"""
```

判定逻辑直接复用 `src/status_tracker.py` 的 `get_all_chapters_status`，不要另写一套。

### 措辞要贴合实际语义

因为 TTS 是按内容 md5 增量的，**分块 wav 天然幂等**（命中就复用，不会重复合成），
真正会被「覆盖」的只有 `timeline.json` 和成品 MP3。所以预检要报的是：

> 本次涉及 12 章：新生成 8 章，3 章已有成品将被重新生成，1 章无需处理。

而不是列一堆 wav 文件名。**唯一一种会大规模作废 audio_cache 的操作是「重新导入章节正文」**
——正文一变，`calculate_md5(speaker_text_emotion)` 全变，整章缓存作废。
这种情况 `invalidated_cache_count` 要如实报出来。

### estimated_gpu_minutes 的纪律

`tools/gpu_arbiter.py:237 get_expected_swap_seconds` 有一条明确的注释：没记录过就返回 None，
调用方不许编造数字。**这里沿用同样的纪律。**

新增 `.cache/tts_stats.json` 记录「每句合成耗时」的滑动平均（在 TTS handler 里更新）。
有历史数据才给估算，没有就返回 None，界面显示「未知（尚无历史数据）」。
不要用固定系数瞎猜。

---

## 任务 7：src/api/ FastAPI 应用

### 文件结构

```
src/api/__init__.py
src/api/app.py              # create_app() + lifespan
src/api/deps.py             # TaskQueue / config 的单例
src/api/schemas.py          # pydantic 请求/响应模型
src/api/routers/novels.py
src/api/routers/chapters.py
src/api/routers/segments.py
src/api/routers/roles.py
src/api/routers/tasks.py
src/api/routers/system.py   # 配置 / GPU / 素材库
src/api/routers/events.py   # SSE
```

### lifespan 必须做的四件事

```python
@asynccontextmanager
async def lifespan(app):
    # 启动
    gpu_arbiter.recover_orphaned_suspension(config)   # 捡回被强杀进程遗留的 llama-server
    queue.recover_on_startup()                        # 残留的 running 任务标记为 failed
    queue.start()
    yield
    # 关闭
    queue.stop(wait=True)
    gpu_arbiter.recover_orphaned_suspension(config)   # best-effort 再兜一次
```

### 路由清单

所有路由前缀 `/api`。响应统一用 pydantic 模型，错误用 `HTTPException`。

**小说**
- `GET /novels` → 列表
- `POST /novels` 入参 `{title, description, levels:{part,volume}}` → `{novel_id}`
- `GET /novels/{nid}` / `PATCH /novels/{nid}` / `DELETE /novels/{nid}`

**目录树**
- `GET /novels/{nid}/tree` → 树结构，每个 chapter 节点内联它的 status（复用
  `get_novel_status_summary`），前端一次请求就能画出带状态标记的完整树
- `POST /novels/{nid}/nodes` 入参 `{type, title, parent_id}` → 新节点
- `PATCH /novels/{nid}/nodes/{node_id}` 入参 `{title}`
- `POST /novels/{nid}/nodes/reorder` 入参 `{node_id, new_parent_id, new_index}`（拖拽排序用）
- `DELETE /novels/{nid}/nodes/{node_id}`：**不带 `?confirm=1` 时不执行删除**，
  只返回 `{affected_chapters: N, has_audio: bool}` 供前端二次确认；
  带了才真删（章节目录移到 `.trash/`）

**章节**
- `GET /novels/{nid}/chapters/{cid}` → 状态 + 统计（总分块 / 已配音 / 未绑定角色数）
- `PUT /novels/{nid}/chapters/{cid}/raw`：multipart 上传正文。
  同样是不带 `?confirm=1` 只返回影响面（「将清空剧本、时间线和成品 MP3」），带了才执行
- `GET /novels/{nid}/chapters/{cid}/script` → `script_final.json`（不存在就回落到 draft）
- `PUT /novels/{nid}/chapters/{cid}/script` → 整份写回，**写完必须调
  `derived_index.invalidate()`**
- `GET /novels/{nid}/chapters/{cid}/audio/{md5}.wav` → 单块人声，静态返回
- `GET /novels/{nid}/chapters/{cid}/output.mp3` → 成品，
  `Content-Disposition` 的文件名用 `{novel_title}_{chapter_title}.mp3`，
  这是跨小说下载不重名的地方

**分块**
- `PATCH /novels/{nid}/chapters/{cid}/segments/{seg_id}` 入参 `{text?, speaker?, emotion?}`
- `POST /novels/{nid}/chapters/{cid}/segments/batch` 入参
  `{seg_ids: [...], set: {speaker?, emotion?}}`；`speaker: null` 表示清空绑定
- `POST /novels/{nid}/chapters/{cid}/segments/{seg_id}/preview` → 单块试听，
  走 `IndexTTSDaemon` 同步合成并返回 wav（不进任务队列，这是交互式操作）

**角色**
- `GET /roles` → 角色列表，带 `derived_index` 的引用计数
- `POST /roles` 入参 `{name, gender, category}` → 新建（内部调 `roles.register_role`）
- `PATCH /roles/{rid}` 入参 `{name?, category?, description?, speed?}`
- `DELETE /roles/{rid}`：不带 `?confirm=1` 只返回引用计数；`narrator` 一律拒绝删除
- `PUT /roles/{rid}/reference`：multipart 上传参考音频。内部调
  `roles.set_role_reference`，它已经会自动删掉旧的 `speaker_embeddings.pt` / `.meta.json`
  强制 embedding 失效，**不要再自己写一遍失效逻辑**
- `GET /role-categories` / `PUT /role-categories` → 分类列表，
  存在 `roles_manifest.json` 顶层新增的 `categories: []` 字段里

**任务**
- `GET /tasks?state=&group_id=&limit=` → 任务列表
- `POST /tasks` 入参 `{type, novel_id, scope: {node_id} | {chapter_ids: [...]}}`
  → `{group_id, tasks: [...]}`。`node_id` 用 `library.iter_chapters` 展开成章节列表，
  这就是「整本 / 整部 / 整卷 / 多选章节」四种选择范围的统一实现
- `POST /tasks/preflight` 入参同上 → 任务 6 的预检结果
- `GET /tasks/{id}` / `DELETE /tasks/{id}`（= 取消）
- `GET /tasks/{id}/log?offset=` → `{text, next_offset}`

**系统**
- `GET /config` / `PATCH /config`：**白名单写回 `global_config.yaml`**。
  允许改的键：`tts.engine`、`tts.sample_rate`、`mixing.output_format`、`mixing.bitrate`、
  `server.cpu_workers`、`server.monitor_interval_ms`、`server.library_root`、
  角色默认语速。**白名单之外的键一律拒绝**——`llm.model_name` 这种路径类配置
  从界面改错了会很难排查。
- `GET /gpu/owner` → `gpu_arbiter.get_current_owner()`
- `POST /gpu/swap` 入参 `{target: "llm"|"tts"}` → 先返回 `plan_swap()` 的描述，
  带 `?confirm=1` 才执行
- `GET /assets` → 素材库列表（接管被删掉的 Flask 服务那两个页面的功能）
- `GET /monitor` → `monitor.snapshot()` 的一次性快照（SSE 不可用时的降级路径）

**事件流**
- `GET /events` → SSE。事件类型三种：
  - `task_update`：任务状态/进度变化（500ms 节流）
  - `resource`：资源快照，按 `server.monitor_interval_ms` 推送
  - `tree_update`：小说树发生变化，前端据此刷新

  用 Starlette 原生 `StreamingResponse`，**不要装 sse-starlette**。
  格式就是标准的 `event: <type>\ndata: <json>\n\n`。

### 静态文件

`app.mount("/", StaticFiles(directory="web/static", html=True))` 挂在**所有 `/api` 路由之后**。
本阶段 `web/static/index.html` 只放一个占位页面写「界面见计划 006」，目录先建出来。

### tests/test_api.py

用 `fastapi.testclient.TestClient`（底层是已装好的 httpx）。
每个测试用 `tmp_path` 造隔离的 library/roles 目录注入，**不许碰真实数据**。
至少覆盖：小说 CRUD、树的增删改和 reorder、章节上传、
分块批量修改、删除节点的两步确认（不带 confirm 不真删）、
配置白名单（改白名单外的键返回 400）、任务提交与取消、preflight 返回结构。

---

## 任务 8：cli.py 与 global_config.yaml

### cli.py 重新加回 webui 子命令

```python
elif args.command == "webui":
    import uvicorn
    from src.api.app import create_app
    print(f"--> 启动管理界面: http://{args.host}:{args.port}/ (Ctrl+C 停止)")
    uvicorn.run(create_app(), host=args.host, port=args.port)
```

端口默认仍是 7860。**不要恢复 `serve` 子命令**，素材浏览已经并入 `/api/assets`。

### global_config.yaml 补全 server 段

```yaml
server:
  library_root: "library"
  host: "127.0.0.1"
  port: 7860
  cpu_workers: 2              # CPU lane 的并发数；GPU lane 恒为 1，不可配
  gpu_chunk_size: 8           # 常驻 daemon 后端的切批大小，决定取消的响应粒度
  monitor_interval_ms: 1000   # 资源监控推送间隔
  task_retention_days: 7      # 任务记录保留天数
  drm_card: null              # null = 自动探测；本机是 /sys/class/drm/card1
```

---

## 验收清单

1. **单元测试与自检全绿**
   ```
   .venv/bin/python -m pytest -q
   .venv/bin/python cli.py test --all
   git status --short          # 空
   ```

2. **服务能起来，四个启动动作都执行了**
   ```
   .venv/bin/python cli.py webui
   ```
   日志里能看到孤儿恢复检查、残留任务清理、worker 启动。

3. **REST API 手工验证**
   ```
   curl -s localhost:7860/api/novels | python -m json.tool
   curl -s localhost:7860/api/monitor | python -m json.tool
   curl -s -X POST localhost:7860/api/tasks/preflight \
        -H 'Content-Type: application/json' \
        -d '{"type":"tts","novel_id":"nv_ceshishujia","scope":{"node_id":null}}'
   ```
   `/api/monitor` 返回的显存数字要和 `rocm-smi --showmeminfo vram` 对得上。

4. **SSE 有输出**
   ```
   curl -N localhost:7860/api/events
   ```
   应该每秒收到一条 `event: resource`。

5. **任务能跑、能取消**（真实 GPU 环境）
   提交一个整章 TTS 任务，跑到一半 `DELETE /api/tasks/{id}`，然后确认：
   - 任务状态变 `cancelled`
   - IndexTTS 子进程确实消失了（`ps aux | grep indextts`）
   - **llama-server 被恢复了**（`curl localhost:8080/v1/models`）
   - 已合成的分块留在 `audio_cache/` 里
   - 重新提交同一个任务，日志显示大部分句子命中缓存，从断点续上

6. **强杀恢复**（这条是任务 1 的核心价值，一定要验）
   起一个 TTS 任务，等它停掉 llama-server 之后 `kill -9` 掉 webui 进程，
   确认 `.cache/gpu_arbiter/llm_suspended.json` 还在；
   然后重新 `cli.py webui`，确认启动日志报告已恢复 llama-server，标记文件被清掉。

7. **GPU 串行**
   同时提交 3 个 TTS 任务，`GET /api/tasks` 里任何时刻都只有 1 个 `running`。
   再提交 2 个 mix 任务，它们可以和 TTS 同时 running（不同 lane）。

---

## 给执行者的注意事项

- **不要引入任何新的第三方依赖。** 任务队列用标准库，SSE 用 Starlette 原生，
  资源监控读 sysfs 和 /proc。004 已经把 fastapi/uvicorn/pydantic 钉进 requirements 了。
- **不要给 GPU lane 开并发。** 那个 assert 不是摆设。
- **不要对 `IndexTTSBackend` 切批。** 会重载模型，65 分钟变 3 小时。
  只有 `supports_chunking = True` 的后端才切。
- **不要先切批再排序。** 必须对已按 `reference_audio` 排好序的列表切连续窗口。
- **不要在 worker 线程里直接操作 asyncio 对象**，一律走 `loop.call_soon_threadsafe`。
- **不要编造 GPU 耗时估算。** 没有历史数据就返回 None。
- **不要自己重写 embedding 失效逻辑。** `roles.set_role_reference` 已经做了。
- **不要用 `shutil.rmtree`。** 删除一律走 004 建立的 `.trash/` 机制。
- 所有新模块的对外函数都要能接受 `library_dir=None` / `tasks_dir=None` 注入，
  否则测试没法隔离。
