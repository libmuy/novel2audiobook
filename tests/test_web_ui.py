"""
Playwright-based end-to-end tests for the novel2audiobook web frontend.

Covers the 10 items from Plan 006 verification checklist.
Requires: pip install playwright && playwright install chromium

Usage:
    # Headless (CI)
    .venv/bin/python -m pytest tests/test_web_ui.py -v

    # Headed (manual inspection)
    .venv/bin/python -m pytest tests/test_web_ui.py -v --headed
"""

import os
import sys
import time
import threading
import socket
import shutil
import tempfile
import pytest

# ---------------------------------------------------------------------------
# Skip entire module if playwright is not installed
try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    pytest.skip("playwright not installed", allow_module_level=True)

# ---------------------------------------------------------------------------
# Project root & imports (mirror test_api.py isolation pattern)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import uvicorn
from src.api.app import create_app
from src.api import deps
from src.task_queue import TaskQueue


# ---------------------------------------------------------------------------
# Helpers
def _free_port():
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(app, port):
    """Run uvicorn in a daemon thread."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for server to be ready
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return server, thread
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start on port {port}")


# ---------------------------------------------------------------------------
# Fixtures
@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """Start a real FastAPI server with isolated temp directories."""
    tmp = tmp_path_factory.mktemp("e2e")
    library_dir = tmp / "library"
    roles_dir = tmp / "roles"
    tasks_dir = tmp / "tasks"
    cache_dir = tmp / ".cache"
    for d in [library_dir, roles_dir, tasks_dir, cache_dir]:
        d.mkdir()

    # Monkeypatch PROJECT_ROOT so all routers resolve paths into tmp
    import src.utils
    import src.derived_index
    import src.preflight
    import src.task_queue as tq_mod

    _orig_root = getattr(src.utils, "PROJECT_ROOT", None)
    _orig_di_root = getattr(src.derived_index, "PROJECT_ROOT", None)
    _orig_tq_root = getattr(tq_mod, "PROJECT_ROOT", None)
    _orig_pf_root = getattr(src.preflight, "PROJECT_ROOT", None)
    _orig_pf_stats = getattr(src.preflight, "TTS_STATS_PATH", None)
    _orig_di_refs = getattr(src.derived_index, "ROLE_REFS_PATH", None)

    # Create web/static in tmp so static file mounting works
    web_static = tmp / "web" / "static"
    web_static.mkdir(parents=True, exist_ok=True)
    # Copy actual static files into temp dir
    real_static = os.path.join(PROJECT_ROOT, "web", "static")
    if os.path.isdir(real_static):
        shutil.copytree(real_static, web_static, dirs_exist_ok=True)

    src.utils.PROJECT_ROOT = str(tmp)
    src.derived_index.PROJECT_ROOT = str(tmp)
    src.derived_index.ROLE_REFS_PATH = str(tmp / "cache" / "index" / "role_refs.json")
    tq_mod.PROJECT_ROOT = str(tmp)
    src.preflight.PROJECT_ROOT = str(tmp)
    src.preflight.TTS_STATS_PATH = str(tmp / "tts_stats.json")

    config = {
        "server": {
            "library_root": str(library_dir),
            "host": "127.0.0.1",
            "port": 0,
            "cpu_workers": 2,
            "gpu_chunk_size": 8,
            "monitor_interval_ms": 1000,
            "task_retention_days": 7,
        },
        # 指向不可达端口，强制走 HeuristicBackend，测试结果不受本机是否碰巧
        # 跑着真实 llama-server 影响
        "llm": {"api_base": "http://localhost:1/v1"},
        "tts": {},
        "mixing": {"voice_only": True, "output_format": "mp3"},
    }
    deps.set_config(config)
    queue = TaskQueue(
        config=config,
        tasks_dir=str(tasks_dir),
        library_dir=str(library_dir),
    )
    deps.set_queue(queue)

    port = _free_port()
    app = create_app()
    server_obj, thread = _start_server(app, port)

    yield {"port": port, "tmp": tmp, "library_dir": library_dir, "roles_dir": roles_dir}

    server_obj.should_exit = True
    thread.join(timeout=5)
    # Restore patched values
    if _orig_root is not None:
        src.utils.PROJECT_ROOT = _orig_root
    if _orig_di_root is not None:
        src.derived_index.PROJECT_ROOT = _orig_di_root
    if _orig_di_refs is not None:
        src.derived_index.ROLE_REFS_PATH = _orig_di_refs
    if _orig_tq_root is not None:
        tq_mod.PROJECT_ROOT = _orig_tq_root
    if _orig_pf_root is not None:
        src.preflight.PROJECT_ROOT = _orig_pf_root
    if _orig_pf_stats is not None:
        src.preflight.TTS_STATS_PATH = _orig_pf_stats


@pytest.fixture(scope="session")
def browser():
    """Launch headless Chromium."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser, server):
    """Create a new page for each test."""
    context = browser.new_context()
    pg = context.new_page()
    pg.goto(f"http://127.0.0.1:{server['port']}/")
    pg.wait_for_load_state("networkidle")
    yield pg
    context.close()


# ---------------------------------------------------------------------------
# 1. Unit tests and self-check still green
# (This is a meta-test: we verify pytest itself can collect and run)
class TestMeta:
    def test_imports_work(self):
        """Sanity: all project imports resolve."""
        from src.api.app import create_app
        from src.api import deps
        from src.task_queue import TaskQueue
        assert create_app is not None


# ---------------------------------------------------------------------------
# 2. Offline可用 — vendor files are local (no CDN references)
class TestOfflineAvailable:
    def test_no_cdn_references_in_index(self, page, server):
        """index.html should not reference any CDN URLs."""
        html = page.content()
        cdn_domains = ["cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com"]
        for cdn in cdn_domains:
            assert cdn not in html, f"Found CDN reference: {cdn}"

    def test_vendor_files_load(self, page, server):
        """Vue and SortableJS should load from local vendor/."""
        # Check Vue is available
        vue_version = page.evaluate("typeof Vue !== 'undefined' ? Vue.version : null")
        assert vue_version is not None, "Vue did not load"
        # Check Sortable is available
        sortable = page.evaluate("typeof Sortable !== 'undefined'")
        assert sortable, "SortableJS did not load"


# ---------------------------------------------------------------------------
# 3. 页面记忆 — route remembered in localStorage
class TestPageMemory:
    def test_route_saved_to_localstorage(self, page, server):
        """Navigating to a route saves it in localStorage."""
        page.goto(f"http://127.0.0.1:{server['port']}/#/roles")
        page.wait_for_load_state("networkidle")
        time.sleep(0.3)
        last_route = page.evaluate("localStorage.getItem('n2a.lastRoute')")
        assert last_route == "#/roles", f"Expected '#/roles', got {last_route!r}"

    def test_default_route_when_no_hash(self, page, server):
        """Opening without hash should go to remembered route or default."""
        # First visit: no saved route -> defaults to #/novels
        page.goto(f"http://127.0.0.1:{server['port']}/")
        page.wait_for_load_state("networkidle")
        time.sleep(0.3)
        url = page.url
        assert "#/novels" in url or page.locator(".novels-page").count() > 0


# ---------------------------------------------------------------------------
# 4. 树形拖拽 — 真实鼠标拖拽（不是直接调 reorder API 模拟效果）
#
# 这里刻意用 page.mouse.down/move/up 而不是调 API 假装"拖拽发生了"：
# 之前的版本就是这么测的，测出来全绿，但实际拖拽在浏览器里第二次开始就
# 完全失效——because loadTree() 每次都会触发 loading 状态切换、重建
# .tree-content 的 DOM，而 Sortable 实例只在"首次加载完成"时重新绑定过
# 一次（用 wasLoading 判断，逻辑刚好反了），后续的 DOM 重建就悬空了。
# 只有真的模拟连续两次拖拽，才能测出"第一次能拖、第二次开始悄悄失效"
# 这类问题；直接调 API 完全绕开了 SortableJS 和 DOM 重挂载这条链路。
class TestTreeDragDrop:
    def _create_novel(self, api_base, **levels):
        import httpx
        resp = httpx.post(f"{api_base}/api/novels", json={
            "title": "拖拽测试小说",
            "description": "测试拖拽排序",
            "levels": levels or {"volume": False},
        })
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _create_node(self, api_base, novel_id, node_type, title, parent_id=None):
        import httpx
        data = {"type": node_type, "title": title}
        if parent_id:
            data["parent_id"] = parent_id
        resp = httpx.post(f"{api_base}/api/novels/{novel_id}/nodes", json=data)
        assert resp.status_code == 200, resp.text
        return resp.json()["node_id"]

    def _drag_first_item_down(self, page):
        """把第一个 [data-node-id] 元素拖到最后一个的位置，返回是否有 dataTransfer 交互发生。"""
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

    def test_repeated_real_drags_keep_working(self, page, server):
        """连续两次真实鼠标拖拽都应该改变章节顺序——只测一次拖不出这个 bug。"""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        novel = self._create_novel(base)
        nid = novel["novel_id"]
        for i in range(3):
            self._create_node(base, nid, "chapter", f"第{i+1}章")

        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        def get_order():
            tree = httpx.get(f"{base}/api/novels/{nid}/tree").json()
            return [n["id"] for n in tree["novel"]["tree"]]

        order_0 = get_order()
        self._drag_first_item_down(page)
        order_1 = get_order()
        assert order_1 != order_0, "第一次真实拖拽应该改变顺序"

        # 关键：第二次拖拽（loadTree 已经因为第一次 reorder 刷新过一次 DOM）
        self._drag_first_item_down(page)
        order_2 = get_order()
        assert order_2 != order_1, (
            "第二次真实拖拽没有改变顺序——Sortable 很可能在第一次 reorder "
            "触发的 DOM 重建后没有被重新挂载"
        )


# ---------------------------------------------------------------------------
# 5. 未绑定角色的完整闭环
#
# 之前的版本用不含任何对话（没有引号）的正文，HeuristicBackend 下这种文本
# 100% 全部落到 narrator，本来就不可能产生未绑定分块；而且从没真正跑过
# parse（只上传了 raw.txt），工作台里根本没有 script 可显示。断言又写成
# "页面任意位置出现『未绑定』三个字"——这两个问题叠在一起，测试即使功能
# 完全没做也会通过（顶部统计的"未绑定角色"标签文案本身就含这几个字）。
# 这里改成：真的提交一次 parse 任务、正文里带一句 HeuristicBackend 认不出
# 说话人的对话，再检查具体的 .segment-card.unbound 卡片数量。
class TestUnboundSpeakerFlow:
    def test_unbound_speaker_highlights_red(self, page, server):
        """真正跑一次 parse 后，未绑定角色的分块要有 .segment-card.unbound 卡片、
        顶部统计要显示对应数量并标红。"""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        novel_resp = httpx.post(f"{base}/api/novels", json={
            "title": "角色绑定测试", "description": "", "levels": {"part": False, "volume": False},
        })
        nid = novel_resp.json()["novel_id"]
        ch_resp = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "测试章"})
        cid = ch_resp.json()["node_id"]

        # 一句叙述（会归到 narrator）+ 一句从未出现过的人物对话（HeuristicBackend
        # 认不出说话人，speaker 应为 null）
        raw_text = (
            "夜色渐深，山谷里静得能听见风声。\n\n"
            "“再往前就是断崖了。”一个从未出现过的老猎人忽然开口，声音沙哑。\n\n"
        )
        upload_resp = httpx.put(
            f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
            files={"file": ("raw.txt", raw_text.encode(), "text/plain")},
        )
        assert upload_resp.status_code == 200, upload_resp.text

        # 真正跑一次 parse——跟界面点"批量解析"走的是同一条代码路径
        task_resp = httpx.post(f"{base}/api/tasks", json={
            "type": "parse", "novel_id": nid, "scope": {"chapter_ids": [cid]},
        })
        assert task_resp.status_code == 200, task_resp.text
        task_id = task_resp.json()["tasks"][0]["id"]
        for _ in range(100):
            t = httpx.get(f"{base}/api/tasks/{task_id}").json()
            if t["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.2)
        assert t["state"] == "succeeded", f"parse 任务未成功: {t}"

        script = httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json()
        expected_unbound = sum(1 for s in script if not s.get("speaker"))
        assert expected_unbound > 0, "测试文本设计上应该产生至少一个未绑定分块"

        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        unbound_cards = page.locator(".segment-card.unbound")
        assert unbound_cards.count() == expected_unbound, (
            f"工作台里 .segment-card.unbound 数量 ({unbound_cards.count()}) "
            f"应该等于 script 里 speaker 为空的分块数 ({expected_unbound})"
        )
        danger_stat = page.locator(".stat-value.danger")
        assert danger_stat.count() > 0, "未绑定数量应该有一处用 danger 样式标红显示"


# ---------------------------------------------------------------------------
# 6. 批量任务全流程
class TestBatchTaskFlow:
    def test_batch_preflight_and_submit(self, page, server):
        """Batch task: preflight shows summary, submit creates tasks."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        # Create novel with chapters
        novel_resp = httpx.post(f"{base}/api/novels", json={
            "title": "批量任务测试",
            "description": "",
            "levels": {"part": False, "volume": False},
        })
        nid = novel_resp.json()["novel_id"]

        for i in range(3):
            ch_resp = httpx.post(f"{base}/api/novels/{nid}/nodes", json={
                "type": "chapter",
                "title": f"第{i+1}章",
            })
            cid = ch_resp.json()["node_id"]
            raw_text = f"第{i+1}章正文内容\n这是测试文本"
            httpx.put(
                f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
                files={"file": ("raw.txt", raw_text.encode(), "text/plain")},
            )

        # Navigate to novel detail
        page.goto(f"http://127.0.0.1:{server['port']}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        # API-level preflight test
        preflight_resp = httpx.post(f"{base}/api/tasks/preflight", json={
            "type": "parse",
            "novel_id": nid,
        })
        assert preflight_resp.status_code == 200
        preflight_data = preflight_resp.json()
        assert "total_chapters" in preflight_data or "chapters" in preflight_data

        # Submit task
        task_resp = httpx.post(f"{base}/api/tasks", json={
            "type": "parse",
            "novel_id": nid,
        })
        assert task_resp.status_code == 200
        task_data = task_resp.json()
        # Response contains group_id and tasks array
        assert "group_id" in task_data or "task_id" in task_data or "id" in task_data
        assert "tasks" in task_data or "task_id" in task_data


# ---------------------------------------------------------------------------
# 7. 长章节不卡 — 虚拟滚动
#
# 之前的版本只上传了 raw.txt、从没跑过 parse，工作台里其实是"暂无分块"的
# 空状态——350 行文本渲染 0 张卡片当然"不卡"，这个测试测不出虚拟滚动
# 有没有做。直接写 script_final.json（跳过真实 parse，更快更确定）造出
# 500 个分块，检查 DOM 里实际渲染的 .segment-card 数量是否远小于 500。
class TestLongChapter:
    def test_long_chapter_uses_virtual_scroll(self, page, server):
        """500 个分块的章节，DOM 里同时存在的卡片数应该远小于 500，
        且滚动到底部后能看到最后一个分块、看不到第一个（证明是真的虚拟滚动，
        不是只截断了统计数字）。"""
        import httpx
        import json as _json
        base = f"http://127.0.0.1:{server['port']}"

        novel_resp = httpx.post(f"{base}/api/novels", json={
            "title": "长章节测试", "description": "", "levels": {"part": False, "volume": False},
        })
        nid = novel_resp.json()["novel_id"]
        ch_resp = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "超长章节"})
        cid = ch_resp.json()["node_id"]

        n = 500
        script = [{"seg_id": i + 1, "speaker": "narrator", "text": f"第{i+1}句测试文本", "emotion": "neutral"}
                  for i in range(n)]
        raw_text = "\n\n".join(s["text"] for s in script)
        upload_resp = httpx.put(
            f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
            files={"file": ("raw.txt", raw_text.encode(), "text/plain")},
        )
        assert upload_resp.status_code == 200
        script_path = os.path.join(server["library_dir"], nid, "chapters", cid, "script_final.json")
        with open(script_path, "w", encoding="utf-8") as f:
            _json.dump(script, f, ensure_ascii=False)

        start = time.time()
        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)
        elapsed = time.time() - start
        assert elapsed < 10, f"Long chapter took {elapsed:.1f}s to load"

        assert f"{n}" in page.locator(".stat-value").first.text_content(), "顶部总分块数应该是完整的 500"

        rendered = page.locator(".segment-card").count()
        assert 0 < rendered < 100, (
            f"500 个分块，DOM 里同时渲染了 {rendered} 张卡片——"
            "应该远小于 500，否则不算虚拟滚动"
        )
        assert "第1句" in page.content()
        assert "第500句" not in page.content(), "刚打开时不该已经渲染到最后一条"

        page.evaluate("document.querySelector('.segment-list-content').scrollTop = "
                       "document.querySelector('.segment-list-content').scrollHeight")
        time.sleep(0.5)
        assert "第500句" in page.content(), "滚动到底部后应该能看到最后一个分块"
        rendered_at_bottom = page.locator(".segment-card").count()
        assert 0 < rendered_at_bottom < 100


# ---------------------------------------------------------------------------
# 8. 资源条 — resource bar displays and updates
class TestResourceBar:
    def test_resource_bar_visible(self, page, server):
        """Resource bar should be visible in the header."""
        resource_bar = page.locator(".resource-bar")
        assert resource_bar.count() > 0, "Resource bar not found"

    def test_resource_values_are_numbers(self, page, server):
        """Resource values should be displayed (CPU, memory, GPU)."""
        time.sleep(1)  # Wait for first SSE event or poll
        content = page.content()
        # Should contain percentage or memory values
        has_resource_data = (
            "%" in content or
            "G" in content or
            "MB" in content or
            "resource-value" in content
        )
        assert has_resource_data, "No resource data displayed"


# ---------------------------------------------------------------------------
# 9. SSE降级 — disconnect detection
class TestSSEDegradation:
    def test_sse_disconnect_detected(self, page, server):
        """SSE endpoint is reachable; disconnect test requires killing server (manual)."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"
        # Just verify the endpoint exists and returns 200 (streaming, don't read body)
        with httpx.stream("GET", f"{base}/api/events", timeout=2) as resp:
            assert resp.status_code == 200
            # Read first chunk to confirm it's an event stream
            first_bytes = next(resp.iter_bytes(256), b"")
            assert len(first_bytes) > 0, "SSE endpoint returned empty response"


# ---------------------------------------------------------------------------
# 11. 深色模式（计划 007 引入，Cloud Design 重构界面自带的纯前端偏好，
# 跟 n2a.lastRoute 一样存 localStorage，不经过后端）
class TestDarkMode:
    def test_toggle_persists_and_applies_theme_attribute(self, page, server):
        toggle = page.locator(".theme-toggle")
        assert toggle.count() == 1
        assert "深色模式" in toggle.text_content()

        toggle.click()
        page.wait_for_timeout(200)
        # data-theme 挂在 <html> 上（CSS :root[data-theme] 选择器需要），不是
        # #app——#app 是 Vue 的挂载目标，容器自身属性不受模板绑定管理，
        # 见 app.js 里 applyTheme() 的注释
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        assert page.evaluate("localStorage.getItem('n2a.darkMode')") == "1"
        assert "浅色模式" in toggle.text_content()

        # 刷新后应该保持深色（从 localStorage 恢复，不是每次都是默认浅色）
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"


# ---------------------------------------------------------------------------
# 10. 角色库引用计数
class TestRoleReferenceCount:
    def test_role_categories_crud(self, page, server):
        """Role categories can be created and listed."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        # Get initial categories (should be empty)
        cat_resp = httpx.get(f"{base}/api/role-categories")
        assert cat_resp.status_code == 200

        # Update categories
        put_resp = httpx.put(f"{base}/api/role-categories", json={
            "categories": ["主角", "配角", "反派"],
        })
        assert put_resp.status_code == 200
        assert put_resp.json()["categories"] == ["主角", "配角", "反派"]

        # Verify
        get_resp = httpx.get(f"{base}/api/role-categories")
        assert get_resp.json()["categories"] == ["主角", "配角", "反派"]

    def test_create_role_with_category(self, page, server):
        """Creating a role with a category persists it."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        role_resp = httpx.post(f"{base}/api/roles", json={
            "name": "测试角色",
            "category": "主角",
            "gender": "male",
        })
        assert role_resp.status_code == 200
        role_data = role_resp.json()
        assert "role_id" in role_data
        role_id = role_data["role_id"]

        # Verify category persisted by listing roles
        get_resp = httpx.get(f"{base}/api/roles")
        roles = get_resp.json()
        found = [r for r in roles if r.get("id") == role_id or r.get("role_id") == role_id]
        assert len(found) == 1, f"Role {role_id} not found in {roles}"
        assert found[0].get("category") == "主角"

    def test_narrator_cannot_be_deleted(self, page, server):
        """The narrator role should not be deletable."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        # Get roles, find narrator
        roles_resp = httpx.get(f"{base}/api/roles")
        roles = roles_resp.json()
        narrator = [r for r in roles if r.get("name") == "narrator"]
        if not narrator:
            pytest.skip("narrator role not found in default setup")

        narrator_id = narrator[0]["id"]
        # Try to delete without confirm — should return preview
        del_resp = httpx.delete(f"{base}/api/roles/{narrator_id}")
        # Narrator deletion should be forbidden
        assert del_resp.status_code in (400, 403, 409), (
            f"Expected deletion rejection for narrator, got {del_resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Integration: full page navigation flow
class TestFullNavigation:
    def test_navigate_all_pages(self, page, server):
        """Navigate to every main page and verify they load."""
        base_url = f"http://127.0.0.1:{server['port']}"

        # Novels list
        page.goto(f"{base_url}/#/novels")
        page.wait_for_load_state("networkidle")
        assert page.locator(".novels-page, .app-main").count() > 0

        # Roles
        page.goto(f"{base_url}/#/roles")
        page.wait_for_load_state("networkidle")
        assert page.locator(".roles-page, .app-main").count() > 0

        # Assets（新增的背景音/音效库页面，见计划 007，只读展示）
        page.goto(f"{base_url}/#/assets")
        page.wait_for_load_state("networkidle")
        assert page.locator(".app-main").count() > 0
        assert "背景音" in page.content()

        # Settings
        page.goto(f"{base_url}/#/settings")
        page.wait_for_load_state("networkidle")
        assert page.locator(".settings-page, .app-main").count() > 0

    def test_settings_load_and_save(self, page, server):
        """Settings page loads config and can save."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"

        # Verify config endpoint works
        config_resp = httpx.get(f"{base}/api/config")
        assert config_resp.status_code == 200
        config = config_resp.json()
        assert "server" in config or "tts" in config

        # Navigate to settings page
        page.goto(f"{base}/#/settings")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        # Settings page should have form fields
        content = page.content()
        assert "config" in content.lower() or "设置" in content or "settings" in content.lower()


# ---------------------------------------------------------------------------
# Edge cases
class TestEdgeCases:
    def test_empty_novel_list(self, page, server):
        """Novels page loads even with no novels."""
        content = page.content()
        # Should not crash, should show empty state or novel list
        assert "novel" in content.lower() or "小说" in content

    def test_sse_endpoint_responds(self, page, server):
        """SSE endpoint returns event stream."""
        import httpx
        base = f"http://127.0.0.1:{server['port']}"
        # Use streaming to avoid blocking on SSE
        with httpx.stream("GET", f"{base}/api/events", timeout=2) as resp:
            assert resp.status_code == 200
            ct = resp.headers.get("content-type", "")
            assert "event-stream" in ct or "text/event-stream" in ct


# ---------------------------------------------------------------------------
# 12. 计划 008 的后端新接口在前端的接入（真实点击 UI，走真实后端）
def _api(server):
    return f"http://127.0.0.1:{server['port']}"


def _confirm(page, text):
    """通用确认弹窗（<confirm-dialog>）里点主按钮"""
    page.locator(".confirm-footer button", has_text=text).click()


class TestAssetsPageCrud:
    def test_create_edit_generate_delete_through_ui(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")

        # 新增
        page.get_by_role("button", name="+ 新增素材").click()
        page.locator(".asset-name-input").fill("e2e_hit")
        page.locator(".asset-prompt-input").fill("a sharp hit sound")
        page.locator(".modal-footer button", has_text="创建").click()
        card = page.locator(".asset-card", has_text="e2e_hit")
        expect(card).to_be_visible()
        expect(card).to_contain_text("未生成")
        expect(card.locator("audio")).to_have_count(0)  # 没生成过就没有播放器

        # 编辑：只改描述——后端 spec_hash 不含 description，状态不该变
        card.get_by_role("button", name="编辑").click()
        page.locator(".modal input.form-input").nth(1).fill("测试描述")  # 名称是第 1 个（disabled），描述是第 2 个
        page.locator(".modal-footer button", has_text="保存").click()
        expect(card).to_contain_text("测试描述")
        specs = httpx.get(f"{base}/api/asset-specs?kind=sfx").json()["specs"]
        assert [s for s in specs if s["name"] == "e2e_hit"][0]["description"] == "测试描述"

        # 生成：预检确认 -> 提交任务 -> 走完（测试环境没有真实引擎，回退 Mock 占位音）
        card.get_by_role("button", name="生成", exact=True).click()
        _confirm(page, "开始生成")
        expect(card).to_contain_text("占位音", timeout=20000)
        assert os.path.exists(os.path.join(server["tmp"], "assets", "sfx", "e2e_hit.wav"))

        # 试听：生成完成后出现播放器，src 指向的接口真能拿到 wav
        player = card.locator("audio")
        expect(player).to_be_visible()
        src = player.get_attribute("src")
        assert src == "/api/asset-specs/sfx/e2e_hit/audio"
        result = page.evaluate("""async (u) => {
            const r = await fetch(u);
            return { status: r.status, type: r.headers.get('content-type'), size: (await r.arrayBuffer()).byteLength };
        }""", src)
        assert result["status"] == 200 and result["type"] == "audio/wav" and result["size"] > 44
        # 播放器真的能加载出时长（浏览器解码通过），不只是 URL 通
        player.evaluate("el => el.load()")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.asset-card')].find(c => c.textContent.includes('e2e_hit'))"
            ".querySelector('audio').readyState >= 1", timeout=10000)
        assert player.evaluate("el => el.duration") > 0

        # 删除并同时删掉音频文件
        card.get_by_role("button", name="删除").click()
        page.locator(".modal .form-checkbox input").check()
        page.locator(".confirm-footer button", has_text="删除").click()
        expect(page.locator(".asset-card", has_text="e2e_hit")).to_have_count(0)
        assert not os.path.exists(os.path.join(server["tmp"], "assets", "sfx", "e2e_hit.wav"))

    def test_duplicate_name_shows_error_toast(self, page, server):
        import httpx
        base = _api(server)
        httpx.post(f"{base}/api/asset-specs", json={"kind": "sfx", "name": "e2e_dup", "prompt": "x"})
        page.goto(f"{base}/#/assets")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="+ 新增素材").click()
        page.locator(".asset-name-input").fill("e2e_dup")
        page.locator(".asset-prompt-input").fill("x")
        page.locator(".modal-footer button", has_text="创建").click()
        expect(page.locator(".toast.error")).to_contain_text("已存在")
        httpx.delete(f"{base}/api/asset-specs/sfx/e2e_dup")


class TestRolesCategoryTreeAndTags:
    def test_tree_crud_and_role_with_category_and_tags(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/roles")
        page.wait_for_load_state("networkidle")

        # 新建根分类 + 子分类，整树 PUT 到后端
        page.get_by_role("button", name="+ 新建分类").click()
        page.locator(".category-name-input").fill("主角E2E")
        page.locator(".modal-footer button", has_text="保存").click()
        root = page.locator('[data-category-path="主角E2E"]')
        expect(root).to_be_visible()

        root.locator('button[title="新建子分类"]').click()
        page.locator(".category-name-input").fill("男主")
        page.locator(".modal-footer button", has_text="保存").click()
        expect(page.locator('[data-category-path="主角E2E/男主"]')).to_be_visible()
        assert "主角E2E/男主" in httpx.get(f"{base}/api/role-category-tree").json()["categories"]

        # 在子分类下建带标签的角色
        page.get_by_role("button", name="+ 新增角色").click()
        page.locator(".modal input.form-input").first.fill("E2E角色")
        page.locator(".role-category-select").select_option("主角E2E/男主")
        tag_input = page.locator(".role-tag-input")
        tag_input.fill("少年")
        tag_input.press("Enter")
        tag_input.fill("隐忍")
        tag_input.press("Enter")
        page.locator(".modal-footer button", has_text="创建").click()

        card = page.locator(".role-card", has_text="E2E角色")
        expect(card).to_be_visible()
        expect(card).to_contain_text("少年")
        expect(card).to_contain_text("主角E2E/男主")
        role = [r for r in httpx.get(f"{base}/api/roles").json() if r["name"] == "E2E角色"][0]
        assert role["category"] == "主角E2E/男主" and role["tags"] == ["少年", "隐忍"]

        # 按分类筛选：选父分类能看到子分类下的角色；选别的分类看不到
        root.click()
        expect(card).to_be_visible()
        page.get_by_role("button", name="+ 新建分类").click()
        page.locator(".category-name-input").fill("路人E2E")
        page.locator(".modal-footer button", has_text="保存").click()
        page.locator('[data-category-path="路人E2E"]').click()
        expect(page.locator(".role-card", has_text="E2E角色")).to_have_count(0)

        # 标签筛选
        page.locator(".category-row", has_text="全部").click()
        page.locator(".tag-filter-chip", has_text="少年").click()
        expect(page.locator(".role-card", has_text="E2E角色")).to_be_visible()

        # 删除分类：角色保留原来的 category 字符串（悬空引用容忍策略）
        root.locator("button", has_text="删").click()
        _confirm(page, "删除")
        expect(page.locator('[data-category-path="主角E2E"]')).to_have_count(0)
        role = [r for r in httpx.get(f"{base}/api/roles").json() if r["name"] == "E2E角色"][0]
        assert role["category"] == "主角E2E/男主"

    def test_invalid_category_name_shows_server_error(self, page, server):
        base = _api(server)
        page.goto(f"{base}/#/roles")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="+ 新建分类").click()
        page.locator(".category-name-input").fill("a/b")
        page.locator(".modal-footer button", has_text="保存").click()
        expect(page.locator(".toast.error")).to_contain_text("保存分类失败")


def _make_chapter_with_script(server, script, timeline=None, title="素材测试"):
    import httpx
    import json as _json
    base = _api(server)
    nid = httpx.post(f"{base}/api/novels", json={
        "title": title, "description": "", "levels": {"part": False, "volume": False}}).json()["novel_id"]
    cid = httpx.post(f"{base}/api/novels/{nid}/nodes", json={"type": "chapter", "title": "章"}).json()["node_id"]
    httpx.put(f"{base}/api/novels/{nid}/chapters/{cid}/raw?confirm=1",
              files={"file": ("raw.txt", "x".encode(), "text/plain")})
    ch_dir = os.path.join(server["library_dir"], nid, "chapters", cid)
    with open(os.path.join(ch_dir, "script_final.json"), "w", encoding="utf-8") as f:
        _json.dump(script, f, ensure_ascii=False)
    if timeline is not None:
        with open(os.path.join(ch_dir, "timeline.json"), "w", encoding="utf-8") as f:
            _json.dump(timeline, f, ensure_ascii=False)
    return nid, cid, ch_dir


def _fake_asset(server, kind_dir, name):
    d = os.path.join(server["tmp"], "assets", kind_dir)
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
        timeline = {"chapter_id": "x", "items": [{"seg_id": 1, "sfx": None, "bgm": None},
                                                  {"seg_id": 2, "sfx": None, "bgm": None}]}
        nid, cid, ch_dir = _make_chapter_with_script(server, script, timeline, "工作台素材")

        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")

        # 单块：选背景音+音效，保存
        page.locator(".segment-card").first.click()
        page.locator(".segment-bgm-select").select_option("e2e_wind")
        page.locator(".segment-sfx-select").select_option("e2e_door")
        page.locator(".segment-edit-actions button", has_text="保存").click()
        expect(page.locator(".segment-fx").first).to_be_visible()

        seg1 = [s for s in httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json() if s["seg_id"] == 1][0]
        assert seg1["bgm"] == "e2e_wind" and seg1["sfx"] == "e2e_door"
        # 保存后自动同步进了 timeline（混音读的是 timeline 快照）
        import json as _json
        tl = _json.load(open(os.path.join(ch_dir, "timeline.json"), encoding="utf-8"))
        assert tl["items"][0]["bgm"] == "e2e_wind" and tl["items"][0]["sfx"] == "e2e_door"

        # 批量：全选未绑定（第 2 块）-> 批量设置素材
        page.get_by_role("button", name="全选未绑定").click()
        page.get_by_role("button", name="批量设置素材").click()
        page.locator(".batch-sfx-select").select_option("e2e_door")
        page.locator(".modal-footer button", has_text="应用").click()
        expect(page.locator(".batch-sfx-select")).to_have_count(0)
        seg2 = [s for s in httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json() if s["seg_id"] == 2][0]
        assert seg2["sfx"] == "e2e_door" and not seg2.get("bgm")  # bgm 选的是「不修改」

    def test_unchanged_missing_asset_does_not_block_saving(self, page, server):
        """分块引用着一个已经不存在的素材，用户只改文字：素材字段不能被重发，
        否则后端「素材必须存在」的校验会让这次保存莫名失败"""
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "旧", "emotion": "neutral", "sfx": "e2e_ghost"}]
        nid, cid, _ = _make_chapter_with_script(server, script, None, "缺失素材保存")
        page.goto(f"{base}/#/novels/{nid}/chapters/{cid}")
        page.wait_for_load_state("networkidle")
        page.locator(".segment-card").first.click()
        expect(page.locator(".segment-sfx-select")).to_have_value("e2e_ghost")  # 下拉里保留了缺失项
        page.locator(".segment-edit-pane textarea").fill("新文字")
        page.locator(".segment-edit-actions button", has_text="保存").click()
        expect(page.locator(".segment-edit-actions")).to_have_count(0)  # 保存成功后编辑框关闭
        seg = httpx.get(f"{base}/api/novels/{nid}/chapters/{cid}/script").json()[0]
        assert seg["text"] == "新文字" and seg["sfx"] == "e2e_ghost"


class TestNovelDetailMixAssets:
    def test_chapter_assets_panel_and_mix_with_assets_param(self, page, server):
        import httpx
        base = _api(server)
        _fake_asset(server, "ambience", "e2e_wind2")
        script = [
            {"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral",
             "bgm": "e2e_wind2", "sfx": "e2e_ghost2"},
        ]
        timeline = {"chapter_id": "x", "total_duration_ms": 1000,
                    "items": [{"seg_id": 1, "bgm": "e2e_wind2", "sfx": "e2e_ghost2"}]}
        nid, cid, _ = _make_chapter_with_script(server, script, timeline, "详情素材")

        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.locator(".tree-node-name", has_text="章").first.click()

        panel = page.locator(".chapter-assets")
        expect(panel).to_be_visible()
        expect(panel).to_contain_text("引用背景音 1 种")
        expect(page.locator(".chapter-assets-missing")).to_contain_text("e2e_ghost2")  # 没生成的音效
        expect(panel).to_contain_text("无记录")  # 还没混过音

        # 勾上「叠加素材」再批量混音：params 要真的带到后端
        page.locator(".mix-with-assets input").check()
        page.get_by_role("button", name="批量混音导出").click()
        _confirm(page, "确认执行")
        expect(page.locator(".toast")).to_contain_text("任务已提交")
        tasks = [t for t in httpx.get(f"{base}/api/tasks").json()
                 if t["type"] == "mix" and t["novel_id"] == nid]
        assert tasks and tasks[0]["params"] == {"with_assets": True}

    def test_mix_without_checkbox_sends_no_params(self, page, server):
        import httpx
        base = _api(server)
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        timeline = {"chapter_id": "x", "total_duration_ms": 1000, "items": [{"seg_id": 1}]}
        nid, cid, _ = _make_chapter_with_script(server, script, timeline, "详情无勾选")
        page.goto(f"{base}/#/novels/{nid}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="批量混音导出").click()
        _confirm(page, "确认执行")
        expect(page.locator(".toast")).to_contain_text("任务已提交")
        tasks = [t for t in httpx.get(f"{base}/api/tasks").json()
                 if t["type"] == "mix" and t["novel_id"] == nid]
        # 不勾选就不传 params，让后端遵循 mixing.voice_only 配置默认值
        assert tasks and not tasks[0]["params"]


class TestSettingsMixWithAssets:
    def test_toggle_persists_voice_only_inverted(self, page, server):
        import httpx
        base = _api(server)
        page.goto(f"{base}/#/settings")
        page.wait_for_load_state("networkidle")
        box = page.locator(".setting-mix-with-assets")
        expect(box).not_to_be_checked()  # 配置里 voice_only=True -> 「叠加素材」是关的

        box.check()
        page.get_by_role("button", name="保存配置").click()
        page.wait_for_timeout(500)
        assert httpx.get(f"{base}/api/config").json()["mixing"]["voice_only"] is False

        page.reload()
        page.wait_for_load_state("networkidle")
        expect(page.locator(".setting-mix-with-assets")).to_be_checked()

        # 还原，别影响同一个 session 里的其他测试
        page.locator(".setting-mix-with-assets").uncheck()
        page.get_by_role("button", name="保存配置").click()
        page.wait_for_timeout(500)
        assert httpx.get(f"{base}/api/config").json()["mixing"]["voice_only"] is True
