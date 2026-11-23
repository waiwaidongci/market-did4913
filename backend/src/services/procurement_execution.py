"""取档方案的后台计算：租约领取、计算、目录换版校验。"""

import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select

from src.config.database import Session
from src.constants.enums import ProcurementStatus
from src.models.procurement import ArchiveCatalog, ProcurementPlan
from src.services import procurement as service
from src.services.audit import record
from src.utils.time import now

log = logging.getLogger(__name__)


def claim():
    """领取最旧的待算方案；运行中且租约过期的也可重新领取。"""
    stamp = now()
    with Session.begin() as db:
        query = select(ProcurementPlan).where(
            or_(ProcurementPlan.status == ProcurementStatus.QUEUED,
                and_(ProcurementPlan.status == ProcurementStatus.RUNNING,
                     ProcurementPlan.lease_until < stamp)))
        plan = db.scalar(query.order_by(ProcurementPlan.id).limit(1).with_for_update(skip_locked=True))
        if plan is None:
            return None
        if plan.attempt >= 3:
            plan.status = ProcurementStatus.FAILED
            plan.error = "后台进程多次中断，请重新发起取档方案"
            plan.finished_at = stamp
            record(db, "worker", "procurement.failed", plan.id, reason=plan.error)
            return None
        plan.status = ProcurementStatus.RUNNING
        plan.attempt += 1
        plan.lease_until = stamp + timedelta(minutes=3)
        return plan.id, plan.attempt


def _compute(db, plan):
    """在当前会话内计算方案；目录已换版时抛 ValueError。"""
    catalog = db.get(ArchiveCatalog, plan.catalog_id)
    if (catalog is None or catalog.superseded_at is not None
            or catalog.checksum != plan.catalog_checksum):
        raise ValueError(service.STALE_REASON)
    needs = [service.replay_need(db, replay) for replay in plan.replays]
    outcome = service.optimize(needs, catalog, plan.budget)
    return service.build_result(needs, outcome, catalog, plan.budget)


def execute(lease):
    plan_id, attempt = lease
    result = failure = stale = False
    with Session() as db:
        plan = db.get(ProcurementPlan, plan_id)
        if plan is None:
            return
        try:
            result = _compute(db, plan)
        except ValueError as error:
            failure = str(error)[:500]
            stale = failure == service.STALE_REASON
        except Exception:
            log.exception("Procurement %s failed", plan_id)
            failure = "计算失败，请检查档案目录与复盘参数后重试"
    with Session.begin() as db:
        plan = db.get(ProcurementPlan, plan_id, with_for_update=True)
        if plan is None or plan.attempt != attempt or plan.status != ProcurementStatus.RUNNING:
            return
        catalog = db.get(ArchiveCatalog, plan.catalog_id)
        expired = (catalog is None or catalog.superseded_at is not None
                   or catalog.checksum != plan.catalog_checksum)
        plan.lease_until = None
        plan.finished_at = now()
        if expired or stale:
            plan.status = ProcurementStatus.STALE
            plan.error = service.STALE_REASON
            plan.result = None
            record(db, "worker", "procurement.stale", plan.id, attempt=attempt)
        elif failure:
            plan.status = ProcurementStatus.FAILED
            plan.error = failure
            record(db, "worker", "procurement.failed", plan.id, attempt=attempt, reason=failure)
        else:
            plan.status = ProcurementStatus.COMPLETE
            plan.result = result
            plan.error = None
            record(db, "worker", "procurement.complete", plan.id, attempt=attempt,
                   completed=result["completed"], cost=result["cost"])
