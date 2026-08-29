#!/usr/bin/env python3
"""
千万字全本地离线有声书生成流水线 CLI 入口
"""
import sys
import os
import argparse
import shutil

from src.utils import get_chapter_dir, normalize_chapter_id
from src.status_tracker import print_status_table
from src.llm_parser import process_chapter_parse
from src.tts_engine import process_chapter_tts
from src.audio_mixer import mix_chapter


def run_test_module(module_name: str):
    """自检脚本路由"""
    print(f"--> 开始运行自检模块: [{module_name}]")
    chapter_dir = get_chapter_dir("0001")

    if module_name in ("llm", "all"):
        print("[Test LLM] 解析 raw.txt -> script_draft.json...")
        draft_path = process_chapter_parse(chapter_dir)
        print(f"  ✓ 生成剧本初稿: {draft_path}")

    if module_name in ("tts", "all"):
        print("[Test TTS] 合成剧本音轨...")
        draft_path = os.path.join(chapter_dir, "script_draft.json")
        final_path = os.path.join(chapter_dir, "script_final.json")
        if not os.path.exists(final_path) and os.path.exists(draft_path):
            shutil.copyfile(draft_path, final_path)
            print("  [Test] 复制 script_draft.json 为 script_final.json 用于测试")

        timeline_path = process_chapter_tts(chapter_dir)
        print(f"  ✓ 生成时间线: {timeline_path}")

    if module_name in ("audio", "all"):
        print("[Test Audio] 执行多轨闪避混音...")
        out_mp3 = mix_chapter(chapter_dir)
        print(f"  ✓ 导出混合音频: {out_mp3}")

    if module_name == "dry-run":
        print("[Dry-Run Test] 一键跑通全流程文件逻辑 (Mock 模式)...")
        # 1. Parse
        process_chapter_parse(chapter_dir)
        # 2. Mock 自动生成 final 剧本
        draft_path = os.path.join(chapter_dir, "script_draft.json")
        final_path = os.path.join(chapter_dir, "script_final.json")
        shutil.copyfile(draft_path, final_path)
        # 3. TTS
        process_chapter_tts(chapter_dir)
        # 4. Mix
        out_mp3 = mix_chapter(chapter_dir)
        print(f"✓ Dry-run 测试完成，成功生成最终导出文件: {out_mp3}")

    print("--> 自检完成，全部指标正常！")


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
