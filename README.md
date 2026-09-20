# novel2audiobook

将小说文本转换为多角色、带情感/音效/环境音的有声书 MP3 的全本地离线流水线。

## 状态机

```
raw.txt → script_draft.json → script_final.json → 增量 TTS → timeline.json → <novel_id>_ch_XXXX.mp3
```

章节都在 `library/<novel_id>/chapters/ch_XXXX/` 下。所有命令都通过根目录唯一的入口脚本
`./run.sh` 执行（自动使用项目 venv，见「环境」）。

| 阶段 | 命令 | 说明 |
|---|---|---|
| 解析 | `./run.sh parse --novel <novel_id> --chapter 0001` | 调用本地 Qwen（llama-server）把 raw.txt 切分为带说话人/情感/音效的剧本 JSON；LLM 不可达时自动回退规则解析器。常驻 TTS 服务占着显存时会先询问是否释放（`--yes` 跳过） |
| 定稿 | 人工审阅 `script_draft.json`，另存为 `script_final.json` | 当前无自动定稿步骤，需人工确认剧本后手动复制/编辑；清单外说话人为 `speaker: null`，须先在配音工作台指派 |
| 合成 | `./run.sh tts --novel <novel_id> --chapter 0001` | 基于 MD5(speaker+text+emotion) 的哈希增量合成，走 IndexTTS-2.5 真实克隆音色（GPU 未就绪时回退占位音）。会自动停/起 llama-server 腾显存（`--yes` 跳过确认） |
| 混音 | `./run.sh mix --novel <novel_id> --chapter 0001` | 按 `mixing.voice_only`（默认 `true`）只导出旁白/角色人声成片到 `output/<novel_id>_ch_0001.mp3`；加 `--with-assets` 单次覆盖为「环境音 + 自动闪避 + 音效叠加」 |
| 状态 | `./run.sh status [--novel <novel_id>]` | 查看各章节各阶段产物是否齐全，及是否存在"上游更新但下游未重跑"的陈旧状态 |
| 自检 | `./run.sh test --module {llm,tts,audio,assets,all,dry-run}` | 隔离临时工作区跑通全链路（Mock 引擎，不依赖网络/GPU），用于快速回归验证 |
| 界面 | `./run.sh webui` | 启动 FastAPI + Vue3 管理界面（小说库、配音工作台、音效库、系统配置）。监听地址/端口取自 `config/global_config.yaml` 的 `server.host`/`server.port`（默认 `0.0.0.0:7860`，监听所有网卡；只想本机访问就把 `server.host` 改为 `127.0.0.1`），可用 `--host`/`--port` 覆盖 |
| 素材 | `./run.sh assets [list\|gen] [--kind ambience\|sfx] [--only a,b] [--force]` | 管理/按 `assets/asset_specs.yaml` 增量生成环境音与音效素材库 |
| 常驻 TTS | `./run.sh tts-serve {start,stop,status}` | 常驻 IndexTTS 推理服务（试听用，避免每次重载模型）；启动会与 llama-server 争抢显存 |
| 管理 | `./run.sh novel\|node\|chapter ...` | 小说/部卷/章节的增删改（见 `./run.sh --help`） |

## 环境

```bash
# 主项目依赖（pydub/pyyaml/numpy/requests/pypinyin/pytest...）
uv venv /srv/unsafe/dev-env/venvs/novel2audiobook --python 3.12
uv pip install --python /srv/unsafe/dev-env/venvs/novel2audiobook/bin/python -r requirements.txt
```

在 `config/local_config.yaml` 里把 `tools.project_python` 指向这个 venv 的 python 之后，直接用
根目录唯一的入口 `./run.sh <子命令>` 即可，它会自己找到 venv，无需先 activate、也不受当前目录影响。
需要交互式使用 venv 时：`source scripts/activate.sh`。

外部依赖（不在 `requirements.txt` 里，均通过配置指向）：

- **llama-server**（OpenAI 兼容接口）：真实 LLM 解析依赖它，地址见 `llm.api_base`。
  GPU 换手时用外部命令 `ai llm serve <name>` 拉起（命令名可配，见 `tools.llm_serve_command`），
  端口探测用 `ss`。
- **IndexTTS-2.5 独立环境**：真实 TTS 合成依赖，搭建见
  [`docs/indextts_setup.md`](docs/indextts_setup.md)（含 ROCm/AMD GPU 适配、显存与
  llama-server 互斥调度等注意事项）。
- **AudioLDM / TangoFlux / ACE-Step 独立环境**：环境音与音效生成，见
  [`docs/audioldm_setup.md`](docs/audioldm_setup.md)、[`docs/audiogen_setup.md`](docs/audiogen_setup.md)。
- **espeak-ng**：仅 `tools/generate_seed_reference.py` 生成种子参考音频时需要，路径见 `tools.espeak_*`。

这些依赖任一未就绪时，管线自动降级为规则/Mock 占位实现，保证 `./run.sh test` 之类的自检不因
外部依赖而失败；正式产出前请用 `./run.sh status` 和 `timeline.json` 里的
`tts_engine`/`used_fallback` 字段确认实际走的是哪个引擎。

## 角色管理

`roles/roles_manifest.json` 是全项目共享的角色清单。`parse` **不会**自动注册角色：
清单外的说话人会被标记为 `speaker: null`，需在配音工作台里指派（或新建角色）后才能执行
TTS。新角色的 `roles/<role_id>/` 下可手动替换更贴合角色气质的 `reference.wav` 并调整
`config.json` 的 `speed`/`pitch`。

## 配置

配置分两层，后者按键深合并覆盖前者：

| 文件 | 是否入库 | 放什么 |
|---|---|---|
| `config/global_config.yaml` | 是 | 与机器无关的参数：超时、采样率、混音、任务队列、引擎选型，语义见文件内注释 |
| `config/local_config.yaml` | **否**（gitignore） | 本机专属：主项目 venv（`tools.project_python`）与模型权重的绝对路径、外部命令、espeak 路径、`server.library_root` |

首次使用：`cp config/local_config.example.yaml config/local_config.yaml`，然后按本机环境修改其中的路径。
换机器只需改这一个文件。设置页（`PATCH /api/config`）改动的键会写回它所在的那一层，
文件里的注释原样保留。不要在代码里硬编码这些数值。

## 文档

全部文档（环境搭建、系统功能说明、阶段计划 001–010）的索引与阅读顺序见
[`docs/README.md`](docs/README.md)。面向 Agent 的操作规范见 [`CLAUDE.md`](CLAUDE.md)。
