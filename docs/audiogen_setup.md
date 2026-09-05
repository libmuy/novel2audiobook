# 音效 / 背景音本地生成环境搭建（RX 7900XTX / ROCm）

本项目的 BGM（环境音）与音效由本地模型批量生成，产物落在 `assets/ambience/`、
`assets/sfx/` 下供 `python cli.py assets gen` 增量生成、`src/audio_mixer.py`
直接使用（见 `src/asset_gen.py` 的模块说明）。两类素材各用一个专门模型，均运行在
**独立于项目主 venv 的隔离环境**中，理由与 `docs/indextts_setup.md` 相同：
依赖版本（尤其 `torch` 的 ROCm 构建）互相冲突，通过子进程 + `jobs.json` ⇄
`result.json` 协议调用。

| 素材类型 | 模型 | 状态 |
|---|---|---|
| ambience（BGM/环境音） | [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) | 本文档已覆盖 |
| sfx（音效） | Stable Audio 3 Small SFX（如 ROCm 上 Flash-Attention 2 装不通则退到 TangoFlux） | 见 Phase 3，待补 |

## 目录结构

```
tools/
├── acestep_repo/       # 官方仓库克隆（ace-step/ACE-Step-1.5）
├── acestep_env/        # 独立 venv（Python 3.11 + ROCm torch），uv 创建
├── acestep_infer.py    # 批量推理脚本，被 src/asset_gen.AceStepBackend 子进程调用
└── gpu_arbiter.py       # llama-server ⇄ 生成模型 显存互斥调度（LlmSuspendedForGpu）
```

权重目录：`/srv/unsafe/models/audiogen/ACE-Step-1.5`（在项目 git 仓库之外，沿用
`/srv/unsafe/models/{llm,tts}` 的既有惯例——体积大、是本机专属产物，不随代码分发）。

## 从零搭建步骤

依据官方 [INSTALL.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md)
的 Linux/ROCm 路径整理：

```bash
cd /home/bjn/novel2audiobook

# 1. 克隆官方仓库
git clone --depth 1 https://github.com/ace-step/ACE-Step-1.5.git tools/acestep_repo

# 2. 建独立 venv（官方要求 Python 3.11-3.12）
uv venv tools/acestep_env --python 3.11

# 3. 装 ROCm 版 torch（官方文档给的是 rocm6.0 索引；ROCm 有前向兼容性，
#    本机是 ROCm 7.0，若该索引装不到匹配 wheel 再换成 rocm6.4/其他可用版本）
uv pip install --python tools/acestep_env/bin/python \
    torch --index-url https://download.pytorch.org/whl/rocm6.0

# 4. 装 ACE-Step 本体依赖
uv pip install --python tools/acestep_env/bin/python -e tools/acestep_repo

# 5. 验证 ROCm torch 没被上一步悄悄换回 CPU/CUDA 版
tools/acestep_env/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"

# 6. RX 7900 XTX 是 RDNA3（gfx1100），若第 5 步 cuda.is_available() 为 False，
#    先跑官方诊断脚本，多半需要设置 HSA_OVERRIDE_GFX_VERSION
tools/acestep_env/bin/python tools/acestep_repo/scripts/check_gpu.py
export HSA_OVERRIDE_GFX_VERSION=11.0.0   # RX 7900 XT/XTX、RX 9070 XT 专用值

# 7. 下载权重到项目外的共享目录，并用官方支持的环境变量指向它
#    （ACE-Step 用 config_path 这个"模型名"定位权重，不接受直接路径参数，
#    实际目录由 ACESTEP_CHECKPOINTS_DIR 决定——见 tools/acestep_infer.py 顶部注释）
mkdir -p /srv/unsafe/models/audiogen/ACE-Step-1.5
ACESTEP_CHECKPOINTS_DIR=/srv/unsafe/models/audiogen/ACE-Step-1.5 \
    tools/acestep_env/bin/python -m acestep.model_downloader --all
    # 国内网络可加 --download-source modelscope
```

## 已知坑与应对

### 1. `torchcodec` 在 ROCm 上不可用
官方文档明确：AMD ROCm（以及 Intel XPU）没有 `torchcodec` wheel，ACE-Step 会
**自动回退到 `soundfile`** 做音频 I/O，功能完整不受影响，无需额外处理
（与 IndexTTS 那边遇到的同一类降级一致，见 `docs/indextts_setup.md` 已知坑 §4
的 BigVGAN 情况——都是"自动降级、仅提示、可忽略"）。

### 2. LM 后端优先用 `pt`，不用 `vllm`
官方 Linux 说明：某些发行版（含 Ubuntu）自带的 Python 是 `3.11.0rc1` 预发布版，
会导致 `vllm` 后端段错误；`nanovllm` 加速在非 NVIDIA 平台上支持也不完整。
`tools/acestep_infer.py` 已把 `LM_BACKEND` 硬编码为 `"pt"`（对应
`LLMHandler.initialize(backend="pt")`），牺牲一点速度换稳定性，这在离线批量
生成场景（不追求实时）里是合理取舍。如果后续验证 `vllm` 在本机 ROCm 环境下能跑，
可以把该常量改成 `"vllm"` 提速。

### 3. `device="cuda"` 在 ROCm 上是对的
ROCm 版 PyTorch 复用 CUDA 的设备命名空间（`torch.cuda.*` API 在 ROCm 构建下就是
指向 HIP 后端），`AceStepHandler.initialize_service(device="cuda")` /
`LLMHandler.initialize(device="cuda")` 不需要改成别的字符串，这与
`tools/indextts_infer.py`、`src/tts_engine.py` 里的既有做法一致。

### 4. 首次运行会下载模型，超时要给够
`tools/acestep_infer.py` 只负责推理，不负责下载——权重必须在步骤 7 里**预先**
下载完整，否则子进程内 `initialize_service()` 触发的按需下载会撞上
`global_config.yaml:asset_gen.ace_step.timeout_sec`（默认 3600 秒）而被杀掉，
留下不完整目录。若怀疑目录不完整，删掉对应子目录重新跑步骤 7（同
`docs/indextts_setup.md` 已知坑 §2 的排障思路）。

### 5. 模型选型对齐显存档位
`tools/acestep_infer.py` 里 `DIT_CONFIG_PATH = "acestep-v15-xl-sft"`、
`LM_MODEL_PATH = "acestep-5Hz-lm-1.7B"` 是按官方选型表里 "20-24GB 显存" 档位选的
（停掉 llama-server 后独占 24GB）。如果生成结果质量不理想，可以换
`acestep-v15-xl-turbo`（更快、质量略低）试一版对比。

## GPU 显存互斥（llama-server ⇄ ACE-Step / Stable Audio）

与 IndexTTS 完全复用同一套机制（原来叫 `LlmSuspendedForTts`，现已泛化改名为
`LlmSuspendedForGpu`，`src/tts_engine.py` 里的旧引用保留别名兼容）：
`src/asset_gen.SubprocessAudioGenBackend.generate_batch()` 在
`with LlmSuspendedForGpu(config):` 块内跑子进程，批量生成前自动停 llama-server，
结束后自动重新拉起。`python cli.py assets gen` 期间 Qwen 服务会短暂不可用，
命令结束后自动恢复，无需手动干预。ACE-Step XL（约 12GB）与 Stable Audio Small
（数 GB）单独驻卡时显存都很宽裕，**只是不与 `parse` 并发**——脚本设计上也没有
让两者同时跑的场景（`generate_assets()` 按 kind 顺序调用，一次只有一个生成模型在跑）。

## 冒烟测试

```bash
# 1. 先用 mock 后端确认脚手架本身没问题（不需要真实环境）
.venv/bin/python cli.py assets gen --kind ambience --only rain_heavy --backend mock

# 2. 真实环境搭好后，去掉 --backend mock，验证会调用 ACE-Step 并自动换卡
.venv/bin/python cli.py assets gen --kind ambience --only rain_heavy --force
.venv/bin/python cli.py assets list   # 期望 rain_heavy 一行 engine=ace_step，used_fallback=false
.venv/bin/python tools/gpu_arbiter.py status   # 确认 llama-server 已自动恢复
```
成功会在 `assets/ambience/rain_heavy.wav` 生成一段真实的雨声环境音（而非 Mock
的滤波噪声占位），试听确认循环接缝（首尾交叉淡化后）没有明显咔哒声。
