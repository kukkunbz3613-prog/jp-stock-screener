"""売上高3000億円以上の日本企業をスクリーニングし、今後の伸びが期待できる上位10社をレポートする。

使い方:
    python screener.py            # Yahoo Finance から取得してレポート生成
    python screener.py --demo     # ネットワークなしで合成データを使って動作確認

出力:
    docs/index.html      レポート（GitHub Pages でそのまま公開できる）
    data/latest.csv      全銘柄のスコア
    data/history.csv     日次の上位10社の履歴（順位変動の表示に使う）
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from universe import UNIVERSE

ROOT = Path(__file__).parent
MIN_REVENUE = 300_000_000_000  # 3000億円
TOP_N = 10

# スコアの重み（各指標は銘柄間のパーセンタイル順位 0〜1 に変換してから加重平均）
WEIGHTS = {
    "mom_12_1": 0.20,     # 12か月モメンタム（直近1か月を除く）
    "ret_3m": 0.10,       # 3か月リターン
    "trend": 0.15,        # 200日移動平均からの乖離（上にあるほど高評価）
    "rev_growth": 0.15,   # 売上高成長率（前年同期比）
    "eps_growth": 0.10,   # 利益成長率（前年同期比）
    "upside": 0.15,       # アナリスト平均目標株価までの上昇余地
    "fwd_pe_inv": 0.10,   # 予想PERの逆数（割安ほど高評価）
    "roe": 0.05,          # ROE
}
# 過熱ペナルティ：RSI(14) がこれを超えたら減点
RSI_OVERHEAT = 80
OVERHEAT_PENALTY = 0.05


# ---------------------------------------------------------------- データ取得

def fetch_live(codes: list[str]) -> tuple[pd.DataFrame, dict[str, dict]]:
    import yfinance as yf

    tickers = [f"{c}.T" for c in codes]
    prices = yf.download(tickers, period="14mo", interval="1d",
                         auto_adjust=True, progress=False, threads=True)["Close"]
    prices.columns = [c.replace(".T", "") for c in prices.columns]

    def info(code: str) -> tuple[str, dict]:
        try:
            return code, yf.Ticker(f"{code}.T").info or {}
        except Exception as e:  # noqa: BLE001 — 1社の失敗で全体を止めない
            print(f"  info取得失敗 {code}: {e}", file=sys.stderr)
            return code, {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = dict(ex.map(info, codes))
    return prices, infos


def fetch_demo(codes: list[str]) -> tuple[pd.DataFrame, dict[str, dict]]:
    rng = np.random.default_rng(int(dt.date.today().strftime("%Y%m%d")))
    idx = pd.bdate_range(end=dt.date.today(), periods=290)
    prices, infos = {}, {}
    for c in codes:
        drift = rng.normal(0.0004, 0.0008)
        vol = rng.uniform(0.01, 0.025)
        start = rng.uniform(500, 15000)
        prices[c] = start * np.exp(np.cumsum(rng.normal(drift, vol, len(idx))))
        last = prices[c][-1]
        infos[c] = {
            "totalRevenue": rng.uniform(1e11, 4e13),
            "revenueGrowth": rng.normal(0.05, 0.08),
            "earningsGrowth": rng.normal(0.08, 0.2),
            "targetMeanPrice": last * rng.normal(1.08, 0.12),
            "forwardPE": rng.uniform(6, 40),
            "returnOnEquity": rng.normal(0.10, 0.05),
            "marketCap": rng.uniform(5e11, 5e13),
        }
    return pd.DataFrame(prices, index=idx), infos


# ---------------------------------------------------------------- 指標計算

def rsi(series: pd.Series, n: int = 14) -> float:
    d = series.diff().dropna()
    up = d.clip(lower=0).ewm(alpha=1 / n).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n).mean().iloc[-1]
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def build_table(prices: pd.DataFrame, infos: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for code, name in UNIVERSE.items():
        inf = infos.get(code, {})
        rev = inf.get("totalRevenue")
        if code not in prices or rev is None or rev < MIN_REVENUE:
            continue
        s = prices[code].dropna()
        if len(s) < 210:
            continue
        last = float(s.iloc[-1])
        ma200 = float(s.iloc[-200:].mean())

        def ret(days: int, skip: int = 0) -> float:
            if len(s) <= days:
                return np.nan
            return float(s.iloc[-1 - skip] / s.iloc[-1 - days] - 1)

        fpe = inf.get("forwardPE")
        tgt = inf.get("targetMeanPrice")
        rows.append({
            "code": code,
            "name": name,
            "price": last,
            "revenue_oku": rev / 1e8,
            "market_cap_oku": (inf.get("marketCap") or np.nan) / 1e8,
            "chg_1d": ret(1),
            "ret_1m": ret(21),
            "ret_3m": ret(63),
            "ret_12m": ret(250),
            "mom_12_1": ret(250, skip=21),
            "trend": last / ma200 - 1,
            "rsi14": rsi(s),
            "rev_growth": inf.get("revenueGrowth"),
            "eps_growth": inf.get("earningsGrowth"),
            "upside": (tgt / last - 1) if tgt else np.nan,
            "fwd_pe": fpe,
            "fwd_pe_inv": (1 / fpe) if fpe and fpe > 0 else np.nan,
            "roe": inf.get("returnOnEquity"),
            "spark": s.iloc[-250:].round(1).tolist(),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    score = 0.0
    for col, w in WEIGHTS.items():
        pct = pd.to_numeric(df[col], errors="coerce").rank(pct=True)
        score = score + w * pct.fillna(0.5)  # 欠損は中立扱い
    score = score - np.where(df["rsi14"] > RSI_OVERHEAT, OVERHEAT_PENALTY, 0)
    df["score"] = (score * 100).round(1)
    return df.sort_values("score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- 履歴

def update_history(df: pd.DataFrame, today: str) -> dict[str, int]:
    """今日の上位10社を履歴に追記し、前回の順位を {code: rank} で返す。"""
    path = ROOT / "data" / "history.csv"
    hist = pd.read_csv(path, dtype={"code": str}) if path.exists() else pd.DataFrame(
        columns=["date", "rank", "code", "name", "score"])
    hist = hist[hist["date"] != today]
    prev_dates = sorted(hist["date"].unique())
    prev = {}
    if prev_dates:
        last = hist[hist["date"] == prev_dates[-1]]
        prev = dict(zip(last["code"], last["rank"].astype(int)))
    top = df.head(TOP_N)
    new = pd.DataFrame({"date": today, "rank": range(1, len(top) + 1),
                        "code": top["code"], "name": top["name"], "score": top["score"]})
    pd.concat([hist, new]).to_csv(path, index=False)
    return prev


# ---------------------------------------------------------------- HTML

def pct(v, digits=1, sign=True) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"


def num(v, fmt="{:,.0f}") -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return fmt.format(v)


def tone(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)) or v == 0:
        return ""
    return "pos" if v > 0 else "neg"


def render(df: pd.DataFrame, prev: dict[str, int], today: str, demo: bool) -> str:
    top = df.head(TOP_N)
    cards = []
    for i, r in top.iterrows():
        rank = i + 1
        p = prev.get(r["code"])
        if not prev:
            move = ""
        elif p is None:
            move = '<span class="move new">NEW</span>'
        elif p > rank:
            move = f'<span class="move up">▲{p - rank}</span>'
        elif p < rank:
            move = f'<span class="move down">▼{rank - p}</span>'
        else:
            move = '<span class="move flat">―</span>'
        cards.append(f"""
      <article class="card">
        <div class="card-head">
          <span class="rank">{rank}</span>
          <div class="who"><div class="name">{html.escape(r['name'])}</div>
            <div class="code">{r['code']} · 売上 {num(r['revenue_oku'])}億円</div></div>
          <div class="score"><b>{r['score']:.1f}</b><small>スコア</small>{move}</div>
        </div>
        <div class="price-row"><span class="price">¥{num(r['price'], '{:,.1f}')}</span>
          <span class="{tone(r['chg_1d'])}">{pct(r['chg_1d'], 2)}</span></div>
        <svg class="spark" data-series='{json.dumps(r["spark"])}' role="img"
             aria-label="{html.escape(r['name'])} 直近1年の株価"></svg>
        <dl class="kpis">
          <div><dt>3か月</dt><dd class="{tone(r['ret_3m'])}">{pct(r['ret_3m'])}</dd></div>
          <div><dt>12か月</dt><dd class="{tone(r['ret_12m'])}">{pct(r['ret_12m'])}</dd></div>
          <div><dt>売上成長</dt><dd class="{tone(r['rev_growth'])}">{pct(r['rev_growth'])}</dd></div>
          <div><dt>目標株価まで</dt><dd class="{tone(r['upside'])}">{pct(r['upside'])}</dd></div>
          <div><dt>予想PER</dt><dd>{num(r['fwd_pe'], '{:.1f}')}</dd></div>
          <div><dt>RSI(14)</dt><dd>{num(r['rsi14'], '{:.0f}')}</dd></div>
        </dl>
      </article>""")

    rows = []
    for i, r in df.iterrows():
        rows.append(
            f"<tr><td>{i + 1}</td><td>{r['code']}</td><td class='l'>{html.escape(r['name'])}</td>"
            f"<td data-v='{r['score']}'>{r['score']:.1f}</td>"
            f"<td data-v='{r['price']}'>{num(r['price'], '{:,.1f}')}</td>"
            + "".join(f"<td data-v='{0 if pd.isna(r[c]) else r[c]}' class='{tone(r[c])}'>{pct(r[c])}</td>"
                      for c in ["chg_1d", "ret_1m", "ret_3m", "ret_12m", "rev_growth", "upside"])
            + f"<td data-v='{0 if pd.isna(r['fwd_pe']) else r['fwd_pe']}'>{num(r['fwd_pe'], '{:.1f}')}</td>"
            f"<td data-v='{r['revenue_oku']}'>{num(r['revenue_oku'])}</td></tr>")

    weights = " / ".join(f"{LABELS[k]} {int(w * 100)}%" for k, w in WEIGHTS.items())
    demo_note = ('<p class="banner">⚠ デモモード：合成データです。実際の株価ではありません。</p>'
                 if demo else "")
    out = TEMPLATE
    for k, v in {"TODAY": today, "N": str(len(df)), "CARDS": "".join(cards),
                 "ROWS": "".join(rows), "WEIGHTS": weights, "DEMO_NOTE": demo_note,
                 "OVERHEAT": str(RSI_OVERHEAT)}.items():
        out = out.replace(f"%%{k}%%", v)
    return out


LABELS = {"mom_12_1": "12か月モメンタム", "ret_3m": "3か月リターン", "trend": "200日線乖離",
          "rev_growth": "売上成長率", "eps_growth": "利益成長率", "upside": "目標株価乖離",
          "fwd_pe_inv": "割安度(予想PER)", "roe": "ROE"}

TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="合成データで実行")
    args = ap.parse_args()

    codes = list(UNIVERSE)
    print(f"{len(codes)} 銘柄のデータを取得中…")
    prices, infos = (fetch_demo if args.demo else fetch_live)(codes)
    df = build_table(prices, infos)
    if df.empty:
        sys.exit("データを取得できませんでした（ネットワーク接続を確認してください）")

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    prev = {} if args.demo else update_history(df, today)
    df.drop(columns="spark").to_csv(ROOT / "data" / "latest.csv", index=False)
    (ROOT / "docs" / "index.html").write_text(render(df, prev, today, args.demo), encoding="utf-8")

    print(f"\n対象 {len(df)} 社（売上高3000億円以上）/ 上位{TOP_N}社:")
    for i, r in df.head(TOP_N).iterrows():
        print(f"  {i + 1:>2}. {r['code']} {r['name']:<16} スコア {r['score']:5.1f}  "
              f"3M {pct(r['ret_3m']):>7}  12M {pct(r['ret_12m']):>7}")
    print(f"\nレポート: {ROOT / 'docs' / 'index.html'}")


if __name__ == "__main__":
    main()
