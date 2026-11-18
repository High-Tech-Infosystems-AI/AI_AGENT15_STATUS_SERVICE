from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class EndpointCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    url: str = Field(..., min_length=1, max_length=2048)
    events: List[str] = Field(..., min_length=1)
    is_active: bool = True
    organization_id: Optional[int] = None


class EndpointUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    events: Optional[List[str]] = Field(default=None, min_length=1)
    is_active: Optional[bool] = None
