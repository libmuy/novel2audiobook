# 计划 004: 多小说数据层 + 拆除旧界面 + parse 语义变更

> **状态：已实现**（本地 Qwen3.8 + OpenCode 执行，2026-09-13；经 Claude review
> 并修复两处死代码、补齐 `get_novel_status_summary` 测试覆盖，见
> `10f16f9` 提交）。经 297+3 个 pytest 用例、`cli.py test --all`、以及真实
> GPU 环境下两本小说 parse → 人工定稿 → tts → mix 全链路手工验证通过。
>
> 本计划是「多小说 Web 管理界面」三部曲的第一步，后续见
> `docs/plan/005-task-queue-and-api.md`、`docs/plan/006-web-frontend.md`。
> 三份计划必须按 004 → 005 → 006 的顺序执行，不要跳步。

## 目标

把项目从「单本小说 + 扁平 `chapters/` 目录」改造成「多小说 + 部/卷/章树形结构」的数据层，
同时拆掉即将被替换的 Gradio 界面与 Flask 只读服务，并变更 parse 的角色分配语义。

本阶段**不产出任何 Web 界面**，全部通过 `cli.py` 验收。

## 背景

项目原本假设只有一本小说，所有章节平铺在 `chapters/ch_0001/` 这样的目录下。
现在要支持多本小说，每本小说可以自由选择是否启用「部」「卷」两级结构。

同时有两个必须在本阶段一起做掉的变更：

1. **旧界面要删**。Gradio 界面（`src/webui_app.py`）和 Flask 只读服务（`src/web_server.py`）
   都会被 005/006 的 FastAPI 应用完整取代。留着它们会导致两套代码都要跟着数据层改动走，
   而且它们都会抢 GPU 和端口。
2. **parse 不再自动注册角色**。现在 `src/llm_parser.py` 遇到清单外的说话人会自动注册一个
   继承 narrator 占位音色的新角色，结果是悄悄生成一堆音色全都一样的假角色。
   新语义是：把已注册角色的清单和简介连同正文一起送给大模型，模型只能从清单里选；
   选不到就留空（`speaker: null`），留空的分块后续在配音工作台里高亮，由人工指派。

## 前置条件

无。这是三部曲的第一步。

## 重要前提：现有数据全部作废

`chapters/` 下的全部内容都是测试数据，**本阶段直接删除整个目录，不做任何迁移**。

⚠️ **但删之前必须先做任务 1**——`cli.py` 的自检逻辑当前依赖
`chapters/ch_0001/raw.txt` 作为样例文本，先删目录会让 `python cli.py test --all` 立刻崩掉。

---

## 目标磁盘布局

```
library/                                  # 新增：小说库根目录
  <novel_id>/
    novel.yaml                            # 元信息 + 部/卷/章的树与排序
    chapters/
      ch_0001/                            # 章节隔离工作区，内部结构与现在完全一致
        raw.txt
        script_draft.json
        script_final.json
        timeline.json
        audio_cache/
        output/
        .status.json
    .trash/                               # 删除的节点移到这里，绝不物理删除
roles/                                    # 公共资产，顶级共享，不动
assets/                                   # 公共资产，顶级共享，不动
```

**核心设计决策：树结构只活在 `novel.yaml` 里，磁盘上章节目录永远是扁平的。**

这么做有三个理由，执行时不要擅自改成「按部/卷建子目录」：

1. 部/卷的增删和拖拽排序只需要重写 YAML，**不移动任何目录**——
   `audio_cache/` 里辛苦合成出来的 wav 零风险。
2. `process_chapter_parse` / `process_chapter_tts` / `mix_chapter` 都只接一个 `chapter_dir`
   参数，传绝对路径就能原样复用，**流水线代码一行不用改**。
3. `src/status_tracker.py` 的 `get_all_chapters_status(chapters_dir)` 本来就是「扫一层子目录」，
   传 `library/<nid>/chapters` 就直接能用，**零改动**。

## novel.yaml 完整 schema

```yaml
schema_version: 1
novel_id: nv_xianni
title: 仙逆
description: 顺为凡，逆则仙
levels:
  part: false          # 是否启用「部」层级
  volume: true         # 是否启用「卷」层级
next_chapter_seq: 3    # 下一个可用的章节序号，单调递增，删除不回收
created_at: "2026-09-13 10:00:00"
updated_at: "2026-09-13 10:30:00"
tree:                  # 数组顺序即权威排序，不要额外存 order 字段
  - type: volume
    id: vol_001
    title: 第一卷 凡人篇
    children:
      - type: chapter
        id: ch_0001
        title: 第一章 隐村
      - type: chapter
        id: ch_0002
        title: 第二章 测试
  - type: chapter      # levels.volume 为 true 时，章节也可以直接挂在根下
    id: ch_0003
    title: 番外
```

### 树的约束（`validate_tree` 必须强制执行）

- 节点 `type` 只能是 `part` / `volume` / `chapter`。
- `levels.part` 为 false 时，树里不允许出现 `part` 节点；`levels.volume` 同理。
- `chapter` 节点必须是叶子，不能有 `children`。
- `part` 的 children 只能是 `volume` 或 `chapter`；`volume` 的 children 只能是 `chapter`。
- 同一棵树内所有节点 id 必须唯一。
- `next_chapter_seq` 必须大于树中所有章节序号的最大值。

---

## 执行顺序

按顺序做，每完成一项跑一次 `../dev-env/venvs/novel2audiobook/bin/python -m pytest` 确认没把已有测试搞红。

- [ ] 任务 1：样例文本外置 + 修 `cli.py` 自检（**必须最先做**）
- [ ] 任务 2：新增 `src/library.py` 与 `tests/test_library.py`
- [ ] 任务 3：`src/llm_parser.py` parse 语义变更
- [ ] 任务 4：`src/tts_engine.py` 未绑定角色前置校验
- [ ] 任务 5：`src/audio_mixer.py` 加 `output_stem`
- [ ] 任务 6：`src/utils.py` 与 `src/status_tracker.py` 小改
- [ ] 任务 7：`cli.py` 子命令改造
- [ ] 任务 8：删除旧界面文件与 `chapters/` 目录
- [ ] 任务 9：`requirements.txt` 与 `global_config.yaml`

---

## 任务 1：样例文本外置 + 修 cli.py 自检

**这是删数据时最容易漏的一枪，必须最先做。**

`cli.py:34` 现在从 `chapters/ch_0001/raw.txt` 拷贝样例文本到隔离临时工作区。
删掉 `chapters/` 之后 `python cli.py test --all` 会直接 `FileNotFoundError`。

1. 新建 `tests/fixtures/sample_raw.txt`。内容要求：
   - 从原 `chapters/ch_0001/raw.txt` 里截取前若干段即可（几百字够了，自检要快）。
   - **必须包含至少一段「不在 `roles/roles_manifest.json` 清单里的人物」说的对话**，
     这样自检才能真正走到任务 3 引入的「未绑定角色」路径。
2. 修改 `cli.py:22 _make_isolated_test_workspace`：
   - `src_raw` 改为 `os.path.join(root, "tests", "fixtures", "sample_raw.txt")`。
   - 临时章节目录路径保持 `tmp_root/chapters/ch_0001` 不变（这是临时目录，跟 `library/` 无关，
     不要改成 `library/...`，改了没有任何好处）。
3. 修改 `tests/test_cli.py:67` 附近的「仓库污染」断言：把检查 `<root>/chapters` 是否被修改，
   改为检查 `<root>/library` 是否被修改。

---

## 任务 2：新增 src/library.py

新建 `src/library.py`。这是本阶段的核心新增模块。

### 模块级约定

- 所有对外函数都接受可选的 `library_dir: str = None` 参数，未传时才落到
  `resolve_path("library")`。**这是测试能用隔离 tmp 目录的关键，一个都不能漏。**
  参考 `src/roles.py` 里 `roles_dir=None` 的写法，保持一致。
- 写 `novel.yaml` 一律走原子写：先写 `<path>.tmp`、`flush` + `fsync`、再 `os.replace`。
  参考 `tools/gpu_arbiter.py:226 record_swap_seconds` 里已有的写法。
- 每本小说一把 `threading.RLock`，模块级字典缓存，字典本身用一把全局锁保护。
  拖拽排序和批量任务会并发写同一个 `novel.yaml`。
- **删除一律移动到 `library/<nid>/.trash/<时间戳>_<原名>/`，绝对不要用 `shutil.rmtree`。**
  用户合成一章可能花了一个多小时，误删不可接受。

### 函数清单

```python
LIBRARY_DIR_NAME = "library"
NOVEL_FILE_NAME = "novel.yaml"
TRASH_DIR_NAME = ".trash"
SCHEMA_VERSION = 1
VALID_NODE_TYPES = ("part", "volume", "chapter")


# ---- 路径 ----
def library_root(library_dir: str = None) -> str
def get_novel_dir(novel_id: str, library_dir: str = None) -> str
def get_chapters_dir(novel_id: str, library_dir: str = None) -> str
def get_chapter_dir(novel_id: str, chapter_id: str, library_dir: str = None) -> str
    """必须调用 src.utils.get_chapter_dir(chapter_id, base_dir=get_chapters_dir(...))，
    不要自己重新拼路径——章节 ID 归一化（'1' -> 'ch_0001'）的逻辑只能有一份。"""


# ---- 小说 CRUD ----
def slugify_novel_id(title: str, existing_ids: set) -> str
    """用 pypinyin 把中文标题转成 nv_<拼音> 形式的 ID；重名时追加 _2 / _3。
    参考 src/roles.py 里 register_role 生成 role_id 的写法，保持风格一致。"""

def list_novels(library_dir: str = None) -> list
    """返回 [{novel_id, title, description, levels, chapter_count, updated_at}, ...]，
    按 title 排序。目录里没有 novel.yaml 的子目录直接跳过（不要报错）。"""

def create_novel(title, description="", levels=None, library_dir=None) -> str
    """建目录、写初始 novel.yaml、返回 novel_id。levels 默认 {part: False, volume: False}。"""

def load_novel(novel_id, library_dir=None) -> dict
def save_novel(novel_data, library_dir=None) -> None
    """保存前必须先跑 validate_tree()，并刷新 updated_at。"""

def delete_novel(novel_id, library_dir=None) -> None
    """整个小说目录移到 library/.trash/ 下。"""


# ---- 树操作（都是纯函数，只改内存里的 novel_data，调用方负责 save_novel）----
def validate_tree(novel_data: dict) -> None
    """违反本文档「树的约束」一节的任何一条就抛 ValueError，错误信息要说清是哪个节点。"""

def find_node(novel_data, node_id) -> tuple
    """返回 (node, siblings_list, index)；找不到返回 (None, None, -1)。"""

def tree_insert(novel_data, parent_id, node, index=None) -> None
    """parent_id 为 None 表示插到根。index 为 None 表示追加到末尾。"""

def tree_move(novel_data, node_id, new_parent_id, new_index) -> None
    """拖拽排序用。必须防止把节点移动到它自己的子树里（会造成树断裂），检测到就抛 ValueError。"""

def tree_rename(novel_data, node_id, title) -> None

def tree_delete(novel_data, node_id) -> list
    """从树里摘掉该节点及其整棵子树，返回受影响的 chapter_id 列表。
    只改树，不碰磁盘——磁盘目录的搬迁由调用方根据返回值处理。"""

def iter_chapters(novel_data, node_id=None) -> list
    """深度优先按树的顺序返回 chapter_id 列表。node_id 为 None 表示整本。
    这是「批量任务选中范围」的基础函数：整本传 None，整部/整卷传对应 node_id。"""


# ---- 章节 ----
def alloc_chapter_id(novel_data) -> str
    """用 next_chapter_seq 生成 ch_XXXX 并把计数器加一。序号只增不减，
    删掉的章节号不回收——避免新章节复用旧目录名后命中残留的 audio_cache。"""

def add_chapter(novel_id, title, raw_text, parent_id=None, library_dir=None) -> str
    """分配 ID、建章节目录、写 raw.txt、插入树、保存。返回 chapter_id。"""

def import_chapter_raw(novel_id, chapter_id, raw_text, library_dir=None) -> dict
    """重新导入章节正文。会清空该章的 script_draft.json / script_final.json /
    timeline.json / output/，但**保留 audio_cache/**（按内容 md5 命名，
    万一用户撤销导入还能命中，而且删了也省不下多少磁盘）。
    返回 {removed: [...], kept_cache_count: N} 供上层做提示。"""

def delete_chapter(novel_id, chapter_id, library_dir=None) -> None
    """从树里摘掉 + 章节目录移到 .trash/。"""


# ---- 一致性 ----
def validate_tree_vs_disk(novel_id, library_dir=None) -> dict
    """返回 {"orphans": [磁盘上有目录但树里没有的 chapter_id],
             "missing": [树里有但磁盘上没目录的 chapter_id]}。
    用户手工动过目录、或者程序中途崩溃时用来发现漂移。本阶段只要能报出来就行，
    自动修复入口留给 006 的界面。"""
```

### tests/test_library.py 必须覆盖的用例

- `create_novel` + `load_novel` 往返，字段完整
- `slugify_novel_id` 中文转拼音正确、重名时追加后缀
- `levels.part=False` 时插入 part 节点抛 ValueError
- chapter 节点带 children 时 `validate_tree` 抛 ValueError
- `tree_move` 把节点移进自己的子树时抛 ValueError
- `iter_chapters` 对整本 / 单个 part / 单个 volume 三种范围返回的顺序正确
- `alloc_chapter_id` 删除章节后不复用旧号
- `delete_chapter` 后目录出现在 `.trash/` 下而不是消失
- `import_chapter_raw` 清掉了下游产物但 `audio_cache/` 还在
- `validate_tree_vs_disk` 能同时报出 orphans 和 missing
- 所有用例都用 `tmp_path`，**不允许碰真实 `library/` 目录**

---

## 任务 3：src/llm_parser.py parse 语义变更

这是本阶段第二大的改动。目标是：**大模型只能从已注册角色清单里选说话人，选不到就留空。**

### 3.1 改 prompt

新增渲染函数：

```python
def _render_role_table(manifest: dict) -> str:
    """把角色清单渲染成「- 中文名：简介」的多行文本，供 prompt 注入。
    简介来自 roles_manifest.json 的 description 字段，缺失时只输出名字。"""
```

修改 `_SYSTEM_PROMPT_TEMPLATE`（`src/llm_parser.py:131`）的第 2 条规则，从现在的
「不在列表中就直接输出中文全名」改为：

```
2. speaker 字段只能填 "narrator"（一切叙述性文字）或下面这份已注册角色清单里的中文名之一：
{role_table}
   如果某句台词的说话人不在这份清单里，speaker 必须填 null。
   不要编造新名字，也不要硬套一个不相干的角色——填 null 比填错更有价值，后续会由人工指派。
```

第 7 条的 JSON 示例里补一句说明 `speaker` 允许为 `null`。
`_build_system_prompt`（`:177`）里把 `role_names` 换成 `role_table=_render_role_table(manifest)`。

### 3.2 改 QwenLLMBackend 的响应解析

`parse_paragraph`（`:220`）里现在是
`"speaker": str(item.get("speaker", "narrator")).strip() or "narrator"`，
改成：

```python
raw_speaker = item.get("speaker")
speaker = None if raw_speaker is None else str(raw_speaker).strip()
# 小模型经常把 null 写成字符串，这里一并归一
if speaker in ("", "null", "None", "NULL", "未知", "无"):
    speaker = None
```

### 3.3 改 HeuristicBackend

`_make_segment`（`:114`）现在对话认不出说话人时回退 `"narrator"`，改为返回 `None`：

```python
speaker = speaker_hint if is_dialogue else "narrator"
```

（`guess_name_from_context` 本来就只返回 manifest 里存在的名字，所以它天然满足
「只从清单选」的新语义，不用改。）

### 3.4 改 parse_text_to_json —— 删掉自动注册

`src/llm_parser.py:290-303` 那整段「resolve 失败 → register_role → 失败回退 narrator
并把名字前缀塞进正文」的逻辑**整块删掉**，替换为：

```python
for seg in raw_segments:
    raw_speaker = seg.get("speaker")
    if raw_speaker is None:
        role_id = None                       # 未绑定，留给人工指派
    elif raw_speaker == "narrator":
        role_id = "narrator"
    else:
        role_id = roles_mod.resolve_role_id(raw_speaker, manifest)  # 认不出就是 None
```

`roles_mod.register_role` 和 `guess_name_from_context` **保留不删**——
它们改由用户在配音工作台手动新建角色时调用（006 阶段）。

同步更新模块顶部 docstring 第 10-11 行对「三层处理」的描述。

### 3.5 跟着改的测试

`tests/test_llm_parser.py` 里所有断言「清单外角色会被自动注册」的用例，
改为断言「清单外角色的 speaker 为 None，且 `roles_manifest.json` 没有新增条目」。

---

## 任务 4：src/tts_engine.py 未绑定角色前置校验

`generate_tts_incremental`（`src/tts_engine.py:209`）**函数最开头**加：

```python
unbound = [seg.get("seg_id") for seg in script_final_data if not seg.get("speaker")]
if unbound:
    shown = unbound[:20]
    more = f"（共 {len(unbound)} 个，只列前 20 个）" if len(unbound) > 20 else ""
    raise ValueError(
        f"以下分块尚未绑定角色，无法合成{more}：seg_id={shown}。"
        "请先在配音工作台为它们指派角色。"
    )
```

**不要**静默回退成 narrator——那正是这次要根除的行为。

### 连带影响：cli.py 自检会被这条校验挡住

`cli.py:83` 的自检把 `script_draft.json` 直接拷成 `script_final.json` 就去跑 TTS。
任务 3 之后草稿里会含 `speaker: null` 的分块，TTS 就会抛错。

在 `run_test_module` 的 `run_tts` 分支里、拷贝 `script_final.json` 之后，加一步
「模拟人工定稿」：把所有 `speaker` 为空的分块指派为 `narrator`，并打印
`  [Test] 将 N 个未绑定分块指派为 narrator（模拟人工定稿）`。
这既让自检能跑通，也顺带验证了未绑定分块确实产生了。

---

## 任务 5：src/audio_mixer.py 加 output_stem

`mix_chapter`（`src/audio_mixer.py:189`）签名末尾加 `output_stem: str = None`。

在决定输出文件名的地方（`:274` 附近，现在是从目录 basename 剥 `ch_` 前缀拼
`chapter_<num>.mp3`），改成：

```python
if output_stem is None:
    # 默认行为保持不变，现有测试依赖这个命名
    base = os.path.basename(os.path.abspath(chapter_dir))
    num = base[3:] if base.startswith("ch_") else base
    output_stem = f"chapter_{num}"
```

Web 层后续会传 `f"{novel_id}_{chapter_id}"`，解决跨小说下载时文件重名的问题。
**默认值必须保持原样**，否则 `tests/test_audio_mixer.py` 会红。

---

## 任务 6：src/utils.py 与 src/status_tracker.py 小改

### src/utils.py

- `update_chapter_status`（`:86`）加可选参数 `chapter_id: str = None`，
  内部 `ch_id = chapter_id or os.path.basename(...)`。
- **`normalize_chapter_id`（`:53`）和 `get_chapter_dir`（`:61`）一个字都不要动。**
  `tests/test_utils.py:111-123` 断言了 `get_chapter_dir("0001") == "chapters/ch_0001"`，
  多小说寻址一律走 `src/library.py`。

### src/status_tracker.py

现有函数**零改动**。新增：

```python
def get_novel_status_summary(novel_id: str, library_dir: str = None) -> dict:
    """返回 {"total": N, "by_status": {"completed": 3, ...},
             "chapters": [get_all_chapters_status 的原结构]}。
    内部直接调 get_all_chapters_status(library.get_chapters_dir(novel_id, library_dir))。"""
```

---

## 任务 7：cli.py 子命令改造

### 新增 `novel` 子命令

```
python cli.py novel list
python cli.py novel create --title 仙逆 [--description "..."] [--part] [--volume]
python cli.py novel delete --novel nv_xianni
```

### 新增 `node` 子命令（建部/卷，界面出来之前用它搭结构）

```
python cli.py node add --novel nv_xianni --type volume --title "第一卷" [--parent <node_id>]
python cli.py node rm --novel nv_xianni --node vol_001
```

### 新增 `chapter` 子命令

```
python cli.py chapter add --novel nv_xianni --title "第一章" --raw path/to.txt [--parent vol_001]
python cli.py chapter list --novel nv_xianni          # 缩进打印整棵树，带每章状态
python cli.py chapter rm --novel nv_xianni --chapter ch_0001
python cli.py chapter reimport --novel nv_xianni --chapter ch_0001 --raw path/to.txt [--yes]
```

`chapter reimport` 在交互式终端下必须先打印后果并要求确认：
`该操作会清空 ch_0001 的剧本、时间线和成品 MP3（audio_cache 保留），确认继续？`
沿用 `cli.py:141 _prompt_yes_no` 已有的写法，`--yes` 跳过。

### 改造现有子命令

- `parse` / `tts` / `mix`：新增 **`--novel <novel_id>`（必填）**，
  `chapter_dir` 改为 `library.get_chapter_dir(args.novel, args.chapter)`。
  **不要保留「不传 --novel 就走扁平 chapters/」的兼容模式**——数据反正清空了，
  留双路径只会长期拖累每一处改动。
- `status`：新增 `--novel <novel_id>`（可选）。传了就打印该小说的章节表；
  不传就列出所有小说的汇总（小说名 / 章节数 / 各状态计数）。
- **删除 `serve` 子命令**（`:229` 的 parser 和 `:321` 的分支一起删）。
- **删除 `webui` 子命令**（`:241` 的 parser 和 `:367` 的分支一起删）。
  005 阶段会以 FastAPI 的形式重新加回来，端口仍是 7860。

`tts-serve` 和 `assets` 子命令不动。

---

## 任务 8：删除文件

```
webui.py
src/webui_app.py
tests/test_webui_app.py
src/web_server.py
tests/test_web_server.py
chapters/            （整个目录）
```

删完后全局搜索一遍 `webui_app`、`web_server`、`gradio`、`from flask`，
确认没有任何残留引用（`cli.py` 的 import 段、`README.md`、`AGENTS.md`、`CLAUDE.md`）。

`CLAUDE.md` 和 `AGENTS.md` 里关于 `cli.py serve` / `cli.py webui` 的说明要同步更新为
「已移除，等待计划 005 重新提供」，并把 `parse/tts/mix` 的示例命令补上 `--novel`。

---

## 任务 9：requirements.txt 与 global_config.yaml

### requirements.txt

移除 `gradio>=5.50.0,<6.0.0` 和 `flask>=3.0.0`。

⚠️ **fastapi / uvicorn / pydantic / python-multipart / aiofiles 目前是 gradio 的传递依赖，
卸掉 gradio 会把它们一起带走。必须显式写进 requirements.txt。**
版本号用 `../dev-env/venvs/novel2audiobook/bin/pip show <pkg>` 查到的实际版本钉住：

```
fastapi==0.141.1
uvicorn[standard]==0.52.4
pydantic==2.12.3
python-multipart==0.0.32
aiofiles==<pip show 查到的版本>
```

### global_config.yaml

新增 `server:` 段（本阶段只用得到前三个，其余 005 阶段补）：

```yaml
server:
  library_root: "library"   # 小说库根目录，相对项目根
  host: "127.0.0.1"
  port: 7860
```

---

## 验收清单

全部命令用项目 venv 执行（`../dev-env/venvs/novel2audiobook/bin/python`）。

1. **单元测试全绿**
   ```
   ../dev-env/venvs/novel2audiobook/bin/python -m pytest -q
   ```

2. **自检全绿，且不污染仓库**
   ```
   ../dev-env/venvs/novel2audiobook/bin/python cli.py test --all
   git status --short          # 必须是空的
   ```
   自检输出里应该能看到「将 N 个未绑定分块指派为 narrator」这一行。

3. **两本小说端到端跑通**
   ```
   ../dev-env/venvs/novel2audiobook/bin/python cli.py novel create --title 测试书甲 --volume
   ../dev-env/venvs/novel2audiobook/bin/python cli.py novel create --title 测试书乙
   ../dev-env/venvs/novel2audiobook/bin/python cli.py node add --novel nv_ceshishujia --type volume --title 第一卷
   ../dev-env/venvs/novel2audiobook/bin/python cli.py chapter add --novel nv_ceshishujia --title 第一章 \
       --raw tests/fixtures/sample_raw.txt --parent vol_001
   ../dev-env/venvs/novel2audiobook/bin/python cli.py chapter add --novel nv_ceshishuyi --title 第一章 \
       --raw tests/fixtures/sample_raw.txt
   ../dev-env/venvs/novel2audiobook/bin/python cli.py chapter list --novel nv_ceshishujia
   ../dev-env/venvs/novel2audiobook/bin/python cli.py status
   ```
   两本小说的 `ch_0001` 目录互不干扰。

4. **parse 新语义生效**（需要 llama-server 在跑；不在跑就是走 HeuristicBackend，同样要验）
   ```
   ../dev-env/venvs/novel2audiobook/bin/python cli.py parse --novel nv_ceshishujia --chapter 0001
   ```
   - `library/nv_ceshishujia/chapters/ch_0001/script_draft.json` 里存在
     `"speaker": null` 的分块
   - `roles/roles_manifest.json` **没有**新增任何角色
   - 正文里没有出现 `角色名：` 这种被塞进 text 的前缀

5. **未绑定角色会挡住 TTS**
   直接把 draft 拷成 final 然后跑 tts，应该收到明确的错误信息列出 seg_id：
   ```
   cp library/nv_ceshishujia/chapters/ch_0001/script_{draft,final}.json
   ../dev-env/venvs/novel2audiobook/bin/python cli.py tts --novel nv_ceshishujia --chapter 0001 --yes
   ```

6. **补齐角色后能跑通全链路**
   手工把 final 里的 null 都改成 `narrator`，然后：
   ```
   ../dev-env/venvs/novel2audiobook/bin/python cli.py tts --novel nv_ceshishujia --chapter 0001 --yes
   ../dev-env/venvs/novel2audiobook/bin/python cli.py mix --novel nv_ceshishujia --chapter 0001
   ```

7. **依赖干净**
   ```
   ../dev-env/venvs/novel2audiobook/bin/pip uninstall -y gradio flask
   ../dev-env/venvs/novel2audiobook/bin/python -m pytest -q          # 仍然全绿
   ../dev-env/venvs/novel2audiobook/bin/python cli.py test --all     # 仍然全绿
   ```
   如果这一步报 `ModuleNotFoundError: fastapi`（或 pydantic / multipart / aiofiles），
   说明任务 9 的显式声明漏了。

8. **旧界面彻底消失**
   ```
   grep -rn "webui_app\|web_server\|gradio\|from flask" --include=*.py --include=*.md . \
       | grep -v tools/indextts_repo
   ```
   应该没有输出（`tools/indextts_repo/` 是 IndexTTS 官方源码，不归我们管）。

---

## 给执行者的注意事项

- **不要改 `src/utils.py` 的 `normalize_chapter_id` 和 `get_chapter_dir`。**
  已有测试断言了它们的返回值，多小说寻址走 `src/library.py`。
- **不要改 `src/status_tracker.py` 的现有函数**，只新增 `get_novel_status_summary`。
- **不要在 `src/library.py` 里重新实现章节路径拼接**，调用 `src/utils.py` 的 `get_chapter_dir`。
- **不要用 `shutil.rmtree` 删小说或章节**，一律移到 `.trash/`。
- **每个新函数都要能接受 `library_dir=None` 注入**，否则测试没法隔离，会污染真实数据。
- **不要给 `parse/tts/mix` 保留「无 --novel 的兼容模式」**，也不要留 `chapters/` 的向后兼容代码。
- 注释风格跟现有代码保持一致：中文，只解释「为什么这么写」，不复述「这行在做什么」。
  遇到故意吞异常的地方沿用 `# noqa: BLE001` 的标注习惯。
- 每改完一个任务就跑一次 `pytest`，不要攒到最后一起调。
