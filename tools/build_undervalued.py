"""割安の可能性がある候補のページ（docs/undervalued.html）を作る。

根拠・リスク・出典は data/undervalued.json（手作業で調査）、
株価指標は data/valuation.csv（tools/fetch_valuation.py が取得）から読み込む。
"""
import datetime as dt
import html
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def num(v):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) or pd.isna(v) else float(v)


def pct(v, sign=True):
    v = num(v)
    return "—" if v is None else (f"{v * 100:+.1f}%" if sign else f"{v * 100:.1f}%")


def x(v, d=1):
    v = num(v)
    return "—" if v is None else f"{v:.{d}f}倍"


def tone(v):
    v = num(v)
    return "" if v is None or v == 0 else ("pos" if v > 0 else "neg")


def main():
    spec = json.loads((ROOT / "data" / "undervalued.json").read_text(encoding="utf-8"))
    val = pd.read_csv(ROOT / "data" / "valuation.csv").set_index("code")
    topix = val.loc["1306.T"] if "1306.T" in val.index else None
    peers = val.drop(index="1306.T", errors="ignore")
    peer_pe = peers["trailingPE"][(peers["trailingPE"] > 0) & (peers["trailingPE"] < 80)].median()
    asof = val["last_date"].dropna().max()
    e = html.escape

    cards = []
    for c in spec["companies"]:
        m = val.loc[c["code"]] if c["code"] in val.index else pd.Series(dtype=float)
        price, tgt = num(m.get("currentPrice")), num(m.get("targetMeanPrice"))
        up = tgt / price - 1 if price and tgt else None
        rec = num(m.get("recommendationMean"))
        n_an = num(m.get("numberOfAnalystOpinions"))
        rel = (num(m.get("ret_1y")) - num(topix["ret_1y"])) if topix is not None and num(m.get("ret_1y")) is not None else None
        bad = set(c.get("suspect", []))  # データ提供元の異常値（手で確認して指定）
        if bad:
            m = m.copy()
            for k in bad:
                m[k] = float("nan")
        badge = {"A": "根拠 強", "B": "根拠 中", "C": "根拠 弱"}.get(c["strength"], "参考")
        kpis = [
            ("株価", "—" if price is None else f"¥{price:,.0f}", ""),
            ("1年騰落（対TOPIX）", f"{pct(m.get('ret_1y'))}（{pct(rel)}）", tone(m.get("ret_1y"))),
            ("52週高値から", pct(m.get("from_52w_high")), tone(m.get("from_52w_high"))),
            ("PER（実績）", x(m.get("trailingPE")), ""),
            ("PBR", x(m.get("priceToBook"), 2), ""),
            ("ROE / 営業利益率", f"{pct(m.get('returnOnEquity'), False)} / {pct(m.get('operatingMargins'), False)}", ""),
            ("売上成長（前年同期比）", pct(m.get("revenueGrowth")), tone(m.get("revenueGrowth"))),
            ("アナリスト平均目標まで", f"{pct(up)}" + (f"（{n_an:.0f}人）" if n_an else ""), tone(up)),
            ("推奨度（1買い〜5売り）", "—" if rec is None else f"{rec:.2f}", ""),
        ]
        kpi_html = "".join(f"<div><dt>{e(k)}</dt><dd class='{t}'>{e(v)}</dd></div>" for k, v, t in kpis)
        if bad:
            kpi_html += "<div class='warn'>「—」の一部はデータ提供元の値が異常なため非表示（要確認）</div>"
        li = lambda xs: "".join(f"<li>{e(s)}</li>" for s in xs)
        src = "".join(f"<li><a href='{e(u)}' target='_blank' rel='noopener'>{e(t)}</a></li>" for t, u in c["sources"])
        cards.append(f"""
    <article class="card" data-strength="{e(c['strength'])}">
      <div class="head"><div><span class="name">{e(c['name'])}</span> <span class="code">{e(c['code'].replace('.T', ''))}</span></div>
        <span class="badge s{e(c['strength'])}">{e(badge)}</span></div>
      <dl class="kpis">{kpi_html}</dl>
      <div class="cols">
        <section><h3>過小評価と考える根拠</h3><ul>{li(c['thesis'])}</ul></section>
        <section><h3>反対材料・リスク</h3><ul class="risk">{li(c['risks'])}</ul></section>
      </div>
      <details><summary>出典（{len(c['sources'])}件）</summary><ul class="src">{src}</ul></details>
    </article>""")

    ind = spec["industry"]
    ind_src = "".join(f"<li><a href='{e(u)}' target='_blank' rel='noopener'>{e(t)}</a></li>" for t, u in ind["sources"])
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    page = TEMPLATE
    for k, v in {
        "CARDS": "".join(cards), "IND": "".join(f"<li>{e(p)}</li>" for p in ind["points"]), "IND_SRC": ind_src,
        "ASOF": e(str(asof)), "CHECKED": e(spec["checked"]), "TODAY": today,
        "TOPIX": pct(topix["ret_1y"]) if topix is not None else "—", "PEER_PE": x(peer_pe),
        "NPEERS": str(len(peers)), "N": str(sum(c["strength"] != "参考" for c in spec["companies"])),
    }.items():
        page = page.replace(f"%%{k}%%", v)
    (ROOT / "docs" / "undervalued.html").write_text(page, encoding="utf-8")
    print("wrote docs/undervalued.html")


TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>割安候補のIT・SI企業</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --border: #e4e3de;
  --text: #0b0b0b; --text-2: #52514e; --muted: #86857f; --accent: #2a78d6;
  --pos: #c8321f; --neg: #1f6fd1;
  --a: #1d7a4f; --a-bg: #e3f3ea; --b: #8a5a00; --b-bg: #fbf0d9; --c: #6b6a65; --c-bg: #ecebe6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --border: #2e2d2b;
    --text: #fff; --text-2: #c3c2b7; --muted: #8e8d86; --accent: #3987e5;
    --pos: #ff7a66; --neg: #6aa8ff;
    --a: #6fd3a3; --a-bg: #16301f; --b: #f0c36a; --b-bg: #33290f; --c: #b9b8b0; --c-bg: #2a2a28;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --border: #2e2d2b;
  --text: #fff; --text-2: #c3c2b7; --muted: #8e8d86; --accent: #3987e5;
  --pos: #ff7a66; --neg: #6aa8ff;
  --a: #6fd3a3; --a-bg: #16301f; --b: #f0c36a; --b-bg: #33290f; --c: #b9b8b0; --c-bg: #2a2a28;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.6 system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; }
main { max-width: 1000px; margin: 0 auto; padding: 28px 16px 60px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 32px 0 10px; }
h3 { font-size: .85rem; margin: 0 0 4px; color: var(--text-2); }
a { color: var(--accent); }
.sub { color: var(--text-2); margin: 0 0 16px; }
.box { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.box ul { margin: 6px 0 0; padding-left: 20px; }
.box li { margin: 3px 0; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 16px; font-size: .82rem; color: var(--text-2); margin: 10px 0 0; }
.filter { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 12px; }
.filter button { font: inherit; font-size: .85rem; padding: 6px 12px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-2); }
.filter button[aria-pressed="true"] { border-color: var(--accent); color: var(--text); }
.list { display: grid; gap: 14px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-weight: 700; font-size: 1.1rem; }
.code { color: var(--muted); font-size: .85rem; }
.badge { font-size: .75rem; font-weight: 700; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }
.sA { color: var(--a); background: var(--a-bg); } .sB { color: var(--b); background: var(--b-bg); }
.sC, .s参考 { color: var(--c); background: var(--c-bg); }
.kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px 14px; margin: 10px 0 12px; }
.kpis dt { color: var(--muted); font-size: .72rem; }
.kpis dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.pos { color: var(--pos); } .neg { color: var(--neg); }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 640px) { .cols { grid-template-columns: 1fr; } }
.cols ul { margin: 0; padding-left: 18px; font-size: .9rem; }
.cols li { margin: 3px 0; }
details { margin-top: 10px; font-size: .85rem; }
summary { cursor: pointer; color: var(--accent); }
.src { margin: 6px 0 0; padding-left: 18px; }
.note { color: var(--muted); font-size: .82rem; }
.warn { grid-column: 1 / -1; color: var(--muted); font-size: .72rem; }
</style>
</head>
<body>
<main>
  <h1>割安候補のIT・SI企業</h1>
  <p class="sub">株価は低迷しているが、業界の成長性・事業内容・中期経営計画からみて過小評価の可能性がある企業 %%N%%社
    ・ <a href="./">株価ランキング</a> ・ <a href="it-invest.html">IT投資が多い企業</a></p>

  <div class="box">
    <h3>選び方</h3>
    <ul>
      <li>IT・SI業界の上場 %%NPEERS%% 社から、<b>株価が低迷</b>（1年の騰落がマイナス、または52週高値から15%以上下落）し、<b>売上が伸びている</b>（減収の会社は除外）企業を抽出。原則として<b>アナリストの平均目標株価が現在値を上回る</b>ことも条件にしました</li>
      <li>そのうえで中期経営計画・決算・報道を調べ、根拠とリスクを出典つきでまとめました（調査日 %%CHECKED%%）</li>
      <li>同じ期間に TOPIX は %%TOPIX%%。IT・SI業界の PER の中央値は %%PEER_PE%%</li>
    </ul>
    <div class="legend"><span><span class="badge sA">根拠 強</span> 中計の数値目標・実績・アナリスト評価がそろう</span>
      <span><span class="badge sB">根拠 中</span> 一部が未確認、または目立つリスクあり</span>
      <span><span class="badge sC">根拠 弱</span> 低迷に業績上の理由がある、または情報が不足</span></div>
  </div>

  <h2>業界の状況</h2>
  <div class="box"><ul>%%IND%%</ul>
    <details><summary>出典</summary><ul class="src">%%IND_SRC%%</ul></details></div>

  <h2>候補一覧</h2>
  <div class="filter" role="group" aria-label="根拠の強さで絞り込み">
    <button aria-pressed="true" data-f="">すべて</button><button aria-pressed="false" data-f="A">根拠 強</button>
    <button aria-pressed="false" data-f="B">根拠 中</button><button aria-pressed="false" data-f="C">根拠 弱・参考</button>
  </div>
  <div class="list">%%CARDS%%
  </div>

  <h2>注意</h2>
  <ul class="note">
    <li>株価指標は Yahoo Finance から自動取得した %%ASOF%% 時点の値で、毎週更新されます。データ提供元の誤り（異常な PER など）が含まれることがあります。</li>
    <li>根拠・リスクは公開情報を要約したもので、内容は必ず出典で確認してください。</li>
    <li>「過小評価」は一つの見方であり、将来の株価を保証するものではありません。投資助言ではなく、投資判断はご自身の責任で行ってください。</li>
  </ul>
</main>
<script>
(() => {
  const btns = document.querySelectorAll('.filter button');
  btns.forEach(b => b.addEventListener('click', () => {
    btns.forEach(x => x.setAttribute('aria-pressed', x === b));
    const f = b.dataset.f;
    document.querySelectorAll('.card').forEach(c => {
      const s = c.dataset.strength;
      c.hidden = f && !(s === f || (f === 'C' && s === '参考'));
    });
  }));
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
