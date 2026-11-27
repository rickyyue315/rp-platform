"""產生虛擬主數據，供公開版本使用。

所有店號、vendor、SKU、車期代碼都是虛構的，與任何真實系統無關。
執行：
    python scripts/make_seed.py
會在 seed/ 寫出六張表，在 samples/ 寫出兩份示範輸入。
"""
from __future__ import annotations

import os
import random
from itertools import product

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "seed")
SAMPLES = os.path.join(ROOT, "samples")

random.seed(42)

SHOPS = [f"S{i:03d}" for i in range(1, 25)]          # S001–S024
VENDORS = [f"V{i:03d}" for i in range(1, 41)]        # V001–V040
MCS = [f"1{i:05d}" for i in range(1, 21)]            # 100001–100020
SKUS = [f"SKU{i:06d}" for i in range(1, 201)]        # SKU000001–SKU000200
CYCLE_P = ["P01", "P02", "P05", "P07", "W12", "09A", "P10", "P03"]
CYCLE_D = ["D04", "D10", "D14", "D18", "D22", "D28", "W24", "D36"]


def calendar():
    rows = []
    for i, shop in enumerate(SHOPS):
        rows.append([shop, CYCLE_P[i % len(CYCLE_P)], CYCLE_D[i % len(CYCLE_D)]])
    return rows


def shop_class():
    rows = []
    for i, shop in enumerate(SHOPS):
        cls = "ABC"[i % 3]
        base = 8 if i % 5 else 10
        rows.append([shop, cls, "Active", base, base - 3, base - 5])
    return rows


def vendor_schedule():
    rows = []
    for shop in SHOPS:
        for j, vendor in enumerate(VENDORS[:12]):
            rows.append([
                shop, vendor,
                CYCLE_D[(SHOPS.index(shop) + j) % len(CYCLE_D)],
                CYCLE_P[(SHOPS.index(shop) + j * 3) % len(CYCLE_P)],
                str(3 + (j % 10)),
            ])
    return rows


def mss_list():
    rows = []
    for shop, mc in product(SHOPS, MCS[:14]):
        base = 2 + (hash(shop + mc) % 5)
        rows.append([shop, mc, str(base), str(base * 2), str(base * 3)])
    return rows


def d001_moq():
    return [[sku, str(1 + (i % 6))] for i, sku in enumerate(SKUS)]


def exemption_qty():
    header_cols = [f"{c}_{a}" for c in "ABC" for a in "ABC"]
    rows = []
    for i, sku in enumerate(SKUS[:12]):
        rows.append([sku] + [str(1 + ((i + k) % 7)) for k in range(len(header_cols))])
    return rows


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(",".join(header) + "\r\n")
        for row in rows:
            fh.write(",".join(str(v) for v in row) + "\r\n")
    print(f"{os.path.relpath(path, ROOT):<40} {len(rows):>5} rows")


def demo(rp_path, master_path):
    """示範輸入：8 間店 × 6 個 SKU，另加 1 間不存在的店作核對示範。"""
    cal = {r[0]: (r[1], r[2]) for r in calendar()}
    vs = {(r[0], r[1]): (r[3], r[2]) for r in vendor_schedule()}
    shops = SHOPS[:8]

    master = [
        ["SKU000001", "ZTA", "1", "V001"],   # 行貨
        ["SKU000002", "ZTA", "1", "V001"],
        ["SKU000003", "ZTA", "2", "DC"],     # 倉貨
        ["SKU000004", "ZTA", "2", "DC"],
        ["SKU000005", "ZTA", "4", ""],       # Flow Thru
        ["SKU000006", "ZTA", "2", "DC"],
    ]
    ss = {m[0]: (m[2], m[3]) for m in master}

    def cycles(site, sku):
        supply, vendor = ss[sku]
        if supply == "1":
            return vs.get((site, vendor), ("", ""))
        if supply == "4":
            return ("P07", "D14")
        return cal.get(site, ("", ""))

    rows = []
    for site in shops:
        for sku in ss:
            p, d = cycles(site, sku)
            rows.append([
                site, sku, f"Demo item {sku[-3:]}", "ZTA", "100001", "9999",
                "A" if sku.endswith(("1", "3")) else "C",
                ss[sku][0], "ND", "132", p, d, "3", "3", "G", "2", "5", "52",
            ])
    # 一間不存在的店，用來看「數據核對」的紅色提示
    for sku in ("SKU000003", "SKU000006"):
        rows.append(["S999", sku, f"Demo item {sku[-3:]}", "ZTA", "100001", "9999",
                     "C", ss[sku][0], "ND", "132", "", "", "3", "3", "G", "2", "5", "52"])

    write(rp_path,
          ["Site", "Article", "Article Description", "Brand", "MC", "Season category",
           "ABC Indicator", "Supply Source (1=Vendor/2=DC)", "RP Type", "Purchase Group",
           "Planning Cycle", "Delivery Cycle", "Delivery Days", "Target Coverage",
           "Forecast Model", "Historical periods", "Forecast periods", "Periods per season"],
          rows)
    write(master_path, ["Article Number", "Article Type", "Supply Source", "Major vendor (SAP)"], master)


if __name__ == "__main__":
    write(os.path.join(SEED, "warehouse_calendar.csv"), ["Shop", "P", "D"], calendar())
    write(os.path.join(SEED, "shop_class.csv"),
          ["Shop", "Class", "Status", "Coverage - A Items", "Coverage - B Items", "Coverage - C Items"],
          shop_class())
    write(os.path.join(SEED, "vendor_schedule.csv"),
          ["Shop", "Vendor", "Delivery S", "Planning S", "Lead Time"], vendor_schedule())
    write(os.path.join(SEED, "mss_list.csv"), ["Shop", "MC", "A", "B", "C"], mss_list())
    write(os.path.join(SEED, "d001_moq.csv"), ["SKU", "MOQ"], d001_moq())
    write(os.path.join(SEED, "exemption_qty.csv"),
          ["SKU"] + [f"{c}_{a}" for c in "ABC" for a in "ABC"], exemption_qty())

    demo(os.path.join(SAMPLES, "rp_list_demo.csv"),
         os.path.join(SAMPLES, "article_master_demo.csv"))
    demo(os.path.join(SAMPLES, "rp_list_sample.csv"),
         os.path.join(SAMPLES, "article_master_sample.csv"))
