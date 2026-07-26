from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GenerateRequest(BaseModel):
    creator_id: str
    brief: str
    reference_image_ids: list[str] = []


class RegenerateRequest(BaseModel):
    creator_id: str
    item_id: str
    reference_image_ids: list[str] = []


class SetTemplateRequest(BaseModel):
    creator_id: str
    template: str


class Creative(BaseModel):
    item_id: str
    creator_id: str
    caption: str
    hook: str
    style_vector: list[float]
    performance: float = 0.0  # historical engagement score for this creative
    served_by: Optional[str] = None
    created_at: Optional[datetime] = None
    brief: Optional[str] = None  # original brief, so regenerate re-uses intent (not its own output)
