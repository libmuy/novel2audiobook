# 音效 / 背景音本地生成环境搭建（RX 7900XTX / ROCm）

本项目的 BGM（环境音）与音效由本地模型批量生成，产物落在 `assets/ambience/`、
`assets/sfx/` 下供 `python cli.py assets gen` 增量生成、`src/audio_mixer.py`
直接使用（见 `src/asset_gen.py` 的模块说明）。两类素材各用一个专门模型，均运行在
**独立于项目主 venv 的隔离环境**中，理由与 `docs/indextts_setup.md` 相同：
依赖版本（尤其 `torch` 的 ROCm 构建）互相冲突，通过子进程 + `jobs.json` ⇄
`result.json` 协议调用。

| 素材类型 | 模型 | 状态 |
|---|---|---|
| ambience（BGM/环境音） | [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)，GPU | 本文档已覆盖 |
| sfx（音效） | [TangoFlux](https://github.com/declare-lab/TangoFlux)，CPU | 本文档已覆盖 |

原计划音效用 Stable Audio 3 Small SFX（官方明确该档位专为 CPU 设计，不需要
Flash-Attention 2——那只是 Medium/Large 档位的要求，最初调研时的 ROCm+FA2
风险评估其实不成立）。真正卡住的是另一件事：`stabilityai/stable-audio-3-small-sfx`
是 HuggingFace 上的 **gated repo**，申请访问要审批，实测账号点了"同意条款"后
还是拿到 `403 Forbidden`（不在 authorized list 里），流程不确定要等多久。
个人使用没必要死磕这个，改用 **TangoFlux**——公开仓库、无需任何申请、纯
PyTorch（同样是 CPU 友好档位），许可是 non-commercial research，符合本项目
"个人自用/研究"的既定前提。

## 目录结构

```
tools/
├── acestep_repo/        # ACE-Step 官方仓库克隆
├── acestep_env/         # 独立 venv（Python 3.11 + ROCm torch），uv 创建
├── acestep_infer.py     # 批量推理脚本，被 src/asset_gen.AceStepBackend 子进程调用
├── tangoflux_env/       # 独立 venv（Python 3.11 + CPU torch），uv 创建
│                        # （TangoFlux 直接 `pip install git+...` 装包，不需要单独 clone 仓库）
├── tangoflux_infer.py   # 批量推理脚本，被 src/asset_gen.TangoFluxBackend 子进程调用
└── gpu_arbiter.py       # llama-server ⇄ ACE-Step 显存互斥调度（LlmSuspendedForGpu）
                         # TangoFlux 跑 CPU，不参与这套换卡机制
```

权重目录：`/srv/unsafe/models/audiogen/{ACE-Step-1.5,tangoflux}`（在项目 git
仓库之外，沿用 `/srv/unsafe/models/{llm,tts}` 的既有惯例——体积大、是本机专属
产物，不随代码分发）。

## 从零搭建步骤

依据官方 [INSTALL.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md)
的 Linux/ROCm 路径整理：

```bash
cd /home/bjn/novel2audiobook

# 1. 克隆官方仓库
git clone --depth 1 https://github.com/ace-step/ACE-Step-1.5.git tools/acestep_repo

# 2. 建独立 venv（官方要求 Python 3.11-3.12；实测本机 uv 自带 3.11.15 可直接用）
uv venv tools/acestep_env --python 3.11

# 3. 装 ROCm 版 torch（官方文档给的是 rocm6.0 索引；本机 IndexTTS 那边已验证
#    rocm6.4 wheel 在本机 RX 7900XTX 上可正常跑，直接用 rocm6.4）
uv pip install --python tools/acestep_env/bin/python \
    torch --index-url https://download.pytorch.org/whl/rocm6.4

# 4. 装 ACE-Step 本体依赖（会把上一步装的 ROCm torch 覆盖成它 pyproject.toml
#    里硬编码的 CUDA 版，见下方已知坑 §0，必须紧跟着做第 5 步修复）
uv pip install --python tools/acestep_env/bin/python -e tools/acestep_repo

# 5. 【必做】把 torch/torchaudio 强制换回 ROCm 版——不加 --reinstall-package
#    uv 会因为"同名包已安装"直接跳过，不会真的切换 index/build variant
uv pip install --python tools/acestep_env/bin/python \
    --reinstall-package torch --reinstall-package torchaudio \
    torch torchaudio --index-url https://download.pytorch.org/whl/rocm6.4

# 6. 验证
tools/acestep_env/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 期望输出: 2.9.1+rocm6.4 True

# 7. RX 7900 XTX 是 RDNA3（gfx1100），若第 6 步 cuda.is_available() 为 False，
#    先跑官方诊断脚本，多半需要设置 HSA_OVERRIDE_GFX_VERSION
tools/acestep_env/bin/python tools/acestep_repo/scripts/check_gpu.py
export HSA_OVERRIDE_GFX_VERSION=11.0.0   # RX 7900 XT/XTX、RX 9070 XT 专用值

# 8. 下载权重到项目外的共享目录，并用官方支持的环境变量指向它
#    （ACE-Step 用 config_path 这个"模型名"定位权重，不接受直接路径参数，
#    实际目录由 ACESTEP_CHECKPOINTS_DIR 决定——见 tools/acestep_infer.py 顶部注释）
mkdir -p /srv/unsafe/models/audiogen/ACE-Step-1.5
ACESTEP_CHECKPOINTS_DIR=/srv/unsafe/models/audiogen/ACE-Step-1.5 \
    tools/acestep_env/bin/python -m acestep.model_downloader --all
    # 国内网络可加 --download-source modelscope
```

## 已知坑与应对

### 0. `pip install -e .` 会把 ROCm torch 换回 CUDA 版（实测踩到）
`tools/acestep_repo/pyproject.toml` 对 Linux x86_64 硬编码了
`torch==2.10.0+cu128`（精确版本+build 标签，不是范围约束），装 ACE-Step 本体
依赖（步骤 4）时会**无条件覆盖**掉步骤 3 装好的 ROCm 版 torch。这与
`docs/indextts_setup.md` 记录的坑不同：那边是版本冲突需要"先装 ROCm 版占住坑位"，
这边是装完之后才被换掉，必须在步骤 4 **之后**再修一次（步骤 5）。

**排障陷阱**：`uv pip install torch --index-url .../rocm6.4`（不带
`--reinstall-package`）看起来会执行成功（"Checked 2 packages"），但**不会真的换**——
uv 判断"已有同名包满足未锁版本的依赖"就直接跳过，根本不比较 index/build variant。
必须显式加 `--reinstall-package torch --reinstall-package torchaudio`
才会真的卸载重装。验证方式很简单：`torch.__version__` 里能不能看到 `+rocm` 后缀。

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

`--all` 只下主模型（vae/embedding/turbo-2B/lm-1.7B），**XL 系列要单独下**：
```bash
ACESTEP_CHECKPOINTS_DIR=/srv/unsafe/models/audiogen/ACE-Step-1.5 \
    tools/acestep_env/bin/python -m acestep.model_downloader --model acestep-v15-xl-sft
```

### 6. ROCm 下默认 fp32，务必设 `ACESTEP_ROCM_DTYPE=bfloat16`（实测踩到）
`initialize_service()` 检测到 ROCm/HIP 设备时默认用 `torch.float32`（日志会打印
"using dtype=torch.float32 (set ACESTEP_ROCM_DTYPE=bfloat16 or float16 to
override)"），这是保守的默认值，不是显存不够。本机 RX 7900XTX 实测对比：

| dtype | DiT init 峰值内存 | GPU 显存(max allocated) | 备注 |
|---|---|---|---|
| fp32（默认） | ~17GB | 未测（更高） | XL 档位 fp32 加载明显更吃显存，是最初一次真实生成
被系统当成"内存不足"杀掉的疑似诱因之一 |
| bfloat16 | ~17.5GB（含 LM） | max 18.5GB，VAE 解码前留 9GB 余量 | 结果正常（`success: True`），生成 8 步扩散仅 ~5 秒 |

`tools/acestep_infer.py` 已经在 import 前 `os.environ.setdefault("ACESTEP_ROCM_DTYPE", "bfloat16")`，无需手动设置；如果要临时对比 fp32 效果，运行前 `unset ACESTEP_ROCM_DTYPE` 或改成 `float16`。

### 7. `flash_attn` 装了但是 CUDA 版，会报错后自动降级（可忽略）
日志会出现 `flash_attn is installed but failed to import: libcudart.so.12:
cannot open shared object file`——`-e .` 装依赖时把 CUDA 版 flash-attn 也带了进来，
在 ROCm 上打不开是预期的，代码会自动降级到原生 PyTorch attention（`sdpa`），
不影响生成结果，无需处理（也可以 `uv pip uninstall flash-attn` 消除这条日志噪音，
但不是必须）。

### 8. 各阶段耗时参考（RX 7900XTX 实测，10 秒测试片段）
首次调用一次性加载：DiT 权重（XL，4 个 safetensors 分片）约 4.5 分钟、LM
tokenizer+约束解码器+权重约 1 分钟——这部分是子进程启动后**每次调用只付一次**
的固定成本（`tools/acestep_infer.py` 设计成单次加载、批量循环生成，见文件顶部
说明），不会随素材条数线性增加。单条生成里，8 步扩散只要 ~5 秒，VAE 解码
（tiled，本机显存档位下）约 90 秒是单条最大头的部分，60 秒长的 ambience 素材
解码时间预计等比更长——`global_config.yaml:asset_gen.ace_step.timeout_sec`
（3600 秒）留的余量对付批量跑几条是够的，如果一次性生成条目很多可以按需调大。

## GPU 显存互斥（llama-server ⇄ ACE-Step）

与 IndexTTS 完全复用同一套机制（原来叫 `LlmSuspendedForTts`，现已泛化改名为
`LlmSuspendedForGpu`，`src/tts_engine.py` 里的旧引用保留别名兼容）：
`src/asset_gen.SubprocessAudioGenBackend.generate_batch()` 在
`with LlmSuspendedForGpu(config):` 块内跑子进程，批量生成前自动停 llama-server，
结束后自动重新拉起。`python cli.py assets gen` 期间 Qwen 服务会短暂不可用，
命令结束后自动恢复，无需手动干预。ACE-Step XL（约 12GB）单独驻卡显存很宽裕，
**只是不与 `parse` 并发**。TangoFlux 跑在 CPU 上，不占显存，不需要也不参与这套
换卡机制，理论上可以和 llama-server/ACE-Step 同时跑（`SubprocessAudioGenBackend`
统一走同一套 subprocess 协议，TangoFlux 那次调用也会象征性暂停/恢复一下
llama-server，多余但无害，不值得为此分叉逻辑）。

## ACE-Step 冒烟测试

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

## TangoFlux（音效 sfx）环境搭建

比 ACE-Step 简单很多——CPU 推理、无需 ROCm、官方包直接 `pip install git+...`
装，不需要单独 clone 源码仓库。

```bash
# 1. 建独立 venv
uv venv tools/tangoflux_env --python 3.11

# 2. 装 CPU 版 torch/torchaudio/torchvision，版本严格对齐 TangoFlux 的
#    install_requires（均为裸版本号 "==2.4.0" 这类，不含 local 版本段，
#    所以只要主版本号对上，pip 后续不会像 ACE-Step 那样把它们换掉）
uv pip install --python tools/tangoflux_env/bin/python \
    torch==2.4.0 torchaudio==2.4.0 torchvision==0.19.0 \
    --index-url https://download.pytorch.org/whl/cpu

# 3. 装 TangoFlux 本体（直接从 GitHub 装，无需先 git clone）
uv pip install --python tools/tangoflux_env/bin/python \
    "tangoflux @ git+https://github.com/declare-lab/TangoFlux"

# 4. 验证（首次调用会自动下载权重到 HF_HOME 指向的目录，约几 GB）
mkdir -p /srv/unsafe/models/audiogen/tangoflux
HF_HOME=/srv/unsafe/models/audiogen/tangoflux tools/tangoflux_env/bin/python -c "
from tangoflux import TangoFluxInference
model = TangoFluxInference(name='declare-lab/TangoFlux', device='cpu')
print('OK')
"
```

### 已知坑

1. **不是 gated repo，无需申请** —— 这正是弃用 Stable Audio 3 Small SFX 改用
   它的原因，`snapshot_download` 直接拉取即可，不会遇到 401/403。
2. **`generate()` 不支持 `negative_prompt`，也不暴露 `seed` 参数** ——
   `tools/tangoflux_infer.py` 对 `negative_prompt` 直接忽略（协议里保留字段
   只是为了跨后端一致），用 `torch.manual_seed()` 在调用前手动设种子来达到
   可复现效果。
3. **`duration` 官方 CLI 限定 1-30 秒** —— 本项目 sfx 素材（`assets/asset_specs.yaml`
   里的 sfx 类目）全部是几秒钟的短音效，天然在这个范围内，不构成实际约束；
   如果以后往 sfx 类目里加超过 30 秒的条目会需要另外处理。
4. **默认 CPU** —— 和 Stable Audio Small 同样的取舍：SFX 素材生成频率低、
   单条时长短，CPU 慢一点换来不占显存、不用参与 GPU 换卡调度，简单可靠。
   如果批量条目很多嫌慢，`tools/tangoflux_infer.py` 的 `--device` 参数可以
   改成 `cuda`，但需要额外验证 ROCm 下这条链路（未测试）。

## 冒烟测试（sfx / TangoFlux）

```bash
.venv/bin/python cli.py assets gen --kind sfx --only sword_clash --force
.venv/bin/python cli.py assets list   # 期望 sword_clash 一行 engine=tangoflux，used_fallback=false
```
