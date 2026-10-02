from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.models import Priority


class UserCreate(BaseModel):
    email: str
    name: str | None = None


class UserRead(UserCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CalendarCreate(BaseModel):
    user_id: int
    name: str
    provider: str = "local"
    description: str | None = None
    timezone: str = "UTC"


class CalendarRead(CalendarCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    external_id: str | None = None
    is_active: bool


class EventCreate(BaseModel):
    calendar_id: int
    user_id: int
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"
    priority: Priority = Priority.MEDIUM
    location: str | None = None


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = None
    priority: Priority | None = None
    location: str | None = None


class EventRead(EventCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    source: str
    sync_status: str
    external_id: str | None = None


class AssistantMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    timezone: str = "Europe/Moscow"


class AssistantResponse(BaseModel):
    answer: str | None = None
    created_events: list[EventRead] = Field(default_factory=list)
    proposed_events: list[dict] = Field(default_factory=list)


class AssistantConfirmation(BaseModel):
    events: list[dict]
    timezone: str = "Europe/Moscow"