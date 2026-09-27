"""IT投資一覧のサマリーをメールで送る（GitHub Actions から毎週実行）。

必要な環境変数（GitHub の Secrets に保存する。リポジトリが公開なのでコードには書かない）:
    SMTP_USER   送信元の Gmail アドレス
    SMTP_PASS   Gmail のアプリパスワード（16桁）
    MAIL_TO     宛先（カンマ区切りで複数可）

使い方:
    python send_summary.py            # 送信
    python send_summary.py --dry-run  # 送らずに本文を表示
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
PAGE_URL = "https://kukkunbz3613-prog.github.io/jp-stock-screener/it-invest.html"
LAST = ROOT / "data" / "mail_last.json"   # 前回メールで対象だった会社（「今週の新顔」の判定に使う）
TOP_N = 15

# ページの初期表示と同じ条件
MIN_INVEST = 10e8
MIN_REV, MAX_REV = 10e8, 1000e8
LAND = {"不動産業", "電気・ガス業"}


def load() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "docs" / "it_invest.csv", dtype={"証券コード": str})
    for c in ["ソフトウェア投資(円)", "売上高(円)", "投資/売上", "前期比"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def summarize(df: pd.DataFrame) -> dict:
    target = df[(df["ソフトウェア投資(円)"] >= MIN_INVEST) & df["売上高(円)"].between(MIN_REV, MAX_REV)
                & ~df["業種"].isin(LAND)].sort_values("投資/売上", ascending=False)
    kanto = target[target["都道府県"].isin(["東京都", "神奈川県", "千葉県", "埼玉県"])]
    prev = set(json.loads(LAST.read_text(encoding="utf-8"))) if LAST.exists() else None
    new = target[~target["社名"].isin(prev)] if prev is not None else target.iloc[0:0]
    ucsv = ROOT / "data" / "unlisted_candidates.csv"
    unlisted = pd.read_csv(ucsv, dtype=str).fillna("") if ucsv.exists() else pd.DataFrame()
    return {"total": len(df), "target": target, "kanto": kanto, "new": new,
            "first": prev is None, "unlisted": unlisted}


def fmt_row(r) -> tuple[str, str, str, str, str]:
    flag = " ※前期の10倍超（IT以外の可能性）" if pd.notna(r["前期比"]) and r["前期比"] >= 9 else ""
    return (str(r["社名"]).replace("株式会社", "").strip(), f"{r['都道府県'] or '—'}・{r['業種'] or '—'}",
            f"{r['ソフトウェア投資(円)'] / 1e8:,.1f}億円", f"{r['売上高(円)'] / 1e8:,.0f}億円",
            f"{r['投資/売上']:.1%}{flag}")


def build(s: dict, today: str) -> tuple[str, str, str]:
    subject = f"【週次】IT投資が多い企業 {today}（条件該当 {len(s['target'])}社・首都圏 {len(s['kanto'])}社）"
    top = [fmt_row(r) for _, r in s["kanto"].head(TOP_N).iterrows()]
    new = [fmt_row(r) for _, r in s["new"].head(TOP_N).iterrows()]
    cond = "投資10億円以上・売上10億〜1,000億円・不動産/電力ガス除く"

    lines = [f"IT投資が多い企業の週次サマリーです（{today}）。", "", f"一覧ページ: {PAGE_URL}",
             f"非上場の候補: {PAGE_URL}#unlisted", "",
             f"■ 概要", f"・有価証券報告書 {s['total']:,}社のうち、条件（{cond}）に該当 {len(s['target'])}社",
             f"・うち首都圏（東京・神奈川・千葉・埼玉） {len(s['kanto'])}社",
             f"・非上場の候補（手作業で調査） {len(s['unlisted'])}社", ""]
    if s["first"]:
        lines += ["■ 今週の新顔", "・今回が初回のため、次回から前週との差分をお知らせします。", ""]
    else:
        lines += [f"■ 今週の新顔（{len(s['new'])}社）"]
        lines += [f"・{n}（{p}） 投資 {i} / 売上 {v} = {r}" for n, p, i, v, r in new] or ["・なし"]
        lines += [""]
    lines += [f"■ 首都圏 売上に対する投資割合 上位{min(TOP_N, len(top))}社"]
    lines += [f"{k}. {n}（{p}） 投資 {i} / 売上 {v} = {r}" for k, (n, p, i, v, r) in enumerate(top, 1)]
    lines += ["", "※ 投資額はキャッシュ・フロー計算書のソフトウェア（なければ無形固定資産）の取得支出です。",
              "  クラウド利用料・運用委託費は含みません。営業に使う前に原本を確認してください。"]
    text = "\n".join(lines)

    def table(rows, numbered):
        tr = "".join(
            f"<tr><td style='padding:4px 8px;color:#86857f'>{k if numbered else ''}</td>"
            f"<td style='padding:4px 8px'><b>{html.escape(n)}</b><br><span style='color:#86857f;font-size:12px'>{html.escape(p)}</span></td>"
            f"<td style='padding:4px 8px;text-align:right'>{i}</td><td style='padding:4px 8px;text-align:right'>{v}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{html.escape(r)}</td></tr>"
            for k, (n, p, i, v, r) in enumerate(rows, 1))
        head = ("<tr style='color:#52514e;font-size:12px'><th></th><th style='text-align:left;padding:4px 8px'>社名</th>"
                "<th style='padding:4px 8px'>投資</th><th style='padding:4px 8px'>売上</th><th style='padding:4px 8px'>割合</th></tr>")
        return f"<table style='border-collapse:collapse;font-size:14px'>{head}{tr}</table>"

    new_html = ("<p>今回が初回のため、次回から前週との差分をお知らせします。</p>" if s["first"]
                else table(new, False) if new else "<p>なし</p>")
    body = f"""<div style="font-family:system-ui,sans-serif;color:#0b0b0b;max-width:680px">
<p>IT投資が多い企業の週次サマリーです（{today}）。</p>
<p><a href="{PAGE_URL}" style="display:inline-block;padding:8px 14px;background:#2a78d6;color:#fff;border-radius:6px;text-decoration:none">一覧ページを開く</a>
&nbsp;<a href="{PAGE_URL}#unlisted">非上場の候補（{len(s['unlisted'])}社）</a></p>
<h3 style="margin:20px 0 6px">概要</h3>
<ul style="margin:0;padding-left:20px"><li>有価証券報告書 {s['total']:,}社のうち、条件（{cond}）に該当 <b>{len(s['target'])}社</b></li>
<li>うち首都圏（東京・神奈川・千葉・埼玉） <b>{len(s['kanto'])}社</b></li></ul>
<h3 style="margin:20px 0 6px">今週の新顔{'' if s['first'] else f'（{len(s["new"])}社）'}</h3>{new_html}
<h3 style="margin:20px 0 6px">首都圏 売上に対する投資割合 上位{min(TOP_N, len(top))}社</h3>{table(top, True)}
<p style="color:#86857f;font-size:12px;margin-top:20px">投資額はキャッシュ・フロー計算書のソフトウェア（なければ無形固定資産）の取得支出です。
クラウド利用料・運用委託費は含みません。営業に使う前に原本を確認してください。</p></div>"""
    return subject, text, body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    s = summarize(load())
    subject, text, body = build(s, today)
    if args.dry_run:
        print(subject, "\n", text, sep="")
        return

    user, password, to = (os.environ.get(k, "").strip() for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and password and to):
        print("SMTP_USER / SMTP_PASS / MAIL_TO が未設定のため、メールは送りません")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        sys.exit("Gmail にログインできませんでした。SMTP_PASS にアプリパスワードが入っているか確認してください")
    LAST.write_text(json.dumps(sorted(s["target"]["社名"]), ensure_ascii=False), encoding="utf-8")
    print(f"送信しました（宛先 {len([t for t in to.split(',') if t.strip()])} 件）")


if __name__ == "__main__":
    main()
