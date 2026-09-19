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
