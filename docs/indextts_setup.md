# IndexTTS-2.5 推理环境搭建（RX 7900XTX / ROCm）

本项目的人声合成使用 [IndexTTS-2.5](https://github.com/index-tts/index-tts)，
在本机 AMD RX 7900XTX（gfx1100）上通过 ROCm 运行。因为该模型的依赖版本（尤其
`torch==2.8.*` + 一批锁定版本的科学计算库）与项目主 venv（`.venv/`，Python 3.12）
的依赖树不兼容，IndexTTS 运行在**完全独立**的 venv 中，主项目通过子进程调用它。

## 目录结构

```
tools/
├── indextts_repo/      # 官方仓库克隆（git clone index-tts/index-tts），含 checkpoints 软链接
│   └── checkpoints -> /srv/unsafe/models/tts/IndexTTS-2.5   # 见下方"硬编码路径"说明
├── indextts_env/       # 独立 venv（Python 3.11 + ROCm torch），uv 创建
├── indextts_infer.py   # 批量推理脚本，被 src/tts_engine.IndexTTSBackend 子进程调用
└── gpu_arbiter.py       # llama-server ⇄ IndexTTS 显存互斥调度
```

权重目录：`/srv/unsafe/models/tts/IndexTTS-2.5`（在项目 git 仓库之外，因为体积大且
是本机专属产物，不随代码分发）。

## 从零搭建步骤

```bash
cd /home/bjn/novel2audiobook

# 1. 克隆官方仓库
git clone --depth 1 https://github.com/index-tts/index-tts.git tools/indextts_repo

# 2. 下载权重主体（约 5GB，国内网络建议用镜像）
mkdir -p /srv/unsafe/models/tts/IndexTTS-2.5
uv pip install --python .venv/bin/python huggingface_hub
HF_ENDPOINT="https://hf-mirror.com" .venv/bin/hf download IndexTeam/IndexTTS-2.5 \
    --local-dir /srv/unsafe/models/tts/IndexTTS-2.5

# 3. 建独立 venv（官方要求 Python 3.10/3.11，且 torch 锁定 2.8.*）
uv venv tools/indextts_env --python 3.11

# 4. 装 ROCm 版 torch/torchaudio（关键：必须先装，且版本要与 pyproject.toml 的
#    torch==2.8.* 约束匹配，否则下一步会被 CPU/CUDA 版覆盖）
uv pip install --python tools/indextts_env/bin/python \
    torch==2.8.0+rocm6.4 torchaudio==2.8.0+rocm6.4 \
    --extra-index-url https://download.pytorch.org/whl/rocm6.4

# 5. 装其余依赖（不装 webui/deepspeed/accel 这些 NVIDIA-only 或非必需 extras）
uv pip install --python tools/indextts_env/bin/python -e tools/indextts_repo

# 6. 验证 ROCm torch 没被上一步悄悄换回 CPU/CUDA 版
tools/indextts_env/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 期望输出: 2.8.0+rocm6.4 True

# 7. 官方 GPU 检测脚本
cd tools/indextts_repo && /home/bjn/novel2audiobook/tools/indextts_env/bin/python tools/gpu_check.py

# 8. 关键：建 checkpoints 软链接（见下方说明），否则辅助模型缓存路径会错乱
ln -sfn /srv/unsafe/models/tts/IndexTTS-2.5 tools/indextts_repo/checkpoints
```

## 已知坑与应对

### 1. `HF_HUB_CACHE` 被硬编码为相对路径
`indextts/infer_v2_5.py` 顶部有：
```python
os.environ['HF_HUB_CACHE'] = './checkpoints/hf_cache'
```
这是**无条件覆盖**，在导入该模块前设置环境变量也没用。意味着：
- 辅助模型缓存永远落在“当前工作目录下的 `checkpoints/hf_cache`”；
- 因此我们的批量推理脚本（`tools/indextts_infer.py`）**必须 `os.chdir()` 到
  `indextts_repo` 目录**再 import，且该目录下要有 `checkpoints` 软链接指向真正的
  权重目录，这样辅助模型缓存才会落在 `/srv/unsafe/models/tts/IndexTTS-2.5/hf_cache/`
  而不是散落在每次调用时的临时 CWD 里（导致重复下载）。

### 2. 辅助模型不在主仓库里，首次运行自动下载
`w2v-bert-2.0`（语义特征提取，~4.3GB）、MaskGCT 语义编解码器、CAMPPlus、BigVGAN
不包含在 `IndexTeam/IndexTTS-2.5` 主权重里，`IndexTTS2.__init__()` 首次运行时才
下载到 `{model_dir}/hf_cache/`。**这个下载耗时较长（视网络几分钟到几十分钟），
请预留时间，不要用过短的超时把它杀掉**——杀掉会留下不完整的目录，而
`infer_v2_5.py` 判断"是否需要下载"时只检查目录是否存在（不检查是否完整），
导致下次直接判定"已下载"却在加载时报错缺文件。

**排障**：如果报 `OSError: Error no file named ... found in directory .../hf_cache/xxx`，
说明该目录是不完整的半成品，直接删除重跑：
```bash
rm -rf /srv/unsafe/models/tts/IndexTTS-2.5/hf_cache/<有问题的子目录>
```

### 3. 直连 HuggingFace 有时握手成功但下载中途失败
本机对 `huggingface.co` 的 TCP 443 握手是通的，`indextts.utils.network_detection.need_proxy()`
只做 TCP 探测，可能误判为"不需要代理"，进而直连 HF SDK 下载，结果卡住或
`FileMetadataError`。这种情况下用项目自带的 ModelScope 回退路径：
```bash
USE_MODELSCOPE=true <indextts_env python> your_script.py
```
`indextts/utils/model_download.py` 会自动改用 ModelScope 镜像（`AI-ModelScope/w2v-bert-2.0`
等）下载，实测更稳定。`tools/indextts_infer.py` 未强制设置该变量，如遇下载问题可在
调用环境中导出 `USE_MODELSCOPE=true`。

### 4. BigVGAN 自定义 CUDA/HIP 内核加载失败
日志会出现：
```
Failed to load custom CUDA kernel for BigVGAN. Falling back to torch.
RuntimeError('Ninja is required to load C++ extensions (pip install ninja to get it)')
```
这是可接受的降级（自动回退纯 PyTorch 实现，功能不受影响，仅推理速度稍慢）。
如需启用融合内核加速，装 `ninja`：
```bash
uv pip install --python tools/indextts_env/bin/python ninja
```

## GPU 显存互斥（llama-server ⇄ IndexTTS）

RX 7900XTX 共 24GB 显存，`llama-server`（Qwen3.8-27B-UD-Q4_K_M）常驻占用约 22GB，
留给 IndexTTS 的空间不足其所需的 ~6GB。`src/tts_engine.IndexTTSBackend.synthesize_batch()`
通过 `tools/gpu_arbiter.py` 的 `LlmSuspendedForTts` 上下文管理器自动处理：

1. 批量合成前：若 llama-server 正在跑，发送 SIGTERM 停止它；
2. 合成结束（无论成功/失败/超时）：若之前是运行状态，用
   `ai llm serve <serve_model_registry_name> --port <serve_port>`（配置见
   `global_config.yaml` 的 `llm.serve_model_registry_name`）重新拉起，并轮询
   `/v1/models` 直到就绪或超时。

即：`python cli.py tts --chapter XXXX` 期间 Qwen 服务会短暂不可用，命令结束后自动恢复，
无需手动干预。若要单独查看/控制：
```bash
python tools/gpu_arbiter.py status   # 查看 llama-server 是否在跑
python tools/gpu_arbiter.py stop     # 手动停止
python tools/gpu_arbiter.py start    # 手动拉起
```

## 角色参考音频（reference.wav）

项目脚手架自带的 `roles/narrator/reference.wav`、`roles/lin_dong/reference.wav`
实测是 **0.5 秒纯静音**占位文件——零样本声音克隆完全依赖参考音频里的真实音色，
喂静音会导致克隆失败或产出垃圾音频。本机没有录音设备，也不适合未经许可
爬取网络上的真人声音去克隆（IndexTTS 官方声明不核验参考音频授权，获取
同意是使用者责任），因此用本机离线的 espeak-ng 共振峰合成器（产出电子合成音，
不涉及任何真实个人声音授权问题）朗读一段带角色气质的文本，生成种子参考音频：

```bash
python tools/generate_seed_reference.py           # 只处理静音/过短的占位角色
python tools/generate_seed_reference.py --force   # 强制重新生成全部角色
```

espeak-ng 本身通过 `apt-get download` 提取 .deb（`espeak-ng` +
`libespeak-ng1` + `espeak-ng-data` + `libpcaudio0` + `libsonic0`）后用
`dpkg-deb -x` 解包到 `/srv/unsafe/tools/espeak_ng/`，未做系统级安装（无 root）。
后续若有真人配音/已授权样本，直接替换对应 `roles/<role_id>/reference.wav`
即可，IndexTTS 合成质量会显著提升；种子音频只是让管线能先跑通真实克隆流程。

## 冒烟测试

```bash
cd tools/indextts_repo
/home/bjn/novel2audiobook/tools/indextts_env/bin/python -c "
from indextts.infer_v2_5 import IndexTTS2
tts = IndexTTS2(cfg_path='checkpoints/config.yaml', model_dir='checkpoints', use_bf16=True)
tts.infer(spk_audio_prompt='../../roles/narrator/reference.wav', text='测试文本。',
          lang='ZH', output_path='/tmp/out.wav')
"
```
成功会在 `/tmp/out.wav` 生成一段真实语音（而非静音/方波占位音）。
