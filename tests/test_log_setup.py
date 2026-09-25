"""
测试 src/runtime/log_setup.py：文件落盘、print tee、幂等/还原、轮转、归因过滤器
"""
import logging
import os
import sys

import pytest

from src.runtime import log_setup
from src.runtime.log_setup import (
    setup_logging,
    reset_logging,
    request_id_var,
    task_id_var,
)


@pytest.fixture
def log_cfg(tmp_path):
    """装配一个落到 tmp 的日志配置，测试结束必还原（含系统流与 root level）"""
    yield {
        "logging": {
            "level": "INFO",
            "file": str(tmp_path / "n2a.log"),
            "max_bytes": 100000,
            "backup_count": 3,
        }
    }
    reset_logging()
    request_id_var.set("")
    task_id_var.set("")


class TestSetup:
    def test_print_and_logging_land_in_file_exactly_once(self, log_cfg):
        summary = setup_logging(log_cfg)
        assert summary["file"] == log_cfg["logging"]["file"]

        print("print可捕获-abc123")
        logging.getLogger("t.demo").info("logging唯一一份-xyz789")

        with open(log_cfg["logging"]["file"], encoding="utf-8") as f:
            content = f.read()
        assert "print可捕获-abc123" in content
        # logging 记录经 _SinkHandler 直写 sink，不经过 tee → 恰好一份
        assert content.count("logging唯一一份-xyz789") == 1
        # 格式：时间 + 级别 + [logger名]
        assert "INFO [t.demo]" in content

    def test_logging_reaches_original_terminal_stream(self, log_cfg):
        orig_stderr = sys.stderr
        setup_logging(log_cfg)
        logging.getLogger("t.term").info("到终端的一条")
        # 终端 handler 绑定 tee 安装前的原始流（测试环境下即 pytest 的 capture）
        term_handlers = [h for h in logging.getLogger().handlers
                         if isinstance(h, logging.StreamHandler) and h.stream is orig_stderr]
        assert term_handlers, "终端 StreamHandler 应绑定安装前的原始 stderr"

    def test_file_null_keeps_system_streams(self, tmp_path):
        reset_logging()
        orig_out, orig_err = sys.stdout, sys.stderr
        try:
            summary = setup_logging({"logging": {"level": "INFO", "file": None}})
            assert summary["file"] is None
            assert sys.stdout is orig_out
            assert sys.stderr is orig_err
            assert not (tmp_path / "n2a.log").exists()
        finally:
            reset_logging()

    def test_invalid_level_falls_back_to_info(self, log_cfg):
        bad = {"logging": {**log_cfg["logging"], "level": "BOGUS"}}
        summary = setup_logging(bad)
        assert summary["level"] == "INFO"
        assert logging.getLogger().level == logging.INFO

    def test_idempotent_reinstall_no_duplicate_handlers(self, log_cfg):
        setup_logging(log_cfg)
        setup_logging(log_cfg)
        root = logging.getLogger()
        sink_handlers = [h for h in root.handlers if isinstance(h, log_setup._SinkHandler)]
        assert len(sink_handlers) == 1

    def test_reset_restores_streams_and_root_level(self, log_cfg):
        before_out, before_err = sys.stdout, sys.stderr
        before_level = logging.getLogger().level
        setup_logging(log_cfg)
        assert isinstance(sys.stdout, log_setup._TeeStream)
        reset_logging()
        assert sys.stdout is before_out
        assert sys.stderr is before_err
        assert logging.getLogger().level == before_level
        assert not [h for h in logging.getLogger().handlers
                    if isinstance(h, log_setup._SinkHandler)]

    def test_unavailable_file_falls_back_to_terminal(self, tmp_path):
        reset_logging()
        blocker = tmp_path / "blocked"
        blocker.write_text("我是文件不是目录")
        try:
            summary = setup_logging({"logging": {"file": str(blocker / "sub" / "n2a.log")}})
            assert summary["file"] is None
            assert not isinstance(sys.stdout, log_setup._TeeStream)
        finally:
            reset_logging()


class TestRotation:
    def test_rollover_creates_backup(self, tmp_path):
        reset_logging()
        log_file = str(tmp_path / "n2a.log")
        try:
            setup_logging({"logging": {"level": "INFO", "file": log_file,
                                       "max_bytes": 150, "backup_count": 2}})
            print("A" * 100)
            print("B" * 100)
            assert os.path.exists(log_file + ".1")
            with open(log_file + ".1", encoding="utf-8") as f:
                assert "A" * 100 in f.read()
            with open(log_file, encoding="utf-8") as f:
                assert "B" * 100 in f.read()
            # backup_count=2：最多 .1/.2，.3 不应出现
            assert not os.path.exists(log_file + ".3")
        finally:
            reset_logging()


class TestContextAttribution:
    def test_request_and_task_ids_injected(self, log_cfg):
        setup_logging(log_cfg)
        r_tok = request_id_var.set("a1b2c3d4")
        t_tok = task_id_var.set("tsk_demo_001")
        try:
            logging.getLogger("t.attr").info("带归因的一条")
        finally:
            request_id_var.reset(r_tok)
            task_id_var.reset(t_tok)
        with open(log_cfg["logging"]["file"], encoding="utf-8") as f:
            content = f.read()
        assert "[a1b2c3d4]" in content
        assert "[tsk_demo_001]" in content

    def test_no_context_no_placeholder(self, log_cfg):
        setup_logging(log_cfg)
        logging.getLogger("t.noattr").info("无归因的一条")
        with open(log_cfg["logging"]["file"], encoding="utf-8") as f:
            for line in f:
                if "无归因的一条" in line:
                    assert "[t.noattr] 无归因的一条" in line.replace("  ", " ")
                    # logger 名与消息之间没有多余 id 占位
                    import re
                    assert not re.search(r"\[t\.noattr\] \[\w+\]", line)
                    break
            else:
                pytest.fail("未找到目标日志行")


class TestTeeDelegation:
    def test_tee_delegates_tty_and_fileno(self, log_cfg):
        orig_stdout = sys.stdout
        setup_logging(log_cfg)
        tee = sys.stdout
        assert isinstance(tee, log_setup._TeeStream)
        assert tee.isatty() == orig_stdout.isatty()
        try:
            orig_fileno = orig_stdout.fileno()
        except Exception:
            orig_fileno = None
        if orig_fileno is not None:
            assert tee.fileno() == orig_fileno
        assert tee.encoding == getattr(orig_stdout, "encoding", None)
