"""档案目录与取档方案接口。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.procurement import CatalogError, parse_catalog
from src.models.identity import User
from src.models.jobs import Run
from src.models.procurement import ArchiveCatalog, ProcurementPlan
from src.models.source import Dataset
from src.schemas.requests import ArchiveUpload, PlanRequest
from src.services import procurement as service
from src.services.audit import record
from src.utils.wakeup import wake

router = APIRouter()

GROUPS = ("试点组", "对照组")


def _valid_date(value):
    from datetime import date
    if not value:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_catalog_id(db, catalog_id):
    catalog = db.get(ArchiveCatalog, catalog_id)
    if catalog is None:
        raise HTTPException(404, "找不到该档案目录")
    if catalog.superseded_at is not None:
        raise ValueError("该档案目录已更新，请使用最新修订")
    return catalog


def _validate_replays(db, replays):
    keys, names = set(), set()
    for replay in replays:
        if replay.key in keys:
            raise ValueError("复盘编号重复，请为每份复盘设置不同编号")
        if replay.name in names:
            raise ValueError("复盘名称重复，请为每份复盘设置不同名称")
        keys.add(replay.key)
        names.add(replay.name)
        if set(replay.required) - set(GROUPS):
            raise ValueError("复盘分组只支持「试点组」与「对照组」")
        for group, value in replay.required.items():
            if value < 1 or value > 50:
                raise ValueError(f"「{group}」设定家数须为 1–50")
        if replay.run_id is None:
            if not _valid_date(replay.pilot_date) or not 1 <= replay.window_days <= 28:
                raise ValueError(f"复盘「{replay.name}」的原窗口启动日或每段天数无效")
    if len(replays) > service.MAX_REPLAYS:
        raise ValueError(f"一次最多处理 {service.MAX_REPLAYS} 份复盘")


@router.get("/catalogs")
def catalogs(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(ArchiveCatalog).order_by(ArchiveCatalog.id.desc()).limit(60)).all()
    return [service.catalog_view(item) for item in items]


@router.get("/catalogs/{catalog_id}")
def catalog_detail(catalog_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(ArchiveCatalog, catalog_id)
    if item is None:
        raise HTTPException(404, "找不到该档案目录")
    return service.catalog_view(item, detail=True)


@router.post("/catalogs", status_code=201)
def upload_catalog(body: ArchiveUpload, db: Session = Depends(connection),
                   actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        parsed = parse_catalog(body.csv_text)
    except CatalogError as error:
        raise ValueError(str(error)) from None
    item, created = service.save_catalog(db, body.name, body.filename, parsed["packages"], actor.username)
    if created:
        record(db, actor.username, "catalog.imported", item.id,
               packages=item.package_count, days=item.day_count)
    db.commit()
    return {**service.catalog_view(item), "created": created}


@router.get("/plans")
def plans(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(ProcurementPlan).order_by(ProcurementPlan.id.desc()).limit(60)).all()
    stale = False
    for item in items:
        before = item.status
        service.expire_stale(db, item)
        stale = stale or item.status != before
    if stale:
        db.commit()
    return [service.plan_view(item) for item in items]


@router.post("/plans", status_code=202)
def create_plan(body: PlanRequest, db: Session = Depends(connection),
                actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    catalog = _validate_catalog_id(db, body.catalog_id)
    _validate_replays(db, body.replays)
    replays = []
    for replay in body.replays:
        if db.get(Dataset, replay.dataset_id) is None:
            raise HTTPException(404, f"复盘「{replay.name}」引用的数据批次不存在")
        pilot, window = replay.pilot_date, replay.window_days
        dataset_id = replay.dataset_id
        # 关联到已有差分运行时，沿用那次运行的原窗口与批次，避免手抄偏差。
        if replay.run_id is not None:
            run = db.get(Run, replay.run_id)
            if run is None:
                raise HTTPException(404, f"复盘「{replay.name}」关联的运行不存在")
            params = run.params or {}
            pilot = params.get("pilot_date")
            try:
                window = int(params.get("window_days", 0))
            except (TypeError, ValueError):
                window = 0
            dataset_id = run.dataset_id
            if not _valid_date(pilot) or not 1 <= window <= 28:
                raise ValueError(f"复盘「{replay.name}」关联的运行缺少有效的窗口参数")
        else:
            if not _valid_date(pilot) or not 1 <= window <= 28:
                raise ValueError(f"复盘「{replay.name}」的原窗口启动日或每段天数无效")
        replays.append({
            "key": replay.key, "name": replay.name, "dataset_id": dataset_id,
            "pilot_date": pilot, "window_days": window,
            "required": replay.required, "run_id": replay.run_id,
        })
    plan = ProcurementPlan(
        name=body.name, catalog_id=catalog.id, catalog_checksum=catalog.checksum,
        budget=body.budget, replays=replays, author=actor.username,
    )
    db.add(plan)
    db.flush()
    record(db, actor.username, "procurement.queued", plan.id,
           catalog_id=catalog.id, replays=len(plan.replays), budget=plan.budget)
    db.commit()
    wake()
    return service.plan_view(plan, detail=True)


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(ProcurementPlan, plan_id)
    if item is None:
        raise HTTPException(404, "找不到该取档方案")
    before = item.status
    service.expire_stale(db, item)
    if item.status != before:
        db.commit()
    return service.plan_view(item, detail=True)
