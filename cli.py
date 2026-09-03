#!/usr/bin/env python3
"""
千万字全本地离线有声书生成流水线 CLI 入口
"""
import sys
import os
import argparse
import shutil
import tempfile

from src.utils import get_chapter_dir, normalize_chapter_id, get_project_root
from src.status_tracker import print_status_table
from src.llm_parser import process_chapter_parse, HeuristicBackend
from src.tts_engine import process_chapter_tts, generate_tts_incremental, MockTTSBackend
from src.audio_mixer import mix_chapter


def _make_isolated_test_workspace() -> tuple:
    """
    为自检准备隔离的临时工作区：复制 ch_0001 的 raw.txt 到临时章节目录，
    复制 roles/ 到临时角色目录。自检使用 Mock TTS + 启发式解析器，
    既不依赖外部 LLM/GPU 服务，也不会污染真实章节产物或共享角色清单。
    返回 (chapter_dir, roles_dir, cleanup_root)。
    """
    root = get_project_root()
    tmp_root = tempfile.mkdtemp(prefix="n2a_selftest_")

    chapter_dir = os.path.join(tmp_root, "chapters", "ch_0001")
    os.makedirs(chapter_dir, exist_ok=True)
    src_raw = os.path.join(root, "chapters", "ch_0001", "raw.txt")
    shutil.copyfile(src_raw, os.path.join(chapter_dir, "raw.txt"))

    roles_dir = os.path.join(tmp_root, "roles")
    shutil.copytree(os.path.join(root, "roles"), roles_dir)

    return chapter_dir, roles_dir, tmp_root


def run_test_module(module_name: str):
    """
    自检脚本路由。为保证自检快速、可重复、不依赖外部服务（LLM API / GPU），
    统一在隔离临时工作区中运行，使用启发式解析器 + Mock TTS 后端。
    真实 Qwen/IndexTTS 链路的验证见 `python cli.py parse|tts|mix`。
    """
    print(f"--> 开始运行自检模块: [{module_name}]（隔离临时工作区，Mock 引擎）")
    chapter_dir, roles_dir, tmp_root = _make_isolated_test_workspace()

    # 单模块自检语义为“跑通到该阶段为止”：因为每次自检都在全新隔离工作区中进行，
    # 单独测 tts/audio 时需要自动补跑其前置阶段，否则会因缺少上游产物而报错。
    stage_order = ["llm", "tts", "audio"]
    if module_name in ("all", "dry-run"):
        run_llm, run_tts, run_audio = True, True, True
    else:
        target_idx = stage_order.index(module_name)
        run_llm = target_idx >= 0
        run_tts = target_idx >= 1
        run_audio = target_idx >= 2

    try:
        if run_llm:
            print("[Test LLM] 解析 raw.txt -> script_draft.json（启发式解析器）...")
            with open(os.path.join(chapter_dir, "raw.txt"), "r", encoding="utf-8") as f:
                text = f.read()
            from src.llm_parser import parse_text_to_json
            import json
            script_draft = parse_text_to_json(text, backend=HeuristicBackend(), roles_dir=roles_dir)
            draft_path = os.path.join(chapter_dir, "script_draft.json")
            with open(draft_path, "w", encoding="utf-8") as f:
                json.dump(script_draft, f, ensure_ascii=False, indent=2)
            assert len(script_draft) > 0, "剧本初稿为空"
            print(f"  ✓ 生成剧本初稿: {draft_path}（{len(script_draft)} 句段）")

        if run_tts:
            print("[Test TTS] 合成剧本音轨（Mock 引擎）...")
            draft_path = os.path.join(chapter_dir, "script_draft.json")
            final_path = os.path.join(chapter_dir, "script_final.json")
            if not os.path.exists(final_path) and os.path.exists(draft_path):
                shutil.copyfile(draft_path, final_path)
                print("  [Test] 复制 script_draft.json 为 script_final.json 用于测试")

            import json
            with open(final_path, "r", encoding="utf-8") as f:
                script_final_data = json.load(f)
            timeline_data = generate_tts_incremental(
                chapter_dir, script_final_data, backend=MockTTSBackend(), roles_dir=roles_dir
            )
            timeline_path = os.path.join(chapter_dir, "timeline.json")
            assert len(timeline_data["items"]) == len(script_final_data), "时间线句段数与剧本不一致"
            print(f"  ✓ 生成时间线: {timeline_path}")

            # 增量性回归：同一份剧本第二次合成应全部命中缓存
            timeline_data_2 = generate_tts_incremental(
                chapter_dir, script_final_data, backend=MockTTSBackend(), roles_dir=roles_dir
            )
            assert all(it["cached"] for it in timeline_data_2["items"]), "二次合成未能全部命中哈希缓存"
            print("  ✓ 增量缓存回归通过（二次合成全部命中缓存）")

        if run_audio:
            print("[Test Audio] 执行多轨闪避混音...")
            out_mp3 = mix_chapter(chapter_dir)
            assert os.path.exists(out_mp3), "混音未生成成品文件"
            print(f"  ✓ 导出混合音频: {out_mp3}")

        print("--> 自检完成，全部指标正常！")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="千万字全本地离线有声书生成流水线")
    subparsers = parser.add_subparsers(dest="command", help="子命令列表")

    # 1. status
    parser_status = subparsers.add_parser("status", help="扫描 chapters/ 目录，打印各章节进度")

    # 2. parse
    parser_parse = subparsers.add_parser("parse", help="解析 raw.txt 生成 script_draft.json")
    parser_parse.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")

    # 3. tts
    parser_tts = subparsers.add_parser("tts", help="基于 script_final.json 执行哈希增量 TTS 生成")
    parser_tts.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")

    # 4. mix
    parser_mix = subparsers.add_parser("mix", help="根据 timeline.json 执行多轨闪避混音导出成品 MP3")
    parser_mix.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")

    # 5. test
    parser_test = subparsers.add_parser("test", help="内置自检脚本")
    parser_test.add_argument("--module", choices=["llm", "tts", "audio", "all", "dry-run"], default="dry-run", help="要测试的子模块")
    parser_test.add_argument("--all", action="store_true", help="同 --module all")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        print_status_table()

    elif args.command == "parse":
        chapter_dir = get_chapter_dir(args.chapter)
        try:
            draft_path = process_chapter_parse(chapter_dir)
            print(f"成功为章节 [{args.chapter}] 生成剧本初稿: {draft_path}")
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    elif args.command == "tts":
        chapter_dir = get_chapter_dir(args.chapter)
        try:
            timeline_path = process_chapter_tts(chapter_dir)
            print(f"成功为章节 [{args.chapter}] 生成哈希 TTS 时间线: {timeline_path}")
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    elif args.command == "mix":
        chapter_dir = get_chapter_dir(args.chapter)
        try:
            out_path = mix_chapter(chapter_dir)
            print(f"成功完成章节 [{args.chapter}] 闪避混音，成品位置: {out_path}")
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    elif args.command == "test":
        module = "all" if args.all else args.module
        try:
            run_test_module(module)
        except Exception as e:
            print(f"自检错误: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
