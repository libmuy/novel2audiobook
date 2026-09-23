"""素材规格路由（data/assets/asset_specs.yaml 的增删改查）"""
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from src import asset_gen, asset_specs_store as store, category_tree
from src.utils import assets_dir
from src.api.schemas import AssetSpecCreate, AssetSpecUpdate, CategoryTreePut

router = APIRouter(prefix="/asset-specs", tags=["assets"])
# 分类树与标签汇总在 /api 根下（与 /role-category-tree、/role-tags 对称），不挂在
# /asset-specs/{kind}/{name} 的路径空间里
meta_router = APIRouter(tags=["assets"])


def _rows(kind: str = None) -> list:
    """合并规格与生成状态：规格字段来自 load_asset_specs，状态复用
    get_asset_status_list（MISSING/STALE/OK 的判定逻辑不在这里重写）。"""
    specs = asset_gen.load_asset_specs()
    status_by_key = {(r["kind"], r["name"]): r for r in asset_gen.get_asset_status_list(specs)}
    rows = []
    for k in asset_gen.VALID_KINDS:
        if kind and k != kind:
            continue
        for name, spec in sorted(specs.get(k, {}).items()):
            st = status_by_key.get((k, name), {})
            rows.append({
                "kind": k, "name": name, "category": "", "tags": [], **spec,
                "status": st.get("status", "MISSING"),
                "engine": st.get("engine", "-"),
                "expected_engine": st.get("expected_engine"),
                "duration_ms": st.get("duration_ms"),
            })
    return rows


def _status_of(kind: str, name: str) -> str:
    for r in _rows(kind):
        if r["name"] == name:
            return r["status"]
    return "MISSING"


@router.get("")
def list_specs(kind: str = None):
    if kind:
        try:
            store.validate_kind(kind)
        except store.SpecError as e:
            raise HTTPException(400, str(e))
    return {"specs": _rows(kind)}


@router.post("", status_code=201)
def create_spec(data: AssetSpecCreate):
    try:
        store.create_spec(data.kind, data.name, data.model_dump(exclude={"kind", "name"}))
    except store.SpecError as e:
        raise HTTPException(400, str(e))
    except store.SpecExists as e:
        raise HTTPException(409, str(e))
    return {"kind": data.kind, "name": data.name, "status": _status_of(data.kind, data.name)}


@router.patch("/{kind}/{name}")
def update_spec(kind: str, name: str, data: AssetSpecUpdate):
    try:
        store.update_spec(kind, name, data.model_dump(exclude_unset=True))
    except store.SpecError as e:
        raise HTTPException(400, str(e))
    except store.SpecNotFound as e:
        raise HTTPException(404, str(e.args[0]))
    return {"ok": True, "status": _status_of(kind, name)}


@router.delete("/{kind}/{name}")
def delete_spec(kind: str, name: str, delete_files: bool = False):
    try:
        files_deleted = store.delete_spec(kind, name, delete_files=delete_files)
    except store.SpecError as e:
        raise HTTPException(400, str(e))
    except store.SpecNotFound as e:
        raise HTTPException(404, str(e.args[0]))
    return {"ok": True, "files_deleted": files_deleted}


@router.get("/{kind}/{name}/audio")
def get_asset_audio(kind: str, name: str):
    """试听：返回已生成的 wav。kind/name 先过和写入口同一套校验——name 会被拼进
    文件路径，必须是安全标识符，杜绝目录穿越。没生成过（或只有定义没有音频）404。

    Cache-Control: no-cache——同名素材重新生成后 URL 不变，浏览器必须每次带
    ETag/Last-Modified 去问服务器，否则会一直播放缓存里的旧音频。"""
    try:
        store.validate_kind(kind)
        store.validate_name(name)
    except store.SpecError as e:
        raise HTTPException(400, str(e))
    path = os.path.join(assets_dir(), kind, f"{name}.wav")
    if not os.path.exists(path):
        raise HTTPException(404, f"素材 {kind}/{name} 还没有生成音频")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@meta_router.get("/asset-category-tree")
def get_asset_category_tree():
    """还没保存过树就是空树（不像角色库有旧的扁平 categories 可迁移）；
    素材身上的 category 字符串仍然有效，界面把不在树里的显示成「未登记」。"""
    tree = store.load_category_tree()
    return {"tree": tree, "categories": category_tree.flatten_paths(tree)}


@meta_router.put("/asset-category-tree")
def put_asset_category_tree(data: CategoryTreePut):
    """整树替换，语义与 PUT /role-category-tree 相同（校验 → 补 id → 落盘）。
    删除/改名树节点**不会**改动素材自己的 category——同一条悬空引用容忍策略。"""
    tree = category_tree.normalize_tree([n.model_dump() for n in data.tree])
    try:
        category_tree.validate_tree(tree)
    except category_tree.TreeError as e:
        raise HTTPException(400, str(e))
    category_tree.assign_missing_ids(tree)
    store.save_category_tree(tree)
    return {"ok": True, "tree": tree, "categories": category_tree.flatten_paths(tree)}


@meta_router.get("/asset-tags")
def list_asset_tags():
    """聚合所有素材用到的标签及数量（标签只存在素材身上，没有注册表）。"""
    counts = {}
    for bucket in asset_gen.load_asset_specs().values():
        for spec in bucket.values():
            for t in spec.get("tags", []):
                counts[t] = counts.get(t, 0) + 1
    tags = [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"tags": tags}
