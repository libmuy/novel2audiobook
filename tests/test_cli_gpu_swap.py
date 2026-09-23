"""
测试 cli.py 里为计划 003（显式 GPU 换手确认）新增的辅助函数：
_prompt_yes_no / _release_tts_daemon_if_running / _confirm_batch_llm_swap。

全程 monkeypatch 掉 sys.stdin.isatty、input()、src.tools.gpu_arbiter 与
src.tts_daemon.IndexTTSDaemon 的方法——不读真实终端输入，不碰真实
llama-server/常驻 TTS 服务/GPU，也不对 chapters/ 等共享目录做任何操作。
"""
import pytest
from src import cli
from src.tools import gpu_arbiter
from src.tts_daemon import IndexTTSDaemon


@pytest.fixture(autouse=True)
def _isolate_swap_history(tmp_path, monkeypatch):
    """避免 _confirm_batch_llm_swap 读到真实机器上的换手耗时历史"""
    monkeypatch.setattr(gpu_arbiter, "SWAP_HISTORY_PATH", str(tmp_path / "swap_history.json"))


class TestPromptYesNo:
    @pytest.mark.parametrize("raw_input,expected", [
        ("y", True), ("Y", True), ("yes", True), ("YES", True),
        ("n", False), ("", False), ("nope", False),
    ])
    def test_various_inputs(self, monkeypatch, raw_input, expected):
        monkeypatch.setattr("builtins.input", lambda prompt: raw_input)
        assert cli._prompt_yes_no("确认？") is expected

    def test_eof_is_treated_as_no(self, monkeypatch):
        def _raise(prompt):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _raise)
        assert cli._prompt_yes_no("确认？") is False


class TestReleaseTtsDaemonIfRunning:
    def test_daemon_not_running_returns_true_without_prompting_or_shutting_down(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        prompted = []
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: prompted.append(q) or True)
        shutdown_calls = []
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: shutdown_calls.append(1) or {"ok": True})

        assert cli._release_tts_daemon_if_running(auto_yes=False) is True
        assert prompted == []
        assert shutdown_calls == []

    def test_non_interactive_shuts_down_without_prompting(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        prompted = []
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: prompted.append(q) or False)
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: {"ok": True})

        assert cli._release_tts_daemon_if_running(auto_yes=False) is True
        assert prompted == []  # 非交互式不应该弹确认

    def test_auto_yes_skips_prompt_even_when_interactive(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        prompted = []
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: prompted.append(q) or False)
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: {"ok": True})

        assert cli._release_tts_daemon_if_running(auto_yes=True) is True
        assert prompted == []

    def test_interactive_user_declines(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: False)
        shutdown_calls = []
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: shutdown_calls.append(1) or {"ok": True})

        assert cli._release_tts_daemon_if_running(auto_yes=False) is False
        assert shutdown_calls == []  # 用户拒绝了，不应该真的去关常驻服务

    def test_interactive_user_confirms(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: True)
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: {"ok": True})

        assert cli._release_tts_daemon_if_running(auto_yes=False) is True

    def test_shutdown_failure_propagates_as_false(self, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(IndexTTSDaemon, "shutdown", lambda self: {"ok": False, "error": "kaboom"})

        assert cli._release_tts_daemon_if_running(auto_yes=True) is False


class TestConfirmBatchLlmSwap:
    def test_auto_yes_always_true_without_checking_owner(self, monkeypatch):
        owner_calls = []
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: owner_calls.append(1) or gpu_arbiter.OWNER_LLM)
        assert cli._confirm_batch_llm_swap(auto_yes=True) is True
        assert owner_calls == []

    def test_non_interactive_always_true(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        owner_calls = []
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: owner_calls.append(1) or gpu_arbiter.OWNER_LLM)
        assert cli._confirm_batch_llm_swap(auto_yes=False) is True
        assert owner_calls == []

    def test_interactive_but_llm_not_current_owner_skips_prompt(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: None)
        prompted = []
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: prompted.append(q) or False)

        assert cli._confirm_batch_llm_swap(auto_yes=False) is True
        assert prompted == []  # llama-server 本来就没在跑，不会有换手代价，不用问

    def test_interactive_and_llm_running_prompts_user(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_LLM)
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: True)

        assert cli._confirm_batch_llm_swap(auto_yes=False) is True

    def test_user_declines_returns_false(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda: gpu_arbiter.OWNER_LLM)
        monkeypatch.setattr(cli, "_prompt_yes_no", lambda q: False)

        assert cli._confirm_batch_llm_swap(auto_yes=False) is False
