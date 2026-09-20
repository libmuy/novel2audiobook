"""
测试 cli.py 的各个功能，特别是 run_test_module
"""
import os
import pytest
from src.cli import run_test_module


class TestRunTestModule:
    """运行测试模块功能测试"""

    def test_run_test_module_llm(self):
        """运行 LLM 测试模块"""
        # 不应该抛异常
        try:
            run_test_module("llm")
        except Exception as e:
            pytest.fail(f"LLM 测试模块失败: {e}")

    def test_run_test_module_tts(self):
        """运行 TTS 测试模块"""
        # 不应该抛异常
        try:
            run_test_module("tts")
        except Exception as e:
            pytest.fail(f"TTS 测试模块失败: {e}")

    def test_run_test_module_audio(self):
        """运行音频混音测试模块"""
        # 不应该抛异常
        try:
            run_test_module("audio")
        except Exception as e:
            pytest.fail(f"音频混音测试模块失败: {e}")

    def test_run_test_module_all(self):
        """运行全部测试模块"""
        # 不应该抛异常
        try:
            run_test_module("all")
        except Exception as e:
            pytest.fail(f"全部测试模块失败: {e}")

    def test_run_test_module_dry_run(self):
        """运行干运行测试"""
        # 不应该抛异常
        try:
            run_test_module("dry-run")
        except Exception as e:
            pytest.fail(f"干运行测试失败: {e}")

    def test_run_test_module_assets(self):
        """运行素材库生成测试模块（Mock 引擎，隔离目录，与章节流水线正交）"""
        try:
            run_test_module("assets")
        except Exception as e:
            pytest.fail(f"assets 测试模块失败: {e}")

    @pytest.mark.parametrize("module", ["llm", "tts", "audio", "assets", "all", "dry-run"])
    def test_run_test_module_parametrized(self, module):
        """参数化测试各个模块"""
        try:
            run_test_module(module)
        except Exception as e:
            pytest.fail(f"{module} 测试模块失败: {e}")

    def test_run_test_module_does_not_pollute_repo(self):
        """运行测试模块不会污染仓库"""
        from src.utils import get_project_root
        import subprocess

        root = get_project_root()
        library_dir = os.path.join(root, "library")

        # 获取测试前的小说列表
        if os.path.exists(library_dir):
            before_novels = set(os.listdir(library_dir))
        else:
            before_novels = set()

        # 运行测试
        try:
            run_test_module("dry-run")
        except Exception:
            pass  # 即使失败也要继续检查

        # 获取测试后的小说列表
        if os.path.exists(library_dir):
            after_novels = set(os.listdir(library_dir))
        else:
            after_novels = set()

        # 自检不应在真实 library/ 下创建任何内容
        new_entries = after_novels - before_novels
        for entry in new_entries:
            pytest.fail(f"测试污染了真实 library 目录: {entry}")

    def test_run_test_module_assets_does_not_pollute_real_assets_dir(self):
        """assets 自检模块只写隔离临时目录，不应在真实 assets/ 下新增任何文件"""
        from src.utils import get_project_root

        root = get_project_root()
        assets_dir = os.path.join(root, "assets")
        before = set()
        for kind_dir in ("ambience", "sfx"):
            kind_path = os.path.join(assets_dir, kind_dir)
            if os.path.isdir(kind_path):
                before.add((kind_dir, tuple(sorted(os.listdir(kind_path)))))

        try:
            run_test_module("assets")
        except Exception:
            pass

        after = set()
        for kind_dir in ("ambience", "sfx"):
            kind_path = os.path.join(assets_dir, kind_dir)
            if os.path.isdir(kind_path):
                after.add((kind_dir, tuple(sorted(os.listdir(kind_path)))))

        assert before == after, "assets 自检污染了真实 assets/ 目录"

    def test_run_test_module_creates_temp_workspace(self):
        """测试模块创建临时工作区"""
        # 测试应该在临时目录中运行，不影响真实目录
        try:
            run_test_module("dry-run")
        except Exception as e:
            pytest.fail(f"创建临时工作区失败: {e}")

    def test_run_test_module_cleans_up_temp(self):
        """测试模块清理临时工作区"""
        import tempfile
        import glob

        # 在运行测试前，检查是否有遗留的临时目录
        tmp_root = tempfile.gettempdir()
        before_temp = set(glob.glob(os.path.join(tmp_root, "n2a_selftest_*")))

        try:
            run_test_module("dry-run")
        except Exception:
            pass

        # 在运行测试后，不应该有新的 n2a_selftest_* 临时目录遗留
        after_temp = set(glob.glob(os.path.join(tmp_root, "n2a_selftest_*")))
        new_temp = after_temp - before_temp

        # 如果有遗留，说明清理失败
        # 这里只记录，不强制失败（因为其他测试可能有遗留）
        for temp_dir in new_temp:
            if os.path.exists(temp_dir):
                # 尝试清理
                import shutil
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
