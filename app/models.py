from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, StringConstraints

# Creator ids are plain identifiers. Constraining them at the schema boundary
# rejects malformed input with a 422 *before* it reaches any downstream matching
# or rendering — this is what stops a crafted creator_id from ever hitting a
# regex on the hot path (defuses the catastrophic-backtracking DoS at the door).
CreatorId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_]+$", min_length=1, max_length=64)
]


class GenerateRequest(BaseModel):
    creator_id: CreatorId
    brief: Annotated[str, StringConstraints(max_length=4000)]
    reference_image_ids: list[str] = []


class RegenerateRequest(BaseModel):
    creator_id: CreatorId
    item_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    reference_image_ids: list[str] = []


class SetTemplateRequest(BaseModel):
    creator_id: CreatorId
    template: Annotated[str, StringConstraints(max_length=4000)]


class Creative(BaseModel):
    item_id: str
    creator_id: str
    tenant_id: Optional[str] = None  # owning tenant; read paths filter on this
    caption: str
    hook: str
    style_vector: list[float]
    performance: float = 0.0  # historical engagement score for this creative
    served_by: Optional[str] = None
    created_at: Optional[datetime] = None
    brief: Optional[str] = None  # original brief, so regenerate re-uses intent (not its own output)
