# novel2audiobook

千万字全本地离线有声书生成流水线，专为 LLM / AI Agent 自动化工程与协作设计。

---

## 1. 项目概述

`novel2audiobook` 是一个全本地离线运行的有声书自动化生成系统。系统采用“公共资产顶级共享 + 章节隔离工作区”架构，支持将小说原始文本自动化转换为带有角色音色、情感表达、背景音乐 (BGM) 与音效 (SFX) 的多轨有声剧 MP3 成品。

### 核心亮点
- **模块化流水线**：剧本解析、TTS 合成与音频混音三阶段解耦。
- **Agent 友好架构**：专为 Agent 自动化调用设计，状态清晰可检测，具备完整的自检指令集。
- **哈希增量 TTS 缓存**：基于 `MD5(speaker + text + emotion)` 实现句子级增量合成，极大地节省重复生成开销。
- **自动多轨闪避混音 (Audio Ducking)**：基于人声 RMS 响度检测，在台词播放时自动压低 BGM 音量，并在特定时间戳叠加 SFX 音效。

---

## 2. 核心技术栈

- **语言环境**：Python 3.8+
- **音频处理**：`pydub` (音频拼接/混音/闪避/导出), `pedalboard`, `wave`
- **数值计算与工具**：`numpy`, `tqdm`, `pyyaml`
- **LLM 剧本解析**：支持本地部署大模型 (默认配置 Qwen3-8B)
- **TTS 合成引擎**：支持本地 TTS 引擎 (默认配置 Index-TTS-2.5)

---

## 3. 项目架构与目录结构

系统采用 **顶级公共资产 + 章节独立工作区** 的设计，避免不同章节间的数据污染。

```
novel2audiobook/
├── assets/                  # 顶级公共音频资产库
│   ├── ambience/            # 环境背景音 (BGM/Ambience, 如 rain_heavy.wav)
│   └── sfx/                 # 瞬时音效 (SFX, 如 sword_clash.wav)
├── roles/                   # 角色音色库与元数据
│   ├── narrator/            # 旁白音色库
│   ├── lin_dong/            # 角色音色库 (如 林动)
│   └── roles_manifest.json  # 角色配置清单
├── chapters/                # 章节工作区 (章节隔离)
│   └── ch_0001/             # 单章节工作区 (格式: ch_XXXX)
│       ├── .status.json     # 章节进度状态标记文件
│       ├── raw.txt          # 原始小说文本
│       ├── script_draft.json# LLM 解析的剧本初稿
│       ├── script_final.json# 校验/精修后的定稿剧本 (TTS 必选)
│       ├── timeline.json    # TTS 生成时间线与元数据
│       ├── audio_cache/     # 哈希缓存的单句 TTS wav 音频文件
│       └── output/          # 成品音频输出目录 (如 ch_0001.mp3)
├── src/                     # 核心源码模块
│   ├── utils.py             # 通用工具函数 (MD5, 配置加载, ID 规范化)
│   ├── status_tracker.py    # 章节状态扫描与表格打印
│   ├── llm_parser.py        # LLM 文本解析为剧本结构
│   ├── tts_engine.py        # 哈希增量 TTS 推理引擎
│   └── audio_mixer.py       # 多轨 Audio Ducking 闪避混音引擎
├── cli.py                   # 统一命令行 CLI 入口
├── global_config.yaml       # 全局配置文件 (LLM / TTS / Mixing 参数)
├── requirements.txt         # 项目依赖列表
├── AGENTS.md                # Agent 操作指南与规范
└── README.md                # 项目文档
```

---

## 4. 章节流转状态机 (State Machine)

每个章节在生成过程中遵循严格的状态演进逻辑：

```
[ raw.txt ]
    │
    ▼ (python cli.py parse --chapter XXXX)
[ script_draft.json ]  (解析初稿)
    │
    ▼ (人工 / Agent 确认或调整)
[ script_final.json ]  (定稿剧本)
    │
    ▼ (python cli.py tts --chapter XXXX)
[ audio_cache/*.wav ] + [ timeline.json ]  (增量合成与时间线生成)
    │
    ▼ (python cli.py mix --chapter XXXX)
[ output/ch_XXXX.mp3 ]  (闪避混音成品)
```

> **注意**：在调用 `tts` 指令前，对应章节目录下**必须**存在 `script_final.json`，否则处理将中止并报错。

---

## 5. CLI 工具链与 Agent 操作规范

所有操作均通过根目录下的 `cli.py` 执行。

### 常用命令列表

1. **查看所有章节状态**
   ```bash
   python cli.py status
   ```
   打印当前 `chapters/` 下所有章节的文件存在情况（Raw, Draft, Final, Timeline, Audio, Cache Files）及 `.status.json` 中的状态。

2. **文本解析（生成剧本初稿）**
   ```bash
   python cli.py parse --chapter 0001
   ```
   读取 `chapters/ch_0001/raw.txt`，调用 LLM 生成 `script_draft.json`。

3. **增量 TTS 生成（合成音轨与时间线）**
   ```bash
   python cli.py tts --chapter 0001
   ```
   基于 `script_final.json` 执行增量 TTS 合成，并在章节目录下生成 `timeline.json` 与 `audio_cache/` 文件。

4. **多轨闪避混音导出**
   ```bash
   python cli.py mix --chapter 0001
   ```
   根据 `timeline.json` 自动合成人声轨、叠加 SFX 音效轨与带有闪避 (Ducking) 效果的 BGM 轨，导出至 `output/ch_0001.mp3`。

5. **系统自检指令**
   ```bash
   python cli.py test --module {llm,tts,audio,all,dry-run}
   # 或全模块自检：
   python cli.py test --all
   ```

### Agent 行为规范与约束
- **批处理前必自检**：Agent 在执行批处理任务前，必须首先运行 `python cli.py test --all` 确认系统各模块工作正常。
- **禁止修改底层参数**：不得在代码中直接修改模型底层参数，所有配置修改均需通过修改 `global_config.yaml` 或使用 CLI 工具链。
- **目录隔离**：不要跨章节修改或移动文件，严格遵守 `chapters/ch_XXXX/` 隔离规范。

---

## 6. 核心机制实现原理

### 6.1 句段级剧本结构 (`script_draft.json` / `script_final.json`)
由 LLM 解析出的剧本数据结构如下：
```json
[
  {
    "seg_id": 1,
    "speaker": "lin_dong",
    "text": "林动，今日之辱，我必加倍奉还！",
    "emotion": "angry",
    "sfx": "sword_clash",
    "bgm": "rain_heavy"
  }
]
```

### 6.2 哈希增量 TTS 合成
- 每句台词生成计算哈希键：`hash_key = MD5(speaker + "_" + text + "_" + emotion)`
- 缓存文件名：`audio_cache/{hash_key}.wav`
- 如果缓存文件已存在，直接跳过 TTS 生成过程，实现句段级无损秒级复用。

### 6.3 动态 Audio Ducking (多轨闪避)
- **阈值检测**：检测人声轨片段的 RMS 响度，当响度超过 `ducking_threshold` (默认 -20.0 dBFS) 时标记为说话区间。
- **自动衰减**：在说话区间内，背景音乐 (BGM) 按 `ducking_volume_ratio` (默认 30%) 进行衰减，营造清晰的人声听感与沉沉浸式背景氛围。
- **音效叠加**：指定时间戳无缝 Overlay 匹配的 SFX 音效文件。
