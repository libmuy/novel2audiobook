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


class SegmentBatch(BaseModel):
    seg_ids: list
    set: dict


class RoleCreate(BaseModel):
    name: str
    gender: str = "unknown"
    category: str = ""
    description: str = ""


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    speed: Optional[float] = None


class TaskCreate(BaseModel):
    type: str
    novel_id: str
    scope: Optional[dict] = None


class ConfigPatch(BaseModel):
    tts_engine: Optional[str] = None
    tts_sample_rate: Optional[int] = None
    mixing_output_format: Optional[str] = None
    mixing_bitrate: Optional[str] = None
    cpu_workers: Optional[int] = None
    monitor_interval_ms: Optional[int] = None
    library_root: Optional[str] = None


class GPUSwap(BaseModel):
    target: str
