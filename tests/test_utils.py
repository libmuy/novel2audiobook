"""
测试 src/utils.py 模块的各个函数
"""
import os
import json
import tempfile
import pytest
from src import utils


class TestCalculateMd5:
    """MD5 哈希计算功能测试"""

    def test_calculate_md5_basic(self):
        """基本 MD5 哈希计算"""
        result = utils.calculate_md5("test")
        assert isinstance(result, str)
        assert len(result) == 32  # MD5 哈希长度为 32 字符

    def test_calculate_md5_deterministic(self):
        """MD5 哈希的确定性"""
        result1 = utils.calculate_md5("same_text")
        result2 = utils.calculate_md5("same_text")
        assert result1 == result2

    def test_calculate_md5_different_inputs(self):
        """不同输入产生不同哈希"""
        result1 = utils.calculate_md5("text1")
        result2 = utils.calculate_md5("text2")
        assert result1 != result2

    def test_calculate_md5_empty_string(self):
        """空字符串的 MD5 哈希"""
        result = utils.calculate_md5("")
        assert result == "d41d8cd98f00b204e9800998ecf8427e"  # 已知空字符串 MD5


class TestGetProjectRoot:
    """项目根路径函数测试"""

    def test_get_project_root_returns_string(self):
        """返回字符串"""
        root = utils.get_project_root()
        assert isinstance(root, str)

    def test_get_project_root_is_absolute(self):
        """返回绝对路径"""
        root = utils.get_project_root()
        assert os.path.isabs(root)

    def test_get_project_root_exists(self):
        """返回的路径存在"""
        root = utils.get_project_root()
        assert os.path.isdir(root)

    def test_get_project_root_contains_src(self):
        """项目根路径包含 src/ 目录"""
        root = utils.get_project_root()
        assert os.path.isdir(os.path.join(root, "src"))


class TestResolvePath:
    """路径解析函数测试"""

    def test_resolve_path_relative(self):
        """相对路径的解析"""
        result = utils.resolve_path("src")
        assert os.path.isabs(result)
        assert result.endswith("src")

    def test_resolve_path_absolute(self):
        """绝对路径的原样返回"""
        abs_path = "/tmp/test"
        result = utils.resolve_path(abs_path)
        assert result == abs_path

    def test_resolve_path_with_subdirs(self):
        """包含子目录的相对路径"""
        result = utils.resolve_path("assets/sfx")
        assert os.path.isabs(result)
        assert result.endswith(os.path.join("assets", "sfx"))


class TestNormalizeChapterId:
    """章节 ID 规范化函数测试"""

    def test_normalize_numeric_input(self):
        """数字输入的规范化"""
        result = utils.normalize_chapter_id("1")
        assert result == "ch_0001"

    def test_normalize_zero_padded_input(self):
        """零填充的数字输入"""
        result = utils.normalize_chapter_id("0123")
        assert result == "ch_0123"

    def test_normalize_already_normalized(self):
        """已规范化的输入"""
        result = utils.normalize_chapter_id("ch_0001")
        assert result == "ch_0001"

    def test_normalize_with_whitespace(self):
        """带空白的输入"""
        result = utils.normalize_chapter_id("  0001  ")
        assert result == "ch_0001"


class TestGetChapterDir:
    """章节目录路径函数测试"""

    def test_get_chapter_dir_default_base(self):
        """使用默认 base_dir"""
        result = utils.get_chapter_dir("0001")
        assert result == os.path.join("chapters", "ch_0001")

    def test_get_chapter_dir_custom_base(self):
        """使用自定义 base_dir"""
        result = utils.get_chapter_dir("0001", base_dir="/tmp/test_chapters")
        assert result == os.path.join("/tmp/test_chapters", "ch_0001")

    def test_get_chapter_dir_normalizes_id(self):
        """自动规范化章节 ID"""
        result = utils.get_chapter_dir("ch_0001")
        assert "ch_0001" in result


class TestUpdateChapterStatus:
    """章节状态更新函数测试"""

    def test_update_chapter_status_creates_file(self, tmp_chapter_dir):
        """创建 .status.json 文件"""
        utils.update_chapter_status(tmp_chapter_dir, "parsed_draft")
        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        assert os.path.exists(status_file)

    def test_update_chapter_status_format(self, tmp_chapter_dir):
        """生成正确格式的 JSON"""
        utils.update_chapter_status(tmp_chapter_dir, "tts_completed")
        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "chapter_id" in data
        assert "status" in data
        assert "updated_at" in data
        assert data["status"] == "tts_completed"

    def test_update_chapter_status_multiple_times(self, tmp_chapter_dir):
        """多次更新会覆盖之前的状态"""
        utils.update_chapter_status(tmp_chapter_dir, "parsed_draft")
        utils.update_chapter_status(tmp_chapter_dir, "tts_completed")
        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["status"] == "tts_completed"


class TestListAvailableAssets:
    """可用素材列表函数测试"""

    def test_list_available_assets_returns_dict(self):
        """返回正确的数据结构"""
        result = utils.list_available_assets()
        assert isinstance(result, dict)
        assert "sfx" in result
        assert "bgm" in result
        assert isinstance(result["sfx"], list)
        assert isinstance(result["bgm"], list)

    def test_list_available_assets_finds_sfx(self):
        """扫描 assets/sfx 目录"""
        result = utils.list_available_assets()
        # 项目内应该有 sword_clash 素材
        assert "sword_clash" in result["sfx"]

    def test_list_available_assets_finds_bgm(self):
        """扫描 assets/ambience 目录"""
        result = utils.list_available_assets()
        # 项目内应该有 rain_heavy 素材
        assert "rain_heavy" in result["bgm"]

    def test_list_available_assets_no_duplicates(self):
        """返回的列表中没有重复"""
        result = utils.list_available_assets()
        assert len(result["sfx"]) == len(set(result["sfx"]))
        assert len(result["bgm"]) == len(set(result["bgm"]))

    def test_list_available_assets_sorted(self):
        """返回的列表已排序"""
        result = utils.list_available_assets()
        assert result["sfx"] == sorted(result["sfx"])
        assert result["bgm"] == sorted(result["bgm"])


class TestLoadGlobalConfig:
    """全局配置加载函数测试"""

    def test_load_global_config_default_path(self):
        """使用默认路径加载配置"""
        result = utils.load_global_config()
        assert isinstance(result, dict)

    def test_load_global_config_nonexistent_path(self):
        """不存在的配置文件返回空字典"""
        result = utils.load_global_config("/nonexistent/path/config.yaml")
        assert result == {}

    def test_load_global_config_custom_path(self, tmp_project_dir):
        """自定义配置文件路径"""
        config_file = os.path.join(tmp_project_dir, "test_config.yaml")
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("test_key: test_value\n")
        result = utils.load_global_config(config_file)
        assert result.get("test_key") == "test_value"


class TestUpdateChapterStatusLogging:
    """状态流转日志是“这章为什么是这个状态”的单一事实线，且只在变化时记录"""

    def test_logs_transition_only_on_change(self, tmp_chapter_dir, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="src.utils"):
            utils.update_chapter_status(tmp_chapter_dir, "parsed_draft")   # (无) → parsed_draft
            utils.update_chapter_status(tmp_chapter_dir, "parsed_draft")   # 相同：不记
            utils.update_chapter_status(tmp_chapter_dir, "tts_completed")  # 变化

        lines = [r.getMessage() for r in caplog.records if "章节状态流转" in r.getMessage()]
        assert len(lines) == 2, "只有状态真正变化时才记（重复写同值不刷屏）"
        assert "(无) → parsed_draft" in lines[0]
        assert "parsed_draft → tts_completed" in lines[1]
        assert f"chapter={os.path.basename(tmp_chapter_dir)}" in lines[0]
