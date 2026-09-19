# 计划 010：剩余工作收尾

> 状态：执行中。来源是 008/009 之后盘点出的未完成项；方案由 3 个调研代理 + 1 个设计代理（含回头抽查）产出，
> 完整分阶段设计见本文件各阶段。每个阶段一个提交，提交时把「实际改动」追加到对应阶段下。

## 范围
全部剩余项都做，除：**推迟**面板宽度拖拽、每段音效偏移/增益；**砍掉**背景音场景范围；**纳入** `chapter-stats` 数值列。

## 抽查中额外发现（已并入阶段）
1. `PATCH /api/config` 用 `yaml.safe_dump`，会抹掉 `global_config.yaml` 全部 40 行注释（含素材引擎选型决策记录）→ 阶段 2，阻塞阶段 3。
2. Mock 回退污染音频缓存今天就存在（`tts_engine.py:325-328`、`asset_gen.py:459-483`），不只是取消才触发；取消必须晚于消毒 → 阶段 6 先于阶段 12。

## 阶段与依赖
0 文档订正+push → 1 任务面板 → 2 配置写入安全 → 3 混音参数+设置 toast → 4 预计算音色 → 5 素材引擎选择 →
6 子进程卫生+停止污染 → 7 原生对话框替换 → 8 状态 map+对比表 → 9 chapter-stats → 10 分类树抽取 → 11 素材分类标签 → 12 真取消。
硬依赖：2→3；6→12；10→11。**不要在阶段 6 之前上阶段 12**（`os.killpg` 仅在 `start_new_session=True` 时安全）。

## 推迟 / 砍掉 / 已知不动
- 推迟：面板宽度拖拽（`usePaneWidth` + `pane-resizer`，分隔条须放在 `.tree-content` 之外，工作台需 `ResizeObserver` 修 `viewportHeight`）；
  每段音效 `sfx_offset_ms/sfx_gain_db`（约 6 文件 60 行，用户增益须在单向 `sfx_limit_dbfs` 限幅之前应用）。
- 砍掉：背景音场景范围（新数据模型 + 编辑对账，无具体诉求）。
- 已知不动：工作台 `ROW_HEIGHT=88` 与 `.segment-card` 实际 80px 的约 10% 虚拟滚动漂移；`novels-page` N+1（应做 `GET /novels/summary`）；
  常驻 TTS 守护进程不可取消（无 cancel 消息、单线程 socket 循环、队列路径走不到它）。

## 实际改动
### 阶段 0
- `CLAUDE.md`/`AGENTS.md` 订正「等待计划 005 重新提供界面」的过时说法；新建本文件；仓库外的自动记忆已更新；push `891e4da`。
### 阶段 1
- 小说详情页任务面板：`TASK_TYPE_LABEL` 补 `asset_gen`；`taskTitle` 不再拼悬空的「 · 」（全局任务改用 `params` 说明对谁做：素材名 / 角色 id）；
  客户端只保留本书任务 + 全局任务，分「本书任务」「全局任务」两组（`GET /tasks` 与 SSE 都不做服务端 novel 过滤，保持口径一致）。
- 每个任务可展开「日志」（`API.getTaskLog` 之前全库无人调用），按 `next_offset` 增量续拉、随 `task-update` 刷新，终态后再拉一次就停，收起即停止请求。
- 测试：新增 `TestTaskPanel`（徽章数 = 本书 + 全局、别的书的任务不混入、标题为中文且无悬空分隔符、日志展开/收起）；已做变异检查——去掉过滤后该用例确实失败。
  新钉选择器：`.task-section-title`、`.task-group-global`、`.task-log-btn`、`.task-log`。
### 阶段 2
- 新 `src/config_store.py`：`PATCH /api/config` 改用 ruamel round-trip 只改被更新的键。对真实 `global_config.yaml` 未改动往返**字节级一致**，改一个键只多出该键那一行 diff，
  注释、原有引号（`bitrate: "192k"`）、键顺序都保留。之前 `yaml.safe_dump` 整文件回写，第一次在设置页保存就会永久抹掉全部注释（含素材引擎选型的决策记录）。
- 共享 YAML 工厂 `asset_specs_store.round_trip_yaml`（旧名 `_yaml` 保留）加了 `null` 表示器：ruamel 默认把 `drm_card: null  # 注释` 写成 `drm_card:  # 注释`，语义相同但产生无谓 diff。
- `_CONFIG_SPEC` 取代只有 bool 的 `_BOOL_CONFIG_KEYS`：逐键类型/范围/枚举校验（`tts.sample_rate` 8000–96000、`server.cpu_workers` 1–16、`server.monitor_interval_ms` 200–60000、`mixing.output_format ∈ mp3/wav/flac`、`mixing.bitrate` 形如 `192k`…）；
  数值键不再接受字符串（`"24000"` 以前会原样落盘）；`mixing.voice_only` 仍宽松强转（JSON 字符串 `"false"` 是真值）。不合法的键**不写盘**。
- 响应形状 `{ok, applied_keys, rejected_keys}` 与「全部被拒仍 HTTP 200」不变，新增附加字段 `rejected: [{key, reason}]`。先落盘再改内存，写盘失败时内存不会与磁盘不一致。
- 删除死代码 `ConfigPatch`（无法表达点分键，无人引用）。
- 测试：`tests/test_config_store.py`（真实文件字节往返、只改一行、缺失层级/文件、无残留 tmp）+ `TestSystemAPI` 新增（真实配置改后注释保留、7 种非法值拒绝且文件字节不变、合法/非法混合只应用合法的）。
### 阶段 3
- 设置页新增「混音参数」区：闪避触发阈值（`mixing.ducking_threshold`）、闪避衰减量（`mixing.ducking_gain_db`，dB）、闪避渐变时长、背景音基础电平（`ambience_gain_db`）、音效峰值限幅（`sfx_limit_dbfs`）；TTS 区新增句间静音（`tts.segment_gap_ms`）。后端白名单 + 范围校验同步（阶段 2 的 `_CONFIG_SPEC`，新增 `float` 类型）。
- **有意不暴露 `ducking_volume_ratio`**：它是线性比例不是 dB，用户在「闪避」字段填 `-10` 会因混音器 `ratio > 0` 的 else 分支恰好得到 −10 dB，误以为单位是 dB。改为新增面向 UI 的 `mixing.ducking_gain_db`，`audio_mixer` 里**优先于**旧比例（用 `is not None` 判断，`0 dB` 是合法值）；没设过时界面显示由旧比例换算出的实际衰减量（0.3 → −10.46）。有测试断言旧键不在白名单。
- `tts.segment_gap_ms` 取代 `tts_engine.py` 里硬编码的 `200.0`，缺省行为不变；它被烘焙进 `timeline.json`，改后需重跑 TTS（wav 命中缓存，只重算时间线），帮助文字里写明。
- 设置页三处 `alert()` 全部换成 toast，被拒的键带上原因（`音效峰值限幅（超出范围（-60.0–0.0））`）；`saveConfig` 改为**只提交改动过的键**（不然每次保存都会把界面显示的默认值——如从旧比例换算出的 `ducking_gain_db`——实体化写进配置文件）；数值字段清空时前端直接拦截（`Number('') === 0` 会把「没填」悄悄存成 0）；`fillFormFromConfig` 数值一律用 `??`（0 dB / 0 ms 是假值，`||` 会换回默认）。
- `global_config.yaml` 里给两个新键写了注释文档（可选键，未启用）。
- 测试：后端 `test_tts_engine`（句间静音三种取值）、`test_audio_mixer::TestDuckingGainPrecedence`、`TestSystemAPI`（新键接受/拒绝/旧键不暴露）；E2E `TestSettingsMixingParams`（显示换算值、只写改动的键、`0` 不回退、越界带原因 toast、空值拦截、句间静音落在 tts 段）。
### 阶段 4
- **修 handler 谎报成功**（`task_queue._handle_precompute_embedding`）：以前丢弃 `precompute_embedding` 返回的 `{"ok","error"}`，环境未就绪/超时/角色未注册/子进程非零退出全被记成 SUCCEEDED，缺 `role_id` 还是静默空操作。
  现在缺 `role_id` 抛 `ValueError`、`ok` 为假抛 `RuntimeError(error)`，任务以 FAILED 结束并带原因；`POST /tasks` 对该类型缺 `role_id` 直接 400。
- **GPU 换手放在 handler 层**（`LlmSuspendedForGpu`），不改 `src/roles.py`——`roles.precompute_embedding` 的 docstring 明确「调用方负责换手」，`tests/test_roles.py` 也直接调它。
  新增 `roles.embedding_precondition_error`（角色已注册 + 推理环境就绪的廉价检查），在换手**之前**调用：环境没就绪就别去停 llama-server；抛错时 `with` 仍会走完退出（恢复 llama-server）。
- 新配置 `tts.index_tts.precompute_timeout_sec`（`global_config.yaml` 里设 900）；不设回落到整章 TTS 的 `timeout_sec`（10800 秒——拿来管一次挂死的单角色预计算会占住唯一的 GPU 通道 3 小时）。
- 角色卡片：`.role-stats` 里 embedding 徽章之后加「预计算音色」按钮（`.precompute-embedding-btn`），`embedding_status.valid` 时隐藏、无参考音频时禁用；先弹确认框说明 GPU 换手与排队，**不走 preflight**（全局任务的 preflight 只返回写死的 `create:1`）；
  进度用素材库页同一套 SSE 模式（按 `params.role_id` 索引，挂载时接上在跑的任务），终态刷新角色并 toast，失败 toast 带 `task.error`。
- **E2E 抓到并修掉一个真实竞态**：任务失败得比 `createTask` 响应还快（环境未就绪是毫秒级失败）时，SSE 终态事件先清掉条目，随后响应里「排队中」的提交时快照又把它写回去，按钮永远卡在「排队中…」。
  现在记下已见过终态的任务 id，响应晚到不再写回。素材库页的 `genTask` 有同样写法，一并修了。
- 测试：handler 单测（失败变任务失败且带原因、成功也在换手内、环境未就绪不碰 GPU、缺 role_id 不再静默、经真实队列端到端为 failed）、`TestPrecomputeTimeout`、`TestEmbeddingPrecondition`、API 缺 role_id 400；
  E2E 在无 IndexTTS 的环境里断言「任务以 failed + 原因结束、toast 带原因、按钮恢复可重试」以及「无参考音频时禁用」。
### 阶段 5
- **引擎按配置选择**：`asset_gen.py` 新增 `ASSET_BACKENDS` 注册表（`audioldm / tangoflux / ace_step / mock`，键 = 各子类已有的 `name`）、`resolve_engine_id`（同时接受 id 和现有配置里的展示串 `"AudioLDM-S-Full-v2"` / `"TangoFlux"` / `"ACE-Step 1.5"`，**现有 `global_config.yaml` 零迁移**）、`expected_engine(kind, config)`（缺省或认不出 → 回落到此前硬编码的默认 + 警告）。
  之前 `build_asset_gen_backend` 写死 `AudioLDM if kind == "ambience" else TangoFlux`，配置值只进日志。`MockAudioGenBackend` 接受可选 `config`，构造统一；`--repo-dir` 对 `ace_step` 确认会传（配置了 `repo_dir` 就传）。
- **引擎漂移选方案 (b)**：不动 `compute_spec_hash`（加进哈希会让整个已有素材库在提交那一刻全部失效）；`get_asset_status_list` 新增 `STALE(引擎已变更)`——`meta.engine` 既不是当前引擎、也不是 `mock`（占位音有自己的 `OK(占位/Mock)`）、也不是缺失（旧 meta）；显式配成 `mock` 不判漂移。
  漂移**不会**被自动重生成（否则一改配置就整库重做），只有 `force` 才用新引擎重做；预检与此一致（无 `force` → skip 并说明原因，有 `force` → overwrite）。界面卡片显示「引擎已变更」及「这条是 X 生成的，当前配置是 Y」，单条「重新生成」自动带 `force`。已核对：真实素材库 12 条全部仍是 `OK`，没有被误判。
- **顺带修一个发现的不一致**：预检对 `OK(占位/Mock)` 早就承诺「将尝试用真实引擎重新生成」，但 `generate_assets` 的缓存判断把占位音当命中跳过。现在「上次是占位、这次有真实引擎可用」算未命中重试；这次仍是 Mock 则照旧命中（自检的缓存回归和「别每次都白重铺占位音」靠这条）。为此后端改为在规划阶段就构造。
- `cli.py assets --backend` 的 choices 由 `["mock"]` 改为注册表全部 id，并真正生效（覆盖配置）；`global_config.yaml` 里「仅用于日志展示」的注释改成真实语义。
- 测试：新增 `tests/test_asset_engines.py`（37 个；此前工厂**零覆盖**）——id/展示串解析、配置真的选引擎、缺省与未知值、可用/不可用/显式 mock、`engine` 参数覆盖、漂移各分支、漂移不进哈希（无 `force` 保留、`force` 重做）、占位重试与仍为 Mock 时的缓存、预检一致性；E2E 断言漂移徽章/提示/旧音频仍可听/「重新生成」后被重做。
### 阶段 6
不引入取消语义，只修 GPU 真实路径上的四个 bug 和一个接缝（**这是整个收尾计划里对正确性价值最高的一步，也是阶段 12 的前置**）：
1. **超时杀进程的顺序**：`IndexTTSBackend` 的 `TimeoutExpired` 以前先冒出 `with LlmSuspendedForTts` 块、`__exit__` 重启了 llama-server，之后才杀子进程——重启时子进程还占着显存。现在在 `with` 块**内部**捕获并终止。
2. **`_current_proc` 类属性默认值**：新实例调 `terminate_current()` 以前会 `AttributeError`。
3. **素材生成的子进程**：`SubprocessAudioGenBackend.generate_batch` 由阻塞 `subprocess.run`（无 `start_new_session`、超时只打日志、子进程泄漏并继续占显存）改为 `Popen(start_new_session=True)` + `communicate(timeout)` + 超时终止（同样在仲裁器块内部）；stderr 显式 decode（原 `[-1000:]` 假设 `text=True`）。三个引擎子类继承，一处改动全覆盖。
4. **占位噪音污染音频缓存**（今天就存在，不只是取消才触发）：真实引擎没合成成功的句子被 Mock 占位音写进 `audio_cache/<真实 md5>.wav`，再标 `tts_completed`，从此以合法名字永久命中缓存。现在占位音旁写 `<md5>.wav.fallback` 标记，缓存探针遇到带标记的 wav 视为**未命中**、下次运行重试真实引擎；成功后清标记；时间线条目加 `"fallback": true`（加法；`cached`/`used_fallback`/`tts_completed` 保持）。
   新增可选严格模式 `tts.fallback_on_failure: false`（默认 true = 沿用现状）：有句子失败就让整章失败、不产出带占位音的成品，已合成的句子留在缓存里修好后续上。素材生成一侧的对应问题由阶段 5 的「占位音在真实引擎可用时重试」覆盖（meta 的 `used_fallback` + 真实引擎可用 → 不命中缓存）。
5. **接缝 `_delete_unfinished_outputs(jobs, results)`**：子进程把 wav 直接写到最终路径，被 SIGKILL/崩溃会在合法 md5 名下留下**截断甚至 0 字节的 wav**，缓存探针只看「文件存在」，之后 `wave.open` 每次都炸。今天被 Mock 覆盖掩盖，阶段 12 在回退循环前抛错的那一刻就会暴露。按 `result.json`（子进程每完成一个 job 原子重写）精确删除未完成 job 的输出。asset_gen 不需要（原始 wav 在 `TemporaryDirectory`）。
- 新增 `src/killable_proc.py`（TTS 与素材生成共用）：SIGTERM → 宽限 5 秒 → SIGKILL → **收尸**（不留僵尸）。**安全护栏**：`os.killpg` 只有子进程在独立进程组时才安全，动手前核对子进程的 pgid 不等于服务器自己的，不满足就退化成只终止这一个进程——宁可杀不干净也不误杀 API 服务器（有专门测试，含升级到 SIGKILL 时也不 killpg）。
- 测试（此前 `IndexTTSBackend` 子进程行为**零覆盖**，全新写，用 `FakePopen` 不需要 GPU）：`test_killable_proc.py`、`test_index_tts_backend.py`（超时杀进程发生在仲裁器退出之前——用记录顺序的假仲裁器断言、独立 session、新实例 `terminate_current` 安全、stderr 坏字节、截断输出被删）、`test_asset_gen_subprocess.py`、`TestFallbackDoesNotPoisonTheCache`（标记/时间线标记/重跑重试/仍失败继续重试/整章缓存/严格模式/状态不把标记文件算进 `audio_cache_count`）。
  已做变异检查：换回旧版 `tts_engine.py` 后，超时顺序、新实例安全、截断输出三条回归测试都会失败。
### 阶段 7
- 新增 `prompt-dialog` 组件（文本输入 / 下拉选择两种形态，回车确定、Esc 取消、可传 `validate`），根组件 `provide('showPrompt')`，`index.html` 挂载。`showPrompt` 在确定按钮的点击处理里**同步** resolve，
  所以调用方 `await` 之后接着做的事（如触发隐藏文件输入的 `click()`）仍在同一次点击的用户手势窗口里——`uploadChapter` 由此保持可用，有 E2E 用 `page.expect_file_chooser()` 证明文件选择器确实被打开。
- 转换了全部 14 处原生调用（`prompt` ×7、`confirm` ×5、`alert` ×1，加上设置页在阶段 3 已换掉的 3 处 `alert`）；app.js 里不再有任何原生对话框。
- 三处是升级而不是直译：**删除节点**先取 `DELETE` 不带 confirm 返回的影响预览，弹窗写明「将影响 N 个章节（含已生成音频）、数据移入回收站，不是永久删除」（以前是盲确认）；**批量绑定角色**由手敲角色名改为从现有角色里选；**批量改语气**由手敲英文标签改为 8 个语气的下拉（`EMOTION_OPTIONS`）。
- 新建部/卷/章、重命名、创建角色失败以前只 `console.error`（界面毫无反应），现在弹错误 toast。
- 取消任务的确认框措辞如实反映**当前**行为（排队中直接取消；运行中的要等当前步骤结束，配音任务可能要跑完整章）——阶段 12 真取消落地后再更新。
- 测试：新增 `TestNativeDialogsReplaced`（空名称禁用提交、取消不创建、回车/Esc、改名预填、删除影响预览且取消无变化、上传章节文件选择器、失败 toast、全程无原生对话框）与 `TestWorkbenchDialogs`（批量绑定下拉、批量改语气 8 项、批量清空取消时**零请求**确认时才发一次、未绑定的 toast、创建并绑定角色）。这些路径此前全库没有一条测试。
### 阶段 8
纯前端，后端零改动（`GET /tree` 早就返回 `{novel, status}`，只是 `loadTree` 把 `status` 丢了）。
- `loadTree` 把 `data.status.chapters` 按 `chapter_id` 建成 `statusByChapter`；`tree-node` 新增 `statusMap` prop 沿递归往下传。状态点原先只看节点内联的一个 `status` 字符串，看不出「混音时带了素材但缺素材」；现在 `completed` 且 `mixed_with_assets && missing_assets_count > 0` 时显示 `.status-dot.missing`（`--warn`，带一圈浅色光晕，和「已完成」的绿点区分）。
- 删除 novel-detail 里从不被模板调用的重复 `statusClass`（连同导出）。
- 多选 ≥2 个章节时右栏出现对比表（`.chapter-compare`，用早就写好但一直没人用的 `.table-wrap`）：原文 / 解析（定稿·初稿·✗）/ 配音 / 混音（格式 + 仅人声·含素材·缺 N 个）/ 状态。
  「配音」列**只认 `timeline.json`**；`audio_cache_count` 只是缓存 wav 数的上界（含占位、含被丢弃的旧句），故意没有当成「已配音」。
- 混音任务新到终态 → 重取树并 toast：有缺素材的章节提示「N 个章节混音时跳过了缺失素材，可到「音效库」生成后重新混音」（warning），否则「混音完成」。进页面时先把已完成的旧任务登记为「已处理」，不会每次进来重复提示。
- 踩坑：模板用到的变量必须由 `setup` 返回——一开始漏了 `selectedChapterIds`，Vue 生产版对渲染错误只在控制台输出，表现为整页空白，所有 novel-detail 用例一起失败。
- 测试：`TestChapterStatusMap`——树点 `missing`/`pending`、对比表 0/1/2 个选中的出现与内容、混音完成 toast（用 `page.route` 伪造任务列表，同一任务第二次事件不重复提示）。
