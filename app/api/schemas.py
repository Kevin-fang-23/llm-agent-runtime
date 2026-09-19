"""API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    goal: str = Field(min_length=2, max_length=4000, description="自然语言任务目标")
    mode: str = Field(default="react", pattern="^(react|plan_execute)$",
                      description="执行策略：react | plan_execute")
    max_tokens: int | None = Field(default=None, ge=500, le=5_000_000)
    max_steps: int | None = Field(default=None, ge=2, le=200)


class TaskOut(BaseModel):
    id: str
    goal: str
    mode: str
    status: str
    max_tokens: int
    max_steps: int
    tokens_used: int
    steps_used: int
    downgraded: bool
    selfheal_count: int
    result: str
    error: str
    duration_s: float
    created_at: float
    updated_at: float
