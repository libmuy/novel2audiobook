# 计划 007：Cloud Design 界面重构（换皮，不换后端）

> 状态：已完成。用户在 Claude Cloud Design 生成了一套新界面原型（项目
> `系统UI重构讨论`，`Novel2Audiobook.dc.html` + `support.js` + `github.md`），
> 要求把它跟本地代码结合、替换计划 006 交付的手写界面。本计划记录改造范围、
> 与设计稿的偏离点（都是后端能力边界导致，不是偷懒少做）、以及验证结果。

## 背景

计划 006 交付的界面功能完整（四轮 review 补漏，无已知遗留问题），但视觉是
朴素的手写样式。设计稿采用「Modernist」设计语言：红/白配色、零圆角、强分割线、
Archivo 字体、深色模式 token 全套、外加一个全新的「背景音/音效库」页面。

## 改造范围

**只换视觉层，不动后端与业务逻辑。**

- `web/static/css/app.css`：按设计稿的配色/字体/间距 token 整体重写，
  CSS 变量区分浅色/深色（`:root` / `:root[data-theme="dark"]`）。
- `web/static/index.html`：外壳按设计稿重排（顶栏导航加「音效库」tab、
  深色模式切换按钮），Vue 挂载点、路由出口、`<toast>`/`<confirm-dialog>` 不变。
- `web/static/js/app.js`：**绝大多数组件的 `setup()` 逻辑原样保留**——
  SSE 具名事件监听、multipart 上传、`speaker` 存角色 ID、`emotion` 字段、
  删除带 `confirm=1`、批量任务两步 preflight、虚拟滚动、SortableJS 每次
  `loadTree()` 后无条件重挂——这些都是计划 006 四轮 review 踩过坑修出来的
  行为，一处没动。改动只有：
  - 各页面 `template` 里的 class/文案按设计稿调整（大量 class 名称本来就
    语义清晰，直接复用，只重写了 CSS 实现，模板本身没改）；
  - 去掉了几处装饰性 emoji（✏️/🗑️/📚/📝/📋/👤/🔄），改成设计稿风格的纯文字
    按钮（编辑/删除/改/删）或直接省略；
  - 树形目录的多选框从 emoji 字符（☑️/☐）换成真正的 `<input type="checkbox">`；
  - 新增深色模式：`darkMode` 状态存 `localStorage['n2a.darkMode']`，
    `applyTheme()` 把 `data-theme` 属性写到 `document.documentElement`
    （不是 `#app`——`#app` 是 Vue 挂载目标，容器自身属性不会被当成模板
    绑定编译，这是趟出来的一个坑，细节见 `app.js` 里 `applyTheme` 的注释）；
  - 新增 `#/assets` 路由 + `assets-page` 组件（见下）。
- `web/static/vendor/fonts/archivo-latin.woff2`：Archivo 变量字体本地化
  （只下了 latin 子集——中文本来就不在这套字体的覆盖范围内，走 fallback
  字体栈；离线可用是硬约束，不能挂 Google Fonts CDN）。
- `web/static/js/api.js`：只加了一个只读方法 `getAssets()`
  （`GET /api/assets`，后端早就有这个端点，只是原来前端没接）。
- `tests/test_web_ui.py`：新增深色模式测试、新增素材库页面的导航断言；
  **原有的类名断言（`.resource-bar`/`.segment-card`/`.segment-card.unbound`/
  `.stat-value.danger`/`[data-node-id]`/`n2a.lastRoute` 等）一个没改**——
  新 CSS 刻意复用了旧 class 名称，所以这些测试其实不需要跟着改，
  验证下来也确实全部保持绿。

## 与设计稿的偏离点（后端能力边界，均已诚实处理，不假装能用）

设计稿的原型是纯前端 mock（`Novel2Audiobook.dc.html` 里 `state.novels` 等
全是硬编码假数据，没有真实网络请求），有几处设计在真实后端上没有对应能力。
按「不留假 UI」的既有约定（计划 006 已经定下的原则），这些统一做成诚实的
只读/占位展示，而不是照抄设计稿功能但接不上后端：

1. **背景音/音效库页面**：后端只有 `GET /api/assets`
   （`src.utils.list_available_assets`，扫描 `assets/ambience`、`assets/sfx`
   目录拿文件名），没有分类树、标签、CRUD、也没有音频流式播放端点——
   effects 生成流水线本身还没打通（`global_config.yaml` 的
   `mixing.voice_only` 默认 `true`，`CLAUDE.md` 里也写明"效果音流水线待
   后续阶段再打通"）。新页面做成纯只读名称列表 + 顶部提示横幅说明现状，
   不渲染设计稿里的分类树/标签筛选/新增编辑弹窗/试听播放器。
2. **角色分类**：后端 `roles_manifest.json` 顶层 `categories` 是**扁平字符串
   数组**（`GET/PUT /api/role-categories`），不是设计稿里的嵌套分类树。
   继续用原有的扁平标签筛选 UI（`.category-tag`），没有实现嵌套折叠树。
3. **角色标签系统**：设计稿角色卡片上的 `tags`（`['少年','隐忍']` 这类）
   完全是原型假数据，`RoleCreate`/`RoleUpdate` schema 和 `GET /roles` 返回
   都没有这个字段，未实现。
4. **可拖拽调整宽度的分栏（resizer）**：设计稿里树形面板/工作台面板都能
   横向拖拽改变宽度。纯前端功能、不依赖后端，但为控制本次改造范围没有
   实现，面板宽度维持固定值。
5. **多选章节的对比表格**：设计稿在勾选多个章节时会显示一个表格
   （文本上传/已解析/分块数/人声/背景音/混音合成六列）。其中"背景音生成"
   列在后端没有对应状态字段（跟第 1 点一样，effects 流水线没打通），且
   逐章拉取统计会引入明显的架构变化，本次未实现——多选章节仍然只作为
   批量任务的范围输入，不展示对比表格。
6. **拖拽排序机制**：设计稿用原生 HTML5 `draggable`/`onDragStart`/
   `onDrop`。保留了原来的 SortableJS 实现（基于 mousedown/mousemove），
   因为 `tests/test_web_ui.py::TestTreeDragDrop` 用 `page.mouse.down/move/up`
   模拟真实鼠标拖拽验证"连续两次拖拽都要生效"这个计划 006 踩过的真实 bug——
   原生 HTML5 拖放需要真正的浏览器拖放手势或显式派发 dragstart/dragover/drop
   事件，合成的鼠标移动序列触发不了它，换成原生拖放会让这个关键回归测试
   失效。视觉上已经按设计稿的行样式重做（复选框、状态点、强调色左边框）。
7. **设置页"模拟连接断开"演示按钮**：设计稿里这是给原型演示用的假开关，
   没有接入真实状态。我们的 SSE 断线检测是真实机制（连续 3 次 `onerror`
   触发），没有必要也不应该加一个可以撒谎的手动开关，未实现。

## 验证

```bash
source ../dev-env/venvs/novel2audiobook/bin/activate
node --check web/static/js/app.js && node --check web/static/js/api.js
python -m pytest tests/ -q --ignore=tests/test_web_ui.py   # 401 passed
python -m pytest tests/test_web_ui.py -v                    # 19 passed, 1 skipped（跟本次改动无关的既有 skip）
python cli.py test --all                                    # 全部指标正常
```

真人在浏览器里过了一遍（headless Chromium 截图核对，见改造过程记录）：
小说列表、小说详情（树形目录+批量任务+任务队列）、配音工作台（未绑定红色
高亮、语气/角色下拉、试听按钮）、角色库空态、素材库只读态+提示横幅、
系统配置（分区分割线）、新增小说弹窗——浅色/深色两套主题都核对过，
虚拟滚动（500 分块只渲染 <100 张卡片）和连续两次真实拖拽排序都验证通过。

## 已知后续 TODO（不在本次范围内）

- 角色分类 / 素材分类改成嵌套树（需要后端 `roles_manifest.json` /
  素材元数据从扁平数组升级成树结构）。
- （后端部分已在计划 008 完成：CRUD、角色分类树与标签、素材生成任务；音频流式播放端点仍待做）素材库补上 CRUD 端点、标签字段、音频流式播放端点，之后可以把设计稿里
  完整的分类树 + 标签筛选 + 新增/编辑/试听接上。
- 角色打标签（需要 `RoleCreate`/`RoleUpdate` schema 加 `tags` 字段）。
- 面板宽度可拖拽调整（纯前端，可以随时补，非阻塞）。
