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
from src.asset_gen import (
    generate_assets, load_asset_specs, print_asset_status_table,
    MockAudioGenBackend, VALID_KINDS,
)


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
    统一在隔离临时工作区中运行，使用启发式解析器 + Mock TTS/素材生成后端。
    真实 Qwen/IndexTTS/ACE-Step/TangoFlux 链路的验证见
    `python cli.py parse|tts|mix|assets`。
    """
    print(f"--> 开始运行自检模块: [{module_name}]（隔离临时工作区，Mock 引擎）")
    chapter_dir, roles_dir, tmp_root = _make_isolated_test_workspace()

    # 单模块自检语义为“跑通到该阶段为止”：因为每次自检都在全新隔离工作区中进行，
    # 单独测 tts/audio 时需要自动补跑其前置阶段，否则会因缺少上游产物而报错。
    # assets（素材库生成）与章节流水线正交，不依赖也不参与这条前置链。
    stage_order = ["llm", "tts", "audio"]
    if module_name in ("all", "dry-run"):
        run_llm, run_tts, run_audio, run_assets = True, True, True, True
    elif module_name == "assets":
        run_llm = run_tts = run_audio = False
        run_assets = True
    else:
        target_idx = stage_order.index(module_name)
        run_llm = target_idx >= 0
        run_tts = target_idx >= 1
        run_audio = target_idx >= 2
        run_assets = False

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

        if run_assets:
            print("[Test Assets] 生成音效/背景音素材库（Mock 引擎，隔离目录）...")
            specs = load_asset_specs()  # 读取真实项目的 asset_specs.yaml（只读，不写入）
            isolated_assets_dir = os.path.join(tmp_root, "assets")
            mock = MockAudioGenBackend()
            summary = generate_assets(
                specs=specs, assets_dir=isolated_assets_dir,
                backend_map={"ambience": mock, "sfx": mock},
            )
            assert not summary["error"], f"素材生成出现错误: {summary['error']}"
            total = len(summary["generated"]) + len(summary["fallback"])
            assert total > 0, "未生成任何素材（assets/asset_specs.yaml 是否为空？）"
            print(f"  ✓ 生成素材库：{total} 条（隔离目录，不影响真实 assets/）")

            # 增量性回归：同一份 spec 第二次生成应全部命中缓存
            summary_2 = generate_assets(
                specs=specs, assets_dir=isolated_assets_dir,
                backend_map={"ambience": mock, "sfx": mock},
            )
            assert len(summary_2["skipped"]) == total, "二次生成未能全部命中 spec_hash 缓存"
            print("  ✓ 增量缓存回归通过（二次生成全部命中缓存）")

        print("--> 自检完成，全部指标正常！")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _prompt_yes_no(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _release_tts_daemon_if_running(auto_yes: bool) -> bool:
    """
    B5 换手策略之一：`parse` 依赖 llama-server，如果常驻 TTS 服务（计划 003）
    正占着显存，交互式终端下先问一句是否释放，非交互式（管道/脚本/CI）或
    传了 --yes 则直接释放，不阻塞。返回 False 表示用户拒绝，调用方应中止。
    """
    from tools import gpu_arbiter
    if not gpu_arbiter.is_tts_daemon_running():
        return True
    if sys.stdin.isatty() and not auto_yes:
        if not _prompt_yes_no("常驻 TTS 服务正占用显存，需要先释放才能运行 parse，是否释放？"):
            return False
    from src.tts_daemon import IndexTTSDaemon
    result = IndexTTSDaemon().shutdown()
    if not result.get("ok"):
        print(f"释放常驻 TTS 服务失败: {result.get('error')}")
        return False
    return True


def _confirm_batch_llm_swap(auto_yes: bool) -> bool:
    """
    B5 换手策略之二：`tts` 命令的批量链路本身就会通过
    tools.gpu_arbiter.LlmSuspendedForGpu 自动停/起 llama-server（这条路径不变，
    见 src/tts_engine.IndexTTSBackend）；这里只是在交互式终端下、真的会发生
    换手时先告知一声，避免用户在不知情的情况下让 llama-server 被停用一整个
    批次的时长（单章可能耗时 65-70 分钟，见 global_config.yaml 的 timeout_sec
    注释）。非交互式/--yes 直接放行，不阻塞、不改变已有的自动换手行为。
    """
    if auto_yes or not sys.stdin.isatty():
        return True
    from tools import gpu_arbiter
    if gpu_arbiter.get_current_owner() != gpu_arbiter.OWNER_LLM:
        return True  # llama-server 本来就没在跑，不会产生换手代价
    stop_eta = gpu_arbiter.get_expected_swap_seconds("llm_stop")
    start_eta = gpu_arbiter.get_expected_swap_seconds("llm_start")
    eta_str = f"（历史约停 {stop_eta:.0f}s + 恢复 {start_eta:.0f}s）" if stop_eta and start_eta else ""
    print(f"本次 TTS 合成会先停止 llama-server 腾出显存，完成后自动恢复{eta_str}。")
    return _prompt_yes_no("确认继续？")


def main():
    parser = argparse.ArgumentParser(description="千万字全本地离线有声书生成流水线")
    subparsers = parser.add_subparsers(dest="command", help="子命令列表")

    # 1. status
    parser_status = subparsers.add_parser("status", help="扫描 chapters/ 目录，打印各章节进度")

    # 2. parse
    parser_parse = subparsers.add_parser("parse", help="解析 raw.txt 生成 script_draft.json")
    parser_parse.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")
    parser_parse.add_argument("--yes", action="store_true",
                               help="跳过 GPU 换手确认（释放常驻 TTS 服务时不再询问）")

    # 3. tts
    parser_tts = subparsers.add_parser("tts", help="基于 script_final.json 执行哈希增量 TTS 生成")
    parser_tts.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")
    parser_tts.add_argument("--yes", action="store_true",
                            help="跳过 GPU 换手确认（自动停/起 llama-server 时不再询问）")

    # 4. mix
    parser_mix = subparsers.add_parser("mix", help="根据 timeline.json 混音导出成品 MP3（当前默认纯人声）")
    parser_mix.add_argument("--chapter", required=True, help="章节 ID (如 0001 或 ch_0001)")
    parser_mix.add_argument("--with-assets", action="store_true",
                             help="包含环境音/音效的完整闪避混音（覆盖 global_config.yaml 的 mixing.voice_only）")

    # 5. test
    parser_test = subparsers.add_parser("test", help="内置自检脚本")
    parser_test.add_argument("--module", choices=["llm", "tts", "audio", "assets", "all", "dry-run"], default="dry-run", help="要测试的子模块")
    parser_test.add_argument("--all", action="store_true", help="同 --module all")

    # 6. assets
    parser_assets = subparsers.add_parser("assets", help="管理/生成音效与背景音素材库（本地模型批量生成）")
    parser_assets.add_argument("action", nargs="?", choices=["list", "gen"], default="list",
                                help="list=查看素材库状态（默认）；gen=按 asset_specs.yaml 增量生成")
    parser_assets.add_argument("--kind", choices=list(VALID_KINDS), help="仅处理 ambience（BGM）或 sfx 一类")
    parser_assets.add_argument("--only", help="逗号分隔的素材名列表，仅生成/刷新指定几条")
    parser_assets.add_argument("--force", action="store_true", help="忽略增量缓存，全部重新生成")
    parser_assets.add_argument("--backend", choices=["mock"], help="强制使用指定后端（目前仅支持 mock，用于离线自检/占位铺库）")

    # 7. serve
    parser_serve = subparsers.add_parser("serve", help="启动本地 HTTP 服务，浏览素材库与章节成片（只读）")
    parser_serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机可访问 (127.0.0.1)")
    parser_serve.add_argument("--port", type=int, default=8090, help="监听端口，默认 8090（避开 llm.serve_port=8080）")

    # 8. tts-serve（计划 003：常驻 TTS 服务，脱离 webui 单独调试用）
    parser_tts_serve = subparsers.add_parser(
        "tts-serve", help="管理常驻 IndexTTS 推理服务（避免每次试听都重新加载模型）"
    )
    parser_tts_serve.add_argument("action", choices=["start", "stop", "status"])
    parser_tts_serve.add_argument("--yes", action="store_true", help="启动时跳过 GPU 换手确认")

    # 9. webui（计划 002：Gradio 全流程管理界面）
    parser_webui = subparsers.add_parser("webui", help="启动 Gradio 全流程管理界面")
    parser_webui.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机可访问 (127.0.0.1)")
    parser_webui.add_argument("--port", type=int, default=7860, help="监听端口，默认 7860")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        print_status_table()

    elif args.command == "parse":
        if not _release_tts_daemon_if_running(args.yes):
            print("已取消（常驻 TTS 服务仍在运行，需要先释放显存才能执行 parse）")
            sys.exit(1)
        chapter_dir = get_chapter_dir(args.chapter)
        try:
            draft_path = process_chapter_parse(chapter_dir)
            print(f"成功为章节 [{args.chapter}] 生成剧本初稿: {draft_path}")
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    elif args.command == "tts":
        if not _confirm_batch_llm_swap(args.yes):
            print("已取消")
            sys.exit(1)
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
            voice_only = False if args.with_assets else None  # None = 按 global_config.yaml 的 mixing.voice_only
            out_path = mix_chapter(chapter_dir, voice_only=voice_only)
            print(f"成功完成章节 [{args.chapter}] 混音，成品位置: {out_path}")
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

    elif args.command == "assets":
        if args.action == "list":
            print_asset_status_table()
        else:  # gen
            kinds = [args.kind] if args.kind else None
            only = set(args.only.split(",")) if args.only else None
            backend_map = None
            if args.backend == "mock":
                mock = MockAudioGenBackend()
                backend_map = {"ambience": mock, "sfx": mock}
            try:
                summary = generate_assets(kinds=kinds, only=only, force=args.force, backend_map=backend_map)
                print(
                    f"生成完成：新生成 {len(summary['generated'])} 条，"
                    f"Mock 占位 {len(summary['fallback'])} 条，"
                    f"跳过（已缓存）{len(summary['skipped'])} 条，"
                    f"失败 {len(summary['error'])} 条"
                )
                if summary["error"]:
                    sys.exit(1)
            except Exception as e:
                print(f"错误: {e}")
                sys.exit(1)

    elif args.command == "serve":
        from src.web_server import run_server
        print(f"--> 启动资源浏览服务: http://{args.host}:{args.port}/ (Ctrl+C 停止)")
        run_server(host=args.host, port=args.port)

    elif args.command == "tts-serve":
        from tools import gpu_arbiter
        from src.tts_daemon import IndexTTSDaemon
        daemon = IndexTTSDaemon()

        if args.action == "status":
            if daemon.is_running():
                state = gpu_arbiter.read_tts_daemon_state() or {}
                print(f"常驻 TTS 服务：运行中 (pid={state.get('pid')}, started_at={state.get('started_at')})")
            else:
                print("常驻 TTS 服务：未运行")

        elif args.action == "start":
            if daemon.is_running():
                print("常驻 TTS 服务已经在运行，无需重复启动")
            else:
                plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)
                if not plan["noop"] and sys.stdin.isatty() and not args.yes:
                    eta = f"，历史约 {plan['estimated_seconds']:.0f} 秒" if plan["estimated_seconds"] else ""
                    print(f"将执行：{' -> '.join(plan['steps'])}{eta}")
                    if not _prompt_yes_no("确认继续？"):
                        print("已取消")
                        sys.exit(1)
                result = daemon.ensure_started()
                if result["ok"]:
                    print("常驻 TTS 服务已启动")
                else:
                    print(f"启动失败: {result['error']}")
                    sys.exit(1)

        else:  # stop
            if not daemon.is_running():
                print("常驻 TTS 服务本来就没有运行")
            else:
                result = daemon.shutdown()
                if result["ok"]:
                    print("常驻 TTS 服务已停止")
                else:
                    print(f"停止失败: {result['error']}")
                    sys.exit(1)

    elif args.command == "webui":
        from src.webui_app import run_webui
        print(f"--> 启动管理界面: http://{args.host}:{args.port}/ (Ctrl+C 停止)")
        run_webui(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
