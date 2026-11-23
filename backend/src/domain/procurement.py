"""档案取档：按包计费，补齐缺报复盘。

业务口径
--------
- 档案目录按「包」计价，一个包包含若干可补的市场日（市场 + 日期）。
- 一份缺报复盘沿用其原窗口：启动日前两段、启动日后一段，共三段等长。
  某市场三段逐日齐全才算「补齐一家」；两组都达到设定家数才算补齐这份复盘。
- 同一个包可以被多份复盘共用，费用只计一次。
- 目标是预算内补齐份数最多；份数相同时取总价最低。
- 目录可以有六十个以上的包，但枚举的是「要补齐哪几份复盘」而不是全部包组合；
  每个目标再用分支定界精确求最少花费，因此不必逐一试完所有包组合。
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from itertools import combinations

CATALOG_COLUMNS = ("package_code", "package_name", "price", "market_id", "date")

# 精确求解的规模上限：复盘份数少（业务上是「几份」），包可以到六十个以上。
MAX_REPLAYS = 10
MAX_PACKAGES = 400
MAX_PRICE = 10_000_000


class CatalogError(ValueError):
    """档案目录 CSV 不符合字段口径。"""


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def parse_catalog(text: str):
    """解析档案目录 CSV，按包聚合。

    一行代表一个包可补的一个市场日；同一 ``package_code`` 的价格与名称须各自一致。
    """
    reader = csv.DictReader(io.StringIO(text.removeprefix("﻿"), newline=""), strict=True)
    headers = reader.fieldnames or []
    if tuple(headers) != CATALOG_COLUMNS:
        raise CatalogError("档案目录表头须为 package_code,package_name,price,market_id,date")
    packages = {}
    days = set()
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise CatalogError(f"第 {reader.line_num} 行列数与表头不一致")
            code = (row["package_code"] or "").strip()
            name = (row["package_name"] or "").strip()
            market = (row["market_id"] or "").strip()
            raw_price = (row["price"] or "").strip()
            raw_date = (row["date"] or "").strip()
            if not code or len(code) > 60:
                raise CatalogError(f"第 {reader.line_num} 行 package_code 须为 1–60 个字符")
            if not name or len(name) > 120:
                raise CatalogError(f"第 {reader.line_num} 行 package_name 须为 1–120 个字符")
            if not market or len(market) > 300:
                raise CatalogError(f"第 {reader.line_num} 行 market_id 须为 1–300 个字符")
            try:
                price = int(raw_price)
            except ValueError:
                raise CatalogError(f"第 {reader.line_num} 行 price 须为正整数（元）") from None
            if not _positive_int(price) or price > MAX_PRICE:
                raise CatalogError(f"第 {reader.line_num} 行 price 须为 1–{MAX_PRICE} 的整数（元）")
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                raise CatalogError(f"第 {reader.line_num} 行 date 须为 YYYY-MM-DD") from None
            if day.isoformat() != raw_date:
                raise CatalogError(f"第 {reader.line_num} 行 date 须为规范的 YYYY-MM-DD")
            current = packages.get(code)
            entry = (market, day.isoformat())
            if current is None:
                packages[code] = {"code": code, "name": name, "price": price, "days": {entry}}
            else:
                if current["price"] != price:
                    raise CatalogError(f"档案包 {code} 的价格前后不一致")
                if current["name"] != name:
                    raise CatalogError(f"档案包 {code} 的名称前后不一致")
                current["days"].add(entry)
            days.add(entry)
    except csv.Error:
        raise CatalogError(f"第 {reader.line_num} 行附近格式不正确") from None
    if not packages:
        raise CatalogError("档案目录至少需要一个档案包")
    if len(packages) > MAX_PACKAGES:
        raise CatalogError(f"一个档案目录最多 {MAX_PACKAGES} 个包")
    result = [{"code": item["code"], "name": item["name"], "price": item["price"],
               "days": sorted(item["days"])}
              for item in sorted(packages.values(), key=lambda item: item["code"])]
    return {"packages": result, "day_count": len(days)}


def expected_days(pilot_date: str, days: int):
    """三段窗口的全部应报日期（试点前两段 + 试点后一段，启动日计入试点后）。"""
    start = date.fromisoformat(pilot_date) - timedelta(days=2 * days)
    return [start + timedelta(days=offset) for offset in range(3 * days)]


def segment_labels(pilot_date: str, days: int):
    """三个阶段的日期范围标签（含首尾）。"""
    first = date.fromisoformat(pilot_date)
    start = first - timedelta(days=2 * days)
    return {
        "试点前段": [start.isoformat(), (first - timedelta(days=days + 1)).isoformat()],
        "试点前后段": [(first - timedelta(days=days)).isoformat(),
                 (first - timedelta(days=1)).isoformat()],
        "试点后": [first.isoformat(), (first + timedelta(days=days - 1)).isoformat()],
    }


def replay_need(replay, rows):
    """由复盘窗口与其门岗明细，推出各市场仍缺的市场日。

    ``replay``: ``{"key","name","pilot_date","window_days","required":{分组: 家数}}``。
    ``rows``: 该复盘数据批次的门岗日记录（含 market_id/date/group）。
    """
    expected = expected_days(replay["pilot_date"], replay["window_days"])
    expected_set = {day.isoformat() for day in expected}
    present = {}
    for row in rows:
        if row["date"] not in expected_set:
            continue
        observed = present.setdefault(row["market_id"], {"group": row["group"], "dates": set()})
        observed["dates"].add(row["date"])
    groups = {name: {"name": name, "required": required, "markets": []}
              for name, required in replay["required"].items()}
    for market in sorted(present):
        info = present[market]
        if info["group"] not in groups:
            continue
        missing = sorted(expected_set - info["dates"])
        groups[info["group"]]["markets"].append({
            "market_id": market,
            "missing": missing,
            "observed_days": len(info["dates"]),
            "expected_days": len(expected_set),
        })
    return {
        "key": replay["key"], "name": replay["name"],
        "pilot_date": replay["pilot_date"], "window_days": replay["window_days"],
        "segments": segment_labels(replay["pilot_date"], replay["window_days"]),
        "groups": list(groups.values()),
    }


def _prepare(replays, packages):
    """把复盘和包编码成原子位图，并做等价原子合并与包去重/占优剔除。"""
    atom = {}
    prepared = []
    for index, replay in enumerate(replays):
        groups = []
        for group in replay["groups"]:
            units, base = [], 0
            for market in group["markets"]:
                ids = [atom.setdefault((market["market_id"], day), len(atom))
                       for day in market["missing"]]
                if ids:
                    units.append(ids)
                else:
                    base += 1
            groups.append({"name": group["name"], "required": group["required"],
                           "base": base, "units": units})
        prepared.append({"index": index, "key": replay["key"], "groups": groups})
    atom_count = len(atom)

    raw = []
    for index, package in enumerate(packages):
        keys = [key for key in map(tuple, package["days"]) if key in atom]
        ids = sorted({atom[key] for key in keys})
        if ids:
            raw.append({"id": index, "code": package["code"], "name": package["name"],
                        "price": package["price"], "atoms": ids,
                        "days": sorted([list(key) for key in keys])})
    signature = {}
    for position, package in enumerate(raw):
        for value in package["atoms"]:
            signature.setdefault(value, []).append(position)
    classes, remap = {}, {}
    for value in range(atom_count):
        remap[value] = classes.setdefault(tuple(signature.get(value, ())), len(classes))
    covered = 0
    for package in raw:
        mask = 0
        for value in package["atoms"]:
            mask |= 1 << remap[value]
        package["mask"] = mask
        covered |= mask

    for replay in prepared:
        for group in replay["groups"]:
            units = []
            for ids in group["units"]:
                mask = 0
                for value in ids:
                    mask |= 1 << remap[value]
                if not mask & ~covered:
                    units.append(mask)
            group["units"] = units

    by_mask = {}
    for package in raw:
        current = by_mask.get(package["mask"])
        if current is None or (package["price"], package["id"]) < (current["price"], current["id"]):
            by_mask[package["mask"]] = package
    candidates = list(by_mask.values())
    kept = []
    for package in candidates:
        dead = False
        for other in candidates:
            if other is package:
                continue
            if package["mask"] & other["mask"] == package["mask"] and other["price"] <= package["price"]:
                if (other["price"] < package["price"] or other["mask"] != package["mask"]
                        or other["id"] < package["id"]):
                    dead = True
                    break
        if not dead:
            kept.append(package)
    return prepared, kept


class _Solver:
    """在「补齐哪些复盘」上枚举，每次求精确最小包价。"""

    def __init__(self, replays, packages, budget, deadline):
        self.budget = budget
        self.deadline = deadline
        self.timed_out = False
        self.nodes = 0
        self.replays, self.packages = _prepare(replays, packages)
        self.price = [item["price"] for item in self.packages]
        self.mask = [item["mask"] for item in self.packages]
        self.coverers = {}
        for index, mask in enumerate(self.mask):
            value = mask
            while value:
                low = value & -value
                self.coverers.setdefault(low.bit_length() - 1, []).append(index)
                value ^= low
        for key in self.coverers:
            self.coverers[key].sort(key=lambda i: (self.price[i], -self.mask[i].bit_count()))
        self.cheapest = {key: min(self.price[i] for i in values)
                         for key, values in self.coverers.items()}
        self.cooccur = {}
        for mask in self.mask:
            value = mask
            while value:
                low = value & -value
                key = low.bit_length() - 1
                self.cooccur[key] = self.cooccur.get(key, 0) | mask
                value ^= low
        self._covers = {}
        self._replay_masks = {}

    def _bound(self, uncovered):
        """互不能被同一包覆盖的原子的「最便宜覆盖价」之和，是有效下界。"""
        remaining = uncovered
        total = 0
        while remaining:
            pivot, pivot_price = -1, -1
            value = remaining
            while value:
                low = value & -value
                key = low.bit_length() - 1
                if self.cheapest[key] > pivot_price:
                    pivot, pivot_price = key, self.cheapest[key]
                value ^= low
            total += pivot_price
            remaining &= ~self.cooccur[pivot]
        return total

    def _greedy(self, target):
        remaining, cost, plan = target, 0, []
        while remaining:
            best = None
            for index in range(len(self.packages)):
                gain = (self.mask[index] & remaining).bit_count()
                if not gain:
                    continue
                ratio = (self.price[index] / gain, self.price[index], -gain)
                if best is None or ratio < best[0]:
                    best = (ratio, index)
            if best is None:
                return None, None
            index = best[1]
            remaining &= ~self.mask[index]
            cost += self.price[index]
            plan.append(index)
        return cost, plan

    def cover(self, target):
        """精确求覆盖目标原子集合的最低包价。返回 ``(价格, 包下标元组)`` 或 ``(None, None)``。"""
        if target == 0:
            return 0, ()
        if target in self._covers:
            return self._covers[target]
        atoms = [value for value in range(target.bit_length()) if target >> value & 1]
        if any(value not in self.coverers for value in atoms):
            self._covers[target] = (None, None)
            return self._covers[target]
        cost, plan = self._greedy(target)
        if cost is None:
            self._covers[target] = (None, None)
            return self._covers[target]
        state = [cost if cost <= self.budget else self.budget + 1,
                 plan if cost <= self.budget else None]
        memo = {}

        def search(uncovered, spend, chosen):
            self.nodes += 1
            if self.nodes % 1024 == 0 and time.monotonic() > self.deadline:
                self.timed_out = True
                return
            if self.timed_out:
                return
            if not uncovered:
                if spend < state[0]:
                    state[0], state[1] = spend, list(chosen)
                return
            if spend + self._bound(uncovered) >= state[0]:
                return
            previous = memo.get(uncovered)
            if previous is not None and previous <= spend:
                return
            memo[uncovered] = spend
            pivot = min((value for value in atoms if uncovered >> value & 1),
                        key=lambda value: len(self.coverers[value]))
            for index in self.coverers[pivot]:
                spend_next = spend + self.price[index]
                if spend_next >= state[0]:
                    continue
                chosen.append(index)
                search(uncovered & ~self.mask[index], spend_next, chosen)
                chosen.pop()
                if self.timed_out:
                    return

        search(target, 0, [])
        if self.timed_out or state[0] > self.budget or state[1] is None:
            self._covers[target] = (None, None)
        else:
            self._covers[target] = (state[0], tuple(state[1]))
        return self._covers[target]

    def replay_masks(self, index):
        """使某份复盘达标的全部最小原子并集。"""
        cached = self._replay_masks.get(index)
        if cached is not None:
            return cached
        groups = []
        for group in self.replays[index]["groups"]:
            need = group["required"] - group["base"]
            if need <= 0:
                continue
            if need > len(group["units"]):
                self._replay_masks[index] = []
                return []
            combos = []
            for combo in combinations(range(len(group["units"])), need):
                mask = 0
                for position in combo:
                    mask |= group["units"][position]
                combos.append(mask)
            groups.append(combos)
        if not groups:
            self._replay_masks[index] = [0]
            return [0]
        states = {0}
        for combos in groups:
            states = {state | mask for state in states for mask in combos}
        minimal = []
        for mask in sorted(states, key=int.bit_count):
            if not any(known & mask == known for known in minimal):
                minimal.append(mask)
        self._replay_masks[index] = minimal
        return minimal

    def subset(self, subset, upper):
        masks = sorted((self.replay_masks(index) for index in subset), key=len)
        if any(not options for options in masks):
            return None
        states = {0}
        for options in masks:
            combined = set()
            for state in states:
                for mask in options:
                    combined.add(state | mask)
            minimal = []
            for mask in sorted(combined, key=int.bit_count):
                if not any(known & mask == known for known in minimal):
                    minimal.append(mask)
            states = set(minimal)
            if not states:
                return None
        best = None
        for mask in sorted(states, key=int.bit_count):
            cost, plan = self.cover(mask)
            if self.timed_out:
                return None
            if cost is not None and cost < upper and (best is None or cost < best[0]):
                best = (cost, plan)
        return best

    def solve(self):
        count = len(self.replays)
        complete = sum(1 for replay in self.replays
                       if all(group["base"] >= group["required"] for group in replay["groups"]))
        result = {"completed": complete, "cost": 0, "subset": [], "packages": []}
        for size in range(count, complete, -1):
            if time.monotonic() > self.deadline:
                self.timed_out = True
                break
            found = None
            upper = self.budget + 1
            for subset in combinations(range(count), size):
                got = self.subset(subset, upper)
                if got is not None:
                    found = (got[0], subset, got[1])
                    upper = got[0]
                if self.timed_out:
                    break
            if self.timed_out:
                break
            if found is not None:
                result = {"completed": size, "cost": found[0], "subset": list(found[1]),
                          "packages": sorted(found[2])}
                break
        chosen = [self.packages[position] for position in result["packages"]]
        return {
            "completed": result["completed"],
            "cost": result["cost"],
            "optimal": not self.timed_out,
            "replay_indices": [self.replays[index]["index"] for index in result["subset"]],
            "selected_keys": [self.replays[index]["key"] for index in result["subset"]],
            "packages": [{"code": item["code"], "name": item["name"], "price": item["price"],
                          "days": item["days"]} for item in chosen],
            "package_total": len(self.packages),
            "candidate_total": len(self.replays),
        }


def optimize(replay_needs, packages, budget, time_limit=30.0):
    """求预算内补齐份数最多、同数总价最低的取档方案。"""
    if type(budget) is not int or budget < 0:
        raise ValueError("预算须为非负整数（元）")
    if len(replay_needs) > MAX_REPLAYS:
        raise ValueError(f"一次最多处理 {MAX_REPLAYS} 份复盘")
    for replay in replay_needs:
        for group in replay["groups"]:
            if type(group["required"]) is not int or group["required"] < 1:
                raise ValueError("每组设定家数须为正整数")
    solver = _Solver(replay_needs, packages, budget, time.monotonic() + time_limit)
    return solver.solve()


def coverage(replay_need, selected_days):
    """按选中的包核算一份复盘：哪些市场补齐、各组家数是否达标。"""
    selected = {(market, day) for market, day in selected_days}
    groups = {}
    for group in replay_need["groups"]:
        rows = []
        done = 0
        for market in group["markets"]:
            missing = [day for day in market["missing"]
                       if (market["market_id"], day) not in selected]
            complete = not missing
            done += 1 if complete else 0
            rows.append({"market_id": market["market_id"], "complete": complete, "missing": missing})
        groups[group["name"]] = {"required": group["required"], "complete": done,
                                 "markets": rows, "satisfied": done >= group["required"]}
    return {"satisfied": all(group["satisfied"] for group in groups.values()), "groups": groups}
