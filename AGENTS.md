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
