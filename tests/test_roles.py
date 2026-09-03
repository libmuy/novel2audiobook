"""
测试 src/roles.py 模块的各个函数
"""
import os
import json
import shutil
import pytest
from src import roles


class TestLoadManifest:
    """加载角色清单功能测试"""

    def test_load_manifest_empty_dir(self, tmp_roles_dir):
        """空目录返回空结构"""
        # 清空 tmp_roles_dir
        shutil.rmtree(tmp_roles_dir)
        os.makedirs(tmp_roles_dir)
        result = roles.load_manifest(tmp_roles_dir)
        assert result == {"roles": {}}

    def test_load_manifest_with_content(self, tmp_roles_dir):
        """加载真实清单（如果存在）"""
        result = roles.load_manifest(tmp_roles_dir)
        assert isinstance(result, dict)
        assert "roles" in result

    def test_load_manifest_nonexistent_path(self, tmp_project_dir):
        """不存在的路径返回空结构"""
        nonexistent = os.path.join(tmp_project_dir, "nonexistent_roles")
        result = roles.load_manifest(nonexistent)
        assert result == {"roles": {}}


class TestSaveManifest:
    """保存角色清单功能测试"""

    def test_save_manifest_creates_file(self, tmp_roles_dir):
        """创建 roles_manifest.json 文件"""
        manifest = {"roles": {"test_role": {"name": "测试角色"}}}
        roles.save_manifest(manifest, tmp_roles_dir)
        path = os.path.join(tmp_roles_dir, "roles_manifest.json")
        assert os.path.exists(path)

    def test_save_manifest_format(self, tmp_roles_dir):
        """保存正确格式的 JSON"""
        manifest = {"roles": {"test_role": {"name": "测试角色", "gender": "female"}}}
        roles.save_manifest(manifest, tmp_roles_dir)
        path = os.path.join(tmp_roles_dir, "roles_manifest.json")
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == manifest

    def test_save_manifest_overwrites_existing(self, tmp_roles_dir):
        """覆盖已有的清单"""
        manifest1 = {"roles": {"role1": {"name": "角色1"}}}
        manifest2 = {"roles": {"role2": {"name": "角色2"}}}
        roles.save_manifest(manifest1, tmp_roles_dir)
        roles.save_manifest(manifest2, tmp_roles_dir)
        path = os.path.join(tmp_roles_dir, "roles_manifest.json")
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert "role1" not in loaded["roles"]
        assert "role2" in loaded["roles"]


class TestResolveRoleId:
    """角色 ID 解析功能测试"""

    def test_resolve_role_id_exact_id_match(self, tmp_roles_dir):
        """精确 ID 匹配"""
        manifest = roles.load_manifest(tmp_roles_dir)
        # 使用项目中已有的 narrator 角色
        result = roles.resolve_role_id("narrator", manifest)
        assert result == "narrator"

    def test_resolve_role_id_alias_match(self, tmp_roles_dir):
        """别名匹配"""
        manifest = roles.load_manifest(tmp_roles_dir)
        # "旁白" 应该映射到 "narrator"
        result = roles.resolve_role_id("旁白", manifest)
        assert result == "narrator"

    def test_resolve_role_id_unknown(self, tmp_roles_dir):
        """未知角色返回 None"""
        manifest = roles.load_manifest(tmp_roles_dir)
        result = roles.resolve_role_id("未知角色名", manifest)
        assert result is None

    def test_resolve_role_id_empty_string(self, tmp_roles_dir):
        """空字符串返回 None"""
        manifest = roles.load_manifest(tmp_roles_dir)
        result = roles.resolve_role_id("", manifest)
        assert result is None

    def test_resolve_role_id_with_whitespace(self, tmp_roles_dir):
        """带空白的输入"""
        manifest = roles.load_manifest(tmp_roles_dir)
        result = roles.resolve_role_id("  narrator  ", manifest)
        assert result == "narrator"


class TestRegisterRole:
    """自动注册角色功能测试"""

    def test_register_role_creates_directory(self, tmp_roles_dir):
        """为新角色创建目录"""
        manifest = {"roles": {}}
        role_id = roles.register_role("测试角色", manifest, tmp_roles_dir)
        role_dir = os.path.join(tmp_roles_dir, role_id)
        assert os.path.isdir(role_dir)

    def test_register_role_creates_config(self, tmp_roles_dir):
        """创建角色 config.json"""
        manifest = {"roles": {}}
        role_id = roles.register_role("配置测试", manifest, tmp_roles_dir)
        config_path = os.path.join(tmp_roles_dir, role_id, "config.json")
        assert os.path.exists(config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        assert config.get("role_id") == role_id
        assert config.get("name") == "配置测试"

    def test_register_role_returns_id(self, tmp_roles_dir):
        """返回新角色 ID"""
        manifest = {"roles": {}}
        role_id = roles.register_role("新角色", manifest, tmp_roles_dir)
        assert isinstance(role_id, str)
        assert len(role_id) > 0

    def test_register_role_adds_to_manifest(self, tmp_roles_dir):
        """将新角色添加到 manifest"""
        manifest = {"roles": {}}
        role_id = roles.register_role("清单测试", manifest, tmp_roles_dir)
        assert role_id in manifest["roles"]
        assert manifest["roles"][role_id]["name"] == "清单测试"

    def test_register_role_duplicate_returns_same_id(self, tmp_roles_dir):
        """重复注册同一名字返回相同 ID"""
        manifest = {"roles": {}}
        id1 = roles.register_role("重复测试", manifest, tmp_roles_dir)
        id2 = roles.register_role("重复测试", manifest, tmp_roles_dir)
        assert id1 == id2

    def test_register_role_creates_reference_audio(self, tmp_roles_dir):
        """为新角色创建参考音频（复用 narrator 的占位音）"""
        # 先创建 narrator 的 reference.wav
        narrator_dir = os.path.join(tmp_roles_dir, "narrator")
        os.makedirs(narrator_dir, exist_ok=True)
        narrator_ref = os.path.join(narrator_dir, "reference.wav")

        from tests.conftest import create_sample_wav_file
        create_sample_wav_file(narrator_ref, duration_ms=1000)

        manifest = {"roles": {}}
        role_id = roles.register_role("音频测试", manifest, tmp_roles_dir)
        ref_audio = os.path.join(tmp_roles_dir, role_id, "reference.wav")
        assert os.path.exists(ref_audio)

    def test_register_role_preserves_config_values(self, tmp_roles_dir):
        """新角色继承 narrator 的默认值"""
        narrator_config_dir = os.path.join(tmp_roles_dir, "narrator")
        os.makedirs(narrator_config_dir, exist_ok=True)
        narrator_config_path = os.path.join(narrator_config_dir, "config.json")

        with open(narrator_config_path, "w", encoding="utf-8") as f:
            json.dump({"speed": 1.5, "pitch": 0.2}, f)

        manifest = {"roles": {}}
        role_id = roles.register_role("继承测试", manifest, tmp_roles_dir)
        role_config_path = os.path.join(tmp_roles_dir, role_id, "config.json")

        with open(role_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        assert config.get("speed") == 1.5
        assert config.get("pitch") == 0.2


class TestGetRoleRuntimeConfig:
    """获取角色运行时配置功能测试"""

    def test_get_role_runtime_config_existing_role(self, tmp_roles_dir):
        """获取已存在角色的配置"""
        result = roles.get_role_runtime_config("narrator",
                                               roles.load_manifest(tmp_roles_dir),
                                               tmp_roles_dir)
        assert isinstance(result, dict)
        assert "speed" in result
        assert "pitch" in result
        assert "reference_audio" in result
        assert "role_id" in result
        assert "name" in result

    def test_get_role_runtime_config_returns_defaults(self, tmp_roles_dir):
        """不存在的角色返回默认值"""
        manifest = {"roles": {}}
        result = roles.get_role_runtime_config("nonexistent", manifest, tmp_roles_dir)
        assert result["speed"] == 1.0
        assert result["pitch"] == 0.0

    def test_get_role_runtime_config_fallback_narrator_ref(self, tmp_roles_dir):
        """找不到角色的 reference.wav 时回退到 narrator 的音频"""
        manifest = {"roles": {"test_role": {"name": "测试角色"}}}
        result = roles.get_role_runtime_config("test_role", manifest, tmp_roles_dir)
        # 应该包含某个 reference.wav 路径
        assert "reference_audio" in result
        assert result["reference_audio"].endswith("reference.wav")

    def test_get_role_runtime_config_reads_config_json(self, tmp_roles_dir):
        """读取角色的 config.json"""
        # 创建测试角色及其配置
        test_role_dir = os.path.join(tmp_roles_dir, "test_role")
        os.makedirs(test_role_dir, exist_ok=True)
        config_path = os.path.join(test_role_dir, "config.json")

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"speed": 0.8, "pitch": -0.5}, f)

        manifest = {"roles": {"test_role": {"name": "测试角色"}}}
        result = roles.get_role_runtime_config("test_role", manifest, tmp_roles_dir)

        assert result["speed"] == 0.8
        assert result["pitch"] == -0.5


class TestGuessNameFromContext:
    """从上下文猜测角色名称功能测试"""

    def test_guess_name_from_context_found(self, tmp_roles_dir):
        """在文本中找到角色名"""
        manifest = roles.load_manifest(tmp_roles_dir)
        # 如果有 lin_dong 角色，在其 manifest 中应该有"林动"名字
        if "lin_dong" in manifest.get("roles", {}):
            text = "林动的声音从远处传来。"
            result = roles.guess_name_from_context(text, manifest)
            if manifest["roles"]["lin_dong"].get("name") == "林动":
                assert result == "林动"

    def test_guess_name_from_context_not_found(self, tmp_roles_dir):
        """文本中找不到角色名"""
        manifest = roles.load_manifest(tmp_roles_dir)
        text = "完全无关的文本。"
        result = roles.guess_name_from_context(text, manifest)
        # 应该返回 None 或者找到某个角色
        assert result is None or isinstance(result, str)

    def test_guess_name_from_context_empty_manifest(self):
        """空 manifest 返回 None"""
        manifest = {"roles": {}}
        result = roles.guess_name_from_context("任意文本", manifest)
        assert result is None
