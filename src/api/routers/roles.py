"""角色路由"""
import os
import tempfile
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from src import roles, derived_index
from src.api.schemas import RoleCreate, RoleUpdate, CategoryTreePut
from src import category_tree
from src.utils import resolve_path

router = APIRouter(tags=["roles"])


def _get_roles_dir() -> str:
    # resolve_path 在调用时动态读取 PROJECT_ROOT，见 chapters.py 里的详细注释
    return resolve_path("roles")


@router.get("/roles")
def list_roles():
    roles_dir = _get_roles_dir()
    manifest = roles.load_manifest(roles_dir)
    refs = derived_index.get_role_refs()
    role_list = []
    for rid, info in manifest.get("roles", {}).items():
        ref_info = refs.get("roles", {}).get(rid, {})
        has_reference = os.path.exists(os.path.join(roles_dir, rid, "reference.wav"))
        role_list.append({
            "id": rid,
            "name": info.get("name", rid),
            "gender": info.get("gender", "unknown"),
            "category": info.get("category", ""),
            "tags": info.get("tags", []),
            "description": info.get("description", ""),
            "speed": info.get("speed"),
            "segment_count": ref_info.get("segment_count", 0),
            "novels": ref_info.get("novels", []),
            "has_reference": has_reference,
            # embedding 是懒加载的（首次用到才算），大多数角色在这里 exists=False
            # 是正常状态，不是错误——前端要区分"没算过" vs "算过但过期了"
            "embedding_status": roles.get_embedding_status(rid, manifest, roles_dir),
        })
    return role_list


@router.get("/roles/{rid}/reference")
def get_reference_audio(rid: str):
    ref_path = os.path.join(_get_roles_dir(), rid, "reference.wav")
    if not os.path.exists(ref_path):
        raise HTTPException(404, "该角色还没有参考音频")
    return FileResponse(ref_path, media_type="audio/wav")


@router.post("/roles")
def create_role(data: RoleCreate):
    manifest = roles.load_manifest(_get_roles_dir())
    rid = roles.register_role(data.name, manifest, _get_roles_dir(),
                               gender=data.gender, description=data.description,
                               category=data.category,
                               tags=_normalize_str_list(data.tags or []))
    return {"role_id": rid}


@router.patch("/roles/{rid}")
def update_role(rid: str, data: RoleUpdate):
    manifest = roles.load_manifest(_get_roles_dir())
    if rid not in manifest.get("roles", {}):
        raise HTTPException(404, f"角色 {rid} 不存在")
    info = manifest["roles"][rid]
    if data.name is not None:
        info["name"] = data.name
    if data.category is not None:
        info["category"] = data.category
    if data.description is not None:
        info["description"] = data.description
    if data.speed is not None:
        info["speed"] = data.speed
    if data.tags is not None:
        info["tags"] = _normalize_str_list(data.tags)
    roles.save_manifest(manifest, _get_roles_dir())
    return {"ok": True}


@router.delete("/roles/{rid}")
def delete_role(rid: str, confirm: bool = False):
    if rid == "narrator":
        raise HTTPException(400, "narrator 角色不可删除")
    manifest = roles.load_manifest(_get_roles_dir())
    if rid not in manifest.get("roles", {}):
        raise HTTPException(404, f"角色 {rid} 不存在")

    refs = derived_index.get_role_refs()
    ref_count = refs.get("roles", {}).get(rid, {}).get("segment_count", 0)

    if not confirm:
        return {"reference_count": ref_count, "confirmed": False}

    roles.delete_role(rid, manifest, _get_roles_dir())
    derived_index.invalidate()
    return {"ok": True, "confirmed": True}


@router.put("/roles/{rid}/reference")
async def upload_reference(rid: str, file: UploadFile = File(...)):
    manifest = roles.load_manifest(_get_roles_dir())
    if rid not in manifest.get("roles", {}):
        raise HTTPException(404, f"角色 {rid} 不存在")

    # set_role_reference 的 wav_path 参数是"从哪个源文件拷过去"，不能直接传
    # 目标路径本身——那样会变成 shutil.copyfile(dst, dst) 直接报
    # SameFileError。上传内容先落一个临时文件，再交给它做拷贝+失效旧
    # embedding 的完整流程。
    content = await file.read()
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
        roles.set_role_reference(rid, tmp_path, manifest, _get_roles_dir())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return {"ok": True}


def _normalize_str_list(categories: list) -> list:
    """去空白、去重（保序），不强制跟已有角色的 category 字段同步——
    删掉一个分类不会连带清空还在用这个分类名的角色，那些角色只是不再出现在
    "管理分类"列表里，筛选功能仍然靠角色自身的 category 字段兜底。"""
    seen = set()
    result = []
    for c in categories:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


# 旧名保留：其他地方（或测试）可能还在引用
_normalize_categories = _normalize_str_list


@router.get("/role-categories")
def list_role_categories():
    manifest = roles.load_manifest(_get_roles_dir())
    return {"categories": manifest.get("categories", [])}


@router.put("/role-categories")
def update_role_categories(data: dict):
    """（旧接口，保留兼容）整体覆盖扁平 categories。注意它**不会**碰
    category_tree：想改嵌套树请用 PUT /role-category-tree（那个才会重新生成
    这份扁平投影）。两边不做自动对账——用扁平列表重建树会悄悄毁掉嵌套结构。"""
    categories = data.get("categories")
    if not isinstance(categories, list):
        raise HTTPException(400, "categories 必须是字符串数组")
    manifest = roles.load_manifest(_get_roles_dir())
    manifest["categories"] = _normalize_str_list(categories)
    roles.save_manifest(manifest, _get_roles_dir())
    return {"ok": True, "categories": manifest["categories"]}


@router.get("/role-category-tree")
def get_role_category_tree():
    manifest = roles.load_manifest(_get_roles_dir())
    tree = manifest.get("category_tree")
    if tree is None:
        # 还没保存过树：把旧的扁平 categories 合成成一层根节点返回，不写盘——
        # 用户真正保存之前不迁移任何数据
        tree = category_tree.tree_from_flat(manifest.get("categories", []))
    return {"tree": tree, "categories": category_tree.flatten_paths(tree)}


@router.put("/role-category-tree")
def put_role_category_tree(data: CategoryTreePut):
    """整树替换（不做逐节点增删改接口：分类树是几十个节点的量级，没有并发编辑
    的真实场景，整树 PUT 跟既有 PUT /role-categories 的语义一致）。

    树是真相源，扁平 categories 每次都重新生成。删除/改名树节点**不会**改动
    角色自己的 category 字符串——跟扁平分类同一条容忍策略，角色只是变成引用了
    一个不在树里的名字，筛选仍然靠角色自身字段兜底。"""
    tree = category_tree.normalize_tree([n.model_dump() for n in data.tree])
    try:
        category_tree.validate_tree(tree)
    except category_tree.TreeError as e:
        raise HTTPException(400, str(e))
    category_tree.assign_missing_ids(tree)
    manifest = roles.load_manifest(_get_roles_dir())
    manifest["category_tree"] = tree
    manifest["categories"] = category_tree.flatten_paths(tree)
    roles.save_manifest(manifest, _get_roles_dir())
    return {"ok": True, "tree": tree, "categories": manifest["categories"]}


@router.get("/role-tags")
def list_role_tags():
    """聚合所有角色用到的标签及数量。不存标签注册表——标签只存在角色身上，
    所以永远不会有"标签被删了但角色还引用着"的悬空问题。"""
    manifest = roles.load_manifest(_get_roles_dir())
    counts = {}
    for info in manifest.get("roles", {}).values():
        for t in info.get("tags", []) or []:
            counts[t] = counts.get(t, 0) + 1
    tags = [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"tags": tags}
