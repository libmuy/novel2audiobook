"""角色路由"""
import os
import tempfile
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from src import roles, derived_index
from src.api.schemas import RoleCreate, RoleUpdate
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
                               gender=data.gender, description=data.description)
    # register_role 不认 category 参数（那是给自动注册流程设计的，从不带分类）；
    # register_role 内部已经 save 过一次，manifest 这个 dict 引用里已经有了
    # 刚注册的角色，这里补一次分类再存一遍即可
    if data.category:
        manifest["roles"][rid]["category"] = data.category
        roles.save_manifest(manifest, _get_roles_dir())
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


def _normalize_categories(categories: list) -> list:
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


@router.get("/role-categories")
def list_role_categories():
    manifest = roles.load_manifest(_get_roles_dir())
    return {"categories": manifest.get("categories", [])}


@router.put("/role-categories")
def update_role_categories(data: dict):
    categories = data.get("categories")
    if not isinstance(categories, list):
        raise HTTPException(400, "categories 必须是字符串数组")
    manifest = roles.load_manifest(_get_roles_dir())
    manifest["categories"] = _normalize_categories(categories)
    roles.save_manifest(manifest, _get_roles_dir())
    return {"ok": True, "categories": manifest["categories"]}
