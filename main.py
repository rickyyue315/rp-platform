"""RP Parameter 平台（POC）
=======================
啟動： python -m uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import io
import os
import shutil
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import engine
import health
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

app = FastAPI(title="RP Parameter Platform", version="0.1.0")

REFERENCE = engine.Reference()
RUNS: Dict[str, Dict] = {}
MAX_RUNS = 20


# --------------------------------------------------------------------------
# 讀檔
# --------------------------------------------------------------------------
def read_table(raw: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(raw), dtype=str, keep_default_na=False)
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp950", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(400, f"無法解碼檔案：{filename}")
    for sep in ("\t", ",", ";"):
        frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, sep=sep)
        if frame.shape[1] > 1:
            return frame
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, sep=r"\s+", engine="python")


def _prepare(raw: bytes, filename: str, aliases: Dict[str, List[str]]) -> tuple[pd.DataFrame, List[str]]:
    frame = read_table(raw, filename)
    frame.columns = [str(c).replace("\n", " ").strip() for c in frame.columns]
    return engine.map_columns(frame, aliases)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/api/reference")
def reference_stats():
    return {
        "stats": REFERENCE.stats,
        "db": store.info(),
        "duplicates": {k: len(v) for k, v in REFERENCE.duplicates.items()},
        "tables": {
            key: {"label": spec.label, "columns": spec.columns, "csv": spec.csv}
            for key, spec in store.TABLE_SPECS.items()
        },
        "rules": engine.DEFAULT_RULES,
        "modes": engine.MODE_LABELS,
        "output_columns": engine.OUTPUT_COLUMNS,
    }


@app.post("/api/lookup")
def lookup(shops: str = Form(""), vendor: str = Form("")):
    shop_list = [s for s in shops.replace("\n", ",").replace(" ", ",").split(",") if s.strip()]
    if not shop_list:
        raise HTTPException(400, "請輸入至少一個店舖編號")
    rows = engine.lookup_cycles(REFERENCE, shop_list, vendor)
    return {"rows": rows}


@app.post("/api/compute")
async def compute(
    rp_file: UploadFile = File(...),
    master_file: Optional[UploadFile] = File(None),
    ideal_file: Optional[UploadFile] = File(None),
    mode: str = Form("NEW_SKU"),
):
    if mode not in engine.MODE_LABELS:
        raise HTTPException(400, f"未知模式：{mode}")

    rp_raw = await rp_file.read()
    rp_list, unmatched = _prepare(rp_raw, rp_file.filename or "rp", engine.INPUT_ALIASES)
    for required in ("site", "article"):
        if required not in rp_list.columns:
            raise HTTPException(400, f"RP 清單缺少必要欄位 `{required}`，無法識別欄名：{unmatched[:8]}")

    master = None
    if master_file is not None and master_file.filename:
        master, _ = _prepare(await master_file.read(), master_file.filename, engine.MASTER_ALIASES)

    ideal = None
    if ideal_file is not None and ideal_file.filename:
        ideal_raw = await ideal_file.read()
        ideal, _ = _prepare(ideal_raw, ideal_file.filename, {
            "article": ["article", "sku", "articlenumber"],
            "qty": ["qty", "idealstock", "idealstockqty", "quantity"],
        })

    result = engine.compute(rp_list, REFERENCE, mode=mode, article_master=master, ideal_stock=ideal)
    unmatched = [c for c in unmatched if not str(c).strip().lower().startswith("new ")]
    report = health.check(REFERENCE, rp_list, article_master=master, ideal_stock=ideal)

    run_id = uuid.uuid4().hex[:12]
    RUNS[run_id] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "rows": result.rows,
        "problems": result.problems,
        "summary": result.summary,
    }
    while len(RUNS) > MAX_RUNS:
        RUNS.pop(next(iter(RUNS)))

    return {
        "run_id": run_id,
        "summary": result.summary,
        "unmatched_columns": unmatched[:20],
        "columns": engine.OUTPUT_COLUMNS,
        "rows": result.rows[:400],
        "total_rows": len(result.rows),
        "problems": result.problems[:200],
        "total_problems": len(result.problems),
        "health": report,
    }


@app.post("/api/healthcheck")
async def healthcheck(
    rp_file: UploadFile = File(...),
    master_file: Optional[UploadFile] = File(None),
    ideal_file: Optional[UploadFile] = File(None),
):
    """只跑主數據核對，不做參數計算。"""
    rp_list, _ = _prepare(await rp_file.read(), rp_file.filename or "rp", engine.INPUT_ALIASES)
    for required in ("site", "article"):
        if required not in rp_list.columns:
            raise HTTPException(400, f"RP 清單缺少必要欄位 `{required}`")

    master = None
    if master_file is not None and master_file.filename:
        master, _ = _prepare(await master_file.read(), master_file.filename, engine.MASTER_ALIASES)

    ideal = None
    if ideal_file is not None and ideal_file.filename:
        ideal, _ = _prepare(await ideal_file.read(), ideal_file.filename, {
            "article": ["article", "sku", "articlenumber"],
            "qty": ["qty", "idealstock", "idealstockqty", "quantity"],
        })

    return health.check(REFERENCE, rp_list, article_master=master, ideal_stock=ideal)


@app.get("/api/demo")
def demo(mode: str = "NEW_SKU"):
    """用 samples/ 或 seed/ 的示範檔跑一次，供使用說明截圖與新人練習。"""
    demo_dir = next(
        (d for d in (os.path.join(BASE_DIR, "samples"), os.path.join(BASE_DIR, "seed"))
         if os.path.exists(os.path.join(d, "rp_list_demo.csv"))),
        None,
    )
    if demo_dir is None:
        raise HTTPException(404, "找不到示範檔")
    rp_path = os.path.join(demo_dir, "rp_list_demo.csv")
    am_path = os.path.join(demo_dir, "article_master_demo.csv")

    rp_list, unmatched = _prepare(open(rp_path, "rb").read(), "rp_list_demo.csv", engine.INPUT_ALIASES)
    master = None
    if os.path.exists(am_path):
        master, _ = _prepare(open(am_path, "rb").read(), "article_master_demo.csv", engine.MASTER_ALIASES)

    result = engine.compute(rp_list, REFERENCE, mode=mode, article_master=master)
    report = health.check(REFERENCE, rp_list, article_master=master)
    run_id = uuid.uuid4().hex[:12]
    RUNS[run_id] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "rows": result.rows,
        "problems": result.problems,
        "summary": result.summary,
    }
    while len(RUNS) > MAX_RUNS:
        RUNS.pop(next(iter(RUNS)))
    return {
        "run_id": run_id,
        "summary": result.summary,
        "unmatched_columns": [],
        "columns": engine.OUTPUT_COLUMNS,
        "rows": result.rows,
        "total_rows": len(result.rows),
        "problems": result.problems,
        "total_problems": len(result.problems),
        "health": report,
        "demo": True,
    }


@app.get("/api/export/{run_id}")
def export(run_id: str, fmt: str = "xlsx", header: int = 0, encoding: str = "utf-8-sig"):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "找不到這次計算結果，請重新執行")
    frame = pd.DataFrame(run["rows"], columns=engine.OUTPUT_COLUMNS)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "txt":
        body = frame.to_csv(sep="\t", index=False, header=bool(header), lineterminator="\r\n")
        return Response(
            content=body.encode(encoding, errors="replace"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="ZIUMM0033_{stamp}.txt"'},
        )

    path = os.path.join(EXPORT_DIR, f"RP_Parameter_{run['mode']}_{stamp}.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Final Result", index=False)
        if run["problems"]:
            pd.DataFrame(run["problems"]).to_excel(writer, sheet_name="Problem Transactions", index=False)
        meta = pd.DataFrame([{"項目": k, "值": str(v)} for k, v in run["summary"].items()])
        meta.to_excel(writer, sheet_name="Run Info", index=False)
    return FileResponse(path, filename=os.path.basename(path))


TABLE_ALIASES = {
    "calendar": "warehouse_calendar",
    "shop_class": "shop_class",
    "vendor_schedule": "vendor_schedule",
    "mss": "mss_list",
    "moq": "d001_moq",
    "exemption": "exemption_qty",
}


@app.post("/api/reference/{table}")
async def update_reference(table: str, file: UploadFile = File(...)):
    key = TABLE_ALIASES.get(table)
    if key is None:
        raise HTTPException(404, f"未知主數據表：{table}")
    spec = store.TABLE_SPECS[key]

    frame = read_table(await file.read(), file.filename or spec.csv)
    if frame.empty:
        raise HTTPException(400, "檔案沒有資料")
    frame.columns = [str(c).replace("\n", " ").strip() for c in frame.columns]
    frame = store.normalise_columns(frame, spec)

    missing = [c for c in spec.columns if c not in frame.columns]
    if missing:
        raise HTTPException(400, f"缺少欄位：{'、'.join(missing)}（現有：{'、'.join(frame.columns[:8])}）")

    # 上載前把現有內容備份成 CSV（人類可讀，出事可直接比對）
    backup_dir = os.path.join(store.REF_DIR, "backup")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    store.export_frame(key).to_csv(
        os.path.join(backup_dir, f"{spec.csv}.{stamp}.bak"), index=False, encoding="utf-8-sig")

    rows = store.replace_table(key, frame)
    REFERENCE.load()
    return {"ok": True, "rows": rows, "stats": REFERENCE.stats, "db": store.info()}


@app.get("/api/reference/{table}/export")
def export_reference(table: str, fmt: str = "csv"):
    key = TABLE_ALIASES.get(table)
    if key is None:
        raise HTTPException(404, f"未知主數據表：{table}")
    spec = store.TABLE_SPECS[key]
    frame = store.export_frame(key)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "xlsx":
        path = os.path.join(EXPORT_DIR, f"{spec.name}_{stamp}.xlsx")
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            frame.to_excel(writer, sheet_name=spec.label[:31], index=False)
        return FileResponse(path, filename=os.path.basename(path))

    body = frame.to_csv(index=False, lineterminator="\r\n")
    return Response(
        content=body.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{spec.name}_{stamp}.csv"'},
    )


@app.get("/api/problems/{run_id}")
def problems(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "找不到這次計算結果")
    return JSONResponse({"problems": run["problems"]})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
