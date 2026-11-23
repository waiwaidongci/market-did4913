"""档案取档：目录导入、方案计算、目录失效联动。

规则
----
- 每次导入同名目录的新内容会产生一个新修订；旧修订被标记失效，
  依赖旧修订的取档方案一并置为「已失效」，提示重算。
- 计算完成后再校验一次目录修订；若期间目录被替换，结果直接标记失效。
- 失效的方案不再作为可用结果展示。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from src.constants.enums import ProcurementStatus
from src.domain import procurement
from src.models.procurement import ArchiveCatalog, ProcurementPlan
from src.models.source import Dataset
from src.services.audit import record
from src.utils.time import iso, now

MAX_REPLAYS = procurement.MAX_REPLAYS
STALE_REASON = "档案源数据已更新，旧方案失效，请重算后再使用"


def checksum(packages):
    raw = json.dumps(packages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def catalog_view(item, detail=False):
    view = {
        "id": item.id, "name": item.name, "filename": item.filename,
        "checksum": item.checksum, "package_count": item.package_count,
        "day_count": item.day_count, "author": item.author,
        "created_at": iso(item.created_at), "superseded_at": iso(item.superseded_at),
        "fresh": item.superseded_at is None,
    }
    if detail:
        view["packages"] = item.packages
    return view


def plan_view(item, detail=False):
    view = {
        "id": item.id, "name": item.name, "catalog_id": item.catalog_id,
        "catalog_checksum": item.catalog_checksum, "budget": item.budget,
        "replays": item.replays, "status": item.status, "error": item.error,
        "author": item.author, "created_at": iso(item.created_at),
        "finished_at": iso(item.finished_at),
    }
    if detail:
        view["result"] = item.result
    return view


def _mark_stale(db, plan, reason=STALE_REASON):
    if plan.status == ProcurementStatus.STALE:
        return
    plan.status = ProcurementStatus.STALE
    plan.error = reason[:500]
    plan.finished_at = plan.finished_at or now()
    record(db, "system", "procurement.stale", plan.id, reason=reason)


def save_catalog(db, name, filename, packages, actor):
    signature = checksum(packages)
    lookup = select(ArchiveCatalog).where(ArchiveCatalog.name == name,
                                          ArchiveCatalog.checksum == signature)
    existing = db.scalar(lookup)
    if existing is not None:
        return existing, False
    stamp = now()
    older = db.scalars(select(ArchiveCatalog).where(
        ArchiveCatalog.name == name,
        ArchiveCatalog.superseded_at.is_(None))).all()
    for old in older:
        old.superseded_at = stamp
        plans = db.scalars(select(ProcurementPlan).where(
            ProcurementPlan.catalog_id == old.id,
            ProcurementPlan.status != ProcurementStatus.STALE)).all()
        for plan in plans:
            _mark_stale(db, plan)
    item = ArchiveCatalog(name=name, filename=filename, checksum=signature,
                          packages=packages, package_count=len(packages),
                          day_count=len({(day[0], day[1]) for pkg in packages for day in pkg["days"]}),
                          author=actor)
    db.add(item)
    db.flush()
    return item, True


def replay_need(db, replay):
    """读取某份复盘引用的数据批次，推出缺报市场日。"""
    dataset = db.get(Dataset, replay["dataset_id"])
    if dataset is None:
        raise ValueError("复盘「%s」引用的数据批次已不存在" % replay.get("name", ""))
    spec = {
        "key": replay["key"], "name": replay["name"],
        "pilot_date": replay["pilot_date"], "window_days": replay["window_days"],
        "required": replay["required"],
    }
    return procurement.replay_need(spec, dataset.rows)


def optimize(needs, catalog, budget):
    return procurement.optimize(needs, catalog.packages, budget)


def build_result(needs, outcome, catalog, budget):
    selected = outcome["packages"]
    selected_days = [(day[0], day[1]) for item in selected for day in item["days"]]
    selected_codes = {item["code"] for item in selected}
    replays = []
    completed = 0
    for need in needs:
        report = procurement.coverage(need, selected_days)
        completed += 1 if report["satisfied"] else 0
        replays.append({
            "key": need["key"], "name": need["name"], "pilot_date": need["pilot_date"],
            "window_days": need["window_days"], "segments": need["segments"],
            "selected": need["key"] in outcome.get("selected_keys", ()),
            "satisfied": report["satisfied"],
            "groups": [
                {"name": name, "required": info["required"], "complete": info["complete"],
                 "satisfied": info["satisfied"],
                 "markets": [{"market_id": market["market_id"], "complete": market["complete"],
                              "missing": market["missing"]} for market in info["markets"]]}
                for name, info in report["groups"].items()
            ],
        })
    return {
        "completed": completed,
        "cost": sum(item["price"] for item in selected),
        "budget": budget,
        "optimal": outcome["optimal"],
        "package_total": outcome["package_total"],
        "candidate_total": len(needs),
        "catalog": {"id": catalog.id, "name": catalog.name, "checksum": catalog.checksum},
        "packages": [
            {"code": item["code"], "name": item["name"], "price": item["price"],
             "days": item["days"]}
            for item in sorted(selected, key=lambda item: item["code"])
        ],
        "replays": replays,
        "selected_codes": sorted(selected_codes),
    }


def expire_stale(db, plan):
    """在返回视图前校验方案所依赖的目录修订是否仍有效。"""
    catalog = db.get(ArchiveCatalog, plan.catalog_id)
    if (catalog is None or catalog.superseded_at is not None
            or catalog.checksum != plan.catalog_checksum):
        if plan.status not in (ProcurementStatus.STALE, ProcurementStatus.FAILED):
            _mark_stale(db, plan)
    return plan
