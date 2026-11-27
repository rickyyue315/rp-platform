# RP Parameter Platform

把「Excel 手動貼欄 + Access 巨集」的補貨參數維護流程，改成一個可自架的網頁平台：
上載 SAP 匯出的 RP 清單 → 自動計算車期 Code／Coverage／Safety Qty → 輸出 65 欄上載檔與問題清單。

> **本 repo 只含程式碼與虛構示範資料。** 所有店號（`S001`）、vendor（`V001`）、
> SKU（`SKU000001`）、MC（`100001`）與車期代碼（`P01`／`D04`）純屬示例，
> 與任何真實系統、公司或資料集無關。

```
SAP 匯出 RP 清單 ──┐
Article Master ────┼─► 規則引擎（查 6 張主數據）─► 65 欄結果 + 問題清單
Ideal Stock ───────┘                                  │
                                                      ├─► Excel（留底／覆核）
                                                      └─► TXT（上載 SAP）
```

## 為何是「查表 + 條件」而不是接 SAP

舊流程真正做的事只有三件：

1. 按 `Site` 查 `Warehouse Calendar` 拿 Planning / Delivery Cycle（車期 Code）
2. 按 `Site + Vendor` 查 `Vendor Schedule`（行貨），或按 `Site` 查 `Shop_Class`（倉貨）拿 Lead Time / Coverage
3. 按 `Shop + MC + ABC` 查 `MSS List`（或 `Exemption Qty` / `D001 MOQ`）拿 Safety Qty

其餘都是人手插欄、清空欄位、複製貼上。既然是查表加條件判斷，就適合寫成有測試、有版本的程式，
不需要 SAP 帳號，也不需要任何 ABAP。

## 功能

| 頁籤 | 做什麼 |
|---|---|
| **參數計算** | 上載 RP 清單（可選 Article Master／Ideal Stock）→ 選模式 → 出 65 欄結果、問題清單、Excel／TXT |
| **車期查詢** | 貼店號（可加 vendor）→ 即時查 P／D、Lead Time、Coverage，可轉 CSV |
| **主數據** | 六張表可上載 CSV／Excel、可匯出、整表替換包在 transaction 內 |

- 欄名自動識別（`engine.INPUT_ALIASES`），SAP 改欄名只需加一行別名
- 三種模式：`NEW_SKU` / `ND_TO_RF` / `RF_TO_ND`，只影響 RP Type 與 Historical periods
- 有問題的行不會中斷計算，會用替代值頂住並列入 Problem Transactions

## 規則一覽

| Supply Source | 車期 Code 來源 | Delivery Days | Target Coverage |
|---|---|---|---|
| 1 行貨 | `Vendor Schedule`（Shop × Vendor） | Lead Time（新模式上限 10） | ABC × Lead Time（A=2.5、B=1、C=1，上限 7） |
| 2 倉貨 | `Warehouse Calendar`（Shop） | 3 | `Shop_Class` A/B/C = 8/5/3 |
| 4 Flow Thru | 固定 `P07` / `D14` | 7 | 7 |

Safety Qty 優先序：Ideal Stock > `Exemption Qty` > `MSS List` > `D001 MOQ`（四層皆無則暫定 1）。

## 主數據（SQLite）

六張表存在一個 `rp.db`，首次啟動由 CSV 播種，之後所有更新寫入 DB。

| 表 | 欄位 | 示範資料 |
|---|---|---:|
| `warehouse_calendar` | Shop / P / D | 24 |
| `shop_class` | Shop / Class / Status / Coverage A,B,C | 24 |
| `vendor_schedule` | Shop / Vendor / Delivery S / Planning S / Lead Time | 288 |
| `mss_list` | Shop / MC / A / B / C | 336 |
| `d001_moq` | SKU / MOQ | 200 |
| `exemption_qty` | SKU / A_A … C_C（9 欄） | 12 |

- 上載 **CSV 或 Excel** 均可，整表替換在單一 transaction 內，失敗自動回滾
- 上載前自動把現有內容備份成 CSV 到 `reference/backup/`
- 每張表可匯出 CSV／Excel
- 載入時同步偵測「同一 key 有兩行不同值」，主數據頁顯示重複鍵衝突數

真實部署時把六張表換成你自己的資料即可，程式不用改。

## 數據核對（只讀）

`health.py` 在每次計算後，用匯出檔的**現行值**反查主數據，出 11 項檢查。
只讀，永不自動改表——避免「用交易數據反向污染主數據」。

| 檢查 | 級別 | 何時會中 |
|---|---|---|
| 店舖未登記在車期日曆 | error | 倉貨行會冇車期 |
| 行貨 SKU 未指定 vendor | error | SS=1 但 Article Master 冇 Major vendor |
| Vendor Schedule 缺「店舖 × vendor」 | error | 車期與 Lead Time 會變 0 |
| 主數據有重複鍵且值不同 | error | 只會採用最後一行 |
| 倉貨現行車期與日曆表不一致 | warn | 日曆過時或 SAP 未更新 |
| 行貨現行車期與 Vendor Schedule 不一致 | warn | 換 vendor 後仍帶舊值（屬預期） |
| 店舖未在 Shop_Class | warn | Coverage 用預設 8/5/3 |
| MSS 缺「店舖 × MC」 | warn | Safety Qty 退到 MOQ |
| SKU 未在 Article Master | warn | Supply Source 沿用匯出檔現值 |
| SKU 完全沒有 Safety Qty 來源 | info | Safety Qty 暫定 1 |
| Supply Source 兩邊不同 | info | 以 Article Master 為準 |

結果會同時以紅色（error）／黃色（warn）／綠色橫幅顯示在結果卡上方。

## API

| 端點 | 用途 |
|---|---|
| `POST /api/compute` | 上載 rp_file（＋master／ideal 選填）＋mode，回 65 欄結果與 `health` |
| `POST /api/healthcheck` | 只跑核對，不做計算 |
| `POST /api/lookup` | 店舖（＋vendor）→ 車期 |
| `GET /api/reference` | 主數據筆數、DB 大小、重複鍵統計 |
| `POST /api/reference/{table}` | 上載更新某張表（csv／xlsx） |
| `GET /api/reference/{table}/export?fmt=csv\|xlsx` | 匯出 |
| `GET /api/export/{run_id}?fmt=xlsx\|txt` | 匯出計算結果 |
| `GET /api/demo` | 用 `samples/` 的虛擬資料跑一次，供試用 |

## 本機啟動

```bash
pip install -r requirements.txt
python -m uvicorn main:app --port 8000
```

開 http://127.0.0.1:8000 ，按「下載輸入欄位範本」可拿到欄名清單。

首次啟動會自動把 `seed/*.csv` 複製到 `reference/` 並建立 `reference/rp.db`（約 1 秒）。
想重新產生虛擬資料：

```bash
python scripts/make_seed.py
```

## 部署到 Railway

```bash
railway init
railway up
```

或 GitHub 連動：New Project → Deploy from repo，`Dockerfile` 已備好，`PORT` 由 Railway 注入。

### Volume（必要）

1. Service → Settings → Volumes → Mount path 填 **`/data`**
2. `Dockerfile` 已設 `RP_REF_DIR=/data`，首次啟動由 image 內種子建立 `rp.db`
3. 備份＝複製 `/data/rp.db` 一個檔案

### Serverless

服務細、每日只用一兩次，開 **Settings → Deploy → Serverless** 最省。
10 分鐘無 outbound 流量就睡，睡眠期間不計 CPU／RAM。

- 冷啟動約 3–8 秒（載入資料約 1 秒），第一次請求可能 502，重試即可
- 不要加 Postgres 連線池——持續連線會令服務永遠不睡

### 資源參考（本機量度，示範資料規模）

| 項目 | 數值 |
|---|---|
| 常駐 RAM | 約 200 MB |
| 啟動載入 6 張表 | 約 1 秒 |
| 主數據 | `rp.db` 約 11 MB（真實資料規模） |

按 Railway Hobby（RAM $10/GB-月、CPU $20/vCPU-月、Volume $0.15/GB-月）估算：
每日用 1 小時約 $0.1–0.25/月；24/7 常開約 $2–2.5/月。

## 專案結構

```
engine.py            規則引擎：車期、Coverage、Safety Qty、65 欄輸出
health.py            數據核對（11 項，只讀）
store.py             SQLite 儲存層（整表替換 + transaction + 匯出）
main.py              FastAPI 路由
static/index.html    單頁前端（無框架、無 build step）
scripts/make_seed.py 產生虛擬主數據與示範輸入
seed/                虛擬主數據（24 店 × 12 vendor 的縮小版）
samples/             示範 RP 清單與 Article Master
```

## 已知限制

1. **TXT 格式**：現時 Tab 分隔、無標題行、UTF-8、65 欄。實際 SAP 批量上載可能需要不同分隔符或標題行，
   `main.py` 的 `export()` 可直接調整。
2. **行貨與 Flow Thru 路徑**未有真實輸出可回測，只按內部手冊實作，建議用自己的個案核對一次。
3. **權限／審計**：現時無登入，run 記錄只存在記憶體（重啟即清）。正式多人使用前要加。
4. **單機寫入**：SQLite 適合單一使用者；多人同時上載主數據要換 Postgres。
