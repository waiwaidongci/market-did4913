"""档案取档接口测试：权限、目录导入、方案创建与查询、失效联动。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_DEMO", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.controllers import auth
from src.models.identity import User  # noqa: F401  确保建表
from src.models import jobs, procurement as procurement_models, source  # noqa: F401
from src.routes import procurement as procurement_routes


def _catalog_csv(rows):
    lines = ["package_code,package_name,price,market_id,date"]
    lines.extend(",".join(str(part) for part in row) for row in rows)
    return "\n".join(lines) + "\n"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def connection():
        with factory() as db:
            yield db

    from src.main import app
    app.dependency_overrides[auth.current] = lambda: User(username="analyst", role="analyst")
    monkeypatch.setattr(procurement_routes, "wake", lambda: None)

    # 覆盖路由使用的 connection 依赖
    from src.config import database
    app.dependency_overrides[database.connection] = connection
    with TestClient(app) as api:
        yield api, factory
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


def _seed_dataset(factory, rows):
    from src.models.source import Dataset
    with factory() as db:
        item = Dataset(name="批次", filename="demo.csv", checksum="x" * 8,
                       rows=rows, row_count=len(rows), author="analyst")
        db.add(item)
        db.commit()
        return item.id


def test_import_catalog_and_create_plan(client):
    api, factory = client
    dataset_id = _seed_dataset(factory, [
        {"market_id": "南桥", "date": "2026-10-27", "group": "试点组",
         "wait_minutes": 10.0, "truck_count": 5},
        {"market_id": "南桥", "date": "2026-10-28", "group": "试点组",
         "wait_minutes": 10.0, "truck_count": 5},
    ])
    response = api.post("/catalogs", json={
        "name": "十月档案",
        "filename": "catalog.csv",
        "csv_text": _catalog_csv([
            ("P01", "包一", 50, "南桥", "2026-10-26"),
            ("P02", "包二", 50, "新街", "2026-10-27"),
        ]),
    })
    assert response.status_code == 201, response.text
    catalog = response.json()
    assert catalog["package_count"] == 2
    assert catalog["fresh"]

    listing = api.get("/catalogs").json()
    assert listing[0]["id"] == catalog["id"]

    detail = api.get(f"/catalogs/{catalog['id']}").json()
    assert {p["code"] for p in detail["packages"]} == {"P01", "P02"}

    plan = api.post("/plans", json={
        "name": "首版方案",
        "catalog_id": catalog["id"],
        "budget": 100,
        "replays": [{
            "key": "r0", "name": "复盘一", "dataset_id": dataset_id,
            "pilot_date": "2026-10-28", "window_days": 1,
            "required": {"试点组": 1},
        }],
    })
    assert plan.status_code == 202, plan.text
    body = plan.json()
    assert body["status"] == "queued"
    assert body["catalog_checksum"] == catalog["checksum"]


def test_import_new_revision_invalidates_existing_plan(client):
    api, factory = client
    dataset_id = _seed_dataset(factory, [
        {"market_id": "南桥", "date": "2026-10-27", "group": "试点组",
         "wait_minutes": 10.0, "truck_count": 5},
        {"market_id": "南桥", "date": "2026-10-28", "group": "试点组",
         "wait_minutes": 10.0, "truck_count": 5},
    ])
    first = api.post("/catalogs", json={
        "name": "十月档案", "filename": "a.csv",
        "csv_text": _catalog_csv([("P01", "包一", 50, "南桥", "2026-10-26")]),
    }).json()
    plan = api.post("/plans", json={
        "name": "方案", "catalog_id": first["id"], "budget": 100,
        "replays": [{"key": "r0", "name": "复盘一", "dataset_id": dataset_id,
                     "pilot_date": "2026-10-28", "window_days": 1,
                     "required": {"试点组": 1}}],
    }).json()
    assert plan["status"] == "queued"

    second = api.post("/catalogs", json={
        "name": "十月档案", "filename": "b.csv",
        "csv_text": _catalog_csv([("P01", "包一", 70, "南桥", "2026-10-26")]),
    }).json()
    assert second["id"] != first["id"]

    listed = api.get("/plans").json()
    assert listed[0]["status"] == "stale"
    assert "失效" in listed[0]["error"]

    # 旧修订不能再用于新建方案
    rejected = api.post("/plans", json={
        "name": "重算", "catalog_id": first["id"], "budget": 100,
        "replays": [{"key": "r0", "name": "复盘一", "dataset_id": dataset_id,
                     "pilot_date": "2026-10-28", "window_days": 1,
                     "required": {"试点组": 1}}],
    })
    assert rejected.status_code == 422


def test_viewer_cannot_import_or_create(client):
    api, factory = client
    from src.main import app
    app.dependency_overrides[auth.current] = lambda: User(username="viewer", role="viewer")
    blocked = api.post("/catalogs", json={"name": "x", "filename": "x.csv",
                                          "csv_text": "package_code,package_name,price,market_id,date\n"})
    assert blocked.status_code == 403


def test_plan_links_existing_run_to_reuse_original_window(client):
    api, factory = client
    from src.models.jobs import Run
    from src.models.source import Dataset
    with factory() as db:
        dataset = Dataset(name="批次", filename="d.csv", checksum="y" * 8, rows=[
            {"market_id": "南桥", "date": "2026-11-09", "group": "试点组",
             "wait_minutes": 10.0, "truck_count": 5},
        ], row_count=1, author="analyst")
        db.add(dataset)
        db.flush()
        run = Run(name="历史运行", dataset_id=dataset.id,
                  params={"pilot_date": "2026-11-09", "window_days": 5, "pretrend_threshold": 2},
                  author="analyst")
        db.add(run)
        db.commit()
        dataset_id, run_id = dataset.id, run.id
    catalog = api.post("/catalogs", json={
        "name": "目录", "filename": "c.csv",
        "csv_text": _catalog_csv([("P01", "包一", 50, "南桥", "2026-10-26")]),
    }).json()
    plan = api.post("/plans", json={
        "name": "方案", "catalog_id": catalog["id"], "budget": 100,
        "replays": [{"key": "r0", "name": "复盘", "dataset_id": dataset_id,
                     "pilot_date": "2026-01-01", "window_days": 7,
                     "required": {"试点组": 1}, "run_id": run_id}],
    })
    assert plan.status_code == 202, plan.text
    stored = plan.json()["replays"][0]
    # 原窗口来自关联运行，而不是提交时随手填的日期
    assert stored["pilot_date"] == "2026-11-09"
    assert stored["window_days"] == 5


def test_manual_window_requires_valid_dates(client):
    api, factory = client
    from src.models.source import Dataset
    with factory() as db:
        dataset = Dataset(name="批次", filename="d.csv", checksum="z" * 8, rows=[
            {"market_id": "南桥", "date": "2026-11-09", "group": "试点组",
             "wait_minutes": 10.0, "truck_count": 5},
        ], row_count=1, author="analyst")
        db.add(dataset)
        db.commit()
        dataset_id = dataset.id
    catalog = api.post("/catalogs", json={
        "name": "目录", "filename": "c.csv",
        "csv_text": _catalog_csv([("P01", "包一", 50, "南桥", "2026-10-26")]),
    }).json()
    bad = api.post("/plans", json={
        "name": "方案", "catalog_id": catalog["id"], "budget": 100,
        "replays": [{"key": "r0", "name": "复盘", "dataset_id": dataset_id,
                     "pilot_date": "2026/11/09", "window_days": 7,
                     "required": {"试点组": 1}}],
    })
    assert bad.status_code == 422
    ok = api.post("/plans", json={
        "name": "方案", "catalog_id": catalog["id"], "budget": 100,
        "replays": [{"key": "r0", "name": "复盘", "dataset_id": dataset_id,
                     "pilot_date": "2026-11-09", "window_days": 7,
                     "required": {"试点组": 1}}],
    })
    assert ok.status_code == 202, ok.text
