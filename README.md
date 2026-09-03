# novel2audiobook

将小说文本转换为多角色、带情感/音效/环境音的有声书 MP3 的全本地离线流水线。

## 状态机

```
raw.txt → script_draft.json → script_final.json → 增量 TTS → timeline.json → chapter_XXXX.mp3
```

| 阶段 | 命令 | 说明 |
|---|---|---|
| 解析 | `python cli.py parse --chapter 0001` | 调用本地 Qwen（llama-server）把 raw.txt 切分为带说话人/情感/音效的剧本 JSON；LLM 不可达时自动回退规则解析器 |
| 定稿 | 人工审阅 `script_draft.json`，另存为 `script_final.json` | 当前无自动定稿步骤，需人工确认剧本后手动复制/编辑 |
| 合成 | `python cli.py tts --chapter 0001` | 基于 MD5(speaker+text+emotion) 的哈希增量合成，走 IndexTTS-2.5 真实克隆音色（GPU 未就绪时回退占位音） |
| 混音 | `python cli.py mix --chapter 0001` | 场景级环境音 + 自动闪避（Ducking）+ 音效叠加，导出 `chapters/ch_0001/output/chapter_0001.mp3` |
| 状态 | `python cli.py status` | 查看各章节各阶段产物是否齐全，及是否存在"上游更新但下游未重跑"的陈旧状态 |
| 自检 | `python cli.py test --module {llm,tts,audio,all,dry-run}` | 隔离临时工作区跑通全链路（Mock 引擎，不依赖网络/GPU），用于快速回归验证 |

## 环境

```bash
# 主项目依赖（pydub/pyyaml/numpy/requests/pypinyin/pytest...）
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

真实 LLM 解析依赖本机 `llama-server`（OpenAI 兼容接口，见 `global_config.yaml`
的 `llm.api_base`）。真实 TTS 合成依赖 IndexTTS-2.5 独立环境，搭建步骤见
[`docs/indextts_setup.md`](docs/indextts_setup.md)（含 ROCm/AMD GPU 适配、
显存与 llama-server 互斥调度等注意事项）。两者均未就绪时，管线自动降级为
Mock 占位实现，保证 `cli.py test` 之类的自检不因外部依赖而失败。

## 角色管理

`roles/roles_manifest.json` 是全项目共享的角色清单。解析阶段遇到未注册的
新角色时，会按拼音自动生成角色 ID（如"苏砚" → `su_yan`）并注册（继承
narrator 的默认语速/音高、临时复用 narrator 的参考音频占位），随后可以
到 `roles/<role_id>/` 下手动替换更贴合角色气质的 `reference.wav` 和调整
`config.json` 的 `speed`/`pitch`。

## 配置

`global_config.yaml` 集中管理 LLM/TTS/混音参数，语义见文件内注释；不建议
在代码里硬编码这些数值。
