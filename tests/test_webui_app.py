"""
测试 src/webui_app.py（计划 002：Gradio 管理界面）里的纯逻辑函数。

不启动真实 Gradio server（那部分只在 test_webui_app_build 里做一次性构建/
启动/关闭冒烟测试）；不碰真实 GPU/llama-server/常驻 TTS 服务——涉及 GPU 的
函数（precompute_role_embedding/run_tts_one 等）全部 monkeypatch 掉底层调用，
只验证 webui_app.py 自己的编排逻辑（该不该阻止执行、日志文案对不对、
是否正确转发参数）。
"""
import json
import os
import shutil

import pytest

from src import webui_app
from src.utils import get_project_root
from tools import gpu_arbiter


# --------------------------------------------------------------------------
# 显存状态 / 换手前置检查
# --------------------------------------------------------------------------

class TestGpuStatusText:
    def test_tts_owner(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        assert "常驻 TTS 服务" in webui_app.get_gpu_status_text()

    def test_llm_owner(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_LLM)
        assert "LLM" in webui_app.get_gpu_status_text()

    def test_no_owner(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        assert "空闲" in webui_app.get_gpu_status_text()


class TestCheckDaemonNotBlocking:
    def test_blocked_when_tts_daemon_running(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        ok, msg = webui_app.check_daemon_not_blocking()
        assert ok is False
        assert "常驻 TTS 服务" in msg

    def test_allowed_when_llm_owner(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_LLM)
        ok, msg = webui_app.check_daemon_not_blocking()
        assert ok is True
        assert msg == ""

    def test_allowed_when_no_owner(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        ok, msg = webui_app.check_daemon_not_blocking()
        assert ok is True


# --------------------------------------------------------------------------
# Tab 1：仪表盘
# --------------------------------------------------------------------------

@pytest.fixture
def multi_chapter_dirs(tmp_chapters_dir):
    """构造两个章节目录：一个只有 raw.txt，一个有完整产物+成片"""
    ch1 = os.path.join(tmp_chapters_dir, "ch_0001")
    os.makedirs(ch1)
    with open(os.path.join(ch1, "raw.txt"), "w", encoding="utf-8") as f:
        f.write("测试章节一")

    ch2 = os.path.join(tmp_chapters_dir, "ch_0002")
    os.makedirs(os.path.join(ch2, "output"))
    with open(os.path.join(ch2, "raw.txt"), "w", encoding="utf-8") as f:
        f.write("测试章节二")
    with open(os.path.join(ch2, "output", "chapter_0002.mp3"), "wb") as f:
        f.write(b"fake mp3")
    return tmp_chapters_dir


class TestDashboardRows:
    def test_lists_all_chapters_with_marks(self, multi_chapter_dirs):
        rows = webui_app.dashboard_rows(multi_chapter_dirs)
        ids = [r[0] for r in rows]
        assert ids == ["ch_0001", "ch_0002"]
        # ch_0001: 只有 raw
        row1 = rows[0]
        assert row1[2] == "✓"  # raw
        assert row1[6] == "✗"  # mp3
        # ch_0002: 有 mp3
        row2 = rows[1]
        assert row2[6] == "✓"

    def test_empty_dir_returns_empty_list(self, tmp_chapters_dir):
        assert webui_app.dashboard_rows(tmp_chapters_dir) == []


class TestListChapterIds:
    def test_lists_sorted_ids(self, multi_chapter_dirs):
        assert webui_app.list_chapter_ids(multi_chapter_dirs) == ["ch_0001", "ch_0002"]

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert webui_app.list_chapter_ids(str(tmp_path / "no_such_dir")) == []


class TestRunParseTtsMixOne:
    def test_run_parse_one_no_chapter_selected(self):
        assert webui_app.run_parse_one("", "x") == "请先选择章节"

    def test_run_parse_one_blocked_by_daemon(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        result = webui_app.run_parse_one("ch_0001", tmp_chapters_dir)
        assert "常驻 TTS 服务" in result

    def test_run_parse_one_success(self, monkeypatch, tmp_chapter_dir, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        monkeypatch.setattr(webui_app, "process_chapter_parse", lambda ch_dir: os.path.join(ch_dir, "script_draft.json"))
        result = webui_app.run_parse_one("ch_0001", tmp_chapters_dir)
        assert result.startswith("✓")

    def test_run_parse_one_reports_exception(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)

        def _raise(ch_dir):
            raise FileNotFoundError("raw.txt 不存在")
        monkeypatch.setattr(webui_app, "process_chapter_parse", _raise)
        result = webui_app.run_parse_one("ch_0001", tmp_chapters_dir)
        assert result.startswith("✗")
        assert "raw.txt" in result

    def test_run_tts_one_blocked_by_daemon(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        result = webui_app.run_tts_one("ch_0001", tmp_chapters_dir)
        assert "常驻 TTS 服务" in result

    def test_run_tts_one_success(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_LLM)
        monkeypatch.setattr(webui_app, "process_chapter_tts", lambda ch_dir: os.path.join(ch_dir, "timeline.json"))
        result = webui_app.run_tts_one("ch_0001", tmp_chapters_dir)
        assert result.startswith("✓")

    def test_run_mix_one_no_chapter(self):
        assert webui_app.run_mix_one("", "x") == "请先选择章节"

    def test_run_mix_one_success(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(webui_app, "mix_chapter", lambda ch_dir: os.path.join(ch_dir, "output", "x.mp3"))
        result = webui_app.run_mix_one("ch_0001", tmp_chapters_dir)
        assert result.startswith("✓")
        assert "纯人声" in result


class TestRunAllGenerators:
    def test_run_parse_all_blocked_by_daemon(self, monkeypatch, multi_chapter_dirs):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        outputs = list(webui_app.run_parse_all(multi_chapter_dirs))
        assert len(outputs) == 1
        assert "常驻 TTS 服务" in outputs[0]

    def test_run_parse_all_empty_dir(self, monkeypatch, tmp_chapters_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        outputs = list(webui_app.run_parse_all(tmp_chapters_dir))
        assert "未发现任何章节工作区" in outputs[-1]

    def test_run_parse_all_processes_every_chapter(self, monkeypatch, multi_chapter_dirs):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        calls = []
        monkeypatch.setattr(webui_app, "process_chapter_parse", lambda ch_dir: calls.append(ch_dir))
        outputs = list(webui_app.run_parse_all(multi_chapter_dirs))
        assert len(calls) == 2
        final_log = outputs[-1]
        assert "ch_0001" in final_log and "ch_0002" in final_log
        assert "全部解析完成" in final_log

    def test_run_parse_all_continues_after_single_chapter_failure(self, monkeypatch, multi_chapter_dirs):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)

        def _maybe_raise(ch_dir):
            if "ch_0001" in ch_dir:
                raise RuntimeError("boom")
        monkeypatch.setattr(webui_app, "process_chapter_parse", _maybe_raise)
        outputs = list(webui_app.run_parse_all(multi_chapter_dirs))
        final_log = outputs[-1]
        assert "✗ ch_0001" in final_log
        assert "✓ ch_0002" in final_log

    def test_run_tts_all_processes_every_chapter(self, monkeypatch, multi_chapter_dirs):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        calls = []
        monkeypatch.setattr(webui_app, "process_chapter_tts", lambda ch_dir: calls.append(ch_dir))
        outputs = list(webui_app.run_tts_all(multi_chapter_dirs))
        assert len(calls) == 2
        assert "全部 TTS 合成完成" in outputs[-1]

    def test_run_mix_all_processes_every_chapter(self, monkeypatch, multi_chapter_dirs):
        calls = []
        monkeypatch.setattr(webui_app, "mix_chapter", lambda ch_dir: calls.append(ch_dir))
        outputs = list(webui_app.run_mix_all(multi_chapter_dirs))
        assert len(calls) == 2
        assert "全部混音完成" in outputs[-1]


# --------------------------------------------------------------------------
# Tab 2：角色管理
# --------------------------------------------------------------------------

class TestRoleRowsAndChoices:
    def test_role_rows_includes_embedding_badge(self, tmp_roles_dir):
        rows = webui_app.role_rows(tmp_roles_dir)
        assert any(r[0] == "narrator" for r in rows)
        narrator_row = next(r for r in rows if r[0] == "narrator")
        assert "未预计算" in narrator_row[3]

    def test_role_choices_returns_ids(self, tmp_roles_dir):
        choices = webui_app.role_choices(tmp_roles_dir)
        assert "narrator" in choices


class TestGetRoleDetail:
    def test_no_role_selected(self, tmp_roles_dir):
        detail = webui_app.get_role_detail("", tmp_roles_dir)
        assert detail["reference_audio"] is None

    def test_unknown_role(self, tmp_roles_dir):
        detail = webui_app.get_role_detail("no_such_role", tmp_roles_dir)
        assert detail["embedding_text"] == "角色不存在"

    def test_known_role_returns_speed_and_audio(self, tmp_roles_dir):
        detail = webui_app.get_role_detail("narrator", tmp_roles_dir)
        assert detail["speed"] == 1.0
        assert detail["reference_audio"] is not None


class TestSaveRoleSpeed:
    def test_no_role_selected(self, tmp_roles_dir):
        assert webui_app.save_role_speed("", 1.0, tmp_roles_dir) == "请先选择角色"

    def test_missing_config_json(self, tmp_roles_dir):
        result = webui_app.save_role_speed("no_such_role", 1.2, tmp_roles_dir)
        assert result.startswith("✗")

    def test_updates_speed_field(self, tmp_roles_dir):
        result = webui_app.save_role_speed("narrator", 1.3, tmp_roles_dir)
        assert result.startswith("✓")
        with open(os.path.join(tmp_roles_dir, "narrator", "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["speed"] == 1.3
        # 其余字段不应丢失
        assert cfg["name"] == "旁白"


class TestUploadRoleReference:
    def test_no_role_selected(self, tmp_roles_dir, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"x")
        assert webui_app.upload_role_reference("", str(wav), tmp_roles_dir) == "请先选择角色"

    def test_no_file_selected(self, tmp_roles_dir):
        assert webui_app.upload_role_reference("narrator", None, tmp_roles_dir) == "未选择音频文件"

    def test_success_replaces_reference_and_invalidates_cache(self, tmp_roles_dir, tmp_path):
        # 先放一份旧的 embedding 缓存，验证替换后被清理
        role_dir = os.path.join(tmp_roles_dir, "narrator")
        with open(os.path.join(role_dir, "speaker_embeddings.pt"), "wb") as f:
            f.write(b"stale")

        new_wav = tmp_path / "new_ref.wav"
        new_wav.write_bytes(b"brand new audio")
        result = webui_app.upload_role_reference("narrator", str(new_wav), tmp_roles_dir)

        assert result.startswith("✓")
        with open(os.path.join(role_dir, "reference.wav"), "rb") as f:
            assert f.read() == b"brand new audio"
        assert not os.path.exists(os.path.join(role_dir, "speaker_embeddings.pt"))


class TestAddRemoveRole:
    def test_add_role_requires_name(self, tmp_roles_dir):
        assert webui_app.add_role("", "unknown", tmp_roles_dir).startswith("✗")

    def test_add_role_success(self, tmp_roles_dir):
        result = webui_app.add_role("测试角色", "female", tmp_roles_dir)
        assert result.startswith("✓")
        assert "测试角色" in result

    def test_remove_role_no_selection(self, tmp_roles_dir):
        assert webui_app.remove_role("", tmp_roles_dir) == "请先选择角色"

    def test_remove_role_narrator_protected(self, tmp_roles_dir):
        result = webui_app.remove_role("narrator", tmp_roles_dir)
        assert result.startswith("✗")

    def test_remove_role_success(self, tmp_roles_dir):
        webui_app.add_role("待删除角色", "unknown", tmp_roles_dir)
        choices = webui_app.role_choices(tmp_roles_dir)
        new_id = [c for c in choices if c not in ("narrator", "lin_dong", "shu_ban", "ma_tie_cheng", "liu_he", "su_yan")][0]
        result = webui_app.remove_role(new_id, tmp_roles_dir)
        assert result.startswith("✓")
        assert new_id not in webui_app.role_choices(tmp_roles_dir)


class TestPrecomputeRoleEmbedding:
    def test_no_role_selected(self, tmp_roles_dir):
        assert webui_app.precompute_role_embedding("", tmp_roles_dir) == "请先选择角色"

    def test_blocked_when_daemon_running(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_TTS)
        result = webui_app.precompute_role_embedding("narrator", tmp_roles_dir)
        assert "常驻 TTS 服务" in result

    def test_wraps_with_llm_suspended_and_reports_success(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: False)  # llm 本来没在跑

        calls = []

        class _FakeRoles:
            @staticmethod
            def precompute_embedding(role_id, manifest, roles_dir, config=None, force=True):
                calls.append(role_id)
                return {"ok": True, "error": None}

        monkeypatch.setattr(webui_app, "roles_mod", _FakeRoles)
        # LlmSuspendedForGpu 需要 manifest.load_manifest 的替身，直接绕过（precompute_role_embedding
        # 内部先调用 roles_mod.load_manifest，用同一个 fake 模块补上）
        _FakeRoles.load_manifest = staticmethod(lambda roles_dir=None: {"roles": {"narrator": {}}})

        result = webui_app.precompute_role_embedding("narrator", tmp_roles_dir, config={"llm": {}})
        assert result.startswith("✓")
        assert calls == ["narrator"]

    def test_reports_failure(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: False)

        class _FakeRoles:
            load_manifest = staticmethod(lambda roles_dir=None: {"roles": {"narrator": {}}})

            @staticmethod
            def precompute_embedding(role_id, manifest, roles_dir, config=None, force=True):
                return {"ok": False, "error": "模型加载失败"}

        monkeypatch.setattr(webui_app, "roles_mod", _FakeRoles)
        result = webui_app.precompute_role_embedding("narrator", tmp_roles_dir, config={"llm": {}})
        assert result.startswith("✗")
        assert "模型加载失败" in result


# --------------------------------------------------------------------------
# Tab 3：章节管理
# --------------------------------------------------------------------------

class TestChapterFileReaders:
    def test_read_raw_text_missing(self, tmp_chapters_dir):
        assert "不存在" in webui_app.read_chapter_raw_text("ch_0001", tmp_chapters_dir)

    def test_read_raw_text_present(self, tmp_chapters_dir):
        ch_dir = os.path.join(tmp_chapters_dir, "ch_0001")
        os.makedirs(ch_dir)
        with open(os.path.join(ch_dir, "raw.txt"), "w", encoding="utf-8") as f:
            f.write("这是测试正文")
        assert webui_app.read_chapter_raw_text("ch_0001", tmp_chapters_dir) == "这是测试正文"

    def test_read_final_json_missing(self, tmp_chapters_dir):
        assert "不存在" in webui_app.read_chapter_final_json_text("ch_0001", tmp_chapters_dir)

    def test_read_final_json_present(self, tmp_chapters_dir):
        ch_dir = os.path.join(tmp_chapters_dir, "ch_0001")
        os.makedirs(ch_dir)
        data = [{"seg_id": 1, "speaker": "narrator", "text": "你好"}]
        with open(os.path.join(ch_dir, "script_final.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        text = webui_app.read_chapter_final_json_text("ch_0001", tmp_chapters_dir)
        assert json.loads(text) == data

    def test_no_chapter_selected_returns_empty(self, tmp_chapters_dir):
        assert webui_app.read_chapter_raw_text("", tmp_chapters_dir) == ""
        assert webui_app.read_chapter_final_json_text("", tmp_chapters_dir) == ""

    def test_status_text(self, tmp_chapters_dir):
        ch_dir = os.path.join(tmp_chapters_dir, "ch_0001")
        os.makedirs(ch_dir)
        with open(os.path.join(ch_dir, "raw.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        text = webui_app.get_chapter_status_text("ch_0001", tmp_chapters_dir)
        assert "状态" in text

    def test_status_text_no_chapter(self, tmp_chapters_dir):
        assert webui_app.get_chapter_status_text("", tmp_chapters_dir) == ""


# --------------------------------------------------------------------------
# Tab 4：TTS 试听
# --------------------------------------------------------------------------

class TestEmotionChoices:
    def test_includes_neutral_and_happy(self):
        choices = webui_app.emotion_choices()
        assert "neutral" in choices
        assert "happy" in choices


class TestDaemonStatusAndPlan:
    def test_status_text_reflects_running_state(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: True)
        assert "运行中" in webui_app.get_daemon_status_text()
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: False)
        assert "未运行" in webui_app.get_daemon_status_text()

    def test_plan_daemon_start_delegates_to_gpu_arbiter(self, monkeypatch):
        sentinel = {"noop": True, "steps": [], "estimated_seconds": 0.0,
                    "current_owner": None, "target_owner": "tts"}
        monkeypatch.setattr(gpu_arbiter, "plan_swap", lambda target: sentinel)
        assert webui_app.plan_daemon_start() is sentinel

    def test_start_daemon_reports_success(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "ensure_started",
                             lambda self: {"ok": True, "error": None, "already_running": False})
        assert webui_app.start_daemon().startswith("✓")

    def test_start_daemon_already_running(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "ensure_started",
                             lambda self: {"ok": True, "error": None, "already_running": True})
        assert "已经在运行" in webui_app.start_daemon()

    def test_start_daemon_failure(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "ensure_started",
                             lambda self: {"ok": False, "error": "加载失败", "already_running": False})
        result = webui_app.start_daemon()
        assert result.startswith("✗")
        assert "加载失败" in result

    def test_release_gpu_to_llm_success(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "shutdown",
                             lambda self: {"ok": True, "error": None, "was_running": True})
        assert webui_app.release_gpu_to_llm().startswith("✓")

    def test_release_gpu_to_llm_nothing_to_release(self, monkeypatch):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "shutdown",
                             lambda self: {"ok": True, "error": None, "was_running": False})
        assert "没有运行中" in webui_app.release_gpu_to_llm()


class TestPreviewTts:
    def test_no_role_selected(self, tmp_roles_dir):
        wav, msg = webui_app.preview_tts("", "文字", "neutral", tmp_roles_dir)
        assert wav is None
        assert "选择角色" in msg

    def test_no_text(self, tmp_roles_dir):
        wav, msg = webui_app.preview_tts("narrator", "  ", "neutral", tmp_roles_dir)
        assert wav is None
        assert "文字" in msg

    def test_daemon_not_running(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: False)
        wav, msg = webui_app.preview_tts("narrator", "你好", "neutral", tmp_roles_dir)
        assert wav is None
        assert "尚未启动" in msg

    def test_success_returns_wav_path(self, monkeypatch, tmp_roles_dir, tmp_path):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: True)
        out_wav = str(tmp_path / "out.wav")

        def _fake_synthesize_batch(self, jobs):
            with open(jobs[0]["out"], "wb") as f:
                f.write(b"fake wav")
            return {"preview": True}

        monkeypatch.setattr(webui_app.IndexTTSDaemon, "synthesize_batch", _fake_synthesize_batch)
        wav, msg = webui_app.preview_tts("narrator", "你好", "neutral", tmp_roles_dir)
        assert wav is not None and os.path.exists(wav)
        assert msg.startswith("✓")

    def test_synthesis_failure_reported(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: True)

        def _fake_synthesize_batch(self, jobs):
            return {"preview": False}

        monkeypatch.setattr(webui_app.IndexTTSDaemon, "synthesize_batch", _fake_synthesize_batch)
        wav, msg = webui_app.preview_tts("narrator", "你好", "neutral", tmp_roles_dir)
        assert wav is None
        assert msg.startswith("✗")

    def test_synthesis_raises_reported(self, monkeypatch, tmp_roles_dir):
        monkeypatch.setattr(webui_app.IndexTTSDaemon, "is_running", lambda self: True)

        def _fake_synthesize_batch(self, jobs):
            raise RuntimeError("崩了")

        monkeypatch.setattr(webui_app.IndexTTSDaemon, "synthesize_batch", _fake_synthesize_batch)
        wav, msg = webui_app.preview_tts("narrator", "你好", "neutral", tmp_roles_dir)
        assert wav is None
        assert "崩了" in msg


# --------------------------------------------------------------------------
# 构建自检：只验证能无异常构建 + 启动 + 关闭，不测内部交互
# --------------------------------------------------------------------------

class TestBuildApp:
    def test_build_app_with_real_project_root(self):
        demo = webui_app.build_app()
        assert demo is not None

    def test_build_app_with_isolated_project_root(self, tmp_path):
        os.makedirs(tmp_path / "chapters")
        shutil.copytree(os.path.join(get_project_root(), "roles"), tmp_path / "roles")
        demo = webui_app.build_app(project_root=str(tmp_path))
        assert demo is not None

    def test_launch_and_close_does_not_raise(self, tmp_path):
        os.makedirs(tmp_path / "chapters")
        shutil.copytree(os.path.join(get_project_root(), "roles"), tmp_path / "roles")
        demo = webui_app.build_app(project_root=str(tmp_path))
        demo.queue()
        try:
            demo.launch(prevent_thread_lock=True, quiet=True, show_error=True, server_port=17861)
        finally:
            demo.close()
