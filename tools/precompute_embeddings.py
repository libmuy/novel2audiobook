#!/usr/bin/env python3
"""
IndexTTS-2.5 speaker embedding 批量预计算脚本（计划 001）。

运行环境同 tools/indextts_infer.py：独立的 tools/indextts_env venv
（Python 3.11 + ROCm torch），不与项目主 venv 混用。

对 roles/roles_manifest.json 中登记的角色逐个提取参考音频（reference.wav）的
speaker embedding，落盘为 roles/<role_id>/speaker_embeddings.pt。
后续 tools/indextts_infer.py 的批量合成、以及常驻 TTS 服务都会自动检测并复用
这份缓存，跳过 Wav2Vec2Bert + CAMPPlus + length_regulator 的在线提取步骤。

用法：
    tools/indextts_env/bin/python tools/precompute_embeddings.py \
        --repo-dir tools/indextts_repo \
        --checkpoints-dir /srv/unsafe/models/tts/IndexTTS-2.5 \
        --roles-dir roles \
        [--role lin_dong] [--force]

--role 不指定时处理清单中所有角色；--force 忽略已有 .pt，强制重新计算。
"""
import argparse
import json
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 speaker embedding 预计算")
    parser.add_argument("--repo-dir", required=True, help="index-tts 源码仓库目录")
    parser.add_argument("--checkpoints-dir", required=True, help="IndexTTS-2.5 权重目录")
    parser.add_argument("--roles-dir", required=True, help="roles/ 目录（含 roles_manifest.json）")
    parser.add_argument("--role", help="仅处理单个角色 ID；不指定则处理清单中所有角色")
    parser.add_argument("--force", action="store_true", help="忽略已有 .pt，强制重新计算")
    parser.add_argument("--use-bf16", action="store_true", default=True)
    args = parser.parse_args()

    roles_dir_abs = os.path.abspath(args.roles_dir)
    manifest_path = os.path.join(roles_dir_abs, "roles_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"错误: 未找到角色清单 {manifest_path}")
        sys.exit(1)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    roles = manifest.get("roles", {})

    if args.role:
        if args.role not in roles:
            print(f"错误: 角色清单中不存在角色 {args.role!r}")
            sys.exit(1)
        role_ids = [args.role]
    else:
        role_ids = list(roles.keys())

    if not role_ids:
        print("角色清单为空，无事可做")
        return

    # tools/ 目录本身先以绝对路径塞进 sys.path，这样下面 chdir 到 repo_dir
    # 之后依然能 `import indextts_infer`（兄弟模块，非包相对导入）。
    if TOOLS_DIR not in sys.path:
        sys.path.insert(0, TOOLS_DIR)
    sys.path.insert(0, args.repo_dir)
    # 同 indextts_infer.py：IndexTTS2 内部把 HF_HUB_CACHE 硬编码为相对路径，
    # 必须在导入前把 cwd 切到仓库目录。
    os.chdir(args.repo_dir)

    import indextts_infer as infer_mod

    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception as e:  # noqa: BLE001
        print(f"错误: 导入 IndexTTS2 失败: {e}")
        sys.exit(1)

    print(f">> 加载 IndexTTS2 模型（{args.checkpoints_dir}）...")
    tts = IndexTTS2(
        cfg_path=os.path.join(args.checkpoints_dir, "config.yaml"),
        model_dir=args.checkpoints_dir,
        use_bf16=args.use_bf16,
    )

    ok_count = 0
    skip_count = 0
    fail_count = 0
    for role_id in role_ids:
        info = roles.get(role_id, {})
        ref_audio_rel = info.get("reference_audio", os.path.join("roles", role_id, "reference.wav"))
        # roles_manifest.json 里的路径是相对项目根目录写的，这里统一按
        # roles_dir 的上一级（即项目根目录）解析，兜底直接拼 roles_dir/role_id。
        ref_audio_abs = os.path.abspath(os.path.join(roles_dir_abs, "..", ref_audio_rel))
        if not os.path.exists(ref_audio_abs):
            ref_audio_abs = os.path.join(roles_dir_abs, role_id, "reference.wav")
        if not os.path.exists(ref_audio_abs):
            print(f"  ✗ [{role_id}] 参考音频不存在，跳过: {ref_audio_abs}")
            fail_count += 1
            continue

        try:
            recomputed = infer_mod.extract_and_cache(tts, ref_audio_abs, force=args.force)
        except Exception as e:  # noqa: BLE001 - 单个角色失败不应中断整批
            print(f"  ✗ [{role_id}] 预计算失败: {e}")
            fail_count += 1
            continue

        if recomputed:
            print(f"  ✓ [{role_id}] 已重新计算并保存 speaker_embeddings.pt")
            ok_count += 1
        else:
            print(f"  · [{role_id}] 已有有效缓存，跳过")
            skip_count += 1

    print(f">> 完成：新计算 {ok_count} 个，跳过（已缓存）{skip_count} 个，失败 {fail_count} 个")
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
