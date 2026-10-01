"""Pydantic 请求模型与校验。"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field, field_validator


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class LabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RelationIn(BaseModel):
    outer_id: int
    inner_id: int
    kind: str

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("nested", "mutex"):
            raise ValueError("kind 必须为 nested 或 mutex")
        return v


class AnnotatorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    weight: int = Field(ge=1, le=10)


class AnnotationItem(BaseModel):
    label_id: int
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class AnnotationSubmission(BaseModel):
    annotator_id: int
    annotations: list[AnnotationItem]


class OverrideIn(BaseModel):
    label_id: int
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    action: str
    reason: str = Field(default="", max_length=1000)

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in ("force_include", "force_exclude"):
            raise ValueError("action 必须为 force_include 或 force_exclude")
        return v


def parse_tokens(raw: str) -> list[tuple[int, int]]:
    """解析词元边界 JSON，格式为 [[start,end], ...]，半开区间、按起点升序。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"词元边界不是合法 JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("词元边界必须是非空数组")
    tokens: list[tuple[int, int]] = []
    prev_end = -1
    for i, item in enumerate(data):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, int) for x in item)
        ):
            raise ValueError(f"第 {i + 1} 个词元必须是 [start,end] 整数对")
        s, e = item
        if s < 0 or e <= s:
            raise ValueError(f"第 {i + 1} 个词元边界非法: {item}")
        if s < prev_end:
            raise ValueError(f"第 {i + 1} 个词元与前词元重叠/乱序: {item}")
        tokens.append((s, e))
        prev_end = e
    return tokens
