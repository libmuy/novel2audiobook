# 计划 011：按新 Cloud Design 稿重写 Web 界面

> 状态：已实现。

## 背景

用户在 Claude Cloud Design 又出了一版新界面原型（项目 `c192401f-708e-412d-b3f3-3add28dd3e64`，
`Novel2Audiobook.dc.html` + `support.js`），要求**删掉现有界面**（计划 006–010 交付、007 换过一次皮的
版本），按新设计稿重新实现前端，后端按需补齐。跟计划 007「只换视觉层」不同，这次是前端全量重写：新
设计稿是纯前端假数据原型（React 风格的 dc 运行时），项目约定是 Vue3 全局构建、无构建工具、离线运行，
两者技术栈不同，且设计稿的页面/交互比现有实现少（比如没有工作台逐块素材设置、素材引擎漂移提示、角色
预计算音色、混音参数、任务日志展开）。

已与用户确认的三条前提：
1. 设计稿之外、现有实现已有的功能全部保留，按新设计语言重做，不因为"设计稿没画"就砍掉。
2. 前端移植为 Vue3 全局构建（`window.N2A` 共享状态 + IIFE 组件），不引入 React/dc 运行时。
3. 后端暂不支持的设置项（Edge TTS 等）界面保留（禁用态），本文档列出未实现清单。

## 一、后端补齐（`src/domain/`、`src/api/routers/`）

先做这些，全部带 pytest（`tests/test_api.py`、`tests/test_library.py`、`tests/test_status_tracker.py`、
`tests/test_asset_categories.py`、新增 `tests/test_events.py`）：

- **章节状态四态归一**：`status_tracker.normalize_state()` 把内部状态标签/产物存在情况归一成
  `unparsed`/`parsed`/`voiced`/`stale`，`get_all_chapters_status` 每行带 `state` 字段；`/novels/{nid}/tree`
  把 `state`/`raw`/`missing_assets_count` 一并内联进每个 chapter 节点。
- **小说列表附计数**：`library.list_novels()` 每本小说带 `counts:{total,unparsed,parsed,voiced,stale}`，
  消掉列表页逐本再拉一次 `/tree` 统计的 N+1。
- **章节对比表数据**：`chapter-stats` 每章加 `bgm_count`/`sfx_count`/`mixed_with_assets`。
- **SSE 资源事件**：推送间隔改读 `server.monitor_interval_ms`（不再硬编码 1000ms）；payload 加
  `gpu_owner`（`src/api/routers/events.py::_current_owner()`，3 秒节流缓存，避免每次资源推送都去探测
  llama-server 端口）。
- **角色**：`RoleUpdate` 加 `gender`；`speed` 改动同时写 `data/roles/<id>/config.json`——之前只改
  manifest，TTS 合成实际读的是 `config.json`，界面调语速从未真正生效过（真 bug，不是这次顺带改的口味）。
- **分类树重映射**：`category_tree.diff_paths(old_tree, new_tree)` 按节点 id 比较保存前后的树，`PUT
  /role-category-tree`、`PUT /asset-category-tree` 据此同步重映射角色/素材的 `category` 字符串——改名
  的分类换成新路径，删除的分类清空为「未分类」。**这是对计划 008/010 已有行为的改动**：之前的策略是
  「悬空引用容忍」（删除/改名分类不碰角色自己的 category 字段），`tests/test_api.py`/
  `tests/test_asset_categories.py` 里原本锁着这条行为的用例已经改写并保留新行为的断言。
- **草稿自动转正**：`library.promote_draft_to_final()`，只有 `script_draft.json`（还没跑过 TTS/没人工
  定过稿）的章节，第一次分块编辑会原子转正成 `script_final.json`，不再对这类章节的分块 PATCH 一律 404；
  `GET script` 加 `X-Script-Source: final|draft` 响应头；写 `script_final.json` 改原子写（`.tmp` +
  `os.replace`）。
- **语气校验**：`SegmentUpdate`/`SegmentBatch` 的 `emotion` 校验 `llm_parser.VALID_EMOTIONS`，非法值 400。
- **节点改名/reorder**：加小说锁（`library.novel_lock`，公开别名指向原来的 `_get_novel_lock`）避免并发
  覆盖；`reorder` 的 `tree_move` 抛 `ValueError`（挪进自己子树）改映射成 400，之前是未处理异常变 500。

## 二、前端重写

删除 `src/web/static/{index.html,css/app.css,js/*.js}` 与 `vendor/fonts/archivo-latin.woff2`；保留
`vendor/vue.global.prod.js`、`vendor/Sortable.min.js`（拖拽排序继续用它——真实鼠标拖拽回归测试
`TestTreeDragDrop::test_repeated_real_drags_reorder_siblings` 依赖它，视觉按设计稿重做）；新增
`vendor/fonts/manrope-latin.woff2`（从 Google Fonts 下载后本地化，变量字体单文件覆盖 400–800 全部字重）。

### 文件结构

```
src/web/static/
  css/tokens.css        浅色/深色静态变量；主题色派生色用 CSS color-mix() 现场算（不用 JS 搬一遍配色算法）
  css/app.css            语义化布局/组件样式，860px 响应式断点
  js/api.js              fetch 封装 + 全部端点方法；SSE 指数退避自动重连（1s→2s→…→封顶 30s）
  js/core.js             window.N2A：toast、弹窗服务（Promise 化）、usePaneSize、hash 路由、主题应用、任务存储
  js/components/
    modal-host.js         通用弹窗（取代原生 prompt/confirm/alert 与旧版的三个独立对话框组件）
    toast-stack.js
    tree-node.js           小说树节点，递归 + 同父节点内 SortableJS 排序
    category-node.js       角色库/素材库分类树节点，递归
    category-pane.js       分类树组合式函数 useCategoryTree（增删改都整树 PUT，服务端补 id 后刷新本地）
    task-panel.js           本书任务/全局任务分组、进度、取消、日志展开
  js/pages/
    novels.js novel-detail.js workbench.js roles.js assets.js settings.js
  js/app.js               根组件：顶栏、资源条、路由出口、SSE 接线、深色模式
```

### 关键实现细节（踩过的坑）

- **`body { height:100vh; overflow:hidden }` 不是 `min-height`**：设计稿原型用 `min-height:100vh`，照抄
  会导致长列表（工作台 500 个分块）把整个 `<body>` 撑高，`.wb-segment-list` 的 `flex:1;overflow:auto`
  永远量不出"超出一屏"的部分，虚拟滚动和内部滚动条全部失效，变成整页可以无限往下滚。同理
  `.n2a-treepane`/`.wb-tree` 补了 `min-height:0`（flex column 子项的经典坑）。`.n2a-main` 补
  `overflow-y:auto` 给不自己管理内部滚动区域的简单页面（小说列表/设置/角色库/素材库）用。
- **小说树的根级也要挂 Sortable**：`tree-node.js` 只给每个节点自己的子节点容器挂 Sortable，小说树的
  **根级列表**（无部/卷层级时的顶层章节）容易被漏掉——它由页面组件而不是递归组件渲染，novel-detail.js
  里单独复刻了一份跟 tree-node.js 一致的 rebind-on-mounted/updated 逻辑。
- **拖拽只在同一父节点的兄弟间进行**：不给每层 Sortable 实例设共享 `group`，天然阻止跨父级拖拽——后端
  `library.tree_move` 不校验节点类型跟目标父级是否合法（可以把章节拖进部级），前端用这个限制规避掉。
- **分块编辑只发真的改过的字段**：`workbench.js::saveSegment()` 会把 `editDraft` 跟原始分块逐字段 diff，
  只有变了的字段才进 PATCH body——分块引用着一个已经不存在的素材（生成脚本被删过）时，如果无脑把
  `sfx`/`bgm` 原样带回去，后端「素材必须存在」的校验会让用户只是想改改文字都 400。
- **SSE 事件和 HTTP 响应的到达顺序没有保证**：`roles.js::precompute()` 提交预计算任务后，不能只信
  `POST /tasks` 响应体里的任务快照——环境未就绪时任务失败得比这个 await 还快，SSE 是另一条早就建立好的
  长连接，它的 `task_update`（连带 `onTaskUpdate` 里清空"计算中"状态、弹结果 toast）完全可能先于这个
  await 返回。提交后要跟 `N2A.tasksById`（SSE 写入的最新状态）取一次最新值，不能無条件把"还在算"的状态
  覆盖回去，否则按钮会卡死在"预计算中…"（这条真实竞态是本轮测试跑出来的，不是臆测）。
- **下拉框要给"已经不在列表里"的当前值补一个可见选项**：分类被删过的角色/素材、素材被删过的分块，
  编辑弹窗/工作台的 `<select>` 如果找不到匹配的 `<option>` 会直接显示空白——看起来像"没设"，容易被
  误保存成清空。`modal-host.js`（分类）和 `workbench.js`（bgm/sfx）都补了"缺失值"兜底选项。

### 界面 ↔ 后端映射要点

- 批量任务预检弹窗用真实 `POST /tasks/preflight`（create/overwrite/skip 计数、失效缓存数、预计 GPU
  分钟），不是设计稿里的假算式。
- 「重新生成本块人声」「批量生成人声」= 提交整章增量 TTS（后端粒度只到章级），弹窗/toast 如实说明；
  选中范围里有未绑定角色的分块会先拦下来。
- 「仅人声」播放 `segments/{id}/audio`；「混音预览」播放章节成品 `output.mp3`。
- 外观偏好（主题色/圆角/字体/深色模式）是浏览器本地偏好，存 `localStorage['n2a.ui']`，改动立即生效，
  不走「保存配置」；其余设置走 `PATCH /api/config`，只提交真正改动过的键（`??` 不吞 0，区分"从没设过"
  和"清空了一个原本有值的字段"两种空值语义，后者才拦截保存）。
- 设置页新增「高级混音参数」折叠区（句间静音、闪避阈值/衰减/渐变、背景音电平、音效限幅），延续计划
  010 阶段 3 定下的字段与校验规则。

## 三、未实现 / 待后续（界面保留，如实标注，不假装能用）

- **TTS 引擎「Edge TTS」**：设置页下拉里保留这一项但标 disabled + "未接入，暂不可用"；后端只支持
  IndexTTS。
- **后台并发任务数改动需要重启**：`server.cpu_workers` 只在任务队列创建时读一次，设置页保存后提示需要
  重启 `./run.sh webui` 才生效。
- **单块独立合成**：后端只有整章增量 TTS，没有单分块级别的合成接口；「重新生成本块人声」的弹窗文案
  如实说明这一点。
- **自定义音频上传为素材、单个素材指定生成引擎**：后端只支持生成，不支持上传；引擎按 `ambience`/`sfx`
  两个大类在配置里选，不能素材级别覆盖。
- **Noto Sans SC 未本地化**：只打包了 Manrope（拉丁文变量字体，24KB）；中文字体走系统字体栈
  （`PingFang SC`/`Microsoft YaHei` 等），离线可用但不保证跨系统视觉一致。
- **任务历史跨重启不恢复**：`task_queue._load_tasks_from_disk` 从未被调用（计划 010 就记录的已知缺口，
  这次没有顺手修）；任务组整体取消没有专门的 API 路由，只能逐个任务取消。

## 验证

```bash
./run.sh test --all                                                    # 自检管线，CLAUDE.md 要求
for f in $(find src/web/static/js -name "*.js"); do node --check "$f"; done
grep -rn "googleapis\|gstatic\|cdn\.\|cdnjs\|jsdelivr\|unpkg" src/web/static/   # 必须为空（离线）
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python -m pytest tests/ -q --ignore=tests/test_web_ui.py  # 747 passed
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python -m pytest tests/test_web_ui.py -v                  # 57 passed
```

Playwright 覆盖：离线可用（无 CDN、字体本地served）、路由记忆、真实鼠标两次拖拽（同父节点兄弟重排）、
未绑定角色真跑一次 parse 后的红色高亮、批量任务预检+提交、500 分块虚拟滚动（DOM 内同时 <120 张卡片）、
资源条数值、SSE 断线徽章 + 自动重连、深色模式 + 主题色/圆角即时生效、面板拖宽持久化、860px 窄屏断点、
角色/素材分类树 CRUD + 标签筛选 + 改名删除后的重映射、素材 CRUD + 引擎漂移提示、工作台逐块/批量设置
背景音音效 + 同步进 timeline、任务面板本书/全局分组 + 日志展开 + 取消确认文案、设置页只提交改动键 +
0 是合法值 + 越界拒绝带原因 + 空值拦截、预计算音色（含失败原因上报、按钮状态在竞态下依然正确）、应用内
对话框取代原生 prompt/confirm（含 Enter 提交/Escape 取消/失败上报不静默）。
