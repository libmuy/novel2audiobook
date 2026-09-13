"""角色路由"""
import os
from fastapi import APIRouter, HTTPException, UploadFile, File
from src import roles, derived_index
from src.api.schemas import RoleCreate, RoleUpdate
from src.utils import resolve_path

router = APIRouter(tags=["roles"])


def _get_roles_dir() -> str:
    # resolve_path 在调用时动态读取 PROJECT_ROOT，见 chapters.py 里的详细注释
    return resolve_path("roles")


@router.get("/roles")
def list_roles():
    manifest = roles.load_manifest(_get_roles_dir())
    refs = derived_index.get_role_refs()
    role_list = []
    for rid, info in manifest.get("roles", {}).items():
        ref_info = refs.get("roles", {}).get(rid, {})
        role_list.append({
            "id": rid,
            "name": info.get("name", rid),
            "gender": info.get("gender", "unknown"),
            "category": info.get("category", ""),
            "description": info.get("description", ""),
            "speed": info.get("speed"),
            "segment_count": ref_info.get("segment_count", 0),
            "novels": ref_info.get("novels", []),
        })
    return role_list


@router.post("/roles")
def create_role(data: RoleCreate):
    manifest = roles.load_manifest(_get_roles_dir())
    rid = roles.register_role(data.name, manifest, _get_roles_dir(),
                               gender=data.gender, description=data.description)
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

    ref_dir = os.path.join(_get_roles_dir(), rid)
    os.makedirs(ref_dir, exist_ok=True)
    ref_path = os.path.join(ref_dir, "reference.wav")

    content = await file.read()
    with open(ref_path, "wb") as f:
        f.write(content)

    roles.set_role_reference(rid, ref_path, manifest, _get_roles_dir())
    return {"ok": True}
