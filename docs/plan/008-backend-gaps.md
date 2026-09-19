# 计划 008：补齐后端缺口（混音用素材 / 素材库 CRUD / 素材生成任务 / 角色分类树与标签）

> 状态：已实现。来源是计划 007 换皮时盘点出的 5 处后端缺口（见 007「已知后续 TODO」）。
> **只做后端 + API，前端没动**——音效库页面、角色库页面仍是 007 的只读/扁平形态，
> 后续接这些新接口是独立的前端工作。

## 分阶段实现

**Phase 1 — 混音真的用上 bgm/sfx**
- `POST /tasks` 支持 `params`；`mix` 任务 `params.with_assets=true` → `voice_only=False`，不传保持 `None`
  （遵循 `mixing.voice_only` 配置，不覆盖用户设置）。`mixing.voice_only` 加进配置白名单，带 bool 强转
  （JSON 字符串 `"false"` 是真值，不转会把开关钉死）。
- 分块 `PATCH`/`batch` 支持 `sfx`/`bgm`（`null` = 清空；非空值校验必须存在于素材库——用户显式编辑写错要报错，
  不像 LLM 输出那样静默纠正）。`POST .../timeline/refresh-assets` 把 script_final 的 sfx/bgm 同步进
  timeline（混音器读的是 TTS 时的快照，不同步的话编辑要等一次完整重新 TTS 才生效）。
- 未知任务类型（含声明了 lane 却没 handler 的 `import`）在提交时 400。
- 顺带修：`_handle_parse/tts/mix` 硬编码 `PROJECT_ROOT/library` 忽略 `server.library_root`，改用
  `library.get_chapter_dir`；API 混音输出文件名对齐 CLI（`{novel}_{chapter}`）；`GET .../output.mp3`
  按 mtime 取最新而不是字母序第一个（两种命名并存时会稳定返回过期文件）。

**Phase 2 — 章节「是否混了素材」状态**
- `mix_chapter` 写 sidecar `output/mix_meta.json`（不往 `.status.json` 加字段——它被 parse/tts/mix 三处整体覆写）。
  纯人声混音如实记「没混进任何素材」，不把 timeline 里潜在的引用当成用上了。
- `status_tracker` 新增 `output`/`output_format`/`mixed_with_assets`/`missing_assets_count`；
  **无 sidecar 时 `mixed_with_assets` 是 `None` 而不是 `False`**（不知道就不下结论）。`mp3` 字段保留作别名，
  语义放宽为「有任意格式成品」——修掉 wav/flac 产物被误判为未混音的 bug。
- `GET .../chapters/{cid}/assets`：引用/缺失素材、是否需要重新混音。

**Phase 3 — 素材规格 CRUD**（新依赖 `ruamel.yaml`）
- `src/asset_specs_store.py`：ruamel round-trip 写入，文件头说明与条目行内诊断注释原样保留
  （未改动的 round-trip 字节级一致，有测试）。`/api/asset-specs` 增删改查；**不支持改名**
  （改名会让所有引用旧名的剧本/时间线失联）；`DELETE` 默认只删定义、保留 wav，`?delete_files=true` 才删文件。
- 接上了一直没人读的 `asset_gen.spec_file` 配置项——读写两边必须认同一个文件。

**Phase 4 — 素材生成作为队列任务**
- `asset_gen` 任务类型放 **gpu lane**（生成子进程内部会停/起 llama-server，不可重入，必须跟 parse/tts 互斥）；
  `Task.novel_id` 可选、`submit_global()`；`POST /tasks` 对无章节类型提前分支——顺带让
  `precompute_embedding` 第一次能通过 API 提交。预检 `preflight_assets` 复用 `get_asset_status_list`，
  `estimated_gpu_minutes` 恒为 `None`（不编造）。
- `generate_assets` 加 `progress_cb`/`should_cancel`。**取消只做到 kind 之间和每次 `generate_batch` 之前的协作式检查，
  不做杀子进程**：`TaskQueue.cancel()` 从来不会去杀任何后端子进程（TTS 也一样），单独给素材生成造一套不对称的机制
  不划算。
- 顺带修 `update_tts_stats`：首次采样没记 `sample_count`，第二次采样会直接覆盖而非加权平均。

**Phase 5 — 角色分类嵌套树 + 标签**
- `src/category_tree.py`（不复用 `library.py`：`validate_tree` 硬编码 part/volume/chapter）。
  `roles_manifest.json` 新增 `category_tree` 为真相源，扁平 `categories` 变成每次树变更后重新生成的投影，
  旧接口/旧前端/旧测试不用改。`GET/PUT /api/role-category-tree`（整树 PUT）。
  从未保存过树时 `GET` 合成一层根节点、不写盘。旧 `PUT /role-categories` 不碰树（两边不自动对账）。
- 删除/改名树节点**不动**角色自己的 `category` 字符串（沿用扁平分类的容忍策略，有测试锁）。
- 角色 `tags`：`POST/PATCH /roles` 支持，`GET /roles` 返回，`GET /api/role-tags` 聚合计数（不存标签注册表，不会悬空）。
- `save_manifest` 改原子写 + 加锁；删掉 `create_role` 里的「二次保存」权宜代码。

## 验证
`pytest tests/`（531 passed，含 Playwright 前端）、`python cli.py test --all` 通过。新增
`test_asset_specs_api.py`、`test_asset_gen_api.py`、`test_category_tree.py`，并扩展了 api/roles/task_queue/
audio_mixer/status_tracker/preflight 既有测试。

## 明确留作后续（不在本次范围）
- **真正杀子进程的取消**（`TaskQueue.cancel` → 后端 `terminate_current`），应对 TTS 和素材生成一起做。
- ~~素材音频试听端点~~：已在计划 009 补上。
- 素材分类树（`category_tree.py` 已通用，接到素材元数据即可）。
- 前端接入以上所有新接口。
- 既有的已知小问题、本次没动：`asset_gen.ambience_engine/sfx_engine` 配置只是日志字符串，
  `build_asset_gen_backend` 实际硬编码引擎选择。
