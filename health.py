"""主數據核對（唯讀）
=====================
把 ZIUMM003 的現行參數與平台六張主數據表逐行比對，輸出差異報告。

設計原則：
- **只讀**。任何檢查都不會改動主數據，避免「用交易數據反向污染主數據」。
- 每一項都帶「為甚麼重要」與「怎樣修」，報告本身要能直接行動。
- 計數與樣本分開：數量可能上萬，樣本只留前 N 筆給人核對。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

import engine


# --------------------------------------------------------------------------
# 檢查項目定義（順序即報告顯示順序）
# --------------------------------------------------------------------------
CHECKS: List[Dict] = [
    {
        "key": "unknown_site",
        "label": "店舖未登記在車期日曆",
        "severity": "error",
        "why": "這批 DC 行會沒有 Planning / Delivery Cycle，上載 ZIUMM0033 後等於冇車期。",
        "fix": "主數據 → Warehouse Calendar 補上該店 P / D。",
    },
    {
        "key": "vendor_missing",
        "label": "行貨 SKU 未在 Article Master 指定 vendor",
        "severity": "error",
        "why": "Supply Source = 1 但取不到 vendor，Vendor Schedule 無法 lookup，車期會空白。",
        "fix": "Article Master 補 Major vendor (SAP)，或確認 Supply Source 是否應為 2。",
    },
    {
        "key": "vendor_not_in_schedule",
        "label": "Vendor Schedule 缺「店舖 × vendor」組合",
        "severity": "error",
        "why": "該店沒有這個 vendor 的車期與 Lead Time，Delivery Days 與 Coverage 會變 0。",
        "fix": "Vendor Schedule 補上該店 × vendor 一行（Delivery S / Planning S / Lead Time）。",
    },
    {
        "key": "cycle_mismatch_dc",
        "label": "倉貨現行車期與日曆表不一致",
        "severity": "warn",
        "why": "SAP 現行值與 Warehouse Calendar 不同；可能日曆表過時，也可能 SAP 未更新。",
        "fix": "核對該店 SAP 現行值，再決定改日曆表還是通知 SAP 改正。",
    },
    {
        "key": "cycle_mismatch_vendor",
        "label": "行貨現行車期與 Vendor Schedule 不一致",
        "severity": "warn",
        "why": "SKU 換 vendor 後 SAP 仍帶舊 vendor 的車期，屬預期；若 vendor 沒換就要查。",
        "fix": "先確認 Article Master 的 vendor 最近有否改動，再核對 Vendor Schedule。",
    },
    {
        "key": "shop_class_missing",
        "label": "店舖未在 Shop_Class",
        "severity": "warn",
        "why": "取不到店舖等級，Target Coverage 會用預設 A8 / B5 / C3，而非該店實際設定。",
        "fix": "Shop_Class 補上該店 Class 與 Coverage。",
    },
    {
        "key": "mss_missing",
        "label": "MSS 缺「店舖 × MC」",
        "severity": "warn",
        "why": "Safety Qty 會退到 MOQ 或 1，不是該店該 MC 的陳列量。",
        "fix": "MSS List 補上該店 × MC。",
    },
    {
        "key": "moq_missing",
        "label": "SKU 完全沒有 Safety Qty 來源",
        "severity": "info",
        "why": "Ideal Stock、Exemption、MSS、MOQ 全部沒有，Safety Qty 暫定為 1。",
        "fix": "D001 MOQ 補上該 SKU，或提供 Ideal Stock。",
    },
    {
        "key": "master_missing_sku",
        "label": "SKU 未在 Article Master",
        "severity": "warn",
        "why": "Supply Source 與 vendor 只能沿用 ZIUMM003 現值，新 SKU 最容易出錯。",
        "fix": "確認 Article Master 是否已更新到最新版本。",
    },
    {
        "key": "ss_conflict",
        "label": "Supply Source 兩邊不同",
        "severity": "info",
        "why": "Article Master 與 ZIUMM003 不一致，計算以 Article Master 為準。",
        "fix": "確認哪邊正確；若 SAP 未改，上載後會被覆蓋成新值。",
    },
    {
        "key": "duplicate_master",
        "label": "主數據有重複鍵且值不同",
        "severity": "error",
        "why": "同一 key 出現兩行不同數值，系統只會採用最後一行，結果不可預期。",
        "fix": "匯出該表，刪除重複行後重新上載。",
    },
]

TABLE_LABELS = {
    "warehouse_calendar": "Warehouse Calendar",
    "shop_class": "Shop_Class",
    "vendor_schedule": "Vendor Schedule",
    "mss_list": "MSS List",
    "d001_moq": "D001 MOQ",
    "exemption_qty": "Exemption Qty",
}

SAMPLE_CAP = 40          # 每項最多保留的樣本數（避免大 batch 佔記憶體）
DEFAULT_SAMPLE = 20      # 回給前端的樣本數


def _finding(site: str = "", article: str = "", current: str = "",
             master_value: str = "", note: str = "") -> Dict[str, str]:
    return {
        "Site": site,
        "Article": article,
        "Current": current,
        "Master": master_value,
        "Note": note,
    }


def _master_map(frame: Optional[pd.DataFrame]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    if frame is None or frame.empty:
        return out
    for row in frame.to_dict("records"):
        sku = engine._clean(row.get("article"))
        if sku:
            out[sku] = row
    return out


def _ideal_map(frame: Optional[pd.DataFrame]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if frame is None or frame.empty:
        return out
    for row in frame.to_dict("records"):
        sku = engine._clean(row.get("article"))
        qty = engine._to_int(row.get("qty"))
        if sku and qty is not None:
            out[sku] = qty
    return out


def check(
    reference,
    rp_list: pd.DataFrame,
    article_master: Optional[pd.DataFrame] = None,
    ideal_stock: Optional[pd.DataFrame] = None,
    sample_limit: int = DEFAULT_SAMPLE,
) -> Dict:
    """回傳核對報告（純資料，不會改動 reference）。"""
    master = _master_map(article_master)
    ideal = _ideal_map(ideal_stock)

    counts: Counter = Counter()
    findings: Dict[str, List[Dict]] = defaultdict(list)
    sites = set()
    articles = set()
    ss_split: Counter = Counter()
    bad_rows = 0

    def add(key: str, item: Dict) -> None:
        counts[key] += 1
        bucket = findings[key]
        if len(bucket) < SAMPLE_CAP:
            bucket.append(item)

    for row in rp_list.to_dict("records"):
        site = engine._get(row, "site").upper()
        article = engine._get(row, "article")
        if not site or not article:
            bad_rows += 1
            continue
        sites.add(site)
        articles.add(article)

        mc = engine._get(row, "mc")
        mrow = master.get(article)

        # ---- Supply Source：與 compute() 完全一致的判定順序 ----
        ss = engine._to_int(mrow.get("supply_source") if mrow else None)
        rp_ss = engine._to_int(engine._get(row, "supply_source"))
        if ss is None:
            ss = rp_ss or 2
        ss_split[ss] += 1

        vendor = engine._get(mrow, "major_vendor_sap") if mrow else ""
        abc = engine._abc_letter(engine._get(row, "abc_indicator"))
        cur_p = engine._get(row, "planning_cycle").upper()
        cur_d = engine._get(row, "delivery_cycle").upper()
        cur = f"{cur_p or '—'} / {cur_d or '—'}"

        calendar = reference.calendar.get(site)
        shop_info = reference.shop_class.get(site)
        vendor_row = reference.vendor_schedule.get((site, vendor)) if vendor else None
        mss = reference.mss.get((site, mc))
        exemption = reference.exemption.get(article)
        moq = reference.moq.get(article)

        # 1) 店舖不在日曆（只影響倉貨）
        if ss == 2 and calendar is None:
            add("unknown_site", _finding(site, article, cur, "（無此店）",
                                         f"MC {mc}" if mc else ""))

        # 2/3) 行貨 vendor
        if ss == 1:
            if not vendor:
                add("vendor_missing", _finding(site, article, cur, "（空白）",
                                               "Article Master 的 Major vendor 為空"))
            elif vendor_row is None:
                add("vendor_not_in_schedule",
                    _finding(site, article, cur, f"vendor {vendor}", "查無此店 × vendor"))

        # 4) 倉貨車期與日曆不一致
        if ss == 2 and calendar is not None and (cur_p or cur_d):
            if (cur_p and cur_p != calendar[0]) or (cur_d and cur_d != calendar[1]):
                add("cycle_mismatch_dc",
                    _finding(site, article, cur, f"{calendar[0]} / {calendar[1]}",
                             "SAP 現行值與日曆表不同"))

        # 5) 行貨車期與 Vendor Schedule 不一致
        if ss == 1 and vendor_row is not None and (cur_p or cur_d):
            if (cur_p and cur_p != vendor_row["planning"]) or (cur_d and cur_d != vendor_row["delivery"]):
                add("cycle_mismatch_vendor",
                    _finding(site, article, cur,
                             f"{vendor_row['planning']} / {vendor_row['delivery']}",
                             f"vendor {vendor}"))

        # 6) 店舖不在 Shop_Class
        if ss in (1, 2) and shop_info is None:
            add("shop_class_missing", _finding(site, article, cur, "（無此店）",
                                               "Coverage 會用預設值"))

        # 7) MSS 缺（只在其餘 Safety Qty 來源都沒有時才真正影響）
        if ss in (1, 2) and mss is None:
            class_key = f"{shop_info['class'] if shop_info else 'C'}_{abc}"
            has_exemption = bool(exemption and exemption.get(class_key))
            if article not in ideal and not has_exemption:
                fallback = moq if moq else 1
                add("mss_missing", _finding(site, article, cur,
                                            f"MOQ {moq}" if moq else "（無 MOQ）",
                                            f"Safety Qty 暫用 {fallback}"))

        # 8) 完全沒有 Safety Qty 來源
        if article not in ideal and exemption is None and mss is None and moq is None:
            add("moq_missing", _finding(site, article, cur, "（無）",
                                        "Safety Qty 暫定 1"))

        # 9/10) 與 Article Master 的對照
        if master:
            if mrow is None:
                add("master_missing_sku", _finding(site, article, cur, "（無此 SKU）",
                                                   "Supply Source 沿用 ZIUMM003"))
            elif rp_ss is not None and rp_ss != ss:
                add("ss_conflict", _finding(site, article, f"SS {rp_ss}", f"SS {ss}",
                                            "計算以 Article Master 為準"))

    # 11) 主數據重複鍵（掃 DB，與上載檔無關）
    for table, items in (reference.duplicates or {}).items():
        label = TABLE_LABELS.get(table, table)
        for dup in items:
            add("duplicate_master",
                _finding(str(dup.get("key", "")), "", str(dup.get("first", "")),
                         str(dup.get("second", "")),
                         f"{label} · {dup.get('field', '')}"))

    # ---- 組裝報告 ----
    out: List[Dict] = []
    issues = 0
    blocking = 0
    for spec in CHECKS:
        n = counts[spec["key"]]
        items = findings.get(spec["key"], [])
        if n:
            issues += n
            if spec["severity"] == "error":
                blocking += n
        out.append({
            **spec,
            "count": n,
            "severity": spec["severity"] if n else "ok",
            "sample": items[:sample_limit],
            "truncated": max(0, n - len(items[:sample_limit])),
        })

    status = "error" if blocking else ("warn" if issues else "ok")
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "issues": issues,
        "blocking": blocking,
        "scope": {
            "rows": len(rp_list) - bad_rows,
            "bad_rows": bad_rows,
            "sites": len(sites),
            "articles": len(articles),
            "supply_source_split": dict(sorted(ss_split.items())),
            "has_master": bool(master),
            "has_ideal": bool(ideal),
        },
        "checks": out,
    }
