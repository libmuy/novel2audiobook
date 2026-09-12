"""
Gradio 全流程管理界面（计划 002）。

设计原则：所有业务逻辑写成不依赖 Gradio 的纯函数（本文件上半部分），
`build_app()` 只是把这些函数薄薄地包装成 Gradio 事件回调（下半部分）。
这样单元测试可以直接调用纯函数，不需要真的起 Gradio server。

与已有模块的分工：
- 章节流水线（parse/tts/mix）复用 src.llm_parser / src.tts_engine / src.audio_mixer
  的既有函数，不重新实现。
- 角色管理（增删/参考音频/embedding 预计算）复用计划 001 落地的 src.roles API。
- TTS 试听复用计划 003 的 src.tts_daemon.IndexTTSDaemon（常驻服务），不直接调
  IndexTTSBackend（那条路径每次都要重新加载模型）。
- 显存占用展示与换手确认复用 tools.gpu_arbiter 的 get_current_owner()/plan_swap()。
- src/web_server.py（只读浏览服务）保持不动，两者共存，端口不冲突
  （serve 默认 8090，webui 默认 7860）。

GPU 互斥：仪表盘的「全部解析/全部 TTS」、角色管理的「预计算 Embedding」、章节管理的
单章 parse/tts、TTS 试听的启动常驻服务/生成试听——这些事件统一打上
concurrency_id="gpu", concurrency_limit=1，让 Gradio 的队列把它们强制串行化，
不需要自己手写 threading.Lock；不碰 GPU 的操作（刷新列表、混音、查看文件）不受影响。
"""
import json
import os
import tempfile
from datetime import datetime

import gradio as gr

from src.utils import get_chapter_dir, resolve_path, load_global_config
from src import roles as roles_mod
from src.llm_parser import process_chapter_parse
from src.tts_engine import process_chapter_tts, EMOTION_TO_VECTOR
from src.audio_mixer import mix_chapter
from src.status_tracker import get_all_chapters_status
from src.tts_daemon import IndexTTSDaemon
from tools import gpu_arbiter
from tools.gpu_arbiter import LlmSuspendedForGpu

# --------------------------------------------------------------------------
# 路径解析小工具：所有对外暴露的函数都接受可选的 roles_dir/chapters_dir，
# 未传时才落到真实项目目录——这是测试能用隔离 tmp_path 而不污染真实数据的关键。
# --------------------------------------------------------------------------

def _chapters_base_dir(chapters_dir: str = None) -> str:
    return chapters_dir or resolve_path("chapters")


def _roles_base_dir(roles_dir: str = None) -> str:
    return roles_dir or resolve_path("roles")


# --------------------------------------------------------------------------
# 显存状态 / 换手前置检查
# --------------------------------------------------------------------------

def get_gpu_status_text() -> str:
    """页面顶部展示当前显存占用方；纯只读探测，不做任何写操作"""
    owner = gpu_arbiter.get_current_owner()
    return {
        gpu_arbiter.OWNER_LLM: "🟢 当前显存占用：LLM（llama-server）",
        gpu_arbiter.OWNER_TTS: "🔵 当前显存占用：常驻 TTS 服务",
    }.get(owner, "⚪ 当前显存空闲")


def check_daemon_not_blocking() -> tuple:
    """
    parse（依赖 llama-server）、批量/单章 tts、预计算 embedding（各自起一个独立的
    IndexTTS 子进程）都不能跟常驻 TTS 服务同时占用同一张卡。执行前统一做这个检查。
    返回 (是否可以继续, 提示文案)；tts/precompute 这类子进程自己会通过
    LlmSuspendedForGpu 处理 llama-server 的停/起，这里不重复处理。
    """
    if gpu_arbiter.get_current_owner() == gpu_arbiter.OWNER_TTS:
        return False, "⚠️ 常驻 TTS 服务正占用显存，请先在「TTS 试听」页释放显存后再试"
    return True, ""


# --------------------------------------------------------------------------
# Tab 1：仪表盘
# --------------------------------------------------------------------------

DASHBOARD_HEADERS = ["章节", "状态", "Raw", "Draft", "Final", "Timeline", "MP3", "Cache 数"]


def dashboard_rows(chapters_dir: str = None) -> list:
    rows = get_all_chapters_status(chapters_dir=_chapters_base_dir(chapters_dir))

    def _mark(v):
        return "✓" if v else "✗"

    return [
        [r["chapter_id"], r["status"], _mark(r["raw"]), _mark(r["draft"]), _mark(r["final"]),
         _mark(r["timeline"]), _mark(r["mp3"]), r["audio_cache_count"]]
        for r in rows
    ]


def list_chapter_ids(chapters_dir: str = None) -> list:
    base = _chapters_base_dir(chapters_dir)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))


def run_parse_one(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return "请先选择章节"
    ok, msg = check_daemon_not_blocking()
    if not ok:
        return msg
    try:
        draft_path = process_chapter_parse(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
        return f"✓ [{ch_id}] 解析完成：{draft_path}"
    except Exception as e:  # noqa: BLE001 - 界面操作要把任何异常转成可读文案，不能整个界面崩掉
        return f"✗ [{ch_id}] 解析失败：{e}"


def run_tts_one(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return "请先选择章节"
    ok, msg = check_daemon_not_blocking()
    if not ok:
        return msg
    try:
        timeline_path = process_chapter_tts(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
        return f"✓ [{ch_id}] TTS 完成：{timeline_path}"
    except Exception as e:  # noqa: BLE001
        return f"✗ [{ch_id}] TTS 失败：{e}"


def run_mix_one(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return "请先选择章节"
    try:
        out_path = mix_chapter(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
        return f"✓ [{ch_id}] 混音完成（当前纯人声）：{out_path}"
    except Exception as e:  # noqa: BLE001
        return f"✗ [{ch_id}] 混音失败：{e}"


def run_parse_all(chapters_dir: str = None):
    """生成器：逐章解析，累积日志逐步 yield，供 Gradio 流式展示进度"""
    ok, msg = check_daemon_not_blocking()
    if not ok:
        yield msg
        return
    lines = [msg] if msg else []
    ch_ids = list_chapter_ids(chapters_dir)
    if not ch_ids:
        yield "未发现任何章节工作区"
        return
    yield "\n".join(lines + ["开始解析…"])
    for ch_id in ch_ids:
        try:
            process_chapter_parse(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
            lines.append(f"✓ {ch_id} 解析完成")
        except Exception as e:  # noqa: BLE001 - 单章失败不应中断整批
            lines.append(f"✗ {ch_id} 解析失败：{e}")
        yield "\n".join(lines)
    lines.append("全部解析完成。")
    yield "\n".join(lines)


def run_tts_all(chapters_dir: str = None):
    ok, msg = check_daemon_not_blocking()
    if not ok:
        yield msg
        return
    lines = [msg] if msg else []
    ch_ids = list_chapter_ids(chapters_dir)
    if not ch_ids:
        yield "未发现任何章节工作区"
        return
    lines.append("开始 TTS 合成（如 llama-server 正在运行会自动暂停，完成后恢复）…")
    yield "\n".join(lines)
    for ch_id in ch_ids:
        try:
            process_chapter_tts(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
            lines.append(f"✓ {ch_id} TTS 完成")
        except Exception as e:  # noqa: BLE001
            lines.append(f"✗ {ch_id} TTS 失败：{e}")
        yield "\n".join(lines)
    lines.append("全部 TTS 合成完成。")
    yield "\n".join(lines)


def run_mix_all(chapters_dir: str = None):
    lines = ["开始混音（当前纯人声，不含环境音/音效）…"]
    ch_ids = list_chapter_ids(chapters_dir)
    if not ch_ids:
        yield "未发现任何章节工作区"
        return
    yield "\n".join(lines)
    for ch_id in ch_ids:
        try:
            mix_chapter(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)))
            lines.append(f"✓ {ch_id} 混音完成")
        except Exception as e:  # noqa: BLE001
            lines.append(f"✗ {ch_id} 混音失败：{e}")
        yield "\n".join(lines)
    lines.append("全部混音完成。")
    yield "\n".join(lines)


# --------------------------------------------------------------------------
# Tab 2：角色管理
# --------------------------------------------------------------------------

ROLE_HEADERS = ["角色 ID", "中文名", "性别", "Embedding 状态", "说明"]


def _embedding_badge(status: dict) -> str:
    if status["valid"]:
        return "✅ 有效" + (f"（{status['created_at']}）" if status.get("created_at") else "")
    if status["exists"]:
        return f"⚠️ {status['stale_reason']}"
    return "◯ 未预计算"


def role_rows(roles_dir: str = None) -> list:
    manifest = roles_mod.load_manifest(roles_dir)
    rows = []
    for role_id, info in manifest.get("roles", {}).items():
        status = roles_mod.get_embedding_status(role_id, manifest, roles_dir)
        rows.append([role_id, info.get("name", role_id), info.get("gender", "unknown"),
                     _embedding_badge(status), info.get("description", "")])
    return rows


def role_choices(roles_dir: str = None) -> list:
    manifest = roles_mod.load_manifest(roles_dir)
    return list(manifest.get("roles", {}).keys())


def get_role_detail(role_id: str, roles_dir: str = None) -> dict:
    """供角色详情面板展示：参考音频路径、speed、embedding 状态文案"""
    if not role_id:
        return {"reference_audio": None, "speed": 1.0, "embedding_text": ""}
    manifest = roles_mod.load_manifest(roles_dir)
    if role_id not in manifest.get("roles", {}):
        return {"reference_audio": None, "speed": 1.0, "embedding_text": "角色不存在"}
    cfg = roles_mod.get_role_runtime_config(role_id, manifest, roles_dir)
    status = roles_mod.get_embedding_status(role_id, manifest, roles_dir)
    ref_audio = cfg["reference_audio"] if os.path.exists(cfg["reference_audio"]) else None
    return {"reference_audio": ref_audio, "speed": cfg.get("speed", 1.0), "embedding_text": _embedding_badge(status)}


def save_role_speed(role_id: str, speed: float, roles_dir: str = None) -> str:
    """更新 roles/<role_id>/config.json 的 speed 字段（预计算/合成读的是同一份文件）"""
    if not role_id:
        return "请先选择角色"
    base = _roles_base_dir(roles_dir)
    cfg_path = os.path.join(base, role_id, "config.json")
    if not os.path.exists(cfg_path):
        return f"✗ 角色 {role_id} 的 config.json 不存在"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["speed"] = float(speed)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return f"✓ 已保存 {role_id} 的语速为 {speed}"


def upload_role_reference(role_id: str, wav_path: str, roles_dir: str = None) -> str:
    if not role_id:
        return "请先选择角色"
    if not wav_path:
        return "未选择音频文件"
    manifest = roles_mod.load_manifest(roles_dir)
    try:
        roles_mod.set_role_reference(role_id, wav_path, manifest, roles_dir)
    except (ValueError, FileNotFoundError) as e:
        return f"✗ {e}"
    return f"✓ 已更新 {role_id} 的参考音频（旧的 embedding 缓存已失效）"


def add_role(chinese_name: str, gender: str, roles_dir: str = None) -> str:
    chinese_name = (chinese_name or "").strip()
    if not chinese_name:
        return "✗ 请输入角色中文名"
    manifest = roles_mod.load_manifest(roles_dir)
    try:
        role_id = roles_mod.register_role(chinese_name, manifest, roles_dir, gender=gender or "unknown")
    except Exception as e:  # noqa: BLE001
        return f"✗ 新增失败：{e}"
    return f"✓ 已新增角色 {chinese_name}（ID: {role_id}）"


def remove_role(role_id: str, roles_dir: str = None) -> str:
    if not role_id:
        return "请先选择角色"
    manifest = roles_mod.load_manifest(roles_dir)
    try:
        roles_mod.delete_role(role_id, manifest, roles_dir)
    except ValueError as e:
        return f"✗ {e}"
    return f"✓ 已删除角色 {role_id}"


def precompute_role_embedding(role_id: str, roles_dir: str = None, config: dict = None) -> str:
    """
    预计算 embedding：这是一次真实的 GPU 推理（独立 IndexTTS 子进程），src.roles.
    precompute_embedding 本身不做 GPU 仲裁，这里用 LlmSuspendedForGpu 包一层，
    行为与 cli.py 的批量 tts 命令一致（自动停/起 llama-server，仅记日志不二次确认——
    真正需要二次确认的是「切到持续状态」的常驻服务，见 TTS 试听 Tab）。
    """
    if not role_id:
        return "请先选择角色"
    ok, msg = check_daemon_not_blocking()
    if not ok:
        return msg
    if config is None:
        config = load_global_config()
    manifest = roles_mod.load_manifest(roles_dir)
    with LlmSuspendedForGpu(config):
        result = roles_mod.precompute_embedding(role_id, manifest, roles_dir, config=config, force=True)
    if result["ok"]:
        return f"✓ {role_id} 的 embedding 预计算完成"
    return f"✗ 预计算失败：{result['error']}"


# --------------------------------------------------------------------------
# Tab 3：章节管理
# --------------------------------------------------------------------------

def read_chapter_raw_text(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return ""
    path = os.path.join(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)), "raw.txt")
    if not os.path.exists(path):
        return "（raw.txt 不存在）"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_chapter_final_json_text(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return ""
    path = os.path.join(get_chapter_dir(ch_id, base_dir=_chapters_base_dir(chapters_dir)), "script_final.json")
    if not os.path.exists(path):
        return "（script_final.json 不存在——请先确认 script_draft.json 后手动创建）"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return json.dumps(data, ensure_ascii=False, indent=2)


def get_chapter_status_text(ch_id: str, chapters_dir: str = None) -> str:
    if not ch_id:
        return ""
    rows = [r for r in get_all_chapters_status(_chapters_base_dir(chapters_dir)) if r["chapter_id"] == ch_id]
    return f"状态：{rows[0]['status']}" if rows else "状态：未知"


# --------------------------------------------------------------------------
# Tab 4：TTS 试听（常驻服务）
# --------------------------------------------------------------------------

def emotion_choices() -> list:
    return list(EMOTION_TO_VECTOR.keys())


def get_daemon_status_text() -> str:
    return "🔵 常驻 TTS 服务：运行中" if IndexTTSDaemon().is_running() else "⚪ 常驻 TTS 服务：未运行"


def plan_daemon_start() -> dict:
    """返回 gpu_arbiter.plan_swap('tts')，供 UI 决定是否需要弹二次确认"""
    return gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)


def start_daemon() -> str:
    """真正执行启动（调用方已经在需要时完成了用户确认）"""
    result = IndexTTSDaemon().ensure_started()
    if result["ok"]:
        return "常驻 TTS 服务已经在运行" if result["already_running"] else "✓ 常驻 TTS 服务已启动"
    return f"✗ 启动失败：{result['error']}"


def release_gpu_to_llm() -> str:
    result = IndexTTSDaemon().shutdown()
    if result["ok"]:
        return "✓ 已释放显存并恢复 llama-server" if result["was_running"] else "当前没有运行中的常驻 TTS 服务"
    return f"✗ 释放失败：{result['error']}"


def preview_tts(role_id: str, text: str, emotion: str, roles_dir: str = None) -> tuple:
    """
    单句试听：需要常驻服务已启动（由调用方先走确认/启动流程），合成一句到临时文件。
    返回 (wav_path 或 None, 状态文案)。
    """
    if not role_id:
        return None, "请先选择角色"
    text = (text or "").strip()
    if not text:
        return None, "请输入试听文字"

    daemon = IndexTTSDaemon()
    if not daemon.is_running():
        return None, "常驻 TTS 服务尚未启动，请先点击上方「启动常驻服务」"

    manifest = roles_mod.load_manifest(roles_dir)
    role_cfg = roles_mod.get_role_runtime_config(role_id, manifest, roles_dir)
    out_dir = os.path.join(tempfile.gettempdir(), "n2a_webui_preview")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"preview_{role_id}_{datetime.now().strftime('%H%M%S%f')}.wav")

    job = {"id": "preview", "text": text, "role_cfg": role_cfg, "emotion": emotion,
           "out": out_path, "sample_rate": 24000}
    try:
        results = daemon.synthesize_batch([job])
    except Exception as e:  # noqa: BLE001
        return None, f"✗ 合成失败：{e}"
    if not results.get("preview"):
        return None, "✗ 合成失败（未生成音频）"
    return out_path, "✓ 合成完成"


# --------------------------------------------------------------------------
# UI 构建
# --------------------------------------------------------------------------

_GPU_CONCURRENCY = {"concurrency_id": "gpu", "concurrency_limit": 1}


def _dashboard_tab(chapters_dir, gpu_status_box):
    with gr.Tab("仪表盘"):
        gr.Markdown("### 章节状态总表")
        table = gr.Dataframe(headers=DASHBOARD_HEADERS, value=dashboard_rows(chapters_dir), interactive=False)
        refresh_btn = gr.Button("刷新")
        with gr.Row():
            parse_all_btn = gr.Button("全部解析")
            tts_all_btn = gr.Button("全部 TTS")
            mix_all_btn = gr.Button("全部混音（纯人声，不含环境音/音效）")
        log_box = gr.Textbox(label="操作日志", lines=12, interactive=False)

        refresh_btn.click(lambda: dashboard_rows(chapters_dir), outputs=table)

        parse_all_btn.click(
            lambda: run_parse_all(chapters_dir), outputs=log_box, **_GPU_CONCURRENCY
        ).then(lambda: dashboard_rows(chapters_dir), outputs=table)

        tts_all_btn.click(
            lambda: run_tts_all(chapters_dir), outputs=log_box, **_GPU_CONCURRENCY
        ).then(lambda: dashboard_rows(chapters_dir), outputs=table
        ).then(lambda: get_gpu_status_text(), outputs=gpu_status_box)

        mix_all_btn.click(
            lambda: run_mix_all(chapters_dir), outputs=log_box
        ).then(lambda: dashboard_rows(chapters_dir), outputs=table)


def _roles_tab(roles_dir):
    with gr.Tab("角色管理"):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown("### 角色列表")
                role_table = gr.Dataframe(headers=ROLE_HEADERS, value=role_rows(roles_dir), interactive=False)
                refresh_list_btn = gr.Button("刷新列表")
            with gr.Column(scale=1):
                gr.Markdown("### 角色详情")
                role_dropdown = gr.Dropdown(choices=role_choices(roles_dir), label="选择角色")
                ref_audio = gr.Audio(label="参考音频（试听）", type="filepath", interactive=False)
                speed_slider = gr.Slider(0.5, 2.0, value=1.0, step=0.05, label="语速 speed")
                save_speed_btn = gr.Button("保存语速")
                embedding_text = gr.Markdown("")
                precompute_btn = gr.Button("预计算 Embedding（占用 GPU）")
                gr.Markdown("上传新的参考音频（会使旧的 embedding 缓存失效）：")
                upload_audio = gr.Audio(label="上传 reference.wav", type="filepath")
                upload_btn = gr.Button("替换参考音频")
                role_msg = gr.Markdown("")

        with gr.Row():
            with gr.Column():
                gr.Markdown("### 新增角色")
                new_name = gr.Textbox(label="中文名")
                new_gender = gr.Dropdown(choices=["male", "female", "unknown"], value="unknown", label="性别")
                add_btn = gr.Button("新增")
            with gr.Column():
                gr.Markdown("### 删除角色（narrator 不可删除）")
                delete_btn = gr.Button("删除当前选中角色", variant="stop")

        def _on_select_role(role_id):
            detail = get_role_detail(role_id, roles_dir)
            return detail["reference_audio"], detail["speed"], detail["embedding_text"]

        def _refresh_role_list():
            return role_rows(roles_dir), gr.update(choices=role_choices(roles_dir))

        role_dropdown.change(_on_select_role, inputs=role_dropdown,
                              outputs=[ref_audio, speed_slider, embedding_text])
        refresh_list_btn.click(_refresh_role_list, outputs=[role_table, role_dropdown])

        save_speed_btn.click(
            lambda rid, sp: save_role_speed(rid, sp, roles_dir), inputs=[role_dropdown, speed_slider], outputs=role_msg
        )

        precompute_btn.click(
            lambda rid: precompute_role_embedding(rid, roles_dir), inputs=role_dropdown, outputs=role_msg,
            **_GPU_CONCURRENCY,
        ).then(lambda rid: get_role_detail(rid, roles_dir)["embedding_text"], inputs=role_dropdown, outputs=embedding_text)

        upload_btn.click(
            lambda rid, wav: upload_role_reference(rid, wav, roles_dir), inputs=[role_dropdown, upload_audio], outputs=role_msg
        ).then(lambda rid: get_role_detail(rid, roles_dir)["embedding_text"], inputs=role_dropdown, outputs=embedding_text)

        add_btn.click(
            lambda name, gender: add_role(name, gender, roles_dir), inputs=[new_name, new_gender], outputs=role_msg
        ).then(_refresh_role_list, outputs=[role_table, role_dropdown])

        delete_btn.click(
            lambda rid: remove_role(rid, roles_dir), inputs=role_dropdown, outputs=role_msg
        ).then(_refresh_role_list, outputs=[role_table, role_dropdown])


def _chapters_tab(chapters_dir):
    with gr.Tab("章节管理"):
        ch_dropdown = gr.Dropdown(choices=list_chapter_ids(chapters_dir), label="选择章节")
        refresh_ch_btn = gr.Button("刷新章节列表")
        status_text = gr.Markdown("")
        with gr.Row():
            raw_box = gr.Textbox(label="raw.txt（只读）", lines=15, interactive=False)
            final_box = gr.Code(label="script_final.json（只读）", language="json", interactive=False)
        with gr.Row():
            parse_btn = gr.Button("解析（parse）")
            tts_btn = gr.Button("TTS 合成")
            mix_btn = gr.Button("混音（纯人声）")
        op_msg = gr.Markdown("")

        def _on_select_chapter(ch_id):
            return (get_chapter_status_text(ch_id, chapters_dir),
                    read_chapter_raw_text(ch_id, chapters_dir),
                    read_chapter_final_json_text(ch_id, chapters_dir))

        ch_dropdown.change(_on_select_chapter, inputs=ch_dropdown, outputs=[status_text, raw_box, final_box])
        refresh_ch_btn.click(lambda: gr.update(choices=list_chapter_ids(chapters_dir)), outputs=ch_dropdown)

        parse_btn.click(
            lambda ch: run_parse_one(ch, chapters_dir), inputs=ch_dropdown, outputs=op_msg, **_GPU_CONCURRENCY
        ).then(_on_select_chapter, inputs=ch_dropdown, outputs=[status_text, raw_box, final_box])

        tts_btn.click(
            lambda ch: run_tts_one(ch, chapters_dir), inputs=ch_dropdown, outputs=op_msg, **_GPU_CONCURRENCY
        ).then(_on_select_chapter, inputs=ch_dropdown, outputs=[status_text, raw_box, final_box])

        mix_btn.click(
            lambda ch: run_mix_one(ch, chapters_dir), inputs=ch_dropdown, outputs=op_msg
        ).then(_on_select_chapter, inputs=ch_dropdown, outputs=[status_text, raw_box, final_box])


def _tts_preview_tab(roles_dir, gpu_status_box):
    with gr.Tab("TTS 试听"):
        gr.Markdown("常驻 TTS 服务会独占显存、与 llama-server 互斥，启动前请先确认。")
        daemon_status = gr.Markdown(get_daemon_status_text())
        with gr.Row():
            start_btn = gr.Button("启动常驻服务")
            release_btn = gr.Button("释放显存 / 关闭常驻服务")
        confirm_info = gr.Markdown(visible=False)
        with gr.Row(visible=False) as confirm_row:
            confirm_btn = gr.Button("确认启动", variant="primary")
            cancel_btn = gr.Button("取消")

        gr.Markdown("---")
        role_dropdown = gr.Dropdown(choices=role_choices(roles_dir), label="角色")
        text_box = gr.Textbox(label="试听文字", lines=3)
        emotion_dropdown = gr.Dropdown(choices=emotion_choices(), value="neutral", label="情感")
        gen_btn = gr.Button("生成并播放")
        audio_out = gr.Audio(label="试听结果")
        gen_msg = gr.Markdown("")

        def _on_click_start():
            plan = plan_daemon_start()
            if plan["noop"]:
                msg = start_daemon()
                return gr.update(visible=False), gr.update(visible=False), msg, get_daemon_status_text(), get_gpu_status_text()
            eta = f"，历史约 {plan['estimated_seconds']:.0f} 秒" if plan["estimated_seconds"] else "（尚无历史耗时数据）"
            info = f"将执行：{' -> '.join(plan['steps'])}{eta}"
            return gr.update(value=info, visible=True), gr.update(visible=True), "", get_daemon_status_text(), get_gpu_status_text()

        start_btn.click(
            _on_click_start, outputs=[confirm_info, confirm_row, gen_msg, daemon_status, gpu_status_box]
        )

        def _on_confirm():
            msg = start_daemon()
            return gr.update(visible=False), gr.update(visible=False), msg, get_daemon_status_text(), get_gpu_status_text()

        confirm_btn.click(
            _on_confirm, outputs=[confirm_info, confirm_row, gen_msg, daemon_status, gpu_status_box], **_GPU_CONCURRENCY
        )

        cancel_btn.click(lambda: (gr.update(visible=False), gr.update(visible=False)),
                          outputs=[confirm_info, confirm_row])

        release_btn.click(
            lambda: release_gpu_to_llm(), outputs=gen_msg
        ).then(lambda: get_daemon_status_text(), outputs=daemon_status
        ).then(lambda: get_gpu_status_text(), outputs=gpu_status_box)

        gen_btn.click(
            lambda rid, txt, emo: preview_tts(rid, txt, emo, roles_dir),
            inputs=[role_dropdown, text_box, emotion_dropdown], outputs=[audio_out, gen_msg],
            **_GPU_CONCURRENCY,
        )


def build_app(project_root: str = None) -> gr.Blocks:
    """构造管理界面 Blocks；project_root 缺省用项目根目录，测试时可传隔离临时目录"""
    roles_dir = os.path.join(project_root, "roles") if project_root else None
    chapters_dir = os.path.join(project_root, "chapters") if project_root else None

    with gr.Blocks(title="novel2audiobook 管理界面") as demo:
        gr.Markdown("# novel2audiobook 管理界面")
        with gr.Row():
            gpu_status_box = gr.Markdown(get_gpu_status_text())
            refresh_gpu_btn = gr.Button("刷新显存状态", size="sm")
        refresh_gpu_btn.click(lambda: get_gpu_status_text(), outputs=gpu_status_box)

        with gr.Tabs():
            _dashboard_tab(chapters_dir, gpu_status_box)
            _roles_tab(roles_dir)
            _chapters_tab(chapters_dir)
            _tts_preview_tab(roles_dir, gpu_status_box)

    return demo


def run_webui(host: str = "127.0.0.1", port: int = 7860, project_root: str = None):
    """启动管理界面，供 `python cli.py webui` 调用"""
    demo = build_app(project_root=project_root)
    demo.queue().launch(server_name=host, server_port=port)
