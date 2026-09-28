"""指定銘柄の日足を取得して data/history/<code>.csv に保存する（個別の分析用）。"""
import sys
from pathlib import Path

import yfinance as yf

out = Path(__file__).resolve().parent.parent / "data" / "history"
out.mkdir(parents=True, exist_ok=True)
for code in sys.argv[1:]:
    df = yf.download(code, period="2y", interval="1d", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]].round(2)
    df.to_csv(out / f"{code.replace('^', '')}.csv")
    print(code, len(df), df.index.min().date(), df.index.max().date())
