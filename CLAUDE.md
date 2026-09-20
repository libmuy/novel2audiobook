# Agent 操作指南与规范

## 项目架构与原则
1. 本项目采用“公共资产顶级共享 + 章节隔离工作区”架构。
2. Agent 执行批处理前必须运行 `./run.sh test --all`。
3. 章节流转状态机：`raw.txt` -> `script_draft.json` -> `script_final.json` -> 增量 TTS 生成 -> `timeline.json` -> `chapter_XXXX.mp3`。
4. 不允许直接修改模型底层参数，所有操作必须通过 `./run.sh` 工具链执行。

## 命令行常用指令
- 查看小说/章节状态：`./run.sh status [--novel <novel_id>]`
- 解析文本为剧本初稿：`./run.sh parse --novel <novel_id> --chapter 0001`
  （若常驻 TTS 服务正占着显存，交互式终端下会先问是否释放，`--yes` 跳过确认）
- 增量生成 TTS 与时间线：`./run.sh tts --novel <novel_id> --chapter 0001`
  （会自动停/起 llama-server 腾显存，交互式终端下先告知一声，`--yes` 跳过确认）
- 多轨闪避混音生成 MP3：`./run.sh mix --novel <novel_id> --chapter 0001`
  （当前 `config/global_config.yaml` 的 `mixing.voice_only` 默认 `true`，只出旁白/
  角色人声成片，不叠加环境音/音效；效果音流水线待后续阶段再打通，需要时加
  `--with-assets` 单次覆盖）
- 执行全流程或单模块自检：`./run.sh test --module {llm,tts,audio,assets,all,dry-run}`（`--all` 同 `--module all`）
- 管理常驻 IndexTTS 推理服务（交互式试听用，避免每次都重新加载模型）：
  `./run.sh tts-serve {start,stop,status}`；启动会与 llama-server
  争抢显存，交互式终端下默认会先询问换手确认（`--yes` 跳过）
- 小说/章节管理：`./run.sh novel|node|chapter ...`（见 `./run.sh --help`）

`./run.sh` 是项目根目录唯一的入口脚本，会自己定位项目 venv 的 python（顺序：`N2A_PYTHON` 环境变量 >
已激活的 venv > `config/local_config.yaml` 的 `tools.project_python` > `./.venv`），任意 CWD 均可调用，
无需先 activate；依赖清单见 `requirements.txt`。CLI 实现在 `src/cli.py`，不要直接 `python src/cli.py`。

## 配置
- 配置分两层：`config/global_config.yaml`（入库，与机器无关的参数）+ `config/local_config.yaml`
  （gitignore，本机专属的 venv/权重绝对路径、外部命令、espeak 路径，模板见
  `config/local_config.example.yaml`），后者按键深合并覆盖前者。换机器只改 local。
- 目录约定：根目录只放 `run.sh`（唯一入口）、`README.md`/`CLAUDE.md`/`AGENTS.md`、`requirements.txt`；
  配置在 `config/`，辅助脚本在 `scripts/`（如 `scripts/activate.sh`），CLI 代码在 `src/`。
- 不要往代码里写死路径/端口；路径类配置缺失时应报错或降级，不要回落到某台机器的字面量。
- 文档索引见 `docs/README.md`（环境搭建、系统功能说明、阶段计划 001–010）。

## 补充说明
- `parse` 依赖本机 llama-server（Qwen），`tts` 依赖独立部署的 IndexTTS-2.5
  环境（见 `docs/indextts_setup.md`）；任一未就绪时自动降级为规则/占位实现，
  不会中断管线，但产出质量会明显下降，正式产出前请用 `./run.sh status`
  和 `timeline.json` 里的 `tts_engine`/`used_fallback` 字段确认实际走的是哪个引擎。
- `tts` 阶段用 IndexTTS 合成时会通过 `tools/gpu_arbiter.py` 自动暂停/恢复
  llama-server（两者共用 GPU 显存，无法同时常驻），命令结束后会自动恢复
  llama-server，无需手动干预。
- `./run.sh test` 全程在隔离临时目录运行，不会污染 `library/`、`roles/` 等共享目录。
- `parse` 不再自动注册角色：清单外说话人会被标记为 `speaker: null`，
  需人工在配音工作台指派后才能执行 TTS。
- Web 界面 = FastAPI + Vue3 全局构建（`web/static/js/app.js`，模板字符串组件，无构建工具、模板内禁嵌套反引号、离线运行）；旧 Gradio/Flask 界面已于计划 005/006 移除。
