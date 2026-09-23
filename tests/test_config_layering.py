"""
配置分层（global_config.yaml + local_config.yaml）及其配套改动的测试：
- load_global_config 的 local 覆盖层深合并
- PATCH /api/config 按键落到 local / global 两层，注释不丢
- 三个此前"写了没人读"的键：server.host / server.port / server.library_root
- 路径类配置缺失时不再回落到某台机器的字面量
- llama-server 拉起命令、espeak 路径改读配置
"""
import importlib.util
import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from src.domain import library
from src import utils
from src.utils import load_global_config, resolve_optional_path


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """把 PROJECT_ROOT 指到临时目录，并保证 library_root 缓存不串到别的测试"""
    monkeypatch.setattr(utils, "PROJECT_ROOT", str(tmp_path))
    library.invalidate_library_root()
    yield tmp_path
    library.invalidate_library_root()


# --------------------------------------------------------------------------
# load_global_config：local 覆盖层
# --------------------------------------------------------------------------

class TestLocalOverlay:
    def test_local_overrides_scalar_and_keeps_siblings(self, project_root):
        _write(project_root / "config" / "global_config.yaml",
               "tts:\n  index_tts:\n    timeout_sec: 100\n    python_bin: from_global\n  sample_rate: 24000\n")
        _write(project_root / "config" / "local_config.yaml",
               "tts:\n  index_tts:\n    python_bin: from_local\n")
        cfg = load_global_config()
        assert cfg["tts"]["index_tts"]["python_bin"] == "from_local"
        assert cfg["tts"]["index_tts"]["timeout_sec"] == 100  # 同层兄弟键保留
        assert cfg["tts"]["sample_rate"] == 24000

    def test_local_adds_new_sections(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "mixing:\n  bitrate: 192k\n")
        _write(project_root / "config" / "local_config.yaml", "tools:\n  llm_serve_command: myai\n")
        cfg = load_global_config()
        assert cfg["tools"]["llm_serve_command"] == "myai"
        assert cfg["mixing"]["bitrate"] == "192k"

    def test_list_is_replaced_not_merged(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "x:\n  items: [1, 2, 3]\n")
        _write(project_root / "config" / "local_config.yaml", "x:\n  items: [9]\n")
        assert load_global_config()["x"]["items"] == [9]

    def test_missing_local_equals_global_only(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "a:\n  b: 1\n")
        assert load_global_config() == {"a": {"b": 1}}

    def test_empty_local_file_is_fine(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "a: 1\n")
        _write(project_root / "config" / "local_config.yaml", "")
        assert load_global_config() == {"a": 1}

    def test_explicit_config_path_does_not_pick_up_local(self, project_root):
        """显式指定配置文件的调用方要的就是那一个文件，不该被本机 local 悄悄改写"""
        _write(project_root / "config" / "local_config.yaml", "a:\n  b: from_local\n")
        custom = project_root / "custom.yaml"
        _write(custom, "a:\n  b: from_custom\n")
        assert load_global_config(str(custom))["a"]["b"] == "from_custom"

    def test_explicit_local_path_is_applied(self, project_root):
        custom = project_root / "custom.yaml"
        other = project_root / "other_local.yaml"
        _write(custom, "a:\n  b: 1\n  c: 2\n")
        _write(other, "a:\n  b: 10\n")
        assert load_global_config(str(custom), local_path=str(other)) == {"a": {"b": 10, "c": 2}}

    def test_deep_merge_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"c": 2}}
        merged = utils._deep_merge(base, overlay)
        assert merged == {"a": {"b": 1, "c": 2}}
        assert base == {"a": {"b": 1}} and overlay == {"a": {"c": 2}}

    def test_resolve_optional_path(self, project_root):
        assert resolve_optional_path(None) is None
        assert resolve_optional_path("") is None
        assert resolve_optional_path("/abs/x") == "/abs/x"
        assert resolve_optional_path("rel/x") == os.path.join(str(project_root), "rel/x")

    def test_real_repo_config_layers_cleanly(self):
        """入库的 global_config.yaml 不含任何本机绝对路径（那些都归 local_config.yaml）"""
        with open(os.path.join(utils.get_project_root(), "config", "global_config.yaml"), encoding="utf-8") as f:
            text = f.read()
        code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert not any("/srv/" in ln.split("#")[0] for ln in code_lines)


# --------------------------------------------------------------------------
# PATCH /api/config：按键落到 local / global
# --------------------------------------------------------------------------

@pytest.fixture
def api(project_root):
    from src.api.app import create_app
    from src.api import deps
    from src.runtime.task_queue import TaskQueue

    os.makedirs(project_root / "library")
    os.makedirs(project_root / "tasks")
    cfg = load_global_config()
    deps.set_config(cfg)
    deps.set_queue(TaskQueue(config=cfg, tasks_dir=str(project_root / "tasks"),
                             library_dir=str(project_root / "library")))
    return TestClient(create_app(), raise_server_exceptions=False)


class TestPatchRouting:
    GLOBAL = "# global 头注释\nmixing:\n  bitrate: \"192k\"  # 码率\nserver:\n  cpu_workers: 2\n"
    LOCAL = "# local 头注释\nserver:\n  library_root: \"library\"  # 库目录\n"

    def _seed(self, root):
        from src.api import deps
        _write(root / "config" / "global_config.yaml", self.GLOBAL)
        _write(root / "config" / "local_config.yaml", self.LOCAL)
        deps.set_config(load_global_config())  # 内存配置与刚写的文件对齐

    def test_key_defined_in_local_is_written_to_local(self, api, project_root):
        self._seed(project_root)

        resp = api.patch("/api/config", json={"server.library_root": "novels"})
        assert resp.json()["applied_keys"] == ["server.library_root"]

        assert yaml.safe_load((project_root / "config" / "local_config.yaml").read_text())["server"]["library_root"] == "novels"
        assert (project_root / "config" / "global_config.yaml").read_text() == self.GLOBAL  # global 一个字节不动

    def test_key_not_in_local_is_written_to_global(self, api, project_root):
        self._seed(project_root)
        resp = api.patch("/api/config", json={"mixing.bitrate": "256k"})
        assert resp.json()["applied_keys"] == ["mixing.bitrate"]

        assert yaml.safe_load((project_root / "config" / "global_config.yaml").read_text())["mixing"]["bitrate"] == "256k"
        assert (project_root / "config" / "local_config.yaml").read_text() == self.LOCAL

    def test_mixed_keys_split_across_both_files_and_keep_comments(self, api, project_root):
        self._seed(project_root)
        resp = api.patch("/api/config", json={"mixing.bitrate": "320k", "server.library_root": "novels"})
        assert sorted(resp.json()["applied_keys"]) == ["mixing.bitrate", "server.library_root"]

        g = (project_root / "config" / "global_config.yaml").read_text()
        l = (project_root / "config" / "local_config.yaml").read_text()
        assert "# global 头注释" in g and "# 码率" in g
        assert "# local 头注释" in l and "# 库目录" in l

    def test_patched_value_is_visible_through_get_config(self, api, project_root):
        self._seed(project_root)
        api.patch("/api/config", json={"server.library_root": "novels"})
        assert api.get("/api/config").json()["server"]["library_root"] == "novels"

    def test_patching_library_root_takes_effect_immediately(self, api, project_root):
        """回归：server.library_root 以前在白名单里、设置页能保存，却没有任何代码读它"""
        self._seed(project_root)
        assert library.library_root() == str(project_root / "library")
        api.patch("/api/config", json={"server.library_root": "novels"})
        assert library.library_root() == str(project_root / "novels")


# --------------------------------------------------------------------------
# server.library_root
# --------------------------------------------------------------------------

class TestLibraryRoot:
    def test_default_when_unconfigured(self, project_root):
        assert library.library_root() == str(project_root / "data" / "library")

    def test_reads_relative_config(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: books\n")
        assert library.library_root() == str(project_root / "books")

    def test_reads_absolute_config(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: /mnt/big/lib\n")
        assert library.library_root() == "/mnt/big/lib"

    def test_explicit_library_dir_wins(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: books\n")
        assert library.library_root("/explicit") == "/explicit"

    def test_cached_until_invalidated(self, project_root):
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: a\n")
        assert library.library_root() == str(project_root / "a")
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: b\n")
        assert library.library_root() == str(project_root / "a")  # 缓存命中，不重读盘
        library.invalidate_library_root()
        assert library.library_root() == str(project_root / "b")

    def test_task_queue_and_preflight_follow_configured_root(self, project_root):
        """回归：这两处以前自己拼 PROJECT_ROOT/library，配了 library_root 会与 library.py 不一致"""
        from src.runtime.task_queue import TaskQueue
        from src.runtime import preflight
        _write(project_root / "config" / "global_config.yaml", "server:\n  library_root: books\n")
        q = TaskQueue(config={"server": {}}, tasks_dir=str(project_root / "tasks"))
        assert q.library_dir == str(project_root / "books")

        (project_root / "books" / "n1" / "chapters" / "ch_0001").mkdir(parents=True)
        result = preflight.preflight("tts", "n1")
        assert [c["chapter_id"] for c in result["chapters"]] == ["ch_0001"]

    def test_role_refs_work_with_non_default_library_dir_name(self, project_root):
        """回归：build_role_refs 以前靠路径里字面的 "library" 段取 novel_id，库目录改名就失效"""
        from src.domain import derived_index
        ch = project_root / "books" / "nv_a" / "chapters" / "ch_0001"
        ch.mkdir(parents=True)
        (ch / "script_final.json").write_text(json.dumps([{"speaker": "su_yan", "text": "x"}]), encoding="utf-8")
        refs = derived_index.build_role_refs(str(project_root / "books"))
        assert refs["roles"]["su_yan"]["novels"] == ["nv_a"]
        assert refs["roles"]["su_yan"]["segment_count"] == 1

    def test_role_refs_not_fooled_by_library_segment_earlier_in_path(self, tmp_path):
        from src.domain import derived_index
        lib = tmp_path / "library" / "sub" / "books"  # 路径里有个更靠前的 "library"
        ch = lib / "nv_a" / "chapters" / "ch_0001"
        ch.mkdir(parents=True)
        (ch / "script_final.json").write_text(json.dumps([{"speaker": "r1", "text": "x"}]), encoding="utf-8")
        assert derived_index.build_role_refs(str(lib))["roles"]["r1"]["novels"] == ["nv_a"]


# --------------------------------------------------------------------------
# server.host / server.port
# --------------------------------------------------------------------------

class TestWebuiBind:
    def test_cli_args_beat_config(self):
        from src.cli import resolve_webui_bind
        cfg = {"server": {"host": "10.0.0.1", "port": 9000}}
        assert resolve_webui_bind("0.0.0.0", 8123, cfg) == ("0.0.0.0", 8123)

    def test_config_used_when_no_args(self):
        from src.cli import resolve_webui_bind
        assert resolve_webui_bind(None, None, {"server": {"host": "10.0.0.1", "port": 9000}}) == ("10.0.0.1", 9000)

    def test_partial_override(self):
        from src.cli import resolve_webui_bind
        assert resolve_webui_bind(None, 8123, {"server": {"host": "10.0.0.1", "port": 9000}}) == ("10.0.0.1", 8123)

    @pytest.mark.parametrize("cfg", [{}, {"server": {}}, {"server": None}, None])
    def test_defaults_to_all_interfaces_when_unconfigured(self, cfg):
        from src.cli import resolve_webui_bind
        assert resolve_webui_bind(None, None, cfg) == ("0.0.0.0", 7860)


# --------------------------------------------------------------------------
# 路径类配置缺失：不回落到某台机器的字面量
# --------------------------------------------------------------------------

class TestNoMachineSpecificFallback:
    def test_index_tts_backend_unavailable_without_config(self):
        from src.pipeline.tts_engine import IndexTTSBackend
        b = IndexTTSBackend({})
        assert b.python_bin is None and b.checkpoints_dir is None
        assert b.is_available() is False

    def test_index_tts_backend_reads_configured_paths(self, tmp_path):
        from src.pipeline.tts_engine import IndexTTSBackend
        b = IndexTTSBackend({"tts": {"index_tts": {"python_bin": "/x/python", "checkpoints_dir": "/x/ckpt"}}})
        assert b.python_bin == "/x/python" and b.checkpoints_dir == "/x/ckpt"
        assert b.is_available() is False  # 路径不存在

    def test_daemon_unavailable_without_config(self):
        from src.pipeline.tts_daemon import IndexTTSDaemon
        d = IndexTTSDaemon({})
        assert d.python_bin is None and d.checkpoints_dir is None
        assert d.is_available() is False

    def test_embedding_precondition_reports_not_ready_without_config(self):
        from src.domain import roles
        err = roles.embedding_precondition_error("r1", {"roles": {"r1": {}}}, config={})
        assert err and "未就绪" in err

    def test_no_srv_literals_left_in_src(self):
        root = utils.get_project_root()
        offenders = []
        for dirpath, _, files in os.walk(os.path.join(root, "src")):
            for fn in files:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    if "/srv/" in open(p, encoding="utf-8").read():
                        offenders.append(os.path.relpath(p, root))
        assert offenders == []


# --------------------------------------------------------------------------
# llama-server 拉起命令 / espeak 路径
# --------------------------------------------------------------------------

class TestExternalTools:
    def test_serve_command_default_and_configured(self):
        from src.runtime import gpu_arbiter
        assert gpu_arbiter.serve_command_from_config({}) == "ai"
        assert gpu_arbiter.serve_command_from_config({"tools": None}) == "ai"
        assert gpu_arbiter.serve_command_from_config({"tools": {"llm_serve_command": "myai"}}) == "myai"

    def test_start_llama_server_uses_given_command(self, monkeypatch):
        from src.runtime import gpu_arbiter
        calls = []
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: bool(calls))
        monkeypatch.setattr(gpu_arbiter.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
        monkeypatch.setattr(gpu_arbiter.time, "sleep", lambda *_: None)
        assert gpu_arbiter.start_llama_server("m", 8080, "http://x/v1", 5.0, serve_command="myai") is True
        assert calls == [["myai", "llm", "serve", "m", "--port", "8080"]]

    def test_start_llama_server_defaults_to_ai(self, monkeypatch):
        from src.runtime import gpu_arbiter
        calls = []
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: bool(calls))
        monkeypatch.setattr(gpu_arbiter.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
        monkeypatch.setattr(gpu_arbiter.time, "sleep", lambda *_: None)
        gpu_arbiter.start_llama_server("m", 8080, "http://x/v1", 5.0)
        assert calls[0][0] == "ai"

    def test_llm_suspended_passes_configured_command(self):
        from src.runtime import gpu_arbiter
        s = gpu_arbiter.LlmSuspendedForGpu({"llm": {}, "tools": {"llm_serve_command": "myai"}})
        assert s.serve_command == "myai"

    @pytest.fixture
    def seed_script(self):
        path = os.path.join(utils.get_project_root(), "src", "tools", "generate_seed_reference.py")
        spec = importlib.util.spec_from_file_location("generate_seed_reference_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_espeak_paths_from_config(self, seed_script, project_root):
        cfg = {"tools": {"espeak_bin": "/e/bin/espeak-ng", "espeak_data": "/e/data", "espeak_lib_dir": "lib"}}
        assert seed_script.load_espeak_paths(cfg) == ("/e/bin/espeak-ng", "/e/data", str(project_root / "lib"))

    def test_espeak_missing_keys_named_in_error(self, seed_script):
        with pytest.raises(RuntimeError) as e:
            seed_script.load_espeak_paths({"tools": {"espeak_bin": "/e/bin"}})
        msg = str(e.value)
        assert "tools.espeak_data" in msg and "tools.espeak_lib_dir" in msg
        assert "tools.espeak_bin" not in msg
        assert "local_config" in msg

    def test_espeak_no_tools_section(self, seed_script):
        with pytest.raises(RuntimeError):
            seed_script.load_espeak_paths({})
