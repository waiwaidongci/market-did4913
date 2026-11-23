"""档案目录的演示数据：覆盖缺报复盘的部分缺报市场日，含共享包。"""

from datetime import date, timedelta

from src.domain.demo import demo_rows
from src.domain.spec import SPEC


def _replay_window(pilot, days):
    start = pilot - timedelta(days=2 * days)
    return [start + timedelta(days=offset) for offset in range(3 * days)]


def demo_catalog():
    """构造一份与缺报复盘相关的档案目录。

    演示批次含 7 家市场 21 天，仅东埠缺 11 月 12 日。目录里给出东埠补档包，
    以及若干覆盖完整市场的组合包（演示目录通常大于实际所需）。
    东埠补档包可让「对照组要求 4 家」的复盘达标，便于核对补齐与共享口径。
    """
    rows = demo_rows()
    observed = {}
    for row in rows:
        observed.setdefault(row["market_id"], set()).add(row["date"])
    pilot = date.fromisoformat(SPEC["params"][0]["default"])
    days = SPEC["params"][1]["default"]
    window = [day.isoformat() for day in _replay_window(pilot, days)]
    packages = []
    counter = 0

    def add(name, price, pairs):
        nonlocal counter
        counter += 1
        packages.append({"code": f"AR-{counter:02d}", "name": name, "price": price,
                         "days": sorted(set(pairs))})

    # 东埠在演示数据中缺少 11 月 12 日；其余市场在窗口内齐全。
    missing = [day for day in window if day not in observed.get("东埠", set())]
    for index, day in enumerate(missing, start=1):
        add(f"东埠 {day} 补档", 60, [("东埠", day)])
    # 以试点前段若干市场日组成组合包，价格更划算，可跨复盘共享。
    add("南桥西苑合并补档", 90, [("南桥", window[0]), ("西苑", window[0])])
    add("滨河两日补档", 110, [("滨河", window[0]), ("滨河", window[1])])
    add("对照组一日补档", 70, [("新街", window[2])])
    add("北仓石桥合并补档", 100, [("北仓", window[1]), ("石桥", window[1])])
    return packages
