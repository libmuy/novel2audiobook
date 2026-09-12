# 计划 001: Speaker Embedding 预计算与持久化

> **状态：已实现**（见 `src/tts_engine.py`、`tools/indextts_infer.py`、
> `tools/precompute_embeddings.py`、`src/roles.py`、`src/utils.py` 及对应测试）。
> 本文档保留为实现记录；与最初草案相比有两处修正，见下方标注。

## 目标
将 IndexTTS 音色提取的 speaker embedding 保存到文件系统，跨次推理复用，
避免每次 TTS 都重新运行提取流程。

## 背景
当前 IndexTTS2 内部仅在单次进程内缓存 embedding（通过 `cache_spk_cond` 等字段），
每次子进程启动都要重新提取。对于固定角色，reference.wav 不变，
embedding 也可以不变，但每次都要跑一遍 Wav2Vec2 + CAMPPlus + length_regulator。

### 前置修复：为什么先做任务排序（已实现，`src/tts_engine.py`）
`generate_tts_incremental` 原先按剧本顺序下发合成任务，多角色交替出现时，
IndexTTS 的音色缓存以 `spk_audio_prompt`（即参考音频路径）为键
（`infer_v2_5.py` 的 `cache_spk_audio_prompt` 判断逻辑），交替顺序会让缓存
逐句失效，每句都要重新提取一次——这才是本计划要解决的性能问题的主要来源。
实现时在下发批量任务前按 `role_cfg["reference_audio"]` 排序，让同一角色的
任务连续下发，这部分收益与 `.pt` 持久化正交、且几乎零成本，因此单独先做。

## 方案

### 1. 存储格式
在 `roles/<role_id>/` 下新增 `speaker_embeddings.pt`：

```
roles/lin_dong/
  config.json
  reference.wav
  speaker_embeddings.pt   ← 新增
```

`.pt` 文件内容（`torch.save` 序列化，字段名对应 `IndexTTS2` 实例上的
`cache_*` 属性，实现时以 `infer_v2_5.py` 源码为准做了对齐）：
```python
{
    "spk_cond_emb": Tensor,       # 对应 tts.cache_spk_cond（Wav2Vec2 speaker conditioning）
    "style": Tensor,              # 对应 tts.cache_s2mel_style（CAMPPlus 全局风格向量 [1,192]）
    "prompt_condition": Tensor,   # 对应 tts.cache_s2mel_prompt（length_regulator 输出）
    "ref_mel": Tensor,            # 对应 tts.cache_mel（mel 频谱）
    "ref_audio_md5": str,         # reference.wav 的 MD5（缓存失效判断）
    "model_version": str,         # IndexTTS 模型版本（兼容性检查）
    "created_at": str,            # ISO 时间戳
}
```

> **修正 1**：草案里的 `prompt_condition` 字段名与 `IndexTTS2` 内部属性
> `cache_s2mel_prompt` 一一对应（不是随意命名）；加载缓存时必须连同
> `tts.cache_spk_audio_prompt` 一起设为触发提取时的确切参考音频路径字符串，
> 否则 `infer_generator` 里"参考音频是否变了"的判断会立刻判定缓存失效、
> 转头又重新在线提取一遍（等于白做）。见 `tools/indextts_infer.py` 的
> `try_load_cached_embedding()`。
>
> **修正 2**：项目主 venv（`src/roles.py` 等）不装 torch，无法
> `torch.load()` 这个 `.pt` 来判断其状态。实现里在同目录额外维护一份
> `speaker_embeddings.meta.json`（纯 JSON，镜像 `ref_audio_md5` /
> `model_version` / `created_at`），供 `get_embedding_status()` 只读查询，
> 不参与推理逻辑本身。

### 2. 缓存失效条件
- `reference.wav` 的 MD5 与 `.pt` 中记录的不同 → 重新计算
- `.pt` 文件不存在 → 重新计算
- IndexTTS 模型版本变化 → 重新计算

### 3. 新增文件

#### `tools/precompute_embeddings.py`（独立脚本）
- 加载 IndexTTS2 模型（复用现有 `indextts_infer.py` 的初始化逻辑）
- 遍历 `roles_manifest.json` 中所有角色
- 对每个角色提取 embedding 并保存到 `.pt`
- 支持 `--role <role_id>` 只处理单个角色
- 支持 `--force` 强制重新计算

#### 修改 `tools/indextts_infer.py`
- 在 `tts.infer()` 调用前，检查 `.pt` 是否存在且有效
- 如果有效，直接 `torch.load()` 填充 `tts.cache_*` 字段，跳过提取
- 如果无效，正常提取并在完成后保存 `.pt`

#### 修改 `src/roles.py`
实际签名统一沿用模块里已有的 `(role_id, manifest, roles_dir=None, ...)` 顺序
（与 `get_role_runtime_config` 等既有函数一致）：
- `precompute_embedding(role_id, manifest, roles_dir=None, config=None, force=True)`：
  以子进程方式调用 `tools/precompute_embeddings.py`（复用与 `IndexTTSBackend`
  相同的独立 venv/权重路径配置）。这是真实 GPU 推理，函数本身**不做任何
  自动的 GPU 仲裁**——调用方（CLI/未来的 webui）必须在用户确认换手后才调用。
- `get_embedding_status(role_id, manifest, roles_dir=None)` → 返回状态
  （不依赖 torch，只读 `.meta.json` + 当前 `reference.wav` 的 MD5）
- `delete_role(role_id, manifest, roles_dir=None)`：删除角色目录与清单条目；
  `narrator` 是所有角色的兜底音色来源，禁止删除
- `set_role_reference(role_id, wav_path, manifest, roles_dir=None)`（草案未列出，
  实现时补上）：替换 `reference.wav` 并立即删除旧的 `.pt`/`.meta.json`，
  供后续 002 的角色管理界面直接调用，不需要在 UI 层重复这段逻辑
- `src/utils.py` 新增 `calculate_file_md5(file_path)`：草案写的是复用
  `calculate_md5`，但那个函数是对文本串（如 `f"{speaker}_{text}_{emotion}"`）
  求哈希，不是对文件字节求哈希，实现时新增了这个文件版本

### 4. 显存优化（可选，后续）
如果预计算了 embedding，理论上可以不加载以下模型：
- `Wav2Vec2BertModel`（~1.2GB）
- `CAMPPlus`（很小）
但这需要改 `IndexTTS2.__init__` 支持 lazy load，复杂度较高，建议二期做。

### 5. 验证
- 单元测试：`python cli.py test --module all`（已跑绿）、完整 `pytest tests/`
  （已跑绿，227 passed）
- 逻辑级冒烟测试（已完成，CPU-only，不占用真实 GPU/不影响运行中的
  llama-server）：用一个假的 `tts` 桩对象跑通
  `try_load_cached_embedding` / `save_embedding_cache` / `extract_and_cache`
  的命中、失效（MD5 变化、model_version 变化）、力保 `cache_spk_audio_prompt`
  设置正确等分支
- **待办（需要真实 GPU 环境，未在本次会话完成）**：
  - 手动验证：预计算后删除 `.pt`，确认能自动回退到在线提取
  - 对比验证：预计算的 embedding 生成的音频与在线提取的音频是否一致
  - 这两项需要真实加载 IndexTTS2 模型，而实现当时机器上 llama-server
    正在运行且 VRAM 占用 90%，为避免打断线上服务未执行，需要用户
    在合适时机手动跑一遍

## 文件清单
| 文件 | 操作 | 状态 |
|------|------|------|
| `tools/precompute_embeddings.py` | 新建 | ✅ |
| `tools/indextts_infer.py` | 修改（加载/落盘缓存逻辑） | ✅ |
| `src/roles.py` | 修改（新增函数，见上） | ✅ |
| `src/utils.py` | 修改（新增 `calculate_file_md5`，草案未列出） | ✅ |
| `src/tts_engine.py` | 修改（`pending_jobs` 按参考音频排序，前置修复） | ✅ |
| `tests/test_roles.py` | 修改（新增测试） | ✅ |
| `tests/test_tts_engine.py` | 修改（新增排序回归测试，草案未列出） | ✅ |
