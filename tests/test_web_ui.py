"""
Playwright 端到端测试：计划 011 重写后的 Web 前端（Vue3 全局构建，无构建工具）。

覆盖范围延续计划 006–010 定下的真实行为断言（离线可用、页面记忆、真实鼠标
两次拖拽、未绑定角色真跑一次 parse、虚拟滚动、资源条、SSE 断线重连、深色
模式、分类树+标签 CRUD、工作台逐块素材、任务面板分组/日志/取消、设置页
只提交改动键、预计算音色、素材引擎漂移、应用内对话框取代原生 prompt/
confirm/alert）；选择器全部换成新界面的类名，另外新增：面板拖宽持久化、
主题色/圆角/字体即时生效、SSE 自动重连、窄屏断点。

Usage:
    /srv/unsafe/dev-env/venvs/novel2audiobook/bin/python -m pytest tests/test_web_ui.py -v
"""

import re
import os
import sys
import time
import json
import threading
import socket
import shutil
import tempfile
import pytest

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    pytest.skip("playwright not installed", allow_module_level=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import uvicorn
from src.api.app import create_app
from src.api import deps
from src.runtime.task_queue import TaskQueue


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return server, thread
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start on port {port}")


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    library_dir = tmp / "data" / "library"
    roles_dir = tmp / "data" / "roles"
    tasks_dir = tmp / "tasks"
    cache_dir = tmp / ".cache"
    for d in [library_dir, roles_dir, tasks_dir, cache_dir]:
        d.mkdir(parents=True)

    import src.utils
    import src.domain.derived_index
    import src.runtime.preflight
    import src.runtime.task_queue as tq_mod

    _orig_root = getattr(src.utils, "PROJECT_ROOT", None)
    _orig_di_root = getattr(src.domain.derived_index, "PROJECT_ROOT", None)
    _orig_tq_root = getattr(tq_mod, "PROJECT_ROOT", None)
    _orig_pf_root = getattr(src.runtime.preflight, "PROJECT_ROOT", None)
    _orig_pf_stats = getattr(src.runtime.preflight, "TTS_STATS_PATH", None)
    _orig_di_refs = getattr(src.domain.derived_index, "ROLE_REFS_PATH", None)

    web_static = tmp / "src" / "web" / "static"
    web_static.mkdir(parents=True, exist_ok=True)
    real_static = os.path.join(PROJECT_ROOT, "src", "web", "static")
    if os.path.isdir(real_static):
        shutil.copytree(real_static, web_static, dirs_exist_ok=True)

    src.utils.PROJECT_ROOT = str(tmp)
    src.domain.derived_index.PROJECT_ROOT = str(tmp)
    src.domain.derived_index.ROLE_REFS_PATH = str(tmp / "cache" / "index" / "role_refs.json")
    tq_mod.PROJECT_ROOT = str(tmp)
    src.runtime.preflight.PROJECT_ROOT = str(tmp)
    src.runtime.preflight.TTS_STATS_PATH = str(tmp / "tts_stats.json")

    config = {
        "server": {
            "library_root": str(library_dir), "host": "127.0.0.1", "port": 0,
            "cpu_workers": 2, "gpu_chunk_size": 8, "monitor_interval_ms": 1000,
            "task_retention_days": 7,
        },
        "llm": {"api_base": "http://localhost:1/v1"},
        "tts": {"engine": "Index-TTS-2.5"},
        "mixing": {"voice_only": True, "output_format": "mp3"},
    }
    deps.set_config(config)
    queue = TaskQueue(config=config, tasks_dir=str(tasks_dir), library_dir=str(library_dir))
    deps.set_queue(queue)
    queue.start()

    port = _free_port()
    app = create_app()
    server_obj, thread = _start_server(app, port)

    yield {"port": port, "tmp": tmp, "library_dir": library_dir, "roles_dir": roles_dir}

    server_obj.should_exit = True
    thread.join(timeout=5)
    queue.stop(wait=False)
    if _orig_root is not None:
        src.utils.PROJECT_ROOT = _orig_root
    if _orig_di_root is not None:
        src.domain.derived_index.PROJECT_ROOT = _orig_di_root
    if _orig_di_refs is not None:
        src.domain.derived_index.ROLE_REFS_PATH = _orig_di_refs
    if _orig_tq_root is not None:
        tq_mod.PROJECT_ROOT = _orig_tq_root
    if _orig_pf_root is not None:
        src.runtime.preflight.PROJECT_ROOT = _orig_pf_root
    if _orig_pf_stats is not None:
        src.runtime.preflight.TTS_STATS_PATH = _orig_pf_stats


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = context.new_page()
    pg.goto(f"http://127.0.0.1:{server['port']}/")
    pg.wait_for_load_state("networkidle")
    yield pg
    context.close()


def _api(server):
    return f"http://127.0.0.1:{server['port']}"


def _confirm(page, text="确定"):
    page.locator(".modal-footer button", has_text=text).click()


def _cancel(page):
    # exact=True：像「取消任务」这种确认按钮本身的文案包含「取消」子串，
    # has_text 子串匹配会连它一起选中，触发 strict-mode 冲突。
    page.locator(".modal-footer").get_by_role("button", name="取消", exact=True).click()


def _expand_advanced_mixing(page):
    toggle = page.locator(".advanced-toggle")
    if "展开" in toggle.text_content():
        toggle.click()


# ---------------------------------------------------------------------------
class TestMeta:
    def test_imports_work(self):
        from src.api.app import create_app
        from src.api import deps
        from src.runtime.task_queue import TaskQueue
        assert create_app is not None


# ---------------------------------------------------------------------------
class TestOfflineAvailable:
    def test_no_cdn_references_in_index(self, page, server):
        html = page.content()
        for cdn in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com", "fonts.gstatic.com"):
            assert cdn not in html, f"Found CDN reference: {cdn}"

    def test_vendor_and_core_globals_load(self, page, server):
        assert page.evaluate("typeof Vue !== 'undefined' ? Vue.version : null") is not None
        assert page.evaluate("typeof Sortable !== 'undefined'")
        assert page.evaluate("typeof N2A !== 'undefined' && typeof N2A.openModal === 'function'")
        assert page.evaluate("typeof API !== 'undefined' && typeof API.getNovels === 'function'")

    def test_manrope_font_served_locally(self, page, server):
        resp = page.request.get(f"{_api(server)}/vendor/fonts/manrope-latin.woff2")
        assert resp.status == 200
        assert len(resp.body()) > 1000


# ---------------------------------------------------------------------------
class TestPageMemory:
    def test_route_saved_to_localstorage(self, page, server):
        page.goto(f"{_api(server)}/#/roles")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        assert page.evaluate("localStorage.getItem('n2a.lastRoute')") == "#/roles"

    def test_reopen_restores_last_route(self, page, server):
        page.goto(f"{_api(server)}/#/settings")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        page.goto(f"{_api(server)}/")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        assert "#/settings" in page.url

    def test_default_route_when_never_visited(self, page, server):
        context = page.context
        fresh = context.new_page()
        fresh.goto(f"{_api(server)}/", wait_until="networkidle")
        assert "#/novels" in fresh.url or fresh.locator("[data-screen-label='小说列表']").count() > 0
        fresh.close()


# ---------------------------------------------------------------------------
# 真实鼠标两次拖拽：只拖同一父节点内的兄弟（设计上不支持跨层级拖拽，见
# tree-node.js 的注释——后端 tree_move 不校验节点类型合法性）
class TestTreeDragDrop:
    def _create_novel(self, base, **levels):
        import httpx
        resp = httpx.post(f"{base}/api/novels", json={
            "title": "拖拽测试小说", "description": "", "levels": levels or {"part": False, "volume": False},
        })
        assert resp.status_code == 200, resp.text
        return resp.json()["novel_id"]

    def _create_chapter(self, base, nid, title):
        import httpx
        resp = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": title})
        assert resp.status_code == 200, resp.text
        return resp.json()["node_id"]

    def _drag_first_to_last(self, page):
        items = page.locator("[data-node-id]")
        first = items.nth(0)
        last = items.nth(items.count() - 1)
        box1 = first.bounding_box()
        box2 = last.bounding_box()
        page.mouse.move(box1["x"] + box1["width"] / 2, box1["y"] + box1["height"] / 2)
        page.mouse.down()
        page.mouse.move(box2["x"] + box2["width"] / 2, box2["y"] + box2["height"] - 2, steps=10)
        page.mouse.move(box2["x"] + box2["width"] / 2, box2["y"] + box2["height"] - 2, steps=5)
        page.mouse.up()
        time.sleep(0.8)

    def test_repeated_real_drags_reorder_siblings(self, page, server):
        import httpx
        base = _api(server)
        nid = self._create_novel(base)
        for i in range(3):
            self._create_chapter(base, nid, f"第{i+1}章")

        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        def order():
            return [n["id"] for n in httpx.get(f"{base}/api/novels/{nid}/tree").json()["novel"]["tree"]]

        order_0 = order()
        self._drag_first_to_last(page)
        order_1 = order()
        assert order_1 != order_0, "第一次真实拖拽应该改变顺序"

        self._drag_first_to_last(page)
        order_2 = order()
        assert order_2 != order_1, "第二次真实拖拽没有改变顺序——Sortable 可能没在 DOM 重建后重新挂载"


# ---------------------------------------------------------------------------
class TestUnboundSpeakerFlow:
    def test_unbound_speaker_highlights_red(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "角色绑定测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "测试章"}).json()["node_id"]
        raw_text = (
            "夜色渐深，山谷里静得能听见风声。\n\n"
            "“再往前就是断崖了。”一个从未出现过的老猎人忽然开口，声音沙哑。\n\n"
        )
        up = httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
                        files={"file": ("raw.txt", raw_text.encode(), "text/plain")})
        assert up.status_code == 200

        task_id = httpx.post(f"{base}/api/tasks", json={
            "type": "parse", "novel_id": nid, "scope": {"chapter_ids": [cid]},
        }).json()["tasks"][0]["id"]
        t = None
        for _ in range(100):
            t = httpx.get(f"{base}/api/tasks/{task_id}").json()
            if t["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.2)
        assert t["state"] == "succeeded", f"parse 未成功: {t}"

        script = httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json()
        expected_unbound = sum(1 for s in script if not s.get("speaker"))
        assert expected_unbound > 0

        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        unbound_cards = page.locator(".segment-card.unbound")
        assert unbound_cards.count() == expected_unbound
        assert page.locator(".stat-value.danger").count() > 0


# ---------------------------------------------------------------------------
class TestBatchTaskFlow:
    def test_batch_preflight_and_submit(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "批量任务测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        for i in range(3):
            cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": f"第{i+1}章"}).json()["node_id"]
            httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
                      files={"file": ("raw.txt", f"第{i+1}章正文".encode(), "text/plain")})

        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")

        page.click("text=批量解析")
        modal = page.locator(".modal", has_text="批量解析")
        expect(modal).to_contain_text("整本小说")
        expect(modal).to_contain_text("新生成")
        _confirm(page, "确认提交")
        expect(page.locator(".toast.success")).to_contain_text("任务已提交")

        tasks = [t for t in httpx.get(f"{base}/api/tasks").json() if t["type"] == "parse" and t["novel_id"] == nid]
        assert len(tasks) == 3


# ---------------------------------------------------------------------------
class TestLongChapter:
    def test_long_chapter_uses_virtual_scroll(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "长章节测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "超长章节"}).json()["node_id"]
        n = 500
        script = [{"seg_id": i + 1, "speaker": "narrator", "text": f"第{i+1}句测试文本", "emotion": "neutral"} for i in range(n)]
        httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
                  files={"file": ("raw.txt", "x".encode(), "text/plain")})
        script_path = os.path.join(server["library_dir"], nid, "chapters", cid, "script_final.json")
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(script, f, ensure_ascii=False)

        start = time.time()
        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)
        assert time.time() - start < 10

        assert page.locator(".stat-value").first.text_content().strip() == str(n)

        rendered = page.locator(".segment-card").count()
        assert 0 < rendered < 120, f"500 个分块渲染了 {rendered} 张卡片，应该远小于 500"
        assert "第1句" in page.content()
        assert "第500句" not in page.content()

        page.evaluate("document.querySelector('.wb-segment-list').scrollTop = document.querySelector('.wb-segment-list').scrollHeight")
        page.wait_for_timeout(500)
        assert "第500句" in page.content()
        assert 0 < page.locator(".segment-card").count() < 120


# ---------------------------------------------------------------------------
class TestResourceBar:
    def test_resource_bar_visible_and_numeric(self, page, server):
        bar = page.locator(".resource-bar")
        assert bar.count() == 1
        page.wait_for_timeout(1200)  # 等第一个 SSE resource 事件
        content = bar.text_content()
        assert "%" in content and "G" in content

    def test_gpu_owner_label_present(self, page, server):
        page.wait_for_timeout(500)
        assert page.locator(".resource-owner-label").text_content() in ("GPU 空闲", "LLM 占用", "TTS 占用")


# ---------------------------------------------------------------------------
class TestSSEDegradation:
    def test_sse_endpoint_is_a_real_event_stream(self, page, server):
        import httpx
        base = _api(server)
        with httpx.stream("GET", f"{base}/api/events", timeout=2) as resp:
            assert resp.status_code == 200
            assert "event-stream" in resp.headers.get("content-type", "")
            first = next(resp.iter_bytes(256), b"")
            assert len(first) > 0

    def test_reconnect_after_transient_error_clears_badge(self, page, server):
        """把 EventSource 换成完全受控的假类，手动触发一次 onerror 再触发重连后的
        onopen，断言断线徽章先出现后消失——验证前端真的会自动重连，不是像旧实现
        那样 3 次失败后永久放弃。不依赖真实网络/data: URL 的 EventSource 行为，
        避免环境差异导致的时序抖动。"""
        # page.evaluate 只改当前文档；reload 会换一个全新的 window，必须用
        # add_init_script 让补丁在每次导航后、app.js 执行前都重新生效。
        page.add_init_script("""
            class FakeES {
                constructor(url) { this.url = url; FakeES.instances.push(this); }
                addEventListener() {}
                close() {}
            }
            FakeES.instances = [];
            window.EventSource = FakeES;
        """)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_function("window.EventSource.instances.length >= 1")

        page.evaluate("window.EventSource.instances[0].onerror()")
        expect(page.locator(".sse-badge")).to_be_visible(timeout=3000)

        page.wait_for_function("window.EventSource.instances.length >= 2", timeout=5000)
        page.evaluate("window.EventSource.instances[1].onopen()")
        expect(page.locator(".sse-badge")).to_have_count(0, timeout=3000)


# ---------------------------------------------------------------------------
class TestDarkMode:
    def test_toggle_persists_theme_attribute_and_accent_swatch(self, page, server):
        toggle = page.locator(".theme-toggle")
        assert "深色模式" in toggle.text_content()
        toggle.click()
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        assert json.loads(page.evaluate("localStorage.getItem('n2a.ui')"))["darkMode"] is True
        assert "浅色模式" in toggle.text_content()

        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        toggle = page.locator(".theme-toggle")
        toggle.click()  # 还原，避免影响其它测试
        page.wait_for_timeout(200)

    def test_accent_color_updates_css_variable_immediately(self, page, server):
        page.goto(f"{_api(server)}/#/settings")
        page.wait_for_load_state("networkidle")
        page.locator(".accent-swatch").nth(1).click()
        page.wait_for_timeout(150)
        applied = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        assert applied.lower() == "#4f5bd5"
        page.locator(".accent-swatch").nth(0).click()  # 还原成默认朱红

    def test_radius_slider_updates_css_variable(self, page, server):
        page.goto(f"{_api(server)}/#/settings")
        page.wait_for_load_state("networkidle")
        slider = page.locator('input[type="range"]')
        slider.fill("0")
        slider.dispatch_event("input")
        page.wait_for_timeout(150)
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--r').trim()") == "0px"
        slider.fill("8")
        slider.dispatch_event("input")


# ---------------------------------------------------------------------------
class TestPaneResize:
    def test_tree_width_persists_after_reload(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "拖宽测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        resizer = page.locator(".n2a-resizer")
        box = resizer.bounding_box()
        page.mouse.move(box["x"] + 2, box["y"] + 20)
        page.mouse.down()
        page.mouse.move(box["x"] + 120, box["y"] + 20, steps=5)
        page.mouse.up()
        page.wait_for_timeout(200)
        stored = page.evaluate("localStorage.getItem('n2a.tree.w')")
        assert stored is not None and int(stored) > 320

        page.reload()
        page.wait_for_load_state("networkidle")
        width = page.evaluate("document.querySelector('.n2a-treepane').offsetWidth")
        assert abs(width - int(stored)) < 5


# ---------------------------------------------------------------------------
class TestNarrowViewport:
    def test_tree_pane_stacks_above_content_below_860px(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "窄屏测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        split_box = page.locator(".n2a-split").bounding_box()
        tree_box = page.locator(".n2a-treepane").bounding_box()
        content_box = page.locator(".n2a-contentpane").bounding_box()
        assert tree_box["width"] >= split_box["width"] - 5  # 全宽
        assert content_box["y"] >= tree_box["y"] + tree_box["height"] - 5  # 内容在树下方
        expect(page.locator(".n2a-vresizer")).to_be_visible()
        expect(page.locator(".resource-bar")).to_be_hidden()


# ---------------------------------------------------------------------------
class TestRoleCategoryTreeAndTags:
    def test_tree_crud_filter_tags_and_remap_on_rename_and_delete(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/roles")
        page.wait_for_load_state("networkidle")

        page.click("text=+ 新建分类")
        page.fill(".modal input[type=text]", "主角E2E")
        _confirm(page, "创建")
        root = page.locator('[data-category-path="主角E2E"]')
        expect(root).to_be_visible()
        root_row = root.locator("> .category-row")

        # 子分类：只有根节点还没有子节点，此时它自己的 .tree-add-row 唯一，
        # 一旦建了子分类，「主角E2E」下面就会有两个 .tree-add-row（自己的 +
        # 子分类继承下来的），后面都改用 > 直接子代把作用域锁死在当前节点。
        root.locator("> .tree-add-row button").click()
        page.fill(".modal input[type=text]", "男主")
        _confirm(page, "创建")
        expect(page.locator('[data-category-path="主角E2E/男主"]')).to_be_visible()
        assert "主角E2E/男主" in httpx.get(f"{base}/api/role-category-tree").json()["categories"]

        # 带标签、带分类新建角色
        page.click("text=+ 新增角色")
        page.locator(".role-name-input").fill("E2E角色")
        page.locator(".role-category-select").select_option("主角E2E/男主")
        tag_input = page.locator(".role-tag-input")
        tag_input.fill("少年")
        tag_input.press("Enter")
        _confirm(page, "创建")

        card = page.locator(".role-card", has_text="E2E角色")
        expect(card).to_be_visible()
        expect(card).to_contain_text("少年")
        role = [r for r in httpx.get(f"{base}/api/roles").json() if r["name"] == "E2E角色"][0]
        assert role["category"] == "主角E2E/男主" and role["tags"] == ["少年"]

        # 分类筛选
        root_row.locator(".tree-node-name").click()
        expect(card).to_be_visible()
        page.locator(".tree-all-row").click()
        expect(card).to_be_visible()

        # 标签筛选
        page.locator(".tag-filter-chip", has_text="少年").click()
        expect(card).to_be_visible()
        page.locator(".tag-filter-chip", has_text="少年").click()

        # 改名：角色 category 字符串跟着换成新路径
        sub = page.locator('[data-category-path="主角E2E/男主"]')
        sub.locator("> .category-row .btn-text-muted").click()
        page.fill(".modal input[type=text]", "男一号")
        _confirm(page, "保存")
        page.wait_for_timeout(300)
        role = [r for r in httpx.get(f"{base}/api/roles").json() if r["name"] == "E2E角色"][0]
        assert role["category"] == "主角E2E/男一号"

        # 删除父分类：子分类一并消失，角色变成未分类
        root_row.locator(".btn-danger-text").click()
        _confirm(page, "删除")
        expect(page.locator('[data-category-path="主角E2E"]')).to_have_count(0)
        role = [r for r in httpx.get(f"{base}/api/roles").json() if r["name"] == "E2E角色"][0]
        assert role["category"] == ""

    def test_invalid_category_name_shows_server_error(self, page, server):
        base = _api(server)
        page.goto(f"{base}/#/roles")
        page.wait_for_load_state("networkidle")
        page.click("text=+ 新建分类")
        page.fill(".modal input[type=text]", "a/b")
        _confirm(page, "创建")
        expect(page.locator(".toast.error")).to_be_visible()

    def test_narrator_cannot_be_deleted(self, page, server):
        import httpx
        base = _api(server)
        rid = httpx.post(f"{base}/api/roles", json={"name": "narrator_dup"}).json()["role_id"]
        # narrator 是保留 id，用真实的角色列表接口确认删除接口拒绝它
        assert httpx.delete(f"{base}/api/roles/narrator").status_code == 400
        httpx.delete(f"{base}/api/roles/{rid}?confirm=true")


# ---------------------------------------------------------------------------
class TestFullNavigation:
    def test_navigate_all_pages(self, page, server):
        base = _api(server)
        for path, label in [("#/novels", "小说列表"), ("#/roles", "全局角色库"),
                             ("#/assets", "背景音与音效库"), ("#/settings", "系统配置")]:
            page.goto(f"{base}/{path}")
            page.wait_for_load_state("networkidle")
            assert label in page.content()

    def test_novel_detail_and_workbench_reachable(self, page, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "导航测试", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "章一"}).json()["node_id"]
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        # networkidle 在持久 SSE 连接下不代表"这次导航的数据请求也完成了"，
        # 用 expect 的自动重试而不是导航后立刻断言一次性的 page.content()。
        expect(page.locator(".detail-header")).to_contain_text("导航测试", timeout=5000)
        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("[data-screen-label='配音工作台']")).to_have_count(1, timeout=5000)


# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_novel_list_shows_empty_state(self, page, server):
        page.goto(f"{_api(server)}/#/novels?__unused=1")
        page.wait_for_load_state("networkidle")
        # 这个 session 里前面的用例已经建过小说，只断言页面结构没崩
        assert page.locator("[data-screen-label='小说列表']").count() == 1

    def test_unknown_novel_id_does_not_crash(self, page, server):
        page.goto(f"{_api(server)}/#/novels/does-not-exist")
        page.wait_for_load_state("networkidle")
        # 不崩溃、顶栏仍然可点
        assert page.locator(".n2a-nav").count() == 1


# ---------------------------------------------------------------------------
class TestAssetsPageCrud:
    def test_create_edit_generate_delete_through_ui(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")

        page.click("text=+ 新增素材")
        page.locator(".modal select").first.select_option("sfx")
        page.locator(".asset-name-input").fill("e2e_hit")
        page.locator(".asset-prompt-input").fill("a sharp hit sound")
        _confirm(page, "创建")
        card = page.locator(".asset-card", has_text="e2e_hit")
        expect(card).to_be_visible()
        expect(card).to_contain_text("MISSING")
        expect(card.locator("audio")).to_have_count(0)

        card.get_by_role("button", name="编辑").click()
        page.locator(".modal textarea").last.fill("测试描述")
        _confirm(page, "保存")
        expect(card).to_contain_text("测试描述")
        specs = httpx.get(f"{base}/api/asset-specs?kind=sfx").json()["specs"]
        assert [s for s in specs if s["name"] == "e2e_hit"][0]["description"] == "测试描述"

        card.get_by_role("button", name="生成", exact=True).click()
        expect(card).to_contain_text("OK", timeout=20000)
        assert os.path.exists(os.path.join(server["tmp"], "data", "assets", "sfx", "e2e_hit.wav"))

        player = card.locator("audio")
        expect(player).to_be_visible()
        assert player.get_attribute("src") == "/api/asset-specs/sfx/e2e_hit/audio"

        card.get_by_role("button", name="删除").click()
        _confirm(page, "删除")
        expect(page.locator(".asset-card", has_text="e2e_hit")).to_have_count(0)
        assert not os.path.exists(os.path.join(server["tmp"], "data", "assets", "sfx", "e2e_hit.wav"))

    def test_duplicate_name_shows_error_toast(self, page, server):
        import httpx
        base = _api(server)
        httpx.post(f"{base}/api/asset-specs", json={"kind": "sfx", "name": "e2e_dup", "prompt": "x"})
        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")
        page.click("text=+ 新增素材")
        page.locator(".modal select").first.select_option("sfx")
        page.locator(".asset-name-input").fill("e2e_dup")
        page.locator(".asset-prompt-input").fill("x")
        _confirm(page, "创建")
        expect(page.locator(".toast.error")).to_be_visible()
        httpx.delete(f"{base}/api/asset-specs/sfx/e2e_dup")


def _make_chapter_with_script(server, script, timeline=None, title="素材测试"):
    import httpx
    base = _api(server)
    nid = httpx.post(f"{base}/api/novels", json={"title": title, "levels": {"part": False, "volume": False}}).json()["novel_id"]
    cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "章"}).json()["node_id"]
    httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1", files={"file": ("raw.txt", b"x", "text/plain")})
    ch_dir = os.path.join(server["library_dir"], nid, "chapters", cid)
    with open(os.path.join(ch_dir, "script_final.json"), "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False)
    if timeline is not None:
        with open(os.path.join(ch_dir, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False)
    return nid, cid, ch_dir


def _fake_asset(server, kind_dir, name):
    d = os.path.join(server["tmp"], "data", "assets", kind_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.wav"), "wb") as f:
        f.write(b"RIFFfake")


class TestWorkbenchAssets:
    def test_single_and_batch_set_bgm_sfx_and_timeline_sync(self, page, server):
        import httpx
        base = _api(server)
        _fake_asset(server, "ambience", "e2e_wind")
        _fake_asset(server, "sfx", "e2e_door")
        script = [
            {"seg_id": 1, "speaker": "narrator", "text": "第一句", "emotion": "neutral"},
            {"seg_id": 2, "speaker": None, "text": "第二句", "emotion": "neutral"},
        ]
        timeline = {"chapter_id": "x", "items": [{"seg_id": 1, "sfx": None, "bgm": None}, {"seg_id": 2, "sfx": None, "bgm": None}]}
        nid, cid, ch_dir = _make_chapter_with_script(server, script, timeline, "工作台素材")

        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")

        page.locator(".segment-card").first.click()
        page.locator(".segment-bgm-select").select_option("e2e_wind")
        page.locator(".segment-sfx-select").select_option("e2e_door")
        page.locator(".wb-editor-footer button", has_text="保存").click()

        seg1 = [s for s in httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json() if s["seg_id"] == 1][0]
        assert seg1["bgm"] == "e2e_wind" and seg1["sfx"] == "e2e_door"
        tl = json.load(open(os.path.join(ch_dir, "timeline.json"), encoding="utf-8"))
        assert tl["items"][0]["bgm"] == "e2e_wind" and tl["items"][0]["sfx"] == "e2e_door"

        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="设置背景音/音效").click()
        page.locator(".batch-sfx-select").select_option("e2e_door")
        _confirm(page, "应用")
        seg2 = [s for s in httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json() if s["seg_id"] == 2][0]
        assert seg2["sfx"] == "e2e_door" and not seg2.get("bgm")

    def test_unchanged_missing_asset_does_not_block_saving(self, page, server):
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "旧", "emotion": "neutral", "sfx": "e2e_ghost"}]
        nid, cid, _ = _make_chapter_with_script(server, script, None, "缺失素材保存")
        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        page.locator(".segment-card").first.click()
        expect(page.locator(".segment-sfx-select")).to_have_value("e2e_ghost")
        page.locator(".wb-editor textarea").fill("新文字")
        page.locator(".wb-editor-footer button", has_text="保存").click()
        page.wait_for_timeout(300)
        seg = httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json()[0]
        assert seg["text"] == "新文字" and seg["sfx"] == "e2e_ghost"


# ---------------------------------------------------------------------------
class TestNovelDetailMixAssets:
    def test_mix_with_assets_confirm_sends_params(self, page, server):
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, cid, _ = _make_chapter_with_script(server, script, {"items": [{"seg_id": 1}]}, "详情素材")
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.click("text=批量混音导出")
        _confirm(page, "带素材混音")
        _confirm(page, "确认提交")
        expect(page.locator(".toast.success")).to_contain_text("任务已提交")
        tasks = [t for t in httpx.get(f"{base}/api/tasks").json() if t["type"] == "mix" and t["novel_id"] == nid]
        assert tasks and tasks[0]["params"] == {"with_assets": True}

    def test_mix_without_assets_sends_false(self, page, server):
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, cid, _ = _make_chapter_with_script(server, script, {"items": [{"seg_id": 1}]}, "详情无素材")
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.click("text=批量混音导出")
        _cancel(page)  # 不带素材
        _confirm(page, "确认提交")
        tasks = [t for t in httpx.get(f"{base}/api/tasks").json() if t["type"] == "mix" and t["novel_id"] == nid]
        assert tasks and tasks[0]["params"] == {"with_assets": False}


# ---------------------------------------------------------------------------
class TestSettingsMixWithAssets:
    def test_toggle_persists_voice_only_inverted(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/settings")
        page.wait_for_load_state("networkidle")
        box = page.locator(".setting-mix-with-assets input")
        expect(box).not_to_be_checked()

        box.check()
        page.click("text=保存配置")
        page.wait_for_timeout(500)
        assert httpx.get(f"{base}/api/config").json()["mixing"]["voice_only"] is False

        page.reload()
        page.wait_for_load_state("networkidle")
        expect(page.locator(".setting-mix-with-assets input")).to_be_checked()

        page.locator(".setting-mix-with-assets input").uncheck()
        page.click("text=保存配置")
        page.wait_for_timeout(500)
        assert httpx.get(f"{base}/api/config").json()["mixing"]["voice_only"] is True


# ---------------------------------------------------------------------------
class TestTaskPanel:
    def test_shows_this_novels_and_global_tasks_only(self, page, server):
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        tl = {"items": [{"seg_id": 1}]}
        nid, cid, _ = _make_chapter_with_script(server, script, tl, "任务面板本书")
        other, ocid, _ = _make_chapter_with_script(server, script, tl, "任务面板别的书")
        httpx.post(f"{base}/api/tasks", json={"type": "mix", "novel_id": other, "scope": {"chapter_ids": [ocid]}})
        httpx.post(f"{base}/api/tasks", json={"type": "mix", "novel_id": nid, "scope": {"chapter_ids": [cid]}})
        asset_task_id = httpx.post(f"{base}/api/tasks", json={
            "type": "asset_gen", "params": {"only": ["e2e_panel_none"]},
        }).json()["tasks"][0]["id"]

        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        panel = page.locator(".task-panel")
        expect(panel).to_be_visible()
        expect(panel.locator(".task-section-title", has_text="本书任务")).to_be_visible()
        expect(panel.locator(".task-section-title", has_text="全局任务")).to_be_visible()
        assert "任务面板别的书" not in panel.inner_text()

        # 用真实任务 id 精确定位——同一 session 里可能已经跑过好几个 asset_gen
        # 任务，group 折叠时标题都是「生成素材 · N/M 完成」，文字互相分不开。
        # 折叠状态下 .task-item 整个不在 DOM 里（v-if，不是隐藏），必须先展开
        # 对应的分组（单任务的 group_id 就是它自己的 id）才能找到它。
        group = panel.locator(f'[data-group-id="{asset_task_id}"]')
        expect(group).to_have_count(1)
        item = group.locator(f'[data-task-id="{asset_task_id}"]')
        if item.count() == 0:
            group.locator(".task-group-header").click()
        expect(item).to_have_count(1)
        item.locator(".task-log-btn").click()
        expect(item.locator(".task-log")).to_be_visible()
        item.locator(".task-log-btn").click()
        expect(item.locator(".task-log")).to_have_count(0)

    def test_cancel_asks_for_confirmation_with_real_wording(self, page, server):
        """asset_gen 在测试环境（无真实引擎）几乎瞬间完成，真等它跑完再测取消
        按钮会是竞态；直接拦截 GET /api/tasks 返回一个写死的"运行中"任务。"""
        nid, cid, _ = _make_chapter_with_script(server, [], None, "取消确认文案")
        fake_task = {
            "id": "t_running_e2e", "type": "asset_gen", "lane": "gpu", "novel_id": None, "chapter_id": None,
            "group_id": None, "params": {"only": ["e2e_cancel_wording"]}, "state": "running",
            "cancel_requested": False, "progress": None, "created_at": "2026-01-01 00:00:00",
            "started_at": None, "finished_at": None, "error": None, "log_path": None,
        }
        page.route("**/api/tasks?limit=200", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps([fake_task])))
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        group = page.locator('[data-group-id="t_running_e2e"]')
        expect(group).to_be_visible()
        item = group.locator('[data-task-id="t_running_e2e"]')
        if item.count() == 0:
            group.locator(".task-group-header").click()
        item.locator(".task-cancel-btn").click()
        modal = page.locator(".modal", has_text="取消任务")
        expect(modal).to_be_visible()
        expect(modal).to_contain_text("终止")
        _cancel(page)


# ---------------------------------------------------------------------------
class TestSettingsSaveOnlyChangedKeys:
    def _open(self, page, server):
        page.goto(f"{_api(server)}/#/settings")
        page.wait_for_load_state("networkidle")

    def test_only_changed_keys_are_submitted(self, page, server):
        import httpx
        base = _api(server)
        self._open(page, server)
        page.evaluate("""() => {
            window.__n2a_patches = [];
            const orig = API.updateConfig.bind(API);
            API.updateConfig = (data) => { window.__n2a_patches.push(data); return orig(data); };
        }""")
        page.locator(".setting-bitrate").select_option("256")
        page.click("text=保存配置")
        page.wait_for_timeout(300)
        patches = page.evaluate("window.__n2a_patches")
        assert patches and list(patches[-1].keys()) == ["mixing.bitrate"]

        page.click("text=保存配置")  # 再点一次：没有改动
        page.wait_for_timeout(200)
        expect(page.locator(".toast.warning", has_text="没有改动")).to_be_visible()

        # 还原
        page.locator(".setting-bitrate").select_option("192")
        page.click("text=保存配置")
        page.wait_for_timeout(300)

    def test_zero_is_a_real_value_not_reset_to_default(self, page, server):
        import httpx
        base = _api(server)
        self._open(page, server)
        _expand_advanced_mixing(page)
        page.locator(".setting-ambience-gain input").fill("0")
        page.click("text=保存配置")
        page.wait_for_timeout(400)
        assert httpx.get(f"{base}/api/config").json()["mixing"]["ambience_gain_db"] == 0
        page.locator(".setting-ambience-gain input").fill("-18")
        page.click("text=保存配置")
        page.wait_for_timeout(300)

    def test_out_of_range_value_is_rejected_with_reason(self, page, server):
        import httpx
        base = _api(server)
        self._open(page, server)
        _expand_advanced_mixing(page)
        before = httpx.get(f"{base}/api/config").json()["mixing"].get("sfx_limit_dbfs")
        page.locator(".setting-sfx-limit input").fill("5")
        page.click("text=保存配置")
        toast = page.locator(".toast.error")
        expect(toast).to_contain_text("sfx_limit_dbfs")
        assert httpx.get(f"{base}/api/config").json()["mixing"].get("sfx_limit_dbfs") == before

    def test_empty_numeric_field_blocked_unless_originally_unset(self, page, server):
        """先真的存一个值（这台测试环境的 global_config.yaml 里
        tts.segment_gap_ms 默认是注释掉的未设置状态），确保清空时测的是
        「清空一个原本有值的字段」，不是「一直就没设过」那条不同的分支。"""
        import httpx
        base = _api(server)
        self._open(page, server)
        _expand_advanced_mixing(page)
        # 350 跟前端兜底默认值 200 不同，确保这次改动真的会被判定为"变了"而
        # 提交（填一个跟默认值恰好相同的数会被当成"没改"，压根不会发请求）。
        page.locator(".setting-segment-gap input").fill("350")
        page.click("text=保存配置")
        page.wait_for_timeout(300)
        assert httpx.get(f"{base}/api/config").json()["tts"]["segment_gap_ms"] == 350

        page.locator(".setting-segment-gap input").fill("")
        page.click("text=保存配置")
        expect(page.locator(".toast.error")).to_contain_text("tts.segment_gap_ms")
        assert httpx.get(f"{base}/api/config").json()["tts"]["segment_gap_ms"] == 350

        page.locator(".setting-segment-gap input").fill("200")
        page.click("text=保存配置")
        page.wait_for_timeout(300)


# ---------------------------------------------------------------------------
class TestPrecomputeEmbeddingUi:
    def _role_with_reference(self, server, name, with_reference):
        import httpx
        base = _api(server)
        rid = httpx.post(f"{base}/api/roles", json={"name": name}).json()["role_id"]
        ref = os.path.join(server["tmp"], "data", "roles", rid, "reference.wav")
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        if with_reference:
            with open(ref, "wb") as f:
                f.write(b"RIFFfake")
        elif os.path.exists(ref):
            os.remove(ref)
        return rid

    def test_button_disabled_without_reference_audio(self, page, server):
        self._role_with_reference(server, "无参考音频角色", with_reference=False)
        page.goto(f"{_api(server)}/#/roles")
        page.wait_for_load_state("networkidle")
        btn = page.locator(".role-card", has_text="无参考音频角色").locator(".precompute-embedding-btn")
        expect(btn).to_be_disabled()
        expect(btn).to_have_attribute("title", "没有参考音频，无法预计算")

    def test_submit_then_failure_is_reported_with_reason(self, page, server):
        import httpx
        base = _api(server)
        rid = self._role_with_reference(server, "有参考音频角色", with_reference=True)
        page.goto(f"{base}/#/roles")
        page.wait_for_load_state("networkidle")
        card = page.locator(".role-card", has_text="有参考音频角色")
        btn = card.locator(".precompute-embedding-btn")
        expect(btn).to_be_enabled()
        btn.click()
        _confirm(page, "确定")

        toast = page.locator(".toast.error")
        expect(toast).to_contain_text("预计算失败", timeout=15000)

        tasks = [t for t in httpx.get(f"{base}/api/tasks").json()
                 if t["type"] == "precompute_embedding" and (t["params"] or {}).get("role_id") == rid]
        assert tasks and tasks[0]["state"] == "failed"
        expect(btn).to_have_text("预计算音色", timeout=5000)


# ---------------------------------------------------------------------------
class TestAssetEngineDrift:
    def test_drifted_asset_is_flagged_and_regenerate_redoes_it(self, page, server):
        import httpx
        base = _api(server)
        spec = {"kind": "sfx", "name": "e2e_drift", "prompt": "drift", "duration_sec": 1, "seed": 3}
        httpx.post(f"{base}/api/asset-specs", json=spec)
        d = os.path.join(server["tmp"], "data", "assets", "sfx")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "e2e_drift.wav"), "wb") as f:
            f.write(b"RIFFfake")
        row = [s for s in httpx.get(f"{base}/api/asset-specs?kind=sfx").json()["specs"] if s["name"] == "e2e_drift"][0]
        from src.pipeline import asset_gen
        spec_hash = asset_gen.compute_spec_hash(row)
        with open(os.path.join(d, "e2e_drift.meta.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "e2e_drift", "kind": "sfx", "spec_hash": spec_hash, "engine": "ace_step", "used_fallback": False}, f)

        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")
        card = page.locator(".asset-card", has_text="e2e_drift")
        expect(card.locator(".asset-engine-drift")).to_contain_text("ace_step")
        expect(card.locator(".asset-engine-drift")).to_contain_text("tangoflux")
        expect(card.locator("audio")).to_have_count(1)

        card.get_by_role("button", name="重新生成").click()
        expect(card).to_contain_text("OK", timeout=20000)
        httpx.delete(f"{base}/api/asset-specs/sfx/e2e_drift?delete_files=true")


# ---------------------------------------------------------------------------
class TestAssetCategoriesAndTags:
    def test_create_filter_and_delete_category_and_default_selection(self, page, server):
        import httpx
        base = _api(server)
        httpx.post(f"{base}/api/asset-specs", json={"kind": "sfx", "name": "e2e_plain", "prompt": "x"})
        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")

        page.click("text=+ 新建分类")
        page.fill(".modal input[type=text]", "e2e环境")
        _confirm(page, "创建")
        row = page.locator('[data-category-path="e2e环境"]')
        expect(row).to_be_visible()
        row_row = row.locator("> .category-row")

        row_row.locator(".tree-node-name").click()  # 选中分类
        page.click("text=+ 新增素材")
        expect(page.locator(".asset-category-select")).to_have_value("e2e环境")  # 新增默认带当前分类
        page.locator(".modal select").first.select_option("sfx")
        page.locator(".asset-name-input").fill("e2e_cat")
        page.locator(".asset-prompt-input").fill("rain")
        tag = page.locator(".asset-tag-input")
        tag.fill("e2e雨声")
        tag.press("Enter")
        _confirm(page, "创建")
        card = page.locator(".asset-card", has_text="e2e_cat")
        expect(card).to_contain_text("分类：e2e环境")
        expect(card.locator(".tag-chip")).to_have_text("e2e雨声")

        page.locator(".tree-all-row").click()
        expect(page.locator(".asset-card", has_text="e2e_plain")).to_be_visible()
        page.locator(".tag-filter-chip", has_text="e2e雨声").click()
        expect(page.locator(".asset-card", has_text="e2e_cat")).to_be_visible()
        expect(page.locator(".asset-card", has_text="e2e_plain")).to_have_count(0)
        page.locator(".tag-filter-chip", has_text="e2e雨声").click()

        row_row.locator(".btn-danger-text").click()
        _confirm(page, "删除")
        expect(page.locator('[data-category-path="e2e环境"]')).to_have_count(0)
        saved = [s for s in httpx.get(f"{base}/api/asset-specs?kind=sfx").json()["specs"] if s["name"] == "e2e_cat"][0]
        assert saved["category"] == ""  # 删分类后重映射为未分类
        for n in ("e2e_cat", "e2e_plain"):
            httpx.delete(f"{base}/api/asset-specs/sfx/{n}")


# ---------------------------------------------------------------------------
class TestChapterStatusMap:
    def _novel_with_two_chapters(self, server):
        import httpx
        base = _api(server)
        nid = httpx.post(f"{base}/api/novels", json={"title": "状态表", "levels": {"part": False, "volume": False}}).json()["novel_id"]
        ids = []
        for title in ("甲章", "乙章"):
            cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": title}).json()["node_id"]
            httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1", files={"file": ("raw.txt", b"x", "text/plain")})
            ids.append(cid)
        a_dir = os.path.join(server["library_dir"], nid, "chapters", ids[0])
        with open(os.path.join(a_dir, "script_final.json"), "w", encoding="utf-8") as f:
            json.dump([{"seg_id": "1", "speaker": "旁白", "text": "a"},
                       {"seg_id": "2", "speaker": None, "text": "b"},
                       {"seg_id": "3", "speaker": "旁白", "text": "c"}], f, ensure_ascii=False)
        with open(os.path.join(a_dir, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"seg_id": "1"}, {"seg_id": "3"}]}, f)
        time.sleep(0.05)
        os.makedirs(os.path.join(a_dir, "output"), exist_ok=True)
        with open(os.path.join(a_dir, "output", "chapter.mp3"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(a_dir, "output", "mix_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"voice_only": False, "missing_assets": {"bgm": ["rain"], "sfx": []}}, f)
        return nid, ids

    def test_tree_dot_reflects_four_states(self, page, server):
        nid, (a, b) = self._novel_with_two_chapters(server)
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        expect(page.locator(f'[data-node-id="{a}"] .tree-dot')).to_have_class(re.compile(r"state-voiced"))
        expect(page.locator(f'[data-node-id="{b}"] .tree-dot')).to_have_class(re.compile(r"state-unparsed"))

    def test_multi_select_shows_comparison_table_with_real_numbers(self, page, server):
        nid, (a, b) = self._novel_with_two_chapters(server)
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "chapter-stats" in r.url else None)
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        expect(page.locator(".chapter-compare")).to_have_count(0)

        page.locator(f'[data-node-id="{a}"] .tree-checkbox').check()
        page.wait_for_timeout(200)
        expect(page.locator(".chapter-compare")).to_have_count(0)  # 只选一个不出现
        assert requests == []

        page.locator(f'[data-node-id="{b}"] .tree-checkbox').check()
        table = page.locator(".chapter-compare")
        expect(table).to_be_visible()
        row_a = table.locator(f'tr[data-chapter-id="{a}"]')
        row_b = table.locator(f'tr[data-chapter-id="{b}"]')
        expect(row_a).to_contain_text("3")  # 分块数
        expect(row_b.locator("td").nth(2)).to_have_text("—")  # 乙章没有解析产物
        assert len(requests) >= 1


# ---------------------------------------------------------------------------
class TestNativeDialogsReplaced:
    def _novel(self, server, title, **levels):
        import httpx
        return httpx.post(f"{_api(server)}/api/novels", json={"title": title, "levels": levels or {"part": False, "volume": False}}).json()["novel_id"]

    def _tree(self, server, nid):
        import httpx
        return httpx.get(f"{_api(server)}/api/novels/{nid}/tree").json()["novel"]["tree"]

    def _no_native_dialog(self, page):
        seen = []
        page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
        return seen

    def test_add_chapter_through_in_app_prompt(self, page, server):
        nid = self._novel(server, "对话框-新增")
        native = self._no_native_dialog(page)
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")

        page.click("text=+ 新增章节")
        confirm = page.locator(".modal-footer button.btn-primary")
        expect(confirm).to_be_disabled()
        page.locator(".modal input[type=text]").fill("第一章 风起")
        confirm.click()
        expect(page.locator(".tree-node-name", has_text="第一章 风起")).to_be_visible()
        assert [n["title"] for n in self._tree(server, nid)] == ["第一章 风起"]
        assert native == []

    def test_cancelling_the_prompt_creates_nothing(self, page, server):
        nid = self._novel(server, "对话框-取消")
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.click("text=+ 新增章节")
        page.locator(".modal input[type=text]").fill("不该出现")
        _cancel(page)
        expect(page.locator(".modal-overlay")).to_have_count(0)
        assert self._tree(server, nid) == []

    def test_enter_submits_and_escape_cancels(self, page, server):
        nid = self._novel(server, "对话框-键盘")
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.click("text=+ 新增章节")
        page.locator(".modal input[type=text]").fill("回车创建")
        page.locator(".modal input[type=text]").press("Enter")
        expect(page.locator(".tree-node-name", has_text="回车创建")).to_be_visible()

        page.click("text=+ 新增章节")
        page.locator(".modal input[type=text]").press("Escape")
        expect(page.locator(".modal-overlay")).to_have_count(0)

    def test_rename_prefills_current_title(self, page, server):
        import httpx
        nid = self._novel(server, "对话框-改名")
        httpx.post(f"{_api(server)}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "旧名字"})
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")

        row = page.locator(".tree-node", has_text="旧名字")
        row.hover()
        row.locator("button", has_text="改").click()
        expect(page.locator(".modal input[type=text]")).to_have_value("旧名字")
        page.locator(".modal input[type=text]").fill("新名字")
        _confirm(page, "保存")
        expect(page.locator(".tree-node-name", has_text="新名字")).to_be_visible()
        assert self._tree(server, nid)[0]["title"] == "新名字"

    def test_delete_shows_impact_preview_before_confirming(self, page, server):
        import httpx
        base = _api(server)
        nid = self._novel(server, "对话框-删除", volume=True)
        vol = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "volume", "title": "第一卷"}).json()["node_id"]
        for t in ("甲", "乙"):
            httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": t, "parent_id": vol})
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")

        row = page.locator(".tree-node", has_text="第一卷")
        row.hover()
        row.locator("button", has_text="删").click()
        dialog = page.locator(".modal", has_text="删除节点")
        expect(dialog).to_contain_text("2")
        _cancel(page)
        assert len(self._tree(server, nid)) == 1

        row.hover()
        row.locator("button", has_text="删").click()
        _confirm(page, "删除")
        expect(page.locator(".tree-node-name", has_text="第一卷")).to_have_count(0)
        assert self._tree(server, nid) == []

    def test_create_failure_is_reported_not_silent(self, page, server):
        nid = self._novel(server, "对话框-失败")
        page.goto(f"{_api(server)}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.route("**/api/novels/*/nodes", lambda route: route.abort())
        page.click("text=+ 新增章节")
        page.locator(".modal input[type=text]").fill("会失败")
        _confirm(page, "创建")
        expect(page.locator(".toast.error")).to_be_visible()
        assert self._tree(server, nid) == []


# ---------------------------------------------------------------------------
def _workbench_with_roles(server, title, speakers):
    import httpx
    base = _api(server)
    for name in ("批量角色甲", "批量角色乙"):
        httpx.post(f"{base}/api/roles", json={"name": name})
    script = [{"seg_id": i + 1, "speaker": sp, "text": f"第{i+1}句", "emotion": "neutral"} for i, sp in enumerate(speakers)]
    return _make_chapter_with_script(server, script, None, title)


class TestWorkbenchDialogs:
    def _open(self, page, server, speakers):
        nid, cid, _ = _workbench_with_roles(server, "工作台对话框" + str(speakers), speakers)
        page.goto(f"{_api(server)}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        return nid, cid

    def _script(self, server, nid, cid):
        import httpx
        return httpx.get(f"{_api(server)}/api/novels/{nid}/chapters/{cid}/script").json()

    def test_batch_bind_role_is_a_dropdown_of_existing_roles(self, page, server):
        import httpx
        nid, cid = self._open(page, server, [None, None])
        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="绑定角色").click()
        select = page.locator(".modal select")
        expect(select).to_be_visible()
        target = [r for r in httpx.get(f"{_api(server)}/api/roles").json() if r["name"] == "批量角色乙"][0]
        select.select_option(target["id"])
        _confirm(page, "绑定")
        expect(page.locator(".segment-card.unbound")).to_have_count(0, timeout=10000)
        assert {s["speaker"] for s in self._script(server, nid, cid)} == {target["id"]}

    def test_batch_change_tone_offers_all_eight_emotions(self, page, server):
        nid, cid = self._open(page, server, [None, None])
        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="修改语气").click()
        select = page.locator(".modal select")
        assert select.locator("option").count() == 8
        select.select_option("angry")
        _confirm(page, "修改")
        page.wait_for_timeout(500)
        assert {s["emotion"] for s in self._script(server, nid, cid)} == {"angry"}

    def test_batch_clear_role_asks_first(self, page, server):
        nid, cid = self._open(page, server, [None, None])
        sent = []
        page.on("request", lambda r: sent.append(r.url) if r.method == "POST" and r.url.endswith("/segments/batch") else None)
        seen = self.__class__.__dict__  # noop, keep flake happy
        native = []
        page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))

        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="清空角色").click()
        dialog = page.locator(".modal", has_text="批量清空角色")
        expect(dialog).to_be_visible()
        _cancel(page)
        page.wait_for_timeout(300)
        assert sent == []

        page.get_by_role("button", name="清空角色").click()
        _confirm(page, "清空")
        page.wait_for_timeout(500)
        assert len(sent) == 1
        assert native == []

    def test_batch_tts_with_unbound_segments_shows_toast_not_alert(self, page, server):
        nid, cid = self._open(page, server, [None, "narrator"])
        native = []
        page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="批量生成人声").click()
        expect(page.locator(".toast.error")).to_contain_text("未绑定角色")
        assert native == []

    def test_create_and_bind_role_through_prompt(self, page, server):
        import httpx
        nid, cid = self._open(page, server, [None])
        page.locator(".segment-card").first.click()
        page.click(".role-dropdown-btn")
        page.click(".role-dropdown-create")
        page.locator(".modal .role-name-input").fill("对话框新角色")
        _confirm(page, "创建并绑定")
        expect(page.locator(".role-dropdown-btn")).to_contain_text("对话框新角色", timeout=10000)
        assert any(r["name"] == "对话框新角色" for r in httpx.get(f"{_api(server)}/api/roles").json())
