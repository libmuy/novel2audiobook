# Agent 操作指南与规范

## 项目架构与原则
1. 本项目采用“公共资产顶级共享 + 章节隔离工作区”架构。
2. Agent 执行批处理前必须运行 `python cli.py test --all`。
3. 章节流转状态机：`raw.txt` -> `script_draft.json` -> `script_final.json` -> 增量 TTS 生成 -> `timeline.json` -> `chapter_XXXX.mp3`。
4. 不允许直接修改模型底层参数，所有操作必须通过 `python cli.py` 工具链执行。

## 命令行常用指令
- 查看所有章节状态：`python cli.py status`
- 解析文本为剧本初稿：`python cli.py parse --chapter 0001`
- 增量生成 TTS 与时间线：`python cli.py tts --chapter 0001`
- 多轨闪避混音生成 MP3：`python cli.py mix --chapter 0001`
- 执行全流程或单模块自检：`python cli.py test --module {llm,tts,audio,all,dry-run}`

以上 `python` 需为项目 venv（`.venv/bin/python`，或先 `source .venv/bin/activate`）；
依赖清单见 `requirements.txt`。

## 补充说明
- `parse` 依赖本机 llama-server（Qwen），`tts` 依赖独立部署的 IndexTTS-2.5
  环境（见 `docs/indextts_setup.md`）；任一未就绪时自动降级为规则/占位实现，
  不会中断管线，但产出质量会明显下降，正式产出前请用 `python cli.py status`
  和 `timeline.json` 里的 `tts_engine`/`used_fallback` 字段确认实际走的是哪个引擎。
- `tts` 阶段用 IndexTTS 合成时会通过 `tools/gpu_arbiter.py` 自动暂停/恢复
  llama-server（两者共用 GPU 显存，无法同时常驻），命令结束后会自动恢复
  llama-server，无需手动干预。
- 解析出的新角色（不在 `roles/roles_manifest.json` 里）会被自动注册（拼音生成
  角色 ID、继承 narrator 默认音色作占位），可事后到 `roles/<role_id>/` 下替换
  更贴合的 `reference.wav`。
- `cli.py test` 全程在隔离临时目录运行，不会污染 `chapters/`、`roles/` 等共享目录。
