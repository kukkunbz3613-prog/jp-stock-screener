"""SI・IT 企業のバリュエーション指標を取得して data/valuation.csv に保存する（個別の分析用）。"""
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
PEERS = dict(pd.read_csv(Path(__file__).resolve().parent / "peers.csv").values)
FIELDS = ["currentPrice", "marketCap", "trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda",
          "dividendYield", "returnOnEquity", "operatingMargins", "revenueGrowth", "earningsGrowth",
          "targetMeanPrice", "targetHighPrice", "targetLowPrice", "numberOfAnalystOpinions",
          "recommendationMean", "recommendationKey", "trailingEps", "forwardEps", "profitMargins",
          "freeCashflow", "totalCash", "totalDebt", "sector", "industry"]

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
    for label, days in [("ret_3m", 63), ("ret_1y", 250), ("ret_3y", 730)]:
        row[label] = float(s.iloc[-1] / s.iloc[-1 - days] - 1) if len(s) > days else None
    if len(s) > 250:
        y = s.iloc[-250:]
        row["from_52w_high"] = float(s.iloc[-1] / y.max() - 1)   # 52週高値からの下落率
        row["high_3y"] = float(s.max())
        row["from_3y_high"] = float(s.iloc[-1] / s.max() - 1)
    row["last_date"] = str(s.index[-1].date()) if len(s) else None
    rows.append(row)
pd.DataFrame(rows).to_csv(ROOT / "data" / "valuation.csv", index=False)
print(pd.DataFrame(rows)[["name", "trailingPE", "forwardPE", "priceToBook", "ret_1y"]])
