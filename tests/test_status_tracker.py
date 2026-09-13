"""
测试 src/status_tracker.py 模块的各个函数
"""
import os
import json
import time
import pytest
from src import status_tracker


class TestGetAllChaptersStatus:
    """获取所有章节状态功能测试"""

    def test_get_all_chapters_status_empty_dir(self, tmp_chapters_dir):
        """空目录返回空列表"""
        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)
        assert result == []

    def test_get_all_chapters_status_single_chapter(self, tmp_chapter_dir, tmp_chapters_dir):
        """单个章节"""
        # 创建一些文件
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write("test content")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert len(result) == 1
        assert result[0]["chapter_id"] == "ch_0001"
        assert result[0]["raw"] is True

    def test_get_all_chapters_status_multiple_chapters(self, tmp_chapters_dir):
        """多个章节"""
        for i in range(1, 4):
            ch_dir = os.path.join(tmp_chapters_dir, f"ch_{i:04d}")
            os.makedirs(ch_dir, exist_ok=True)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert len(result) == 3

    def test_get_all_chapters_status_fields_present(self, tmp_chapter_dir, tmp_chapters_dir):
        """返回的数据结构包含所有字段"""
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write("test")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert len(result) == 1
        item = result[0]
        assert "chapter_id" in item
        assert "status" in item
        assert "raw" in item
        assert "draft" in item
        assert "final" in item
        assert "timeline" in item
        assert "mp3" in item
        assert "audio_cache_count" in item

    def test_get_all_chapters_status_detects_raw(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测 raw.txt 存在"""
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write("content")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["raw"] is True

    def test_get_all_chapters_status_detects_draft(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测 script_draft.json 存在"""
        draft_path = os.path.join(tmp_chapter_dir, "script_draft.json")
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump([], f)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["draft"] is True

    def test_get_all_chapters_status_detects_final(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测 script_final.json 存在"""
        final_path = os.path.join(tmp_chapter_dir, "script_final.json")
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump([], f)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["final"] is True

    def test_get_all_chapters_status_detects_timeline(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测 timeline.json 存在"""
        timeline_path = os.path.join(tmp_chapter_dir, "timeline.json")
        with open(timeline_path, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["timeline"] is True

    def test_get_all_chapters_status_detects_mp3(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测 MP3 文件存在"""
        output_dir = os.path.join(tmp_chapter_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        mp3_path = os.path.join(output_dir, "chapter_0001.mp3")
        with open(mp3_path, "w") as f:
            f.write("dummy mp3")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["mp3"] is True

    def test_get_all_chapters_status_counts_audio_cache(self, tmp_chapter_dir, tmp_chapters_dir):
        """统计 audio_cache 中的 WAV 文件数"""
        cache_dir = os.path.join(tmp_chapter_dir, "audio_cache")
        os.makedirs(cache_dir, exist_ok=True)

        for i in range(3):
            wav_path = os.path.join(cache_dir, f"audio_{i}.wav")
            with open(wav_path, "w") as f:
                f.write("dummy")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["audio_cache_count"] == 3

    def test_get_all_chapters_status_stale_detection(self, tmp_chapter_dir, tmp_chapters_dir):
        """检测陈旧状态（raw.txt 比 script_draft.json 更新）"""
        # 创建 script_draft.json
        draft_path = os.path.join(tmp_chapter_dir, "script_draft.json")
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump([], f)

        # 等待一下，然后创建 raw.txt（使其 mtime 更新）
        time.sleep(0.1)
        raw_path = os.path.join(tmp_chapter_dir, "raw.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write("content")

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        # 状态应该包含 "STALE" 字样
        assert "STALE" in result[0]["status"]

    def test_get_all_chapters_status_completed_no_mp3(self, tmp_chapter_dir, tmp_chapters_dir):
        """completed 状态但没有 MP3 文件"""
        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({"status": "completed"}, f)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        # 状态应该变成 "STALE(no mp3)"
        assert "STALE" in result[0]["status"]

    def test_get_all_chapters_status_reads_status_json(self, tmp_chapter_dir, tmp_chapters_dir):
        """读取 .status.json 中的状态"""
        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({"status": "parsed_draft"}, f)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        assert result[0]["status"] == "parsed_draft"

    def test_get_all_chapters_status_nonexistent_dir(self):
        """不存在的目录返回空列表"""
        result = status_tracker.get_all_chapters_status("/nonexistent/path")
        assert result == []

    def test_get_all_chapters_status_sorted(self, tmp_chapters_dir):
        """章节按字母顺序排序"""
        for ch_id in ["ch_0003", "ch_0001", "ch_0002"]:
            ch_dir = os.path.join(tmp_chapters_dir, ch_id)
            os.makedirs(ch_dir, exist_ok=True)

        result = status_tracker.get_all_chapters_status(tmp_chapters_dir)

        chapter_ids = [item["chapter_id"] for item in result]
        assert chapter_ids == ["ch_0001", "ch_0002", "ch_0003"]


class TestGetNovelStatusSummary:
    """get_novel_status_summary 对多小说树级聚合的测试"""

    def test_empty_novel_has_zero_total(self, tmp_path):
        from src import library
        lib = str(tmp_path / "library")
        nid = library.create_novel("空书", library_dir=lib)

        summary = status_tracker.get_novel_status_summary(nid, library_dir=lib)

        assert summary["total"] == 0
        assert summary["by_status"] == {}
        assert summary["chapters"] == []

    def test_aggregates_by_status(self, tmp_path):
        from src import library
        lib = str(tmp_path / "library")
        nid = library.create_novel("聚合测试", library_dir=lib)
        library.add_chapter(nid, "第一章", "正文一", library_dir=lib)
        library.add_chapter(nid, "第二章", "正文二", library_dir=lib)

        summary = status_tracker.get_novel_status_summary(nid, library_dir=lib)

        assert summary["total"] == 2
        assert summary["by_status"] == {"UNKNOWN": 2}
        assert {c["chapter_id"] for c in summary["chapters"]} == {"ch_0001", "ch_0002"}

    def test_only_counts_this_novels_chapters(self, tmp_path):
        from src import library
        lib = str(tmp_path / "library")
        nid_a = library.create_novel("小说甲", library_dir=lib)
        nid_b = library.create_novel("小说乙", library_dir=lib)
        library.add_chapter(nid_a, "第一章", "正文", library_dir=lib)
        library.add_chapter(nid_b, "第一章", "正文", library_dir=lib)
        library.add_chapter(nid_b, "第二章", "正文", library_dir=lib)

        summary_a = status_tracker.get_novel_status_summary(nid_a, library_dir=lib)
        summary_b = status_tracker.get_novel_status_summary(nid_b, library_dir=lib)

        assert summary_a["total"] == 1
        assert summary_b["total"] == 2
