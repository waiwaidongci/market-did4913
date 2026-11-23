from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base
from src.constants.enums import ProcurementStatus
from src.utils.time import now


class ArchiveCatalog(Base):
    """档案目录的不可变修订。

    同名目录再次导入不同内容时生成新修订，旧修订被标记 ``superseded_at``，
    依赖旧修订的取档方案随之失效。
    """

    __tablename__ = 'archive_catalogs'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    packages: Mapped[list] = mapped_column(JSON)
    package_count: Mapped[int]
    day_count: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    superseded_at: Mapped[datetime | None]


class ProcurementPlan(Base):
    """缺报复盘的取档方案：预算内补齐份数最多的档案包组合。"""

    __tablename__ = 'procurement_plans'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    catalog_id: Mapped[int] = mapped_column(ForeignKey('archive_catalogs.id'))
    catalog_checksum: Mapped[str] = mapped_column(String(64))
    budget: Mapped[int]
    replays: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default=ProcurementStatus.QUEUED, index=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]
