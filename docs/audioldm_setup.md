# 环境音本地生成环境搭建：AudioLDM-S-Full-v2（CPU）

本项目的环境音（BGM/ambience）原先用 ACE-Step 1.5 生成，诊断发现该模型本质是
音乐生成模型（`text2music` + `lyrics="[Instrumental]"`），生成"雨声/矿洞/风声"
这类写实环境录音时会带出音乐化的调性音色（频谱分析显示多条素材有异常突出的
单一音高主峰，占总能量 15%-34%）。改用 **AudioLDM-S-Full-v2**——训练数据是
真实音频事件（非音乐），且是标准 `diffusers` pipeline，接入成本远低于 ACE-Step
（不需要 clone 官方仓库、不需要 ROCm 专用 torch 索引、不需要 `--reinstall-package`
那套坑）。模型仅约 0.2B 参数，直接用 **CPU** 推理即可，规避 ROCm 兼容性风险。

音效（sfx）仍由 TangoFlux 生成，见 `docs/audiogen_setup.md`；ACE-Step 相关代码/
环境未删除（`AceStepBackend`、`tools/acestep_*`），只是不再被
`src/asset_gen.build_asset_gen_backend()` 默认选用。

## 目录结构

```
tools/
├── audioldm_infer.py     # 批量推理脚本，被 src/asset_gen.AudioLDMBackend 子进程调用
```

独立 venv（Python 3.11 + CPU torch + diffusers）：`/srv/unsafe/dev-env/venvs/audioldm`

权重目录：`/srv/unsafe/dev-env/models/audiogen/audioldm`（作为 `HF_HOME`，沿用
`/srv/unsafe/dev-env/models/{llm,tts,audiogen}` 的既有惯例，体积大、是本机专属产物，
不随代码分发）。

## 从零搭建步骤

```bash
cd /home/bjn/novel2audiobook

# 1. 建独立 venv
uv venv /srv/unsafe/dev-env/venvs/audioldm --python 3.11

# 2. 装 CPU 版 torch
uv pip install --python /srv/unsafe/dev-env/venvs/audioldm/bin/python \
    torch --index-url https://download.pytorch.org/whl/cpu

# 3. 装 diffusers 及推理所需依赖
uv pip install --python /srv/unsafe/dev-env/venvs/audioldm/bin/python \
    diffusers transformers accelerate soundfile scipy

# 4. 准备权重缓存目录（首次调用会自动从 HuggingFace 下载模型到这里，约 1.6GB）
mkdir -p /srv/unsafe/dev-env/models/audiogen/audioldm

# 5. 冒烟测试（会触发模型下载，网络慢的话预计几分钟到十几分钟）
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python cli.py assets gen --kind ambience --only rain_heavy --force
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python cli.py assets list   # 期望 rain_heavy 一行 engine=audioldm，used_fallback=false
```

## 关键点

### 0. 时长策略：不需要生成场景实际时长那么长的素材
`src/audio_mixer.py` 的 `_build_scene_bgm_track()` 本来就会把 ambience 素材
按 `loop_count = ceil(scene_duration / len(bgm_seg))` 循环拼接铺满整个场景时长
（见该函数第 152-153 行），素材本身不需要是完整的 45-60 秒。AudioLDM 类扩散
模型在训练时长范围（约 10 秒量级）内保真度最好，`assets/asset_specs.yaml`
里 ambience 各条的 `duration_sec` 因此统一定为 10 秒，靠
`src/asset_gen._post_process_ambience()` 已有的等功率交叉淡化循环拼接
（裁掉首尾各 `ambience_loop_crossfade_ms`，落盘时长约 8 秒）+ 混音阶段的循环
铺轨，效果不受影响。

### 1. `negative_prompt` 这次真正生效
`diffusers.AudioLDMPipeline.__call__()` 原生支持 `negative_prompt` 参数，走
classifier-free guidance；这是相对 ACE-Step 的实质性修复——ACE-Step 的
`GenerationParams` 根本没有负向提示词槽位，`asset_specs.yaml` 里写的
`negative_prompt` 之前从未真正传给模型。

### 2. CPU 推理速度参考
本机实测：10 秒音频、10 步扩散，单条约 78 秒（含首次调用的模型加载时间不算
在内，模型只需加载一次，`tools/audioldm_infer.py` 设计为单次加载、批量循环，
同 `tools/tangoflux_infer.py`）。6 条 ambience 素材全量生成约 8-9 分钟，可以
接受，不需要上 GPU；如果后续素材条目变多、CPU 速度不够用，可以评估切
`--device cuda`（ROCm 下未测试，需注意与 llama-server/ACE-Step 的显存互斥，
复用 `tools/gpu_arbiter.LlmSuspendedForGpu`）。

### 3. `AudioLDMPipeline` 已被 diffusers 标为 deprecated
首次运行会打印 `The AudioLDMPipeline has been deprecated and will not receive
bug fixes...`——这是 diffusers 后续推荐迁移到 `AudioLDM2Pipeline`（效果更好但
模型更大）的提示，不影响当前功能，可以先不处理；如果以后想进一步提升质量，
`AudioLDM2Pipeline` + `cvssp/audioldm2` 是升级路径。

### 4. 原生采样率是 16kHz
`pipe.vocoder.config.sampling_rate` 为 16000（比 TangoFlux/ACE-Step 的 44.1kHz
低），`tools/audioldm_infer.py` 按此采样率写出原始 wav；后续
`_post_process_ambience()` 统一重采样到 `target_sample_rate`（24kHz）时是
上采样，不会引入额外失真，环境音以中低频内容为主，实测频谱分析未发现问题。

## 冒烟测试（全量）

```bash
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python cli.py assets gen --kind ambience --force
/srv/unsafe/dev-env/venvs/novel2audiobook/bin/python cli.py assets list   # 期望全部 6 条 engine=audioldm，used_fallback=false
```
成功后可用 `python cli.py webui` 启动的管理界面（音效库页）逐条试听确认。
