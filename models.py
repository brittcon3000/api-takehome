from datetime import datetime
from typing import Any, Literal, Optional, List

from pydantic import BaseModel, Field


Source = Literal["stripe", "spreedly_status", "braze_status"]
Kind = Literal["incident", "status", "payment"]
Severity = Literal["info", "warning", "critical"]


class NormalizedEvent(BaseModel):
    event_id: str = Field(min_length=1)
    source: Source
    kind: Kind
    severity: Severity
    service: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    description: Optional[str] = None
    started_at: datetime
    resolved_at: Optional[datetime] = None
    raw: Any


class StoredEvent(NormalizedEvent):
    routed: bool = False
    delivered_to: List[str] = Field(default_factory=list)
    

class AISummarizeRequest(BaseModel):
    text: str = Field(min_length=1)


class AISummarizeResponse(BaseModel):
    summary: str
    suggested_severity: Literal["info", "warning", "critical"]