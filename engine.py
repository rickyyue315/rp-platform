"""
RP Parameter 計算引擎
====================
把 Access MDB 的三個 Macro Step（Clear → Generate Result → Generate Result 2）
重寫成可測試的 Python 規則，輸入 = SAP ZIUMM003 匯出的 RP 清單 + Article Master 分類。

所有「車期 Code」/ Safety Qty / Coverage 的來源都在 SQLite（rp.db），
換言之後只維護數據，不用再改程式。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = store.SEED_DIR
# 主數據存 SQLite（rp.db）；雲端部署時整個目錄指向 volume（例如 /data）
REF_DIR = store.REF_DIR


# --------------------------------------------------------------------------
# 規則（可調，之後可搬到 DB / YAML）
# --------------------------------------------------------------------------
DEFAULT_RULES: Dict = {
    "dc_delivery_days": 3,                     # 倉貨 D001 → 舖 Delivery Days
    "dc_coverage": {"A": 8, "B": 5, "C": 3},   # 倉貨 Target Coverage（Shop_Class 可覆蓋）
    "vendor_coverage_factor": {"A": 2.5, "B": 1.0, "C": 1.0},
    "flow_thru": {                             # Flow Thru（Brand LOI / MAY）
        "planning_cycle": "P07",
        "delivery_cycle": "D14",
        "delivery_days": 7,
        "target_coverage": 7,
        "supply_source": 4,
    },
    "hist_periods": {"NEW_SKU": 2, "ND_TO_RF": 3, "RF_TO_ND": 3},
    "forecast_periods": 5,
    "periods_per_season": 52,
    "vendor_days_cap": 10,                     # 新 SKU 指引：Delivery Days <= 10
    "vendor_coverage_cap": 7,                  # 新 SKU 指引：Target Coverage <= 7
}


# --------------------------------------------------------------------------
# 欄位正規化
# --------------------------------------------------------------------------
def norm_key(value) -> str:
    """把 SAP / Excel 的欄名壓成可比較的 key（去掉空格、符號、大小寫）。"""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


# 輸入欄位：canonical name -> 可接受的 header 變體（正規化後）
INPUT_ALIASES: Dict[str, List[str]] = {
    "site": ["site", "shopsite", "store"],
    "article": ["article", "articlenumber", "sku", "material"],
    "article_description": ["articledescription", "description"],
    "brand": ["brand"],
    "mc": ["mc", "merchandisecategory", "merchandisecategoryforreference"],
    "mc_description": ["mcdescription"],
    "article_category": ["articlecategor", "articlecategory"],
    "article_type": ["articletype"],
    "status": ["status"],
    "first_sales_date": ["firstsalesdat", "firstsalesdate"],
    "season_category": ["seasoncategory", "seasoncat"],
    "available_to": ["availableto"],
    "launch_date": ["launchdate"],
    "sales_qty": ["salesqty", "salesqty2000010120061203"],
    "sales_price": ["salesprice"],
    "avg_weekly_sales": ["avgweeklysales"],
    "cal_stock_turnover": ["calstockturnover", "stockturnover"],
    "stock_on_hand": ["stockonhand", "stockonhand20070827", "soh"],
    "safety_stock": ["safetystock", "safetyqty"],
    "purchase_group": ["purchasegroup"],
    "rp_type": ["rptype", "rptyp"],
    "planning_cycle": ["planningcycle"],
    "delivery_cycle": ["deliverycycle"],
    "stock_planner": ["stockplanner"],
    "reorder_point": ["reorderpoint"],
    "delivery_days": ["deliverydays"],
    "target_coverage": ["targetcoverage", "tragetcoverage"],
    "supply_source": ["supplysource", "supplysource1vendor2dc", "supplysource1vendor2dc4flowthru"],
    "abc_indicator": ["abcindicator", "abc"],
    "smooth_promotion": ["smoothpromotion", "smoothing"],
    "forecast_model": ["forecastmodel"],
    "historical_periods": ["historicalperiods", "historicalperio"],
    "forecast_periods": ["forecastperiods"],
    "periods_per_season": ["periodsperseason"],
    "current_consumption_qty": ["currentconsumptionqty"],
    "week1_forecast": ["week1forecastvalue"],
    "week2_forecast": ["week2forecastvalue"],
    "week3_forecast": ["week3forecastvalue"],
    "week4_forecast": ["week4forecastvalue"],
    "week5_forecast": ["week5forecastvalue"],
}

MASTER_ALIASES: Dict[str, List[str]] = {
    "article": ["articlenumber", "articlenumbersap", "article", "sku"],
    "article_type": ["articletype"],
    "status": ["status"],
    "brand": ["brand"],
    "mc": ["merchandisecategory"],
    "major_vendor_sap": ["majorvendorsap", "majorvendor"],
    "supply_source": ["supplysource"],
    "original_supply_source": ["originalsupplysource"],
    "supply_site": ["supplysite"],
    "purchase_group": ["purchasegroup"],
    "season_category": ["seasoncategory"],
}

# 65 欄輸出（順序與 MDB 的 Final Result 2 一致）
OUTPUT_COLUMNS: List[str] = [
    "Site", "Article", "Article Description", "Brand", "MC", "MC Description",
    "Article categor", "Article Type", "Status", "First Sales Dat", "Season category",
    "Available to", "Launch Date", "Sales Qty", "Sales Price", "Avg Weekly Sales",
    "Cal Stock Turnover", "Stock On Hand", "Safety Stock", "Purchase Group",
    "RP Type", "Planning Cycle", "Delivery Cycle", "Stock Planner", "Reorder Point",
    "Delivery Days", "Target Coverage", "Supply Source (1=Vendor/2=DC)", "ABC Indicator",
    "Smooth Promotion", "Forecast Model", "Historical periods", "Forecast periods",
    "Periods per season", "Current consumption qty", "Week 1 forecast value",
    "Week 2 forecast value", "Week 3 forecast value", "Week 4 forecast value",
    "Week 5 forecast value", "New Safety Qty", "New Purchase Group", "New RP Typ",
    "New Planning Cycle", "New Delivery Cycle", "New Stock Planner", "New Reorder Point",
    "New Delivery Days", "New Traget Coverage", "New Supply Source", "New ABC Indicator",
    "New Smoothing (0/1)", "New Forecast Model", "New Historical perio",
    "New Forecast periods", "New Periods per season", "New Current consumption qty",
    "New Week 1 forecast value", "New Week 2 forecast value", "New Week 3 forecast value",
    "New Week 4 forecast value", "New Week 5 forecast value", "A QTY", "B QTY", "C QTY",
]

NEW_COLUMNS = OUTPUT_COLUMNS[40:]

MODE_LABELS = {
    "NEW_SKU": "新 SKU 建立（RP Type = ND）",
    "ND_TO_RF": "舊 SKU：ND → RF",
    "RF_TO_ND": "舊 SKU：RF → ND",
}


# --------------------------------------------------------------------------
# 參考數據
# --------------------------------------------------------------------------
def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _to_int(value) -> Optional[int]:
    text = _clean(value)
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _read_csv(name: str, dtype=str) -> pd.DataFrame:
    path = os.path.join(REF_DIR, name)
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_csv(path, dtype=dtype, keep_default_na=False)


class Reference:
    """所有 lookup 表：車期日曆、店舖分類、vendor 車期、MSS、MOQ、Exemption。"""

    def __init__(self, ref_dir: str = REF_DIR):
        self.dir = ref_dir
        self.calendar: Dict[str, Tuple[str, str]] = {}
        self.shop_class: Dict[str, Dict[str, object]] = {}
        self.vendor_schedule: Dict[Tuple[str, str], Dict[str, object]] = {}
        self.mss: Dict[Tuple[str, str], Dict[str, int]] = {}
        self.moq: Dict[str, int] = {}
        self.exemption: Dict[str, Dict[str, int]] = {}
        self.stats: Dict[str, int] = {}
        # 重複鍵且值不同：table -> [{key, field, first, second}]
        self.duplicates: Dict[str, List[Dict]] = {}
        self.load()

    def load(self) -> None:
        store.init()
        self.duplicates = {}

        def note(table: str, key: str, field: str, old, new) -> None:
            """記錄「同一 key 出現兩行不同值」，只留前 50 筆免佔記憶體。"""
            bucket = self.duplicates.setdefault(table, [])
            if len(bucket) < 50:
                bucket.append({"key": key, "field": field, "first": old, "second": new})

        self.calendar.clear()
        cal = store.load_frame("warehouse_calendar")
        if not cal.empty:
            for row in cal.itertuples(index=False):
                shop, p, d = _clean(row[0]).upper(), _clean(row[1]).upper(), _clean(row[2]).upper()
                if shop:
                    prev = self.calendar.get(shop)
                    if prev is not None and prev != (p, d):
                        note("warehouse_calendar", shop, "P / D", f"{prev[0]} / {prev[1]}", f"{p} / {d}")
                    self.calendar[shop] = (p, d)

        self.shop_class.clear()
        sc = store.load_frame("shop_class")
        if not sc.empty:
            for row in sc.itertuples(index=False):
                shop = _clean(row[0]).upper()
                if not shop:
                    continue
                entry = {
                    "class": _clean(row[1]).upper(),
                    "status": _clean(row[2]).upper(),
                    "coverage": {
                        "A": _to_int(row[3]),
                        "B": _to_int(row[4]),
                        "C": _to_int(row[5]),
                    },
                }
                prev = self.shop_class.get(shop)
                if prev is not None and prev != entry:
                    old = "/".join(str(prev["coverage"].get(k)) for k in ("A", "B", "C"))
                    new = "/".join(str(entry["coverage"].get(k)) for k in ("A", "B", "C"))
                    note("shop_class", shop, "Coverage A/B/C", old, new)
                self.shop_class[shop] = entry

        self.vendor_schedule.clear()
        vs = store.load_frame("vendor_schedule")
        if not vs.empty:
            for row in vs.itertuples(index=False):
                shop = _clean(row[0]).upper()
                vendor = _clean(row[1])
                if shop and vendor:
                    entry = {
                        "delivery": _clean(row[2]).upper(),
                        "planning": _clean(row[3]).upper(),
                        "lead_time": _to_int(row[4]),
                    }
                    prev = self.vendor_schedule.get((shop, vendor))
                    if prev is not None and prev != entry:
                        note("vendor_schedule", f"{shop} × {vendor}", "Planning / Delivery",
                             f"{prev['planning']} / {prev['delivery']}",
                             f"{entry['planning']} / {entry['delivery']}")
                    self.vendor_schedule[(shop, vendor)] = entry

        self.mss.clear()
        mss = store.load_frame("mss_list")
        if not mss.empty:
            for row in mss.itertuples(index=False):
                shop, mc = _clean(row[0]).upper(), _clean(row[1])
                if shop and mc:
                    entry = {
                        "A": _to_int(row[2]) or 0,
                        "B": _to_int(row[3]) or 0,
                        "C": _to_int(row[4]) or 0,
                    }
                    prev = self.mss.get((shop, mc))
                    if prev is not None and prev != entry:
                        note("mss_list", f"{shop} × {mc}", "A/B/C",
                             "/".join(str(prev[k]) for k in "ABC"),
                             "/".join(str(entry[k]) for k in "ABC"))
                    self.mss[(shop, mc)] = entry

        self.moq.clear()
        moq = store.load_frame("d001_moq")
        if not moq.empty:
            for row in moq.itertuples(index=False):
                sku = _clean(row[0])
                if sku:
                    value = _to_int(row[1]) or 0
                    prev = self.moq.get(sku)
                    if prev is not None and prev != value:
                        note("d001_moq", sku, "MOQ", prev, value)
                    self.moq[sku] = value

        self.exemption.clear()
        ex = store.load_frame("exemption_qty")
        if not ex.empty:
            cols = list(ex.columns)
            for row in ex.itertuples(index=False):
                sku = _clean(row[0])
                if not sku:
                    continue
                self.exemption[sku] = {
                    cols[i]: (_to_int(row[i]) or 0) for i in range(1, len(cols))
                }

        self.stats = {
            "warehouse_calendar": len(self.calendar),
            "shop_class": len(self.shop_class),
            "vendor_schedule": len(self.vendor_schedule),
            "mss_list": len(self.mss),
            "d001_moq": len(self.moq),
            "exemption_qty": len(self.exemption),
        }


# --------------------------------------------------------------------------
# 欄位對映
# --------------------------------------------------------------------------
def map_columns(df: pd.DataFrame, aliases: Dict[str, List[str]]) -> Tuple[pd.DataFrame, List[str]]:
    """按 header 別名把來源檔對到 canonical 欄位，回傳 (df, 未識別欄名)。"""
    lookup = {norm_key(c): c for c in df.columns}
    rename: Dict[str, str] = {}
    matched: set = set()
    for canonical, variants in aliases.items():
        for variant in variants:
            original = lookup.get(variant)
            if original is not None and original not in matched:
                rename[original] = canonical
                matched.add(original)
                break
    out = df.rename(columns=rename)
    unmatched = [c for c in df.columns if c not in matched]
    return out, unmatched


def _get(row, key: str) -> str:
    value = row.get(key, "")
    return _clean(value)


# --------------------------------------------------------------------------
# 核心計算
# --------------------------------------------------------------------------
@dataclass
class ComputeResult:
    rows: List[Dict] = field(default_factory=list)
    problems: List[Dict] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=OUTPUT_COLUMNS)


def _abc_letter(value: str) -> str:
    text = _clean(value).upper()
    for ch in text:
        if ch in ("A", "B", "C"):
            return ch
    return "C"


def _season_forecast_model(season: str, fallback: str) -> str:
    """Season 1/2 → S（季節性）；空白 / 9999 → G（移動平均）。"""
    text = _clean(season)
    if text in ("1", "2"):
        return "S"
    if text in ("", "9999"):
        return "G"
    return _clean(fallback) or "G"


def compute(
    rp_list: pd.DataFrame,
    reference: Reference,
    mode: str = "NEW_SKU",
    article_master: Optional[pd.DataFrame] = None,
    ideal_stock: Optional[pd.DataFrame] = None,
    rules: Optional[Dict] = None,
) -> ComputeResult:
    """把 ZIUMM003 RP 清單逐行算出 New * 參數。

    rp_list        : 已 map 成 canonical 欄位的 DataFrame（site/article 必需）
    article_master : 已 map 成 canonical 欄位的 DataFrame（提供 supply source / vendor）
    ideal_stock    : 可選，欄位 article + qty，覆蓋 Safety Qty
    """
    rules = {**DEFAULT_RULES, **(rules or {})}
    master: Dict[str, Dict] = {}
    if article_master is not None and not article_master.empty:
        for row in article_master.to_dict("records"):
            sku = _clean(row.get("article"))
            if sku:
                master[sku] = row

    ideal: Dict[str, int] = {}
    if ideal_stock is not None and not ideal_stock.empty:
        for row in ideal_stock.to_dict("records"):
            sku = _clean(row.get("article"))
            qty = _to_int(row.get("qty"))
            if sku and qty is not None:
                ideal[sku] = qty

    result = ComputeResult()
    missing_master: List[str] = []

    for row in rp_list.to_dict("records"):
        site = _get(row, "site").upper()
        article = _get(row, "article")
        mc = _get(row, "mc")
        if not site or not article:
            continue

        issues: List[str] = []
        mrow = master.get(article)
        if mrow is None and master:
            missing_master.append(article)

        # ---- Supply Source：Article Master 優先，否則用 RP 清單現值 ----
        supply_source = _to_int(mrow.get("supply_source") if mrow else None)
        if supply_source is None:
            supply_source = _to_int(_get(row, "supply_source")) or 2

        vendor = _get(mrow, "major_vendor_sap") if mrow else ""
        abc = _abc_letter(_get(row, "abc_indicator"))
        shop_info = reference.shop_class.get(site)
        calendar = reference.calendar.get(site)
        vendor_row = reference.vendor_schedule.get((site, vendor)) if vendor else None
        mss = reference.mss.get((site, mc))
        exemption = reference.exemption.get(article)
        moq = reference.moq.get(article)

        # ---- 車期 Code ----
        if supply_source == 4:
            flow = rules["flow_thru"]
            planning, delivery = flow["planning_cycle"], flow["delivery_cycle"]
            delivery_days, coverage = flow["delivery_days"], flow["target_coverage"]
        elif supply_source == 1:
            if vendor_row:
                planning = vendor_row["planning"]
                delivery = vendor_row["delivery"]
                lead_time = vendor_row["lead_time"] or 0
            else:
                planning = delivery = ""
                lead_time = 0
                issues.append(f"Vendor Schedule 缺 (shop={site}, vendor={vendor or '未填'})")
            factor = rules["vendor_coverage_factor"].get(abc, 1.0)
            delivery_days = lead_time
            coverage = int(round(lead_time * factor))
        else:
            if calendar:
                planning, delivery = calendar
            else:
                planning = delivery = ""
                issues.append(f"Warehouse Calendar 缺 (shop={site})")
            delivery_days = rules["dc_delivery_days"]
            coverage = rules["dc_coverage"].get(abc, 3)
            if shop_info:
                coverage = shop_info["coverage"].get(abc) or coverage

        if mode == "NEW_SKU" and supply_source == 1:
            # 新 SKU 指引：行貨 Delivery Days <= 10、Target Coverage <= 7
            delivery_days = min(delivery_days, rules["vendor_days_cap"])
            coverage = min(coverage, rules["vendor_coverage_cap"])

        # ---- Safety Qty：Ideal Stock > Exemption > MSS > MOQ ----
        safety: Optional[int] = ideal.get(article)
        if safety is None and exemption is not None:
            key = f"{shop_info['class'] if shop_info else 'C'}_{abc}"
            safety = exemption.get(key)
        if safety is None and mss is not None:
            safety = mss.get(abc, 0)
        if safety is None:
            safety = moq or 1
            if supply_source == 2:
                issues.append(f"MSS 缺 (shop={site}, MC={mc})，暫用 MOQ={safety}")
            elif supply_source == 1:
                issues.append(f"Safety Qty 無來源 (shop={site}, MC={mc})，暫用 {safety}")
        if safety == 0 and supply_source == 2 and moq:
            safety = moq

        # ---- RP Type / 期數 ----
        if mode == "NEW_SKU":
            rp_type = "ND"
        elif mode == "RF_TO_ND":
            rp_type = "ND"
        else:
            rp_type = "RF"

        hist = rules["hist_periods"].get(mode, 3)
        new_supply_source = rules["flow_thru"]["supply_source"] if supply_source == 4 else supply_source

        out: Dict = {}
        # 32 個 current 欄位原樣帶出
        out["Site"] = site
        out["Article"] = article
        out["Article Description"] = _get(row, "article_description")
        out["Brand"] = _get(row, "brand")
        out["MC"] = mc
        out["MC Description"] = _get(row, "mc_description")
        out["Article categor"] = _get(row, "article_category")
        out["Article Type"] = _get(row, "article_type")
        out["Status"] = _get(row, "status")
        out["First Sales Dat"] = _get(row, "first_sales_date")
        out["Season category"] = _get(row, "season_category")
        out["Available to"] = _get(row, "available_to")
        out["Launch Date"] = _get(row, "launch_date")
        out["Sales Qty"] = _get(row, "sales_qty")
        out["Sales Price"] = _get(row, "sales_price")
        out["Avg Weekly Sales"] = _get(row, "avg_weekly_sales")
        out["Cal Stock Turnover"] = _get(row, "cal_stock_turnover")
        out["Stock On Hand"] = _get(row, "stock_on_hand")
        out["Safety Stock"] = _get(row, "safety_stock")
        out["Purchase Group"] = _get(row, "purchase_group")
        out["RP Type"] = _get(row, "rp_type")
        out["Planning Cycle"] = _get(row, "planning_cycle")
        out["Delivery Cycle"] = _get(row, "delivery_cycle")
        out["Stock Planner"] = _get(row, "stock_planner")
        out["Reorder Point"] = _get(row, "reorder_point")
        out["Delivery Days"] = _get(row, "delivery_days")
        out["Target Coverage"] = _get(row, "target_coverage")
        out["Supply Source (1=Vendor/2=DC)"] = _get(row, "supply_source")
        out["ABC Indicator"] = _get(row, "abc_indicator")
        out["Smooth Promotion"] = _get(row, "smooth_promotion")
        out["Forecast Model"] = _get(row, "forecast_model")
        out["Historical periods"] = _get(row, "historical_periods")
        out["Forecast periods"] = _get(row, "forecast_periods")
        out["Periods per season"] = _get(row, "periods_per_season")
        out["Current consumption qty"] = _get(row, "current_consumption_qty")
        for i in range(1, 6):
            out[f"Week {i} forecast value"] = _get(row, f"week{i}_forecast")

        # 33 個 New 欄位
        out["New Safety Qty"] = safety
        out["New Purchase Group"] = ""
        out["New RP Typ"] = rp_type
        out["New Planning Cycle"] = planning
        out["New Delivery Cycle"] = delivery
        out["New Stock Planner"] = ""
        out["New Reorder Point"] = ""
        out["New Delivery Days"] = delivery_days
        out["New Traget Coverage"] = coverage
        out["New Supply Source"] = new_supply_source
        out["New ABC Indicator"] = abc
        out["New Smoothing (0/1)"] = 0
        out["New Forecast Model"] = _season_forecast_model(_get(row, "season_category"), _get(row, "forecast_model"))
        out["New Historical perio"] = hist
        out["New Forecast periods"] = rules["forecast_periods"]
        out["New Periods per season"] = rules["periods_per_season"]
        out["New Current consumption qty"] = ""
        for i in range(1, 6):
            out[f"New Week {i} forecast value"] = ""
        out["A QTY"] = (mss or {}).get("A", "")
        out["B QTY"] = (mss or {}).get("B", "")
        out["C QTY"] = (mss or {}).get("C", "")

        result.rows.append(out)
        if issues:
            result.problems.append({
                "Site": site,
                "Article": article,
                "MC": mc,
                "Supply Source": supply_source,
                "Issue": "；".join(issues),
            })

    result.summary = {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "rows": len(result.rows),
        "shops": len({r["Site"] for r in result.rows}),
        "articles": len({r["Article"] for r in result.rows}),
        "problem_rows": len(result.problems),
        "missing_article_master": len(set(missing_master)),
        "supply_source_split": {},
    }
    split: Dict[str, int] = {}
    for row in result.rows:
        key = str(row["New Supply Source"])
        split[key] = split.get(key, 0) + 1
    result.summary["supply_source_split"] = dict(sorted(split.items()))
    return result


# --------------------------------------------------------------------------
# 車期 Code 查詢
# --------------------------------------------------------------------------
def lookup_cycles(reference: Reference, shops: List[str], vendor: str = "") -> List[Dict]:
    rows: List[Dict] = []
    for shop in shops:
        shop = _clean(shop).upper()
        if not shop:
            continue
        info = reference.shop_class.get(shop)
        calendar = reference.calendar.get(shop)
        vs = reference.vendor_schedule.get((shop, _clean(vendor))) if vendor else None
        rows.append({
            "Shop": shop,
            "Shop Class": (info or {}).get("class", ""),
            "Warehouse P": calendar[0] if calendar else "",
            "Warehouse D": calendar[1] if calendar else "",
            "Vendor": _clean(vendor),
            "Vendor Planning": (vs or {}).get("planning", ""),
            "Vendor Delivery": (vs or {}).get("delivery", ""),
            "Vendor Lead Time": (vs or {}).get("lead_time", ""),
            "Coverage A": ((info or {}).get("coverage") or {}).get("A", ""),
            "Coverage B": ((info or {}).get("coverage") or {}).get("B", ""),
            "Coverage C": ((info or {}).get("coverage") or {}).get("C", ""),
        })
    return rows
