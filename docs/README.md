# 文档索引

入口是仓库根目录的 [`README.md`](../README.md)（命令、环境、配置）；面向 Agent 的操作规范见
[`CLAUDE.md`](../CLAUDE.md)。本目录放其余文档，分三类。

## 环境搭建

每个外部推理引擎都是独立 venv + 独立权重目录，路径通过 `config/local_config.yaml` 配置（见根 README 的「配置」）。

| 文档 | 内容 | 状态 |
|---|---|---|
| [`indextts_setup.md`](indextts_setup.md) | IndexTTS-2.5（人声合成）的 ROCm 独立环境、已知坑、**GPU 显存互斥机制（权威说明）**、种子参考音频 | 必需 |
| [`audioldm_setup.md`](audioldm_setup.md) | AudioLDM-S-Full-v2（环境音 ambience），CPU 推理；含从 ACE-Step 换引擎的诊断理由 | 当前默认 |
| [`audiogen_setup.md`](audiogen_setup.md) | TangoFlux（音效 sfx，当前默认）+ ACE-Step 1.5（环境音，已不是默认，仅保留作参考） | sfx 部分现行 |

## 系统说明

| 文档 | 内容 | 状态 |
|---|---|---|
| [`SYSTEM_FUNCTIONALITY_SUMMARY.md`](SYSTEM_FUNCTIONALITY_SUMMARY.md) | Web 界面逐页功能说明书 | **快照**：计划 006 交付时的状态，之后被 007/009/010 改动，部分章节已失效，以代码为准（详见该文件开头） |

> 目前没有独立的架构总览：`src/` 各模块的职责分散记录在计划 005（后端/任务队列）与 008（后端缺口）里。

## 阶段计划（`plan/`）

按 `NNN-kebab-case.md` 编号、时间递增，每份开头有「状态」引言块。依赖链：
**004 → 005 → 006 是三部曲，按序阅读**；008 承接 007 的 TODO，009 接入 008 的接口，010 是收尾清单
（同时是逐阶段的变更记录）。

| 计划 | 主题 | 状态 |
|---|---|---|
| [001](plan/001-speaker-embedding-precompute.md) | 角色音色 embedding 预计算 | 已实现 |
| [002](plan/002-gradio-management-ui.md) | Gradio 管理界面 | **已废弃**：模块已于 004 删除，仅作历史归档 |
| [003](plan/003-resident-tts-daemon.md) | 常驻 TTS 守护进程 + GPU 换手确认 | 已实现 |
| [004](plan/004-multi-novel-library.md) | 多小说数据层、拆旧界面、`parse` 语义变更（不再自动注册角色） | 已实现（三部曲之一） |
| [005](plan/005-task-queue-and-api.md) | 后台任务队列 + GPU 安全网 + FastAPI 后端 | 已实现（三部曲之二） |
| [006](plan/006-web-frontend.md) | Vue3 前端四页 | 已实现（三部曲之三） |
| [007](plan/007-design-refresh.md) | Modernist 换皮重构（红白配色 + 深色模式） | 已实现 |
| [008](plan/008-backend-gaps.md) | 补后端缺口：素材入混音 / 素材 CRUD / 角色分类树 | 已实现 |
| [009](plan/009-frontend-wiring.md) | 前端接入 008 的新接口 | 已实现 |
| [010](plan/010-remaining-work.md) | 12 阶段收尾清单 + 每阶段「实际改动」 | 执行中 |

计划文档是当时的设计与决策记录，**不随后续改动回改**；与代码不一致时以代码和较新的计划为准。
计划里出现的 `../dev-env/...` 相对路径是项目搬迁前的旧写法，现行路径为 `/srv/unsafe/dev-env/...`。
