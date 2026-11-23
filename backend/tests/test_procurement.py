"""档案取档的领域、服务、接口与后台联动测试。

使用 SQLite 内存库与 FastAPI TestClient，覆盖：
- 目录解析与校验
- 三段补齐口径（原窗口、两家达标才算一份）
- 共享包只计一次、预算优先、同数取低价
- 精确求解与暴力枚举一致
- 目录更新使旧方案失效、计算晚结束也失效
- 页面可核对包与各份复盘可补齐的市场
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from itertools import combinations

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_DEMO", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.domain import procurement
from src.domain.procurement import (
    CatalogError,
    expected_days,
    optimize,
    parse_catalog,
    replay_need,
)

GROUPS = ("试点组", "对照组")


# ---------------------------------------------------------------- fixtures
def _replay(key, pilot, window_days, required, rows):
    return replay_need(
        {"key": key, "name": key, "pilot_date": pilot, "window_days": window_days,
         "required": required},
        rows,
    )


def _rows(market, group, all_days, skip=()):
    return [{"market_id": market, "date": day, "group": group,
             "wait_minutes": 10, "truck_count": 5}
            for day in all_days if day not in set(skip)]


def _package(index, price, days):
    return {"code": f"P{index:02d}", "name": f"包 {index}", "price": price,
            "days": sorted(days)}


class Brute:
    """独立实现的暴力枚举，仅用于小规模校验。"""

    def __init__(self, replays, packages, budget):
        self.budget = budget
        atom = {}
        self.replays = []
        for replay in replays:
            groups = []
            for group in replay["groups"]:
                units, base = [], 0
                for market in group["markets"]:
                    mask = 0
                    for day in market["missing"]:
                        mask |= 1 << atom.setdefault((market["market_id"], day), len(atom))
                    if mask:
                        units.append(mask)
                    else:
                        base += 1
                groups.append((group["required"], base, units))
            self.replays.append(groups)
        self.masks = []
        for package in packages:
            mask = 0
            for key in package["days"]:
                idx = atom.get(tuple(key))
                if idx is not None:
                    mask |= 1 << idx
            self.masks.append(mask)
        self.packages = packages

    def complete(self, cov):
        done = 0
        for groups in self.replays:
            if all(sum(1 for u in units if cov & u == u) + base >= required
                   for required, base, units in groups):
                done += 1
        return done

    def solve(self):
        best = (-1, 0)
        n = len(self.packages)
        for bits in range(1 << n):
            cost, cov, ok = 0, 0, True
            for i in range(n):
                if bits >> i & 1:
                    cost += self.packages[i]["price"]
                    if cost > self.budget:
                        ok = False
                        break
                    cov |= self.masks[i]
            if not ok:
                continue
            count = self.complete(cov)
            if count > best[0] or (count == best[0] and cost < best[1]):
                best = (count, cost)
        return best


# ---------------------------------------------------------------- catalog
def test_parse_catalog_groups_rows_and_rejects_bad_input():
    csv_text = (
        "package_code,package_name,price,market_id,date\n"
        "A01,包甲,120,南桥,2026-10-26\n"
        "A01,包甲,120,西苑,2026-10-27\n"
        "B02,包乙,80,滨河,2026-10-26\n"
    )
    parsed = parse_catalog(csv_text)
    assert [p["code"] for p in parsed["packages"]] == ["A01", "B02"]
    assert parsed["packages"][0]["price"] == 120
    assert len(parsed["packages"][0]["days"]) == 2
    assert parsed["day_count"] == 3

    with pytest.raises(CatalogError):
        parse_catalog("code,name,price,market,date\nA,包,1,南桥,2026-10-26\n")
    with pytest.raises(CatalogError):
        parse_catalog(
            "package_code,package_name,price,market_id,date\n"
            "A,包,120,南桥,2026-10-26\nA,包,130,西苑,2026-10-27\n")
    with pytest.raises(CatalogError):
        parse_catalog(
            "package_code,package_name,price,market_id,date\n"
            "A,包,0,南桥,2026-10-26\n")


# ---------------------------------------------------------------- 口径
def test_three_segments_all_required_and_original_window():
    days = [d.isoformat() for d in expected_days("2026-11-09", 7)]
    assert len(days) == 21
    # 一个市场缺「试点前段」中的一天
    rows = _rows("南桥", "试点组", days, skip={days[0]})
    need = _replay("r0", "2026-11-09", 7, {"试点组": 1}, rows)
    group = need["groups"][0]
    assert group["markets"][0]["missing"] == [days[0]]
    assert group["markets"][0]["observed_days"] == 20


def test_two_groups_both_must_reach_quota():
    days = [d.isoformat() for d in expected_days("2026-11-09", 7)]
    rows = (
        _rows("南桥", "试点组", days) + _rows("西苑", "试点组", days)
        + _rows("新街", "对照组", days)
    )
    need = _replay("r0", "2026-11-09", 7, {"试点组": 2, "对照组": 2}, rows)
    pkgs = [_package(1, 50, [("新街", days[0])])]  # 只补对照组第 2 家
    out = optimize([need], pkgs, 1000)
    assert out["completed"] == 0  # 对照组仅 1 家，未达 2 家
    out = optimize([need], pkgs + [_package(2, 50, [("石桥", days[0])])], 1000)
    # 石桥没有该市场记录，不在候选 -> 仍无法补齐
    assert out["completed"] == 0


def test_shared_package_charged_once():
    days = [d.isoformat() for d in expected_days("2026-11-09", 7)]
    r0 = _rows("南桥", "试点组", days, skip={days[0]}) + _rows("新街", "对照组", days)
    r1 = _rows("南桥", "试点组", days, skip={days[0]}) + _rows("石桥", "对照组", days)
    replays = [
        _replay("r0", "2026-11-09", 7, {"试点组": 1, "对照组": 1}, r0),
        _replay("r1", "2026-11-09", 7, {"试点组": 1, "对照组": 1}, r1),
    ]
    shared = _package(1, 100, [("南桥", days[0])])
    out = optimize(replays, [shared], 1000)
    # 同一包同时补齐两份，费用只算一次
    assert out["completed"] == 2
    assert out["cost"] == 100
    assert [p["code"] for p in out["packages"]] == ["P01"]


def test_budget_and_tie_break_lowest_price():
    days = [d.isoformat() for d in expected_days("2026-11-09", 7)]
    rows = (
        _rows("南桥", "试点组", days, skip={days[0]})
        + _rows("西苑", "试点组", days, skip={days[1]})
        + _rows("新街", "对照组", days, skip={days[2]})
        + _rows("石桥", "对照组", days, skip={days[3]})
        + _rows("北仓", "对照组", days, skip={days[4]})
    )
    need = _replay("r0", "2026-11-09", 7, {"试点组": 2, "对照组": 2}, rows)
    # 便宜组合 vs 昂贵组合都能补齐；同数取低价
    cheap = [
        _package(1, 30, [("南桥", days[0])]),
        _package(2, 30, [("西苑", days[1])]),
        _package(3, 30, [("新街", days[2])]),
        _package(4, 30, [("石桥", days[3])]),
    ]
    pricey = [
        _package(5, 90, [("南桥", days[0])]),
        _package(6, 90, [("西苑", days[1])]),
        _package(7, 90, [("新街", days[2])]),
        _package(8, 90, [("石桥", days[3])]),
    ]
    out = optimize([need], cheap + pricey, 1000)
    assert out["completed"] == 1
    assert out["cost"] == 120
    # 预算不足时补齐 0 份
    out = optimize([need], cheap + pricey, 100)
    assert out["completed"] == 0
    assert out["cost"] == 0


def test_exact_against_brute_force():
    import random
    rnd = random.Random(20261123)
    for _ in range(40):
        days = [d.isoformat() for d in expected_days("2026-11-09", 3)]
        replays = []
        for r in range(3):
            markets = []
            rows = []
            for g in GROUPS:
                for m in range(3):
                    mid = f"{g}-{m}-{r}"
                    skip = {d for d in days if rnd.random() < 0.5}
                    rows += _rows(mid, g, days, skip=skip)
            replays.append(_replay(f"r{r}", "2026-11-09", 3, {GROUPS[0]: 2, GROUPS[1]: 2}, rows))
        pool = sorted({(m["market_id"], d) for r in replays for g in r["groups"]
                       for m in g["markets"] for d in m["missing"]})
        packages = []
        for i in range(12):
            if not pool:
                break
            chosen = rnd.sample(pool, rnd.randint(1, min(3, len(pool))))
            packages.append(_package(i, rnd.randint(5, 40), chosen))
        if not packages:
            continue
        budget = rnd.randint(0, sum(p["price"] for p in packages))
        got = optimize(replays, packages, budget)
        expect = Brute(replays, packages, budget).solve()
        assert (got["completed"], got["cost"]) == expect, (budget, got, expect)


def test_sixty_packages_solve_quickly():
    import random
    import time
    rnd = random.Random(60)
    replays = []
    for r in range(5):
        days = [d.isoformat() for d in expected_days(f"2026-10-{10 + r:02d}", 7)]
        rows = []
        for g in GROUPS:
            for m in range(5):
                mid = f"{g}-{m}-{r}"
                mode = rnd.random()
                if mode < 0.25:
                    skip = set()
                elif mode < 0.6:
                    skip = {d for d in days if rnd.random() < 0.12}
                else:
                    skip = {d for d in days if rnd.random() < 0.4}
                rows += _rows(mid, g, days, skip=skip)
        replays.append(_replay(f"r{r}", f"2026-10-{10 + r:02d}", 7,
                               {GROUPS[0]: 3, GROUPS[1]: 3}, rows))
    pool = sorted({(m["market_id"], d) for r in replays for g in r["groups"]
                   for m in g["markets"] for d in m["missing"]})
    packages = []
    seen = set()
    while len(packages) < 60 and pool:
        market = rnd.choice(sorted({m for m, _ in pool}))
        dates = sorted(d for m, d in pool if m == market)
        length = rnd.randint(1, min(5, len(dates)))
        start = rnd.randrange(len(dates))
        days = tuple(sorted({(market, dates[(start + j) % len(dates)]) for j in range(length)}))
        if days in seen:
            continue
        seen.add(days)
        packages.append(_package(len(packages), rnd.randint(20, 90), days))
    budget = int(sum(p["price"] for p in packages) * 0.4)
    started = time.monotonic()
    out = optimize(replays, packages, budget, time_limit=20)
    elapsed = time.monotonic() - started
    assert out["optimal"]
    assert elapsed < 20
    assert out["completed"] >= 0


# ---------------------------------------------------------------- service/API
@pytest.fixture()
def session(monkeypatch):
    from src.config.database import Base
    from src.models import identity, jobs, procurement as procurement_models, source  # noqa: F401
    from src.services import procurement_execution
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(procurement_execution, "Session", factory)
    with factory() as db:
        yield db
    Base.metadata.drop_all(engine)


def _catalog_csv(packages):
    lines = ["package_code,package_name,price,market_id,date"]
    for package in packages:
        for market, day in package["days"]:
            lines.append(f"{package['code']},{package['name']},{package['price']},{market},{day}")
    return "\n".join(lines) + "\n"


def _dataset(db, market_days):
    from src.models.source import Dataset
    rows = [{"market_id": m, "date": d, "group": g, "wait_minutes": 10.0, "truck_count": 5}
            for (m, g, d) in market_days]
    item = Dataset(name=f"ds-{len(market_days)}", filename="demo.csv", checksum=str(abs(hash(str(rows)))),
                   rows=rows, row_count=len(rows), author="analyst")
    db.add(item)
    db.flush()
    return item


def test_catalog_import_supersedes_old_revision(session):
    from src.services import procurement as service
    first, created = service.save_catalog(session, "十月目录", "a.csv",
                                          [_package(1, 50, [("南桥", "2026-10-26")])], "analyst")
    session.commit()
    assert created
    second, created = service.save_catalog(session, "十月目录", "b.csv",
                                           [_package(1, 60, [("南桥", "2026-10-26")])], "analyst")
    session.commit()
    assert created
    session.refresh(first)
    session.refresh(second)
    assert first.superseded_at is not None
    assert second.superseded_at is None


def test_worker_marks_stale_when_catalog_replaced_midway(session):
    """旧目录上的计算晚结束也失效。"""
    from src.models.procurement import ProcurementPlan
    from src.services import procurement as service
    from src.services import procurement_execution as runner

    catalog, _ = service.save_catalog(
        session, "目录", "a.csv",
        [_package(1, 50, [("南桥", "2026-10-26")]), _package(2, 50, [("新街", "2026-10-27")])],
        "analyst")
    session.commit()
    dataset = _dataset(session, [
        ("南桥", "试点组", "2026-10-26"), ("南桥", "试点组", "2026-10-27"),
        ("新街", "对照组", "2026-10-26"),
    ])
    session.commit()
    plan = ProcurementPlan(name="方案", catalog_id=catalog.id, catalog_checksum=catalog.checksum,
                           budget=200, replays=[{
                               "key": "r0", "name": "复盘一", "dataset_id": dataset.id,
                               "pilot_date": "2026-10-28", "window_days": 1,
                               "required": {"试点组": 1, "对照组": 1}}], author="analyst")
    session.add(plan)
    session.commit()

    lease = runner.claim()
    assert lease == (plan.id, 1)
    # 计算期间目录被新修订替换
    service.save_catalog(session, "目录", "b.csv",
                         [_package(3, 70, [("南桥", "2026-10-26")])], "analyst")
    session.commit()
    runner.execute(lease)
    session.refresh(plan)
    assert plan.status == "stale"
    assert "失效" in plan.error


def test_worker_completes_plan_with_verifiable_coverage(session):
    from src.models.procurement import ProcurementPlan
    from src.services import procurement as service
    from src.services import procurement_execution as runner

    packages = [
        _package(1, 40, [("南桥", "2026-10-26")]),
        _package(2, 40, [("新街", "2026-10-27")]),
    ]
    catalog, _ = service.save_catalog(session, "目录", "a.csv", packages, "analyst")
    session.commit()
    dataset = _dataset(session, [
        ("南桥", "试点组", "2026-10-27"), ("南桥", "试点组", "2026-10-28"),
        ("新街", "对照组", "2026-10-26"), ("新街", "对照组", "2026-10-28"),
    ])
    session.commit()
    plan = ProcurementPlan(name="方案", catalog_id=catalog.id, catalog_checksum=catalog.checksum,
                           budget=100, replays=[{
                               "key": "r0", "name": "复盘一", "dataset_id": dataset.id,
                               "pilot_date": "2026-10-28", "window_days": 1,
                               "required": {"试点组": 1, "对照组": 1}}], author="analyst")
    session.add(plan)
    session.commit()
    runner.execute(runner.claim())
    session.refresh(plan)
    assert plan.status == "complete"
    result = plan.result
    assert result["completed"] == 1
    assert result["cost"] == 80
    assert [p["code"] for p in result["packages"]] == ["P01", "P02"]
    replay = result["replays"][0]
    assert replay["satisfied"]
    groups = {g["name"]: g for g in replay["groups"]}
    assert groups["试点组"]["complete"] == 1
    assert groups["对照组"]["complete"] == 1


def test_demo_catalog_is_well_formed_and_seedable():
    from src.domain.demo_procurement import demo_catalog
    packages = demo_catalog()
    assert packages
    for package in packages:
        assert package["price"] > 0
        assert package["days"]
        assert all(len(day) == 2 for day in package["days"])
    # 演示批次里东埠缺 11 月 12 日，目录中应有对应补档包
    assert any(("东埠", "2026-11-12") in package["days"] for package in packages)


def test_plan_on_old_revision_never_runs_after_catalog_update(session):
    """方案基于旧的修订、目录已换版：失效在导入时即生效，不再被领取计算。"""
    from src.models.procurement import ProcurementPlan
    from src.services import procurement as service
    from src.services import procurement_execution as runner

    old, _ = service.save_catalog(
        session, "目录", "a.csv", [_package(1, 50, [("南桥", "2026-10-26")])], "analyst")
    session.commit()
    dataset = _dataset(session, [
        ("南桥", "试点组", "2026-10-26"), ("南桥", "试点组", "2026-10-27"),
    ])
    session.commit()
    plan = ProcurementPlan(name="旧方案", catalog_id=old.id, catalog_checksum=old.checksum,
                           budget=100, replays=[{
                               "key": "r0", "name": "复盘一", "dataset_id": dataset.id,
                               "pilot_date": "2026-10-28", "window_days": 1,
                               "required": {"试点组": 1}}], author="analyst")
    session.add(plan)
    session.commit()
    # 目录在方案入队后、计算开始前已被替换
    service.save_catalog(session, "目录", "b.csv",
                         [_package(2, 60, [("南桥", "2026-10-26")])], "analyst")
    session.commit()
    session.refresh(plan)
    assert plan.status == "stale"
    assert plan.result is None
    # 已失效的方案不会被领取计算
    assert runner.claim() is None


def test_reimport_same_content_does_not_invalidate(session):
    from src.models.procurement import ProcurementPlan
    from src.services import procurement as service

    packages = [_package(1, 50, [("南桥", "2026-10-26")])]
    first, _ = service.save_catalog(session, "目录", "a.csv", packages, "analyst")
    session.commit()
    dataset = _dataset(session, [("南桥", "试点组", "2026-10-26")])
    session.commit()
    plan = ProcurementPlan(name="方案", catalog_id=first.id, catalog_checksum=first.checksum,
                           budget=100, replays=[{
                               "key": "r0", "name": "复盘", "dataset_id": dataset.id,
                               "pilot_date": "2026-10-28", "window_days": 1,
                               "required": {"试点组": 1}}], author="analyst")
    session.add(plan)
    session.commit()
    again, created = service.save_catalog(session, "目录", "a.csv", packages, "analyst")
    session.commit()
    assert not created
    assert again.id == first.id
    session.refresh(plan)
    assert plan.status == "queued"
