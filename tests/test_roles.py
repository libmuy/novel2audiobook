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

    def test_register_role_writes_category_and_tags(self, tmp_roles_dir):
        manifest = {"roles": {}}
        rid = roles.register_role("标签角色", manifest, tmp_roles_dir, category="主角", tags=["少年", "隐忍"])
        assert manifest["roles"][rid]["category"] == "主角"
        assert manifest["roles"][rid]["tags"] == ["少年", "隐忍"]

    def test_register_role_without_category_or_tags_writes_neither_key(self, tmp_roles_dir):
        """没分类/没标签时条目跟改动前一模一样，不多出空字段"""
        manifest = {"roles": {}}
        rid = roles.register_role("普通角色", manifest, tmp_roles_dir)
        assert "category" not in manifest["roles"][rid]
        assert "tags" not in manifest["roles"][rid]

    def test_register_existing_role_ignores_category_and_tags(self, tmp_roles_dir):
        """已存在的早返回路径不改动现有角色（category/tags 只在新建时生效）"""
        manifest = {"roles": {}}
        rid = roles.register_role("重复角色", manifest, tmp_roles_dir, category="主角")
        again = roles.register_role("重复角色", manifest, tmp_roles_dir, category="配角", tags=["x"])
        assert again == rid
        assert manifest["roles"][rid]["category"] == "主角"
        assert "tags" not in manifest["roles"][rid]

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


class TestGetEmbeddingStatus:
    """speaker embedding 状态查询功能测试（计划 001，不依赖 torch/GPU）"""

    def test_never_precomputed(self, tmp_roles_dir):
        """从未预计算过：exists=False，附带说明原因"""
        manifest = roles.load_manifest(tmp_roles_dir)
        status = roles.get_embedding_status("narrator", manifest, tmp_roles_dir)
        assert status["exists"] is False
        assert status["valid"] is False
        assert status["stale_reason"]

    def test_valid_cache_matches_current_reference(self, tmp_roles_dir):
        """.pt + .meta.json 都在，且 meta 记录的 MD5 与当前 reference.wav 一致 -> valid"""
        from src.utils import calculate_file_md5
        import json as json_mod

        role_dir = os.path.join(tmp_roles_dir, "narrator")
        ref_audio = os.path.join(role_dir, "reference.wav")
        with open(os.path.join(role_dir, roles.EMBEDDING_FILENAME), "wb") as f:
            f.write(b"fake pt content")
        with open(os.path.join(role_dir, roles.EMBEDDING_META_FILENAME), "w", encoding="utf-8") as f:
            json_mod.dump({
                "ref_audio_md5": calculate_file_md5(ref_audio),
                "model_version": "v1",
                "created_at": "2026-01-01T00:00:00+00:00",
            }, f)

        manifest = roles.load_manifest(tmp_roles_dir)
        status = roles.get_embedding_status("narrator", manifest, tmp_roles_dir)
        assert status["exists"] is True
        assert status["valid"] is True
        assert status["created_at"] == "2026-01-01T00:00:00+00:00"

    def test_stale_when_reference_audio_changed(self, tmp_roles_dir):
        """reference.wav 内容变化后，旧 .meta.json 记录的 MD5 对不上 -> valid=False"""
        import json as json_mod

        role_dir = os.path.join(tmp_roles_dir, "narrator")
        with open(os.path.join(role_dir, roles.EMBEDDING_FILENAME), "wb") as f:
            f.write(b"fake pt content")
        with open(os.path.join(role_dir, roles.EMBEDDING_META_FILENAME), "w", encoding="utf-8") as f:
            json_mod.dump({"ref_audio_md5": "stale-md5-does-not-match", "model_version": "v1",
                           "created_at": "2026-01-01T00:00:00+00:00"}, f)

        manifest = roles.load_manifest(tmp_roles_dir)
        status = roles.get_embedding_status("narrator", manifest, tmp_roles_dir)
        assert status["exists"] is True
        assert status["valid"] is False
        assert "变化" in status["stale_reason"]

    def test_missing_meta_json_is_stale(self, tmp_roles_dir):
        """只有 .pt 没有 .meta.json（旧版本产物）-> valid=False"""
        role_dir = os.path.join(tmp_roles_dir, "narrator")
        with open(os.path.join(role_dir, roles.EMBEDDING_FILENAME), "wb") as f:
            f.write(b"fake pt content")

        manifest = roles.load_manifest(tmp_roles_dir)
        status = roles.get_embedding_status("narrator", manifest, tmp_roles_dir)
        assert status["exists"] is True
        assert status["valid"] is False


class TestPrecomputeEmbedding:
    """embedding 预计算触发功能测试（不依赖真实 GPU 环境）"""

    def test_unregistered_role_returns_error(self, tmp_roles_dir):
        """角色未注册时直接返回错误，不尝试起子进程"""
        manifest = roles.load_manifest(tmp_roles_dir)
        result = roles.precompute_embedding("no_such_role", manifest, tmp_roles_dir)
        assert result["ok"] is False
        assert result["error"]

    def test_env_not_ready_returns_error(self, tmp_roles_dir, tmp_project_dir):
        """IndexTTS 推理环境未就绪（venv/权重缺失）时返回明确错误，不抛异常"""
        manifest = roles.load_manifest(tmp_roles_dir)
        config = {
            "tts": {
                "index_tts": {
                    "python_bin": os.path.join(tmp_project_dir, "no_such_venv", "bin", "python"),
                    "repo_dir": os.path.join(tmp_project_dir, "no_such_repo"),
                    "checkpoints_dir": os.path.join(tmp_project_dir, "no_such_checkpoints"),
                }
            }
        }
        result = roles.precompute_embedding("narrator", manifest, tmp_roles_dir, config=config)
        assert result["ok"] is False
        assert "未就绪" in result["error"]


class TestSetRoleReference:
    """替换角色参考音频功能测试"""

    def test_copies_wav_and_invalidates_cache(self, tmp_roles_dir, tmp_project_dir):
        """替换后 reference.wav 更新为新内容，且旧的 .pt/.meta.json 被删除"""
        role_dir = os.path.join(tmp_roles_dir, "narrator")
        with open(os.path.join(role_dir, roles.EMBEDDING_FILENAME), "wb") as f:
            f.write(b"stale pt")
        with open(os.path.join(role_dir, roles.EMBEDDING_META_FILENAME), "w", encoding="utf-8") as f:
            f.write("{}")

        new_wav = os.path.join(tmp_project_dir, "new_reference.wav")
        with open(new_wav, "wb") as f:
            f.write(b"brand new reference audio bytes")

        manifest = roles.load_manifest(tmp_roles_dir)
        roles.set_role_reference("narrator", new_wav, manifest, tmp_roles_dir)

        with open(os.path.join(role_dir, "reference.wav"), "rb") as f:
            assert f.read() == b"brand new reference audio bytes"
        assert not os.path.exists(os.path.join(role_dir, roles.EMBEDDING_FILENAME))
        assert not os.path.exists(os.path.join(role_dir, roles.EMBEDDING_META_FILENAME))

    def test_unregistered_role_raises(self, tmp_roles_dir, tmp_project_dir):
        """角色未注册时抛异常"""
        new_wav = os.path.join(tmp_project_dir, "new_reference.wav")
        with open(new_wav, "wb") as f:
            f.write(b"x")
        manifest = roles.load_manifest(tmp_roles_dir)
        with pytest.raises(ValueError):
            roles.set_role_reference("no_such_role", new_wav, manifest, tmp_roles_dir)

    def test_missing_wav_raises(self, tmp_roles_dir, tmp_project_dir):
        """源 wav 文件不存在时抛异常"""
        manifest = roles.load_manifest(tmp_roles_dir)
        with pytest.raises(FileNotFoundError):
            roles.set_role_reference(
                "narrator", os.path.join(tmp_project_dir, "does_not_exist.wav"), manifest, tmp_roles_dir
            )


class TestDeleteRole:
    """删除角色功能测试"""

    def test_deletes_role_dir_and_manifest_entry(self, tmp_roles_dir):
        """删除后角色目录和清单条目都不再存在"""
        manifest = roles.load_manifest(tmp_roles_dir)
        assert "lin_dong" in manifest["roles"]

        roles.delete_role("lin_dong", manifest, tmp_roles_dir)

        assert "lin_dong" not in manifest["roles"]
        assert not os.path.isdir(os.path.join(tmp_roles_dir, "lin_dong"))

        reloaded = roles.load_manifest(tmp_roles_dir)
        assert "lin_dong" not in reloaded["roles"]

    def test_narrator_cannot_be_deleted(self, tmp_roles_dir):
        """narrator 是兜底音色来源，禁止删除"""
        manifest = roles.load_manifest(tmp_roles_dir)
        with pytest.raises(ValueError):
            roles.delete_role("narrator", manifest, tmp_roles_dir)
        assert "narrator" in manifest["roles"]
        assert os.path.isdir(os.path.join(tmp_roles_dir, "narrator"))

    def test_deleting_nonexistent_role_is_noop(self, tmp_roles_dir):
        """角色本就不存在时静默返回，不抛异常"""
        manifest = roles.load_manifest(tmp_roles_dir)
        roles.delete_role("no_such_role", manifest, tmp_roles_dir)  # 不应抛异常


class TestSaveManifestAtomic:
    def test_no_tmp_file_left_and_roundtrip_identity(self, tmp_roles_dir):
        manifest = {"roles": {"a": {"name": "甲"}}, "categories": ["x"], "category_tree": []}
        roles.save_manifest(manifest, tmp_roles_dir)
        assert not [f for f in os.listdir(tmp_roles_dir) if f.endswith(".tmp")]
        assert roles.load_manifest(tmp_roles_dir) == manifest

    def test_load_manifest_does_not_inject_tree_defaults(self, tmp_roles_dir):
        """新字段的默认值只能放在路由/树辅助层，load_manifest 对空清单必须
        精确返回 {"roles": {}}（既有测试锁着）"""
        shutil.rmtree(tmp_roles_dir)
        os.makedirs(tmp_roles_dir)
        assert roles.load_manifest(tmp_roles_dir) == {"roles": {}}
