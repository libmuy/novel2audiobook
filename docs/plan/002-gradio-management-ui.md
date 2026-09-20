# 计划 002: Gradio 全流程管理界面

> **状态：已废弃**。Gradio 界面及其全部代码（`src/webui_app.py`、`webui.py`、
> `tests/test_webui_app.py`）已由计划 004 任务 8 删除（见 `004-multi-novel-library.md`），
> 现行界面是 FastAPI + Vue3（计划 005/006）。本文仅作历史归档，以下为当时的原文。
>
> ---
>
> 原状态：已实现。计划 001/003 的产出被直接复用：
> - `src/roles.py` 的 `precompute_embedding` / `get_embedding_status` /
>   `delete_role` / `set_role_reference` 直接调用，本计划没有再往
>   `src/roles.py` 加任何函数。
> - Tab 4（TTS 试听）走 `src.tts_daemon.IndexTTSDaemon` 的
>   `ensure_started()`/`synthesize_batch()`/`shutdown()`，`src/tts_engine.py`
>   未作任何修改。
> - `tools.gpu_arbiter.get_current_owner()`/`plan_swap()` 直接驱动顶部的
>   显存状态展示。
>
> 与原方案相比，实现时做了几处简化，见下方「与原方案的差异」一节。
> 详见 `docs/plan/003-resident-tts-daemon.md`。

## 目标
构建基于 Gradio 的 Web 管理界面，覆盖角色管理、章节管理、TTS 试听、流水线触发等全流程操作。

## 背景
当前所有操作依赖 CLI 命令行，没有可视化管理界面。
IndexTTS 自带 Gradio webui，项目已有 Flask 只读浏览服务，
但缺少一个统一的管理入口。

## 技术选型
**Gradio** — 与 IndexTTS 生态一致，原生音频支持，开发效率高。

## 界面设计（4 个 Tab）

### Tab 1: 仪表盘
- 章节状态总表（复用 `src/status_tracker.py` 的数据）
- 列：章节 ID | Raw | Draft | Final | Timeline | MP3 | 状态 | Cache
- 操作按钮：全部解析 / 全部 TTS / 全部混音（带确认弹窗）
- 实时日志输出区域

### Tab 2: 角色管理
- 左侧：角色列表（表格，标记状态：✅/❌ 有 ref wav / config / embedding）
- 右侧：选中角色的详情编辑区
  - 上传/替换 reference.wav（Gradio Audio 组件）
  - speed 滑块（0.5 ~ 2.0）
  - 试听 reference.wav 播放器
  - **预计算 embedding** 按钮 + 状态显示
  - 保存配置按钮
- 底部：新增角色（输入名称、性别）/ 删除角色按钮

### Tab 3: 章节管理
- 下拉选择章节
- 显示章节文件状态（raw / draft / final / timeline / mp3）
- 查看 `raw.txt` 内容（只读文本框）
- 查看 `script_final.json`（格式化 JSON 展示）
- 触发操作按钮：
  - 解析（parse）
  - TTS（tts）
  - 混音（mix）
- 操作日志/进度输出

### Tab 4: TTS 试听
- 选择角色（下拉）
- 输入文字（文本框）
- 选择情感（下拉：neutral / happy / angry / sad / serious / afraid / surprised / calm）
- 生成按钮 → 播放生成的音频
- 用于快速验证角色音色效果

## 实现方案（实际落地）

### 文件结构
```
webui.py                    ← 极薄入口：build_app() 组装 + demo.queue().launch()
src/
  webui_app.py              ← 新建：所有业务逻辑（纯函数）+ UI 组装，见下方说明
  roles.py                  ← 不变（复用计划 001 的 API）
  tts_engine.py             ← 不变（复用 EMOTION_TO_VECTOR，未新增接口）
  tts_daemon.py             ← 不变（复用计划 003 的 IndexTTSDaemon）
  web_server.py             ← 不变（只读浏览服务保留，8090 端口与 webui 的
                                7860 不冲突，两者共存）
cli.py                      ← 修改：新增 webui 子命令
requirements.txt            ← 修改：新增 gradio 依赖
```

`src/webui_app.py` 分两层：上半部分是一批不依赖 Gradio 的纯函数（章节状态
格式化、角色增删改查、GPU 前置检查、试听合成……），下半部分 `build_app()`
把这些函数薄薄地包装成 Gradio 事件回调。这个分层是本计划测试能不启动真实
Gradio server 就跑通的关键——`tests/test_webui_app.py` 里 68 项用例中
只有最后 3 项是"能否无异常构建/启动/关闭"的冒烟测试，其余全部直接调纯函数。

### 启动方式
```bash
python cli.py webui --port 7860
# 或
python webui.py
# 或（gradio 热重载）
gradio webui.py
```

### 与原方案的差异

原方案设想的交互模型比实际需要的更复杂，落地时做了三处简化：

1. **换手确认只在一处是真正的"两步确认 UI"**：原方案设想"换模型前弹确认"
   要覆盖所有 GPU 相关按钮。实现时发现 GPU 操作其实分两类，代价完全不同：
   - **一次性批处理**（parse、批量/单章 tts、预计算 embedding）：`tts`/
     `precompute_embedding` 内部已经通过 `LlmSuspendedForGpu` 自动停/起
     llama-server（和 CLI 的 `cli.py tts` 一样，这条路径本来就不弹确认）；
     `parse` 则完全不碰显存，只依赖 llama-server 是否可达。这类操作执行前
     只做一个轻量检查：**常驻 TTS 服务是否在跑**——如果在跑，两个独立的
     IndexTTS 进程会抢同一张卡，必须先手动释放；如果没在跑，直接执行，
     不弹确认框（`check_daemon_not_blocking()`）。
   - **切换到持续状态**（启动常驻 TTS 服务）：这是唯一保留真正二次确认
     UI 的地方——点击「启动常驻服务」先调 `plan_swap("tts")`，非 noop
     则展示 steps + 历史耗时并等用户点「确认启动」才真正执行
     `ensure_started()`。「释放显存」按钮则不需要二次确认（关闭操作本身
     代价小、用户点击已是明确意图），点击直接执行并展示结果。

2. **长任务用 Python generator 而非 `threading.Thread` + `gr.Progress()`**：
   `run_parse_all`/`run_tts_all`/`run_mix_all` 写成生成器，每处理完一章
   `yield` 一次累积日志，Gradio 原生支持把生成器函数接到 `.click(...,
   outputs=log_box)` 上做流式更新，不需要手动开线程、也不需要处理"跨线程
   更新 Gradio 组件"这类问题。

3. **"全局一把锁"用 Gradio 自带的并发限制实现**：所有触碰 GPU 的按钮
   （全部解析/全部 TTS、预计算 embedding、单章 parse/tts、启动常驻服务、
   试听生成）统一打上 `concurrency_id="gpu", concurrency_limit=1`，
   Gradio 队列自动把它们串行化；不碰 GPU 的操作（刷新列表、混音、查看
   文件、显存状态刷新）不受影响，不需要手写 `threading.Lock`。

其余两处是范围内的具体实现选择：

4. **角色选择走 `gr.Dropdown` 而非"左侧列表点选"**：`gr.Dataframe` 的行内
   选中交互在 Gradio 里实现复杂度较高（需要处理 `.select()` 事件+行
   索引映射），改为「列表只做展示，详情面板旁另设一个下拉框选角色」，
   交互上等价，实现更简单。

5. **角色语速保存新增了一个小函数，放在 `src/webui_app.py` 而非
   `src/roles.py`**：`save_role_speed()` 直接读写 `roles/<id>/config.json`
   的 `speed` 字段。没有放进 `src/roles.py` 是为了不再扩充计划 001 已经
   锁定的 API 边界——这是 webui 局部使用的薄 helper，不是角色管理的核心能力。

### 关键实现细节

1. **文件上传**：reference.wav 上传后调用 `roles.set_role_reference()`
   写入 `roles/<role_id>/reference.wav`，函数内部已经会顺带删除旧的
   `speaker_embeddings.pt`/`.meta.json`（计划 001 的既有行为）。

2. **CLI 集成**：界面操作直接调用 Python 模块（`src.roles`、
   `src.llm_parser`、`src.tts_engine`、`src.audio_mixer`、
   `src.tts_daemon`），不走 `subprocess` 调用 `cli.py`。

3. **端口与访问**：默认 `127.0.0.1:7860`（仅本地）；可通过 `--host 0.0.0.0`
   开放局域网访问，与 `cli.py serve`（只读浏览服务，8090）互不冲突。

## 依赖
- `gradio>=5.50.0,<6.0.0`（新增到 `requirements.txt`；钉在 5.x 系列，
  避开 6.x 大版本的 breaking changes——6.x 刚发布不久，5.x 是更成熟的主线）

## 文件清单
| 文件 | 操作 | 状态 |
|------|------|------|
| `webui.py` | 新建（薄入口） | ✅ |
| `src/webui_app.py` | 新建（业务逻辑 + UI 组装） | ✅ |
| `cli.py` | 修改（新增 webui 子命令） | ✅ |
| `requirements.txt` | 修改（新增 gradio） | ✅ |
| `tests/test_webui_app.py` | 新建（68 项，纯函数测试为主 + 构建冒烟测试） | ✅ |

## 验证

- `python cli.py test --all`：绿；完整 `pytest tests/`：349 passed。
- 构建冒烟：`build_app()` 在真实项目根目录与隔离临时目录下都能无异常构建；
  `demo.queue().launch(...)` 后能正常 `close()`。
- 全程未启动真实 IndexTTS/llama-server 交互——涉及 GPU 的函数
  （`precompute_role_embedding`、`run_tts_one` 等）测试里全部 monkeypatch
  掉了底层调用；实现期间机器上 llama-server 正在运行，全程确认其 PID
  未变化（未被意外打断）。
- **未做（需要真实浏览器/GPU 环境）**：没有人工过一遍界面点击交互
  （角色上传、预计算 embedding 真实跑一次、试听真实出声、批量解析/TTS
  的日志滚动效果）。这些需要用户在合适时机用 `python cli.py webui` 启动
  后手动验证一遍。
