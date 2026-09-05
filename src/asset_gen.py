"""
音效/背景音本地模型生成模块

架构：与 src/tts_engine.py 同构——
- MockAudioGenBackend：程序化占位音频，无外部依赖，供 `cli.py test` 与无 GPU 环境使用。
- AceStepBackend（ambience/BGM，GPU）、TangoFluxBackend（sfx，CPU）：通过子进程
  调用独立部署的推理环境（各自独立 venv + jobs.json -> result.json 协议，见
  tools/indextts_infer.py 的先例）。ACE-Step 走 GPU，与 llama-server 显存互斥，
  通过 tools/gpu_arbiter.LlmSuspendedForGpu 上下文管理器自动换卡；TangoFlux 跑在
  CPU 上（模型选型见 tools/tangoflux_infer.py 顶部说明——原计划用 Stable Audio 3
  Small SFX，但它是 HuggingFace gated repo 且审批不顺畅，改用公开、无需申请的
  TangoFlux），不占显存，可以和前两者同时跑，但仍统一走同一套 subprocess 协议
  （多余的换卡暂停/恢复只是几秒钟开销，不值得为此分叉逻辑）。

素材以 assets/asset_specs.yaml 为唯一真相源（prompt/负向提示/时长/种子），按影响
生成结果的字段计算 spec_hash 做增量生成缓存；结果落到 assets/ambience/、assets/sfx/
下的具名 24kHz 单声道 wav（src/audio_mixer.py 直接可用）及同名 .meta.json 旁挂文件
（记录生成溯源：引擎/是否回退/生成时间等，list_available_assets() 只 glob *.wav，
不会把 meta 文件当成素材）。

无论使用哪种后端，单条素材生成失败都会回退 Mock 占位音，保证素材库始终齐全、
管线不中断（并在 meta 里记录 used_fallback，供 `assets list` 排查）。
"""
import os
import json
import math
import logging
import subprocess
import tempfile
from datetime import datetime

import numpy as np
import yaml
from pydub import AudioSegment

from src.utils import calculate_md5, load_global_config, resolve_path

logger = logging.getLogger(__name__)

VALID_KINDS = ("ambience", "sfx")
DEFAULT_SPEC_PATH = "assets/asset_specs.yaml"


# --------------------------------------------------------------------------
# Spec 加载与哈希
# --------------------------------------------------------------------------

def load_asset_specs(spec_path: str = None) -> dict:
    """
    读取 asset_specs.yaml，校验必填字段（prompt 必须非空），返回
    {"ambience": {name: spec, ...}, "sfx": {name: spec, ...}} 结构。
    spec 文件不存在时返回空结构（不抛异常——素材生成是可选能力，不应影响主管线）。
    """
    path = resolve_path(spec_path or DEFAULT_SPEC_PATH)
    specs = {kind: {} for kind in VALID_KINDS}
    if not os.path.exists(path):
        return specs

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    for kind in VALID_KINDS:
        for name, spec in (raw.get(kind, {}) or {}).items():
            if not spec or not str(spec.get("prompt", "")).strip():
                logger.warning("素材 %s/%s 缺少 prompt，已跳过", kind, name)
                continue
            specs[kind][name] = {
                "description": str(spec.get("description", "")),
                "prompt": str(spec["prompt"]),
                "negative_prompt": str(spec.get("negative_prompt", "")),
                "duration_sec": float(spec.get("duration_sec", 30.0)),
                "seed": int(spec.get("seed", 0)),
            }
    return specs


def compute_spec_hash(spec: dict) -> str:
    """
    对影响生成结果的字段（prompt/negative_prompt/duration_sec/seed）计算稳定哈希，
    作为增量生成的缓存键；description 是纯中文说明，改动不应触发重新生成。
    """
    hashable = {k: spec.get(k) for k in ("prompt", "negative_prompt", "duration_sec", "seed")}
    canonical = json.dumps(hashable, sort_keys=True, ensure_ascii=False)
    return calculate_md5(canonical)


def get_asset_descriptions(specs: dict = None) -> dict:
    """
    返回 {"sfx": {name: description}, "bgm": {name: description}}，供
    src/llm_parser.py 渲染带中文说明的词表（键名对齐 list_available_assets() 的
    "sfx"/"bgm" 命名，调用方无需关心本模块内部用 "ambience" 表示 BGM 目录）。
    """
    if specs is None:
        specs = load_asset_specs()
    return {
        "sfx": {name: spec.get("description", "") for name, spec in specs.get("sfx", {}).items()},
        "bgm": {name: spec.get("description", "") for name, spec in specs.get("ambience", {}).items()},
    }


# --------------------------------------------------------------------------
# 后处理：统一采样率/声道 + 响度归一 + 无缝循环 / 消爆音
# --------------------------------------------------------------------------

def _equal_power_crossfade(fade_out_seg: AudioSegment, fade_in_seg: AudioSegment, duration_ms: int) -> AudioSegment:
    """
    等功率交叉淡化拼接：fade_out_seg 淡出、fade_in_seg 淡入，用于制作无缝循环边界。
    区别于 audio_mixer._apply_gain_ramp() 的线性 dB 渐变——循环拼接场景下等功率曲线
    (sin/cos, 平方和恒为 1) 在交叉点总能量守恒，听感上不会出现中间凹陷或凸起。
    """
    n = int(duration_ms * fade_out_seg.frame_rate / 1000.0)
    if n <= 0:
        return fade_out_seg.overlay(fade_in_seg)

    t = np.linspace(0, math.pi / 2, n)
    fade_out_curve = np.cos(t)
    fade_in_curve = np.sin(t)

    def _apply_curve(segment: AudioSegment, curve: np.ndarray) -> AudioSegment:
        samples = np.array(segment.get_array_of_samples()).astype(np.float64)
        length = min(len(samples), len(curve))
        samples[:length] *= curve[:length]
        max_val = float(2 ** (8 * segment.sample_width - 1) - 1)
        samples = np.clip(samples, -max_val - 1, max_val)
        return segment._spawn(samples.astype(segment.array_type).tobytes())

    return _apply_curve(fade_out_seg, fade_out_curve).overlay(_apply_curve(fade_in_seg, fade_in_curve))


def _post_process_ambience(seg: AudioSegment, target_sample_rate: int, crossfade_ms: int,
                            target_dbfs: float) -> AudioSegment:
    """
    统一采样率/声道；把尾部 crossfade_ms 与头部等长片段做等功率交叉淡化后拼到正文末尾
    （即 new = body + crossfade(tail, head)），使这段素材首尾相接时听不出循环点；
    最后按平均响度（dBFS）归一，供 audio_mixer._build_scene_bgm_track() 直接铺场景轨。
    """
    seg = seg.set_frame_rate(target_sample_rate).set_channels(1)

    fade = min(crossfade_ms, len(seg) // 2 - 1) if len(seg) > 1 else 0
    if fade > 0:
        head = seg[:fade]
        tail = seg[-fade:]
        body = seg[fade:len(seg) - fade]
        seg = body + _equal_power_crossfade(tail, head, fade)

    if len(seg) > 0 and seg.dBFS != float("-inf"):
        seg = seg.apply_gain(target_dbfs - seg.dBFS)
    return seg


def _post_process_sfx(seg: AudioSegment, target_sample_rate: int, target_dbfs: float) -> AudioSegment:
    """统一采样率/声道；去掉前导静音；短淡入淡出消除边界爆音；按峰值归一（防止叠加人声后爆音）。"""
    seg = seg.set_frame_rate(target_sample_rate).set_channels(1)

    if len(seg) > 0 and seg.dBFS != float("-inf"):
        seg = seg.strip_silence(silence_thresh=seg.dBFS - 16, padding=5)

    fade_in = min(5, len(seg) // 2) if len(seg) > 0 else 0
    fade_out = min(20, len(seg) // 2) if len(seg) > 0 else 0
    if fade_in > 0 or fade_out > 0:
        seg = seg.fade_in(fade_in).fade_out(fade_out)

    if len(seg) > 0 and seg.max_dBFS != float("-inf"):
        seg = seg.apply_gain(target_dbfs - seg.max_dBFS)
    return seg


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------

class MockAudioGenBackend:
    """
    程序化占位音频：ambience 用低通滤波噪声模拟持续环境底噪，sfx 用指数衰减包络的
    滤波噪声模拟打击/碰撞类瞬态。无外部依赖，供 `cli.py test` 与无 GPU 环境使用，
    比 audio_mixer.generate_mock_audio_file() 的纯正弦波占位更接近真实素材的频谱形状，
    足够驱动后处理（循环拼接/响度归一）的回归测试。
    """

    name = "mock"

    def is_available(self) -> bool:
        return True

    def generate(self, kind: str, prompt: str, duration_sec: float, seed: int,
                 output_wav_path: str, sample_rate: int = 44100) -> bool:
        rng = np.random.RandomState(seed if seed else 0)
        n = max(1, int(sample_rate * duration_sec))
        noise = rng.normal(0, 1, n)

        if kind == "sfx":
            decay = np.exp(-np.linspace(0, 8, n))
            samples = noise * decay
        else:
            kernel = np.ones(64) / 64.0
            samples = np.convolve(noise, kernel, mode="same")

        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = samples / peak * 0.5
        pcm = (samples * 32767).astype(np.int16)

        os.makedirs(os.path.dirname(output_wav_path), exist_ok=True)
        seg = AudioSegment(pcm.tobytes(), frame_rate=sample_rate, sample_width=2, channels=1)
        seg.export(output_wav_path, format="wav")
        return True

    def generate_batch(self, jobs: list) -> dict:
        """逐条调用 generate()，Mock 引擎无需批量优化"""
        results = {}
        for job in jobs:
            ok = self.generate(job["kind"], job["prompt"], job["duration_sec"], job["seed"],
                                job["out"], job.get("sample_rate", 44100))
            results[job["id"]] = ok
        return results


class SubprocessAudioGenBackend:
    """
    通过子进程调用独立部署的推理环境：独立 venv + jobs.json -> result.json 协议，
    结构与 src/tts_engine.IndexTTSBackend 完全一致（单次加载模型、批量生成，
    逐条任务失败不影响其余任务）。子类只需指定 name 与 global_config.yaml 里
    `asset_gen.<config_key>` 对应的配置段。
    """

    name = "subprocess_audio_gen"
    config_key = None

    def __init__(self, config: dict):
        self.config = config
        gen_cfg = config.get("asset_gen", {}).get(self.config_key, {}) if self.config_key else {}
        self.python_bin = resolve_path(gen_cfg["python_bin"]) if gen_cfg.get("python_bin") else None
        self.infer_script = resolve_path(gen_cfg["infer_script"]) if gen_cfg.get("infer_script") else None
        self.repo_dir = resolve_path(gen_cfg["repo_dir"]) if gen_cfg.get("repo_dir") else None
        self.checkpoints_dir = resolve_path(gen_cfg["checkpoints_dir"]) if gen_cfg.get("checkpoints_dir") else None
        self.timeout = gen_cfg.get("timeout_sec", 1800)

    def is_available(self) -> bool:
        if not (self.python_bin and self.infer_script and self.checkpoints_dir):
            return False
        ok = (os.path.exists(self.python_bin) and os.path.exists(self.infer_script)
              and os.path.isdir(self.checkpoints_dir))
        if self.repo_dir:
            ok = ok and os.path.isdir(self.repo_dir)
        return ok

    def generate_batch(self, jobs: list) -> dict:
        """
        jobs: [{"id","kind","prompt","negative_prompt","duration_sec","seed","sample_rate","out"}, ...]
        返回 {job_id: bool}。
        """
        if not jobs:
            return {}

        infer_jobs = [{
            "id": job["id"],
            "kind": job["kind"],
            "prompt": job["prompt"],
            "negative_prompt": job.get("negative_prompt", ""),
            "duration_sec": job["duration_sec"],
            "seed": job["seed"],
            "sample_rate": job.get("sample_rate", 44100),
            "out": os.path.abspath(job["out"]),
        } for job in jobs]

        with tempfile.TemporaryDirectory(prefix=f"{self.name}_batch_") as tmp_dir:
            jobs_file = os.path.join(tmp_dir, "jobs.json")
            result_file = os.path.join(tmp_dir, "result.json")
            with open(jobs_file, "w", encoding="utf-8") as f:
                json.dump(infer_jobs, f, ensure_ascii=False)

            cmd = [
                self.python_bin, self.infer_script,
                "--checkpoints-dir", self.checkpoints_dir,
                "--jobs-file", jobs_file,
                "--result-file", result_file,
            ]
            if self.repo_dir:
                cmd += ["--repo-dir", self.repo_dir]

            from tools.gpu_arbiter import LlmSuspendedForGpu  # 延迟导入，避免无网络场景下的循环依赖

            proc = None
            try:
                with LlmSuspendedForGpu(self.config):
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
                if proc.returncode != 0:
                    logger.warning("[%s] 批量生成子进程返回非零: %s", self.name, proc.stderr[-1000:])
            except subprocess.TimeoutExpired:
                logger.warning("[%s] 批量生成超时（%d 条任务）", self.name, len(jobs))

            results = {}
            if os.path.exists(result_file):
                with open(result_file, "r", encoding="utf-8") as f:
                    raw_results = json.load(f)
                for job in jobs:
                    job_result = raw_results.get(job["id"])
                    ok = bool(job_result and job_result.get("ok")) and os.path.exists(job["out"])
                    if job_result and not job_result.get("ok"):
                        logger.warning("[%s] 素材 %s 生成失败: %s", self.name, job["id"], job_result.get("error", "")[:500])
                    results[job["id"]] = ok
            else:
                for job in jobs:
                    results[job["id"]] = False
            return results


class AceStepBackend(SubprocessAudioGenBackend):
    """ACE-Step 1.5：BGM/环境音生成，见 docs/audiogen_setup.md"""
    name = "ace_step"
    config_key = "ace_step"


class TangoFluxBackend(SubprocessAudioGenBackend):
    """TangoFlux：音效生成（CPU），见 docs/audiogen_setup.md"""
    name = "tangoflux"
    config_key = "tangoflux"


def build_asset_gen_backend(kind: str, config: dict = None):
    """按配置为指定 kind（ambience/sfx）选择生成后端；专用推理环境未就绪时自动回退 Mock，保证管线不中断"""
    if config is None:
        config = load_global_config()
    gen_cfg = config.get("asset_gen", {})
    engine_name = gen_cfg.get("ambience_engine" if kind == "ambience" else "sfx_engine", "")

    backend_cls = AceStepBackend if kind == "ambience" else TangoFluxBackend
    backend = backend_cls(config)
    if backend.is_available():
        return backend
    logger.warning(
        "配置要求为 %s 使用 %s，但推理环境未就绪（venv/权重缺失），回退到 Mock 占位生成",
        kind, engine_name or backend.name,
    )
    return MockAudioGenBackend()


# --------------------------------------------------------------------------
# 增量生成主流程
# --------------------------------------------------------------------------

def generate_assets(specs: dict = None, assets_dir: str = None, kinds: list = None,
                     only: set = None, force: bool = False, backend_map: dict = None,
                     config: dict = None) -> dict:
    """
    按 spec_hash 增量生成素材库。

    - specs 缺省时从 asset_specs.yaml 加载
    - assets_dir 缺省时为项目根目录下的 assets/（`cli.py test` 会传入隔离临时目录，
      与 generate_tts_incremental() 的 roles_dir 参数化同理，保证自检不污染共享目录）
    - backend_map: {"ambience": backend实例, "sfx": backend实例}，缺省按配置选择
      真实后端，环境未就绪自动回退 Mock
    - 返回 {"generated": [...], "fallback": [...], "skipped": [...], "error": [...]}
      （generated=真实引擎产出；fallback=回退 Mock 占位但仍产出了 wav；
       error=后处理阶段异常，理论上不应发生，出现即说明有 bug）
    """
    if config is None:
        config = load_global_config()
    if specs is None:
        specs = load_asset_specs()
    if assets_dir is None:
        assets_dir = resolve_path("assets")

    gen_cfg = config.get("asset_gen", {})
    target_sample_rate = gen_cfg.get("target_sample_rate", 24000)
    crossfade_ms = gen_cfg.get("ambience_loop_crossfade_ms", 2000)
    ambience_dbfs = gen_cfg.get("ambience_target_dbfs", -20.0)
    sfx_dbfs = gen_cfg.get("sfx_target_dbfs", -6.0)

    kinds = kinds or list(VALID_KINDS)
    backend_map = backend_map or {}

    summary = {"generated": [], "fallback": [], "skipped": [], "error": []}

    for kind in kinds:
        kind_specs = specs.get(kind, {})
        if only:
            kind_specs = {name: spec for name, spec in kind_specs.items() if name in only}
        if not kind_specs:
            continue

        out_dir = os.path.join(assets_dir, kind)
        os.makedirs(out_dir, exist_ok=True)

        backend = backend_map.get(kind) or build_asset_gen_backend(kind, config)
        backend_is_mock = backend.name == "mock"

        pending = {}
        for name, spec in kind_specs.items():
            spec_hash = compute_spec_hash(spec)
            wav_path = os.path.join(out_dir, f"{name}.wav")
            meta_path = os.path.join(out_dir, f"{name}.meta.json")
            if not force and os.path.exists(wav_path) and os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        old_meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    old_meta = {}
                if old_meta.get("spec_hash") == spec_hash:
                    summary["skipped"].append(name)
                    continue
            pending[name] = (spec, spec_hash, wav_path, meta_path)

        if not pending:
            continue

        with tempfile.TemporaryDirectory(prefix="n2a_assetgen_raw_") as raw_dir:
            jobs = [{
                "id": name, "kind": kind, "prompt": spec["prompt"],
                "negative_prompt": spec.get("negative_prompt", ""),
                "duration_sec": spec["duration_sec"], "seed": spec["seed"],
                "sample_rate": 44100, "out": os.path.join(raw_dir, f"{name}.wav"),
            } for name, (spec, _, _, _) in pending.items()]

            batch_results = backend.generate_batch(jobs)

            for job in jobs:
                name = job["id"]
                spec, spec_hash, wav_path, meta_path = pending[name]
                raw_path = job["out"]
                ok = batch_results.get(name, False)
                used_fallback = backend_is_mock or not ok

                try:
                    if not ok:
                        MockAudioGenBackend().generate(
                            kind, spec["prompt"], spec["duration_sec"], spec["seed"],
                            raw_path, sample_rate=44100,
                        )

                    raw_seg = AudioSegment.from_file(raw_path)
                    if kind == "ambience":
                        final_seg = _post_process_ambience(raw_seg, target_sample_rate, crossfade_ms, ambience_dbfs)
                    else:
                        final_seg = _post_process_sfx(raw_seg, target_sample_rate, sfx_dbfs)
                    final_seg.export(wav_path, format="wav")

                    meta = {
                        "name": name, "kind": kind, "prompt": spec["prompt"],
                        "negative_prompt": spec.get("negative_prompt", ""),
                        "seed": spec["seed"], "duration_sec": spec["duration_sec"],
                        "engine": "mock" if used_fallback else backend.name,
                        "spec_hash": spec_hash, "used_fallback": used_fallback,
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "duration_ms": len(final_seg),
                    }
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)

                    summary["fallback" if used_fallback else "generated"].append(name)
                except Exception as e:  # noqa: BLE001 - 单条素材失败不应中断整批
                    logger.warning("素材 %s/%s 后处理失败: %s", kind, name, e)
                    summary["error"].append(name)

    return summary


# --------------------------------------------------------------------------
# 状态查看（供 `cli.py assets list` 使用，风格对齐 src/status_tracker.py）
# --------------------------------------------------------------------------

def get_asset_status_list(specs: dict = None, assets_dir: str = None) -> list:
    """返回每条 spec 的当前状态：MISSING（未生成）/ STALE（spec 已变更需重新生成）/ OK"""
    specs = specs if specs is not None else load_asset_specs()
    assets_dir = assets_dir or resolve_path("assets")

    rows = []
    for kind in VALID_KINDS:
        for name, spec in sorted(specs.get(kind, {}).items()):
            out_dir = os.path.join(assets_dir, kind)
            wav_path = os.path.join(out_dir, f"{name}.wav")
            meta_path = os.path.join(out_dir, f"{name}.meta.json")

            if not os.path.exists(wav_path):
                rows.append({"name": name, "kind": kind, "status": "MISSING", "engine": "-", "duration_ms": None})
                continue

            meta = {}
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    meta = {}

            spec_hash = compute_spec_hash(spec)
            if meta.get("spec_hash") != spec_hash:
                status = "STALE(spec已变更)"
            elif meta.get("used_fallback"):
                status = "OK(占位/Mock)"
            else:
                status = "OK"

            rows.append({
                "name": name, "kind": kind, "status": status,
                "engine": meta.get("engine", "unknown"),
                "duration_ms": meta.get("duration_ms"),
            })
    return rows


def print_asset_status_table(specs: dict = None, assets_dir: str = None):
    """格式化打印素材库状态表"""
    rows = get_asset_status_list(specs, assets_dir)
    if not rows:
        print("assets/asset_specs.yaml 中未定义任何素材。")
        return

    print("=" * 80)
    print(f"{'名称':<22} | {'类型':<10} | {'状态':<18} | {'引擎':<18} | {'时长(ms)'}")
    print("=" * 80)
    for row in rows:
        dur = row["duration_ms"] if row["duration_ms"] is not None else "-"
        print(f"{row['name']:<22} | {row['kind']:<10} | {row['status']:<18} | {row['engine']:<18} | {dur}")
    print("=" * 80)
