from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.constants.enums import Review


class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')


class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)


class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)


class ArchiveUpload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=4_000_000)


class ReplaySpec(Request):
    key: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    dataset_id: int = Field(gt=0)
    pilot_date: str = Field(default='', max_length=10)
    window_days: int = Field(default=0, ge=0, le=28)
    required: dict[str, int] = Field(min_length=1)
    run_id: int | None = Field(default=None, gt=0)


class PlanRequest(Request):
    name: str = Field(min_length=1, max_length=120)
    catalog_id: int = Field(gt=0)
    budget: int = Field(ge=0, le=100_000_000)
    replays: list[ReplaySpec] = Field(min_length=1, max_length=10)


class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)


class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False
