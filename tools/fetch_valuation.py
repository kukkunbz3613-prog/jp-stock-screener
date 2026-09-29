"""SI・IT 企業のバリュエーション指標を取得して data/valuation.csv に保存する（個別の分析用）。"""
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
PEERS = {
    "2327.T": "日鉄ソリューションズ", "4307.T": "野村総合研究所", "3626.T": "TIS", "8056.T": "BIPROGY",
    "6702.T": "富士通", "6701.T": "NEC", "4739.T": "伊藤忠テクノソリューションズ", "4812.T": "電通総研",
    "4768.T": "大塚商会", "9759.T": "NSD", "4722.T": "フューチャー", "3774.T": "IIJ", "4684.T": "オービック",
    "1306.T": "TOPIX連動ETF",
}
FIELDS = ["currentPrice", "marketCap", "trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda",
          "dividendYield", "returnOnEquity", "operatingMargins", "revenueGrowth", "earningsGrowth",
          "targetMeanPrice", "targetHighPrice", "targetLowPrice", "numberOfAnalystOpinions",
          "recommendationMean", "recommendationKey", "trailingEps", "forwardEps"]

rows = []
hist = yf.download(list(PEERS), period="3y", interval="1d", auto_adjust=True, progress=False)["Close"]
for code, name in PEERS.items():
    row = {"code": code, "name": name}
    try:
        info = yf.Ticker(code).info or {}
        row.update({k: info.get(k) for k in FIELDS})
    except Exception as e:  # noqa: BLE001
        row["error"] = str(e)[:100]
    s = hist[code].dropna() if code in hist else pd.Series(dtype=float)
    for label, days in [("ret_3m", 63), ("ret_1y", 250), ("ret_3y", 740)]:
        row[label] = float(s.iloc[-1] / s.iloc[-1 - days] - 1) if len(s) > days else None
    row["last_date"] = str(s.index[-1].date()) if len(s) else None
    rows.append(row)
pd.DataFrame(rows).to_csv(ROOT / "data" / "valuation.csv", index=False)
print(pd.DataFrame(rows)[["name", "trailingPE", "forwardPE", "priceToBook", "ret_1y"]])
