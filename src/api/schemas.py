"""Pydantic 请求/响应模型"""
from typing import Optional
from pydantic import BaseModel


class NovelCreate(BaseModel):
    title: str
    description: str = ""
    levels: Optional[dict] = None


class NovelUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class NodeCreate(BaseModel):
    type: str
    title: str
    parent_id: Optional[str] = None


class NodeUpdate(BaseModel):
    title: Optional[str] = None


class NodeReorder(BaseModel):
    node_id: str
    new_parent_id: Optional[str] = None
    new_index: int = 0


class SegmentUpdate(BaseModel):
    text: Optional[str] = None
    speaker: Optional[str] = None
    emotion: Optional[str] = None
    sfx: Optional[str] = None
    bgm: Optional[str] = None


class SegmentBatch(BaseModel):
    seg_ids: list
    set: dict


class RoleCreate(BaseModel):
    name: str
    gender: str = "unknown"
    category: str = ""
    description: str = ""
    tags: Optional[list[str]] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    speed: Optional[float] = None
    tags: Optional[list[str]] = None


class TaskCreate(BaseModel):
    type: str
    novel_id: Optional[str] = None  # 全局任务类型（asset_gen 等）不需要
    scope: Optional[dict] = None
    params: Optional[dict] = None


class GPUSwap(BaseModel):
    target: str


class AssetSpecCreate(BaseModel):
    kind: str  # "ambience" | "sfx"
    name: str
    prompt: str
    description: str = ""
    negative_prompt: str = ""
    duration_sec: float = 10.0
    seed: int = 0
    category: str = ""
    tags: list[str] = []


class AssetSpecUpdate(BaseModel):
    # kind/name 不可变——改名会让所有引用旧名的剧本/时间线失联
    description: Optional[str] = None
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    duration_sec: Optional[float] = None
    seed: Optional[int] = None
    # "" / [] 表示清空；None（不传）表示不改
    category: Optional[str] = None
    tags: Optional[list[str]] = None


class CategoryNode(BaseModel):
    id: Optional[str] = None  # 缺省时服务端生成
    title: str
    children: list["CategoryNode"] = []


class CategoryTreePut(BaseModel):
    tree: list[CategoryNode]
