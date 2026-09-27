"""有価証券報告書（EDINET）から「売上の割にソフトウェア投資が多い企業」を一覧にする。

使い方:
    EDINET_API_KEY=xxxx python it_invest.py
    python it_invest.py --demo      # ネットワークなしで合成データを使って表示確認

仕組み:
    1. EDINET の書類一覧 API で、直近 LOOKBACK_DAYS 日に提出された有価証券報告書を集める
    2. 各社の最新の有報を CSV 形式（XBRL を CSV に変換したもの）で取得する
    3. キャッシュ・フロー計算書の「ソフトウェアの取得による支出」（無ければ「無形固定資産の取得による支出」）
       と、主要な経営指標の売上高を取り出し、売上に対する比率を計算する

出力:
    docs/it-invest.html      一覧ページ（GitHub Pages）
    docs/it_invest.csv       同じ内容の CSV
    data/it_invest_raw.csv   取得済みの有報ごとの抽出値（次回以降の差分取得に使う）
    data/edinet_docs.csv     有報の書類一覧のキャッシュ
    data/it_invest_log.txt   実行ログ
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
API = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
LOOKBACK_DAYS = 400      # 有報は年1回なので、1年強さかのぼれば全社の最新版がそろう
RESCAN_DAYS = 7          # 前回スキャン済みでも直近はもう一度見る（訂正・遅延登録対策）
MIN_INVEST_LISTED = 1e8  # ページに載せる最低投資額（1億円）。絞り込みは画面側で行う
WORKERS = 4

DOCS_CACHE = ROOT / "data" / "edinet_docs.csv"
RAW_CACHE = ROOT / "data" / "it_invest_raw.csv"
LOG = ROOT / "data" / "it_invest_log.txt"

log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}"
    print(line, flush=True)
    log_lines.append(line)


# ---------------------------------------------------------------- EDINET API

class Edinet:
    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()

    def get(self, url: str, **params) -> requests.Response:
        params["Subscription-Key"] = self.key
        for attempt in range(4):
            try:
                r = self.s.get(url, params=params, timeout=60)
                if r.status_code == 200:
                    return r
                if r.status_code in (401, 403):
                    sys.exit(f"EDINET API キーが無効です（HTTP {r.status_code}）。Secrets の EDINET_API_KEY を確認してください")
            except requests.RequestException as e:
                if attempt == 3:
                    raise
                log(f"  再試行 {url}: {e}")
            time.sleep(2 ** attempt)
        r.raise_for_status()
        return r

    def documents(self, date: dt.date) -> list[dict]:
        r = self.get(f"{API}/documents.json", date=date.isoformat(), type=2)
        body = r.json()
        # キーが無効でも HTTP 200 で本文に statusCode が入ることがある
        status = str(body.get("statusCode") or body.get("metadata", {}).get("status", "200"))
        if status in ("401", "403"):
            sys.exit("EDINET API キーが無効です。Secrets の EDINET_API_KEY を確認してください")
        return body.get("results") or []

    def csv_zip(self, doc_id: str) -> bytes:
        return self.get(f"{API}/documents/{doc_id}", type=5).content


def scan_documents(api: Edinet) -> pd.DataFrame:
    """直近の有価証券報告書の一覧を作る（日付ごとの一覧はキャッシュして差分だけ取得）。"""
    cols = ["docID", "edinetCode", "secCode", "filerName", "periodEnd", "submitDateTime", "scanDate"]
    cache = pd.read_csv(DOCS_CACHE, dtype=str) if DOCS_CACHE.exists() else pd.DataFrame(columns=cols)
    today = dt.date.today()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    rescan_from = (today - dt.timedelta(days=RESCAN_DAYS)).isoformat()
    marker = ROOT / "data" / "edinet_scanned_dates.txt"
    scanned = set(marker.read_text().split()) if marker.exists() else set()
    scanned = {d for d in scanned if start.isoformat() <= d < rescan_from}

    dates = [start + dt.timedelta(days=i) for i in range(LOOKBACK_DAYS + 1)]
    todo = [d for d in dates if d.isoformat() not in scanned]
    log(f"書類一覧: {len(todo)} 日分を取得（キャッシュ済み {len(dates) - len(todo)} 日）")

    new_rows = []
    for i, d in enumerate(todo, 1):
        for doc in api.documents(d):
            if (doc.get("docTypeCode") == "120" and doc.get("ordinanceCode") == "010"
                    and doc.get("csvFlag") == "1" and doc.get("withdrawalStatus") == "0"):
                new_rows.append({"docID": doc["docID"], "edinetCode": doc["edinetCode"],
                                 "secCode": doc.get("secCode") or "", "filerName": doc["filerName"],
                                 "periodEnd": doc.get("periodEnd") or "",
                                 "submitDateTime": doc.get("submitDateTime") or "",
                                 "scanDate": d.isoformat()})
        if i % 50 == 0:
            log(f"  {i}/{len(todo)} 日")
        time.sleep(0.2)

    rescanned = {d.isoformat() for d in todo}
    cache = cache[~cache["scanDate"].isin(rescanned)]
    docs = pd.concat([cache, pd.DataFrame(new_rows, columns=cols)], ignore_index=True)
    docs = docs[docs["scanDate"] >= start.isoformat()].drop_duplicates("docID")
    docs.to_csv(DOCS_CACHE, index=False)
    marker.write_text("\n".join(sorted(scanned | rescanned)))
    return docs


# ---------------------------------------------------------------- XBRL→CSV の解析

CUR = ["CurrentYearDuration", "CurrentYearDuration_NonConsolidatedMember"]
PRIOR = ["Prior1YearDuration", "Prior1YearDuration_NonConsolidatedMember"]
INST = ["CurrentYearInstant", "CurrentYearInstant_NonConsolidatedMember"]

# 売上に当たる項目（業種によって名前が違う）。先にあるものを優先する
REVENUE_NAMES = [re.compile(p) for p in [
    r"^売上高(?!総利益|原価)", r"^売上収益", r"^営業収益", r"^経常収益", r"^営業総収入", r"^営業総収益",
    r"^売上高及び営業収入", r"^完成工事高", r"^事業収益", r"^営業収入", r"^収益合計", r"^純営業収益",
    r"^総収益", r"^保険料等収入", r"^収益(?!性)",
]]
SOFTWARE = re.compile(r"ソフトウ[エェ]ア")
INTANGIBLE = re.compile(r"無形(固定)?資産")
OUTFLOW = re.compile(r"支出|取得|購入")
# 売却・償却などの逆方向の項目や、有形固定資産・子会社株式と合算された項目は除く
EXCLUDE = re.compile(r"売却|除却|収入|償却|減損|有形|子会社|事業譲受|連結の範囲")
PARSER_VERSION = "2"  # 抽出ルールを変えたら上げる（キャッシュ済みの有報を読み直す）


def num(v: str) -> float | None:
    try:
        return float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def pick(rows, contexts, match) -> tuple[float | None, str]:
    """contexts の優先順に、条件に合う行の最大値とその項目名を返す。"""
    for ctx in contexts:
        vals = [(abs(v), r[1]) for r in rows if r[2] == ctx and match(r) and (v := num(r[8])) is not None]
        if vals:
            return max(vals)
    return None, ""


def parse(rows: list[list[str]]) -> dict:
    summary = [r for r in rows if r[0].endswith("SummaryOfBusinessResults")]
    # 連結を優先し、その中で売上に当たる項目を順に探す（持株会社の単体売上を拾わないため）
    revenue = None
    for ctx in CUR:
        for pat in REVENUE_NAMES:
            revenue, _ = pick(summary, [ctx], lambda r, p=pat: bool(p.search(r[1])))
            if revenue:
                break
        if revenue:
            break

    def is_cf(r, kind):
        name = r[1]
        return (kind.search(name) and OUTFLOW.search(name) and not EXCLUDE.search(name)
                and ("CF" in r[0] or "キャッシュ" in name or "支出" in name))

    sw, item = pick(rows, CUR, lambda r: is_cf(r, SOFTWARE))
    sw_prev, _ = pick(rows, PRIOR, lambda r: is_cf(r, SOFTWARE))
    basis = "ソフトウェア"
    if sw is None:
        sw, item = pick(rows, CUR, lambda r: is_cf(r, INTANGIBLE))
        sw_prev, _ = pick(rows, PRIOR, lambda r: is_cf(r, INTANGIBLE))
        basis = "無形資産"
    employees, _ = pick(rows, INST, lambda r: r[0].endswith(":NumberOfEmployees"))
    return {"revenue": revenue, "sw_invest": sw, "sw_prev": sw_prev,
            "basis": basis if sw is not None else "", "item": item.split("、")[0],
            "employees": employees}


def read_zip(raw: bytes) -> list[list[str]]:
    rows: list[list[str]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1]
            if name.endswith(".csv") and base.startswith("jpcrp"):
                text = z.read(name).decode("utf-16")
                rows += [r for r in csv.reader(io.StringIO(text), delimiter="\t") if len(r) >= 9][1:]
    return rows


# ---------------------------------------------------------------- 取得と集計

RAW_COLS = ["docID", "edinetCode", "secCode", "filerName", "periodEnd",
            "revenue", "sw_invest", "sw_prev", "basis", "item", "employees", "error", "version"]


def extract_all(api: Edinet, docs: pd.DataFrame) -> pd.DataFrame:
    latest = docs.sort_values("submitDateTime").drop_duplicates("edinetCode", keep="last")
    raw = pd.read_csv(RAW_CACHE, dtype={"docID": str, "edinetCode": str, "secCode": str}) \
        if RAW_CACHE.exists() else pd.DataFrame(columns=RAW_COLS)
    if "version" not in raw:
        raw["version"] = ""
    # 失敗したものと、古い抽出ルールで読んだものは読み直す
    raw = raw[(raw["error"].isna() | (raw["error"] == "")) & (raw["version"].astype(str) == PARSER_VERSION)]
    done = set(raw["docID"])
    todo = latest[~latest["docID"].isin(done)]
    log(f"有報: 対象 {len(latest)} 社 / 新たに取得 {len(todo)} 件")

    results: list[dict] = []
    lock = threading.Lock()

    def work(doc: dict) -> dict:
        row = {k: doc.get(k, "") for k in ["docID", "edinetCode", "secCode", "filerName", "periodEnd"]}
        try:
            row.update(parse(read_zip(api.csv_zip(doc["docID"]))))
            row["error"] = ""
            row["version"] = PARSER_VERSION
        except Exception as e:  # noqa: BLE001 — 1社の失敗で全体を止めない
            row["error"] = f"{type(e).__name__}: {e}"[:200]
        time.sleep(0.3)
        return row

    def save():
        with lock:
            pd.concat([raw, pd.DataFrame(results, columns=RAW_COLS)]).to_csv(RAW_CACHE, index=False)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, d) for d in todo.to_dict("records")]
        for i, f in enumerate(as_completed(futures), 1):
            results.append(f.result())
            if i % 200 == 0:
                log(f"  {i}/{len(todo)} 件")
                save()
    save()
    errors = [r for r in results if r["error"]]
    log(f"取得失敗 {len(errors)} 件" + (f"（例: {errors[0]['filerName']} {errors[0]['error']}）" if errors else ""))

    all_raw = pd.read_csv(RAW_CACHE, dtype={"docID": str, "edinetCode": str, "secCode": str})
    return all_raw[all_raw["docID"].isin(set(latest["docID"]))].drop_duplicates("docID", keep="last")


def load_codelist() -> pd.DataFrame:
    r = requests.get(CODELIST_URL, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        df = pd.read_csv(z.open(name), encoding="cp932", skiprows=1, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df.rename(columns={"ＥＤＩＮＥＴコード": "edinetCode", "上場区分": "listed",
                              "提出者業種": "industry", "所在地": "address"})[
        ["edinetCode", "listed", "industry", "address"]]


PREF = re.compile(r"^(東京都|北海道|京都府|大阪府|.{2,3}?県)")
# EDINET の所在地は東京23区・政令指定都市だと都道府県が省かれているので補う
TOKYO_WARDS = ("千代田区 中央区 港区 新宿区 文京区 台東区 墨田区 江東区 品川区 目黒区 大田区 世田谷区 渋谷区 "
               "中野区 杉並区 豊島区 北区 荒川区 板橋区 練馬区 足立区 葛飾区 江戸川区").split()
CITY_PREF = {"札幌市": "北海道", "仙台市": "宮城県", "さいたま市": "埼玉県", "千葉市": "千葉県",
             "横浜市": "神奈川県", "川崎市": "神奈川県", "相模原市": "神奈川県", "新潟市": "新潟県",
             "静岡市": "静岡県", "浜松市": "静岡県", "名古屋市": "愛知県", "京都市": "京都府",
             "大阪市": "大阪府", "堺市": "大阪府", "神戸市": "兵庫県", "岡山市": "岡山県",
             "広島市": "広島県", "北九州市": "福岡県", "福岡市": "福岡県", "熊本市": "熊本県"}


CITIES_URL = "https://geolonia.github.io/japanese-addresses/api/ja.json"  # {都道府県: [市区町村, ...]}
CITIES_CACHE = ROOT / "data" / "cities.json"
_city_list: list[tuple[str, str]] | None = None


def city_list() -> list[tuple[str, str]]:
    """(市区町村名, 都道府県) の一覧。長い名前から順に照合できるよう並べる。"""
    global _city_list
    if _city_list is None:
        data = {}
        try:
            r = requests.get(CITIES_URL, timeout=60)
            r.raise_for_status()
            data = r.json()
            CITIES_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — 取れなければキャッシュか手持ちの対応表で補う
            log(f"市区町村一覧を取得できませんでした: {e}")
            if CITIES_CACHE.exists():
                data = json.loads(CITIES_CACHE.read_text(encoding="utf-8"))
        pairs: dict[str, set[str]] = {}
        for pref, cities in data.items():
            for c in cities:
                pairs.setdefault(c, set()).add(pref)
                # 「安芸郡府中町」のような郡付きの名前は、郡を省いた書き方でも照合する
                if "郡" in c:
                    pairs.setdefault(c.split("郡", 1)[1], set()).add(pref)
        for w in TOKYO_WARDS:
            pairs.setdefault(w, set()).add("東京都")
        for c, p in CITY_PREF.items():
            pairs.setdefault(c, set()).add(p)
        # 同じ名前の市が複数の県にある場合（府中市など）は判定しない
        _city_list = sorted(((c, next(iter(p))) for c, p in pairs.items() if len(p) == 1 and len(c) >= 2),
                            key=lambda x: -len(x[0]))
    return _city_list


def prefecture(address: str) -> str:
    address = (address or "").strip()
    if m := PREF.match(address):
        return m.group(1)
    for city, pref in city_list():
        if address.startswith(city):
            return pref
    return ""


def build(raw: pd.DataFrame, codes: pd.DataFrame) -> pd.DataFrame:
    df = raw.merge(codes, on="edinetCode", how="left")
    for c in ["revenue", "sw_invest", "sw_prev", "employees"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["sw_invest"] >= MIN_INVEST_LISTED].copy()
    df["ratio"] = df["sw_invest"] / df["revenue"]
    df["growth"] = (df["sw_invest"] / df["sw_prev"].where(df["sw_prev"] > 0) - 1)
    df["per_emp"] = df["sw_invest"] / df["employees"]
    df["pref"] = df["address"].fillna("").map(prefecture)
    df["code"] = df["secCode"].fillna("").astype(str).str[:4]
    df["listed"] = df["listed"].fillna("")
    df["industry"] = df["industry"].fillna("")
    df["item"] = df["item"].fillna("") if "item" in df else ""
    return df.sort_values("ratio", ascending=False, na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------- 出力

def write_outputs(df: pd.DataFrame, demo: bool) -> None:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    out = df[["filerName", "code", "listed", "industry", "pref", "address", "periodEnd",
              "sw_invest", "sw_prev", "growth", "basis", "item", "revenue", "ratio", "employees", "per_emp"]]
    out.rename(columns={
        "filerName": "社名", "code": "証券コード", "listed": "上場区分", "industry": "業種",
        "pref": "都道府県", "address": "所在地", "periodEnd": "決算期末",
        "sw_invest": "ソフトウェア投資(円)", "sw_prev": "前期ソフトウェア投資(円)", "growth": "前期比",
        "basis": "投資額の根拠", "item": "投資額の項目名（有報の表記）", "revenue": "売上高(円)", "ratio": "投資/売上", "employees": "従業員数",
        "per_emp": "1人あたり投資(円)",
    }).to_csv(ROOT / "docs" / "it_invest.csv", index=False, encoding="utf-8-sig")

    def r(v, d=4):
        # JSON に NaN / Infinity は書けない（ページが表示されなくなる）ので null にする
        return round(float(v), d) if pd.notna(v) and math.isfinite(float(v)) else None

    records = [[x.filerName, x.code, x.listed, x.industry, x.pref, x.periodEnd,
                r(x.sw_invest / 1e8, 2), r(x.growth), x.basis, r(x.revenue / 1e8 if pd.notna(x.revenue) else None, 1),
                r(x.ratio), r(x.employees, 0), r(x.per_emp / 1e4 if pd.notna(x.per_emp) else None, 1), x.item]
               for x in df.itertuples()]
    page = (ROOT / "it_template.html").read_text(encoding="utf-8")
    note = ('<p class="banner">⚠ デモモード：合成データです。実在の企業の数値ではありません。</p>' if demo else "")
    for k, v in {"TODAY": today, "N": f"{len(df):,}", "DEMO_NOTE": note,
                 "DATA": json.dumps(records, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")}.items():
        page = page.replace(f"%%{k}%%", v)
    (ROOT / "docs" / "it-invest.html").write_text(page, encoding="utf-8")


def demo_data() -> pd.DataFrame:
    import numpy as np
    rng = np.random.default_rng(1)
    inds = ["情報・通信業", "銀行業", "サービス業", "機械", "小売業", "その他金融業", "電気機器", "卸売業"]
    prefs = ["東京都", "大阪府", "愛知県", "静岡県", "岩手県", "福岡県", "山形県"]
    n = 800
    rev = np.exp(rng.normal(np.log(3e10), 1.4, n))
    sw = rev * np.exp(rng.normal(np.log(0.01), 1.0, n))
    return pd.DataFrame({
        "filerName": [f"サンプル企業{i:03d}株式会社" for i in range(n)],
        "secCode": [f"{1000 + i}0" if i % 3 else "" for i in range(n)],
        "listed": ["上場" if i % 3 else "非上場" for i in range(n)],
        "industry": rng.choice(inds, n), "address": [p + "どこか1-1" for p in rng.choice(prefs, n)],
        "periodEnd": "2026-03-31", "revenue": rev, "sw_invest": sw,
        "sw_prev": sw * np.exp(rng.normal(-0.1, 0.3, n)), "basis": "ソフトウェア",
        "employees": (rev / 3e7).round(), "item": "ソフトウエアの取得による支出", "edinetCode": [f"E{i:05d}" for i in range(n)],
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="合成データで実行")
    args = ap.parse_args()

    if args.demo:
        raw = demo_data()
        df = build(raw.drop(columns=["listed", "industry", "address"]),
                   raw[["edinetCode", "listed", "industry", "address"]])
        write_outputs(df, demo=True)
        print(df.head(10)[["filerName", "sw_invest", "revenue", "ratio"]])
        return

    key = os.environ.get("EDINET_API_KEY")
    if not key:
        sys.exit("環境変数 EDINET_API_KEY を設定してください")
    try:
        api = Edinet(key)
        docs = scan_documents(api)
        raw = extract_all(api, docs)
        codes = load_codelist()
        df = build(raw, codes)
        write_outputs(df, demo=False)
        found = raw["sw_invest"].notna().sum()
        log(f"完了: 有報 {len(raw)} 社のうちソフトウェア/無形資産投資を取得 {found} 社、"
            f"1億円以上 {len(df)} 社、10億円以上 {(df['sw_invest'] >= 1e9).sum()} 社")
        for x in df[df["sw_invest"] >= 1e9].head(10).itertuples():
            log(f"  {x.filerName} 投資 {x.sw_invest / 1e8:,.1f}億円 / 売上 {x.revenue / 1e8:,.0f}億円 "
                f"= {x.ratio:.1%}" if pd.notna(x.ratio) else f"  {x.filerName} 投資 {x.sw_invest / 1e8:,.1f}億円")
    finally:
        LOG.write_text("\n".join(log_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
