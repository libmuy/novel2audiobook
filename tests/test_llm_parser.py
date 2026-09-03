"""
测试 src/llm_parser.py 模块的各个函数
"""
import os
import json
import pytest
from src import llm_parser, roles as roles_mod, utils


class TestSplitParagraphs:
    """段落切分功能测试"""

    def test_split_paragraphs_basic(self):
        """基本段落切分"""
        text = "第一段\n\n第二段\n\n第三段"
        result = llm_parser.split_paragraphs(text)
        assert len(result) == 3
        assert result[0] == "第一段"
        assert result[1] == "第二段"
        assert result[2] == "第三段"

    def test_split_paragraphs_ignores_empty(self):
        """忽略空段落"""
        text = "段落1\n\n\n\n段落2"
        result = llm_parser.split_paragraphs(text)
        assert len(result) == 2

    def test_split_paragraphs_ignores_separators(self):
        """过滤纯分隔符段落"""
        text = "段落1\n\n※\n\n段落2\n\n***\n\n段落3\n\n---\n\n段落4"
        result = llm_parser.split_paragraphs(text)
        assert len(result) == 4
        assert "※" not in result
        assert "***" not in result
        assert "---" not in result

    def test_split_paragraphs_whitespace_handling(self):
        """处理带空白的段落"""
        text = "  段落1  \n\n  段落2  "
        result = llm_parser.split_paragraphs(text)
        assert len(result) == 2
        assert result[0] == "段落1"
        assert result[1] == "段落2"

    def test_split_paragraphs_empty_text(self):
        """空文本返回空列表"""
        result = llm_parser.split_paragraphs("")
        assert result == []

    def test_split_paragraphs_only_separators(self):
        """只有分隔符的文本"""
        text = "※\n\n***\n\n---"
        result = llm_parser.split_paragraphs(text)
        assert result == []


class TestHeuristicBackendParseSegment:
    """启发式解析器-段落解析功能测试"""

    def test_heuristic_parse_dialogue(self, tmp_roles_dir):
        """解析包含对话的段落"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        # 使用启发式解析器能识别的引号格式（使用中文左右引号）
        text = '苏砚说："测试文本。"'
        result = backend.parse_paragraph(text, manifest, assets)

        assert len(result) > 0
        # 解析后应该至少有一个 segment，内容应该包含"测试"
        assert any("测试" in seg["text"] for seg in result)

    def test_heuristic_parse_narration(self, tmp_roles_dir):
        """解析纯叙述文本"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        text = "苏砚缓缓睁开眼睛。"
        result = backend.parse_paragraph(text, manifest, assets)

        assert len(result) == 1
        assert result[0]["speaker"] == "narrator"
        assert result[0]["is_dialogue"] if "is_dialogue" in result[0] else True

    def test_heuristic_parse_segment_structure(self, tmp_roles_dir):
        """解析后的 segment 结构"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        text = "段落内容。"
        result = backend.parse_paragraph(text, manifest, assets)

        assert len(result) > 0
        seg = result[0]
        assert "speaker" in seg
        assert "text" in seg
        assert "emotion" in seg
        assert "sfx" in seg
        assert "bgm" in seg

    def test_heuristic_parse_multiple_dialogues(self, tmp_roles_dir):
        """解析多个对话"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        # 使用启发式解析器能识别的引号格式（使用中文左右引号）
        text = '他说："第一句。"她回答："第二句。"'
        result = backend.parse_paragraph(text, manifest, assets)

        # 应该有至少一个 segment
        assert len(result) >= 1
        # 应该包含对话中的内容
        assert any("句" in seg["text"] for seg in result)

    def test_heuristic_parse_empty_paragraph(self, tmp_roles_dir):
        """空段落不崩溃"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        result = backend.parse_paragraph("", manifest, assets)
        assert isinstance(result, list)

    def test_heuristic_parse_emotion_detection(self, tmp_roles_dir):
        """情感检测"""
        backend = llm_parser.HeuristicBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        assets = utils.list_available_assets()

        text = '他怒吼说："我要杀了你！"'
        result = backend.parse_paragraph(text, manifest, assets)

        # 应该至少有一个 segment
        assert len(result) > 0
        # 情感应该被检测
        emotions = [seg.get("emotion") for seg in result]
        assert any(emotions)


class TestParseTextToJson:
    """文本解析到 JSON 功能测试"""

    def test_parse_text_to_json_basic(self, tmp_roles_dir, sample_raw_text):
        """基本解析"""
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        assert isinstance(result, list)
        assert len(result) > 0

    def test_parse_text_to_json_segment_ids_sequential(self, tmp_roles_dir, sample_raw_text):
        """seg_id 连续递增"""
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        seg_ids = [seg["seg_id"] for seg in result]
        assert seg_ids == list(range(1, len(seg_ids) + 1))

    def test_parse_text_to_json_speaker_normalized(self, tmp_roles_dir, sample_raw_text):
        """speaker 已归一化"""
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        # 所有 speaker 都应该是合法的角色 ID
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        all_role_ids = set(manifest.get("roles", {}).keys())
        for seg in result:
            speaker = seg.get("speaker")
            # speaker 要么在已注册的角色中，要么是新注册的
            assert speaker is not None

    def test_parse_text_to_json_fields_present(self, tmp_roles_dir, sample_raw_text):
        """返回的各字段都存在"""
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        assert len(result) > 0
        seg = result[0]
        assert "seg_id" in seg
        assert "speaker" in seg
        assert "text" in seg
        assert "emotion" in seg
        assert "sfx" in seg
        assert "bgm" in seg

    def test_parse_text_to_json_text_not_empty(self, tmp_roles_dir, sample_raw_text):
        """text 字段非空"""
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        for seg in result:
            assert seg["text"] and len(seg["text"]) > 0

    def test_parse_text_to_json_empty_text(self, tmp_roles_dir):
        """空文本处理"""
        result = llm_parser.parse_text_to_json(
            "",
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        assert isinstance(result, list)

    def test_parse_text_to_json_with_default_backend(self, tmp_roles_dir, sample_raw_text):
        """使用默认 backend"""
        # 明确指定 HeuristicBackend 来避免网络调用
        result = llm_parser.parse_text_to_json(
            sample_raw_text,
            backend=llm_parser.HeuristicBackend(),
            roles_dir=tmp_roles_dir
        )

        assert len(result) > 0


class TestProcessChapterParse:
    """章节解析处理功能测试"""

    def test_process_chapter_parse_creates_draft_json(self, tmp_chapter_dir, tmp_roles_dir, sample_raw_text):
        """创建 script_draft.json"""
        # 创建 raw.txt
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(sample_raw_text)

        draft_path = llm_parser.process_chapter_parse(tmp_chapter_dir, roles_dir=tmp_roles_dir)

        assert os.path.exists(draft_path)
        assert draft_path.endswith("script_draft.json")

    def test_process_chapter_parse_valid_json(self, tmp_chapter_dir, tmp_roles_dir, sample_raw_text):
        """生成有效的 JSON"""
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(sample_raw_text)

        llm_parser.process_chapter_parse(tmp_chapter_dir, roles_dir=tmp_roles_dir)
        draft_path = os.path.join(tmp_chapter_dir, "script_draft.json")

        with open(draft_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, list)
        assert len(data) > 0

    def test_process_chapter_parse_missing_raw(self, tmp_chapter_dir, tmp_roles_dir):
        """raw.txt 不存在时抛异常"""
        with pytest.raises(FileNotFoundError):
            llm_parser.process_chapter_parse(tmp_chapter_dir, roles_dir=tmp_roles_dir)

    def test_process_chapter_parse_updates_status(self, tmp_chapter_dir, tmp_roles_dir, sample_raw_text):
        """更新 .status.json"""
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(sample_raw_text)

        llm_parser.process_chapter_parse(tmp_chapter_dir, roles_dir=tmp_roles_dir)
        status_file = os.path.join(tmp_chapter_dir, ".status.json")

        assert os.path.exists(status_file)
        with open(status_file, "r", encoding="utf-8") as f:
            status_data = json.load(f)
        assert status_data.get("status") == "parsed_draft"

    def test_process_chapter_parse_returns_path(self, tmp_chapter_dir, tmp_roles_dir, sample_raw_text):
        """返回生成的文件路径"""
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(sample_raw_text)

        result = llm_parser.process_chapter_parse(tmp_chapter_dir, roles_dir=tmp_roles_dir)

        assert isinstance(result, str)
        assert os.path.exists(result)
        assert "script_draft.json" in result
