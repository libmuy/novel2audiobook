"""
第三方推理引擎源码仓库管理：`./run.sh repos {status,setup}`。

index-tts / ACE-Step-1.5 的源码仓库体积几十 MB、且是外部依赖，不随项目分发；
`config/local_config.yaml` 里配置 repo_dir 指到本机某处（通常和 venv/权重放一起，
见 docs/indextts_setup.md、docs/audiogen_setup.md），`config/global_config.yaml`
固定 repo_url/repo_commit（与机器无关，锁定上游的哪个提交）。本模块把
「克隆到固定提交 + （IndexTTS）建 checkpoints 软链接 + 把 venv 的 editable 安装
指到新位置」这几步脚本化，换机器/重建环境时不用再逐条抄命令。
"""
import os
import shutil
import subprocess

from src.utils import load_global_config, resolve_optional_path

# name -> (global_config 里的配置段路径, 是否需要 checkpoints 软链接, 展示用标签)
REPOS = {
    "indextts": {"config_key": ("tts", "index_tts"), "checkpoints_link": True, "label": "index-tts"},
    "acestep": {"config_key": ("asset_gen", "ace_step"), "checkpoints_link": False, "label": "ACE-Step-1.5"},
}


def _repo_config(config: dict, name: str) -> dict:
    section, sub = REPOS[name]["config_key"]
    cfg = (config.get(section) or {}).get(sub) or {}
    return {
        "repo_url": cfg.get("repo_url"),
        "repo_commit": cfg.get("repo_commit"),
        "repo_dir": resolve_optional_path(cfg.get("repo_dir")),
        "python_bin": resolve_optional_path(cfg.get("python_bin")),
        "checkpoints_dir": resolve_optional_path(cfg.get("checkpoints_dir")),
    }


def _git_head(repo_dir: str):
    try:
        out = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _editable_target(python_bin: str):
    """从 venv 的 site-packages 里找 editable 安装实际指向哪个目录（用于 status 展示 /
    setup 判断是否需要重装），找不到返回 None。"""
    if not python_bin or not os.path.exists(python_bin):
        return None
    venv_root = os.path.dirname(os.path.dirname(python_bin))
    lib_dir = os.path.join(venv_root, "lib")
    if not os.path.isdir(lib_dir):
        return None
    for entry in os.listdir(lib_dir):
        site_packages = os.path.join(lib_dir, entry, "site-packages")
        if not os.path.isdir(site_packages):
            continue
        for fn in os.listdir(site_packages):
            if fn.startswith("_editable_impl_") and fn.endswith(".pth"):
                # 有的构建后端（如 ace-step 用的 uv_build）会在一个 .pth 里重复写
                # 同一条路径（多个模块根，本项目场景下恰好相同），逐行找第一条合法目录
                with open(os.path.join(site_packages, fn), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and os.path.isdir(line):
                            return line
    return None


def status(config: dict = None) -> dict:
    """返回 {name: {...}}，字段见下方 print_status_table 的用法。"""
    config = config or load_global_config()
    result = {}
    for name, meta in REPOS.items():
        info = _repo_config(config, name)
        repo_dir = info["repo_dir"]
        exists = bool(repo_dir and os.path.isdir(repo_dir))
        head = _git_head(repo_dir) if exists else None
        checkpoints_link_ok = None
        if meta["checkpoints_link"] and exists:
            link_path = os.path.join(repo_dir, "checkpoints")
            checkpoints_link_ok = bool(
                info["checkpoints_dir"] and os.path.islink(link_path)
                and os.path.realpath(link_path) == os.path.realpath(info["checkpoints_dir"])
            )
        editable_target = _editable_target(info["python_bin"])
        editable_ok = bool(
            editable_target and repo_dir
            and os.path.realpath(editable_target) == os.path.realpath(repo_dir)
        )
        result[name] = {
            "label": meta["label"],
            "repo_dir": repo_dir,
            "exists": exists,
            "head_commit": head,
            "expected_commit": info["repo_commit"],
            "commit_matches": bool(head and info["repo_commit"] and head == info["repo_commit"]),
            "checkpoints_link_ok": checkpoints_link_ok,
            "editable_target": editable_target,
            "editable_ok": editable_ok,
        }
    return result


def print_status_table(config: dict = None):
    for name, r in status(config).items():
        print(f"[{r['label']}]")
        if not r["repo_dir"]:
            print("  repo_dir 未配置（模板见 config/local_config.example.yaml）")
            continue
        print(f"  路径: {r['repo_dir']}（{'存在' if r['exists'] else '不存在'}）")
        if r["exists"]:
            if r["head_commit"]:
                verdict = "== 固定提交" if r["commit_matches"] else f"!= 固定提交 {r['expected_commit']}"
                print(f"  提交: {r['head_commit']} {verdict}")
            else:
                print("  提交: 无法获取（非 git 仓库？）")
        if r["checkpoints_link_ok"] is not None:
            print(f"  checkpoints 软链接: {'正常' if r['checkpoints_link_ok'] else '缺失/失效'}")
        if r["editable_target"]:
            print(f"  venv editable 安装指向: {r['editable_target']}"
                  f"{'' if r['editable_ok'] else '（与 repo_dir 不一致）'}")
        else:
            print("  venv editable 安装: 未找到（python_bin 未配置，或尚未 install -e）")


def setup(config: dict = None, only: list = None) -> dict:
    """按 repo_url/repo_commit 克隆（已存在则只检查、不覆盖）+ 修 checkpoints 软链接 +
    修 venv 的 editable 安装。返回 {name: {"ok": bool, "steps": [...], "error": str|None}}。"""
    config = config or load_global_config()
    names = only or list(REPOS.keys())
    results = {}
    for name in names:
        meta = REPOS[name]
        info = _repo_config(config, name)
        steps = []
        repo_dir = info["repo_dir"]
        try:
            if not repo_dir:
                raise RuntimeError("repo_dir 未配置（config/local_config.yaml）")

            if not os.path.isdir(repo_dir):
                if not info["repo_url"] or not info["repo_commit"]:
                    raise RuntimeError("repo_url/repo_commit 未配置（config/global_config.yaml）")
                parent = os.path.dirname(repo_dir)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                subprocess.run(["git", "clone", info["repo_url"], repo_dir], check=True)
                steps.append(f"clone {info['repo_url']} -> {repo_dir}")
                subprocess.run(["git", "-C", repo_dir, "checkout", info["repo_commit"]], check=True)
                steps.append(f"checkout {info['repo_commit']}")
            else:
                head = _git_head(repo_dir)
                if info["repo_commit"] and head != info["repo_commit"]:
                    steps.append(f"已存在，HEAD={head} 与固定提交 {info['repo_commit']} 不一致（不自动覆盖，如需重建请先手动移走该目录）")
                else:
                    steps.append("已存在且提交匹配，跳过 clone")

            if meta["checkpoints_link"] and info["checkpoints_dir"]:
                link_path = os.path.join(repo_dir, "checkpoints")
                target_matches = (
                    os.path.islink(link_path)
                    and os.path.realpath(link_path) == os.path.realpath(info["checkpoints_dir"])
                )
                if target_matches:
                    steps.append("checkpoints 软链接已正确")
                else:
                    # 新 clone 出来的仓库自带一个非空的 checkpoints/ 占位目录（如
                    # pinyin.vocab），需要整个替换成指向真实权重目录的软链接
                    if os.path.islink(link_path):
                        os.remove(link_path)
                    elif os.path.isdir(link_path):
                        shutil.rmtree(link_path)
                    elif os.path.exists(link_path):
                        os.remove(link_path)
                    os.symlink(info["checkpoints_dir"], link_path)
                    steps.append(f"重建 checkpoints 软链接 -> {info['checkpoints_dir']}")

            if info["python_bin"] and os.path.exists(info["python_bin"]):
                # --no-deps：docs/audiogen_setup.md 记录过，不带这个参数会把 ROCm torch 换回 CUDA 版
                subprocess.run(
                    ["uv", "pip", "install", "--python", info["python_bin"], "--no-deps", "-e", repo_dir],
                    check=True,
                )
                steps.append(f"uv pip install --no-deps -e {repo_dir}（venv: {info['python_bin']}）")
            else:
                steps.append("python_bin 未配置/不存在，跳过 venv 的 editable 安装")

            results[name] = {"ok": True, "steps": steps, "error": None}
        except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
            results[name] = {"ok": False, "steps": steps, "error": str(e)}
    return results
