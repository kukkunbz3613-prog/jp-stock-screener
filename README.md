# 日本株 成長期待ランキング

売上高3000億円以上の日本企業を対象に、株価トレンドとファンダメンタルズを点数化し、
今後の伸びが期待できる上位10社を毎日レポートします。

- **レポート**: `docs/index.html`（GitHub Pages で公開）
- **自動更新**: GitHub Actions が平日 16:45（日本時間）に実行
- **手動実行**: Actions タブ → `daily-ranking` → `Run workflow`

## ファイル

| ファイル | 内容 |
|---|---|
| `screener.py` | データ取得・スコア計算・レポート生成 |
| `universe.py` | 候補銘柄の一覧（売上高の判定は実行時） |
| `template.html` | レポートのひな形 |
| `data/latest.csv` | 全銘柄の最新スコア |
| `data/history.csv` | 日次の上位10社の履歴 |

## ローカルで動かす

```sh
pip install -r requirements.txt
python screener.py          # 実データ（Yahoo Finance）
python screener.py --demo   # 合成データで表示確認
```

## スコアの考え方

各指標を対象銘柄内のパーセンタイル順位に変換して加重平均します（重みは `screener.py` の `WEIGHTS`）。
12か月・3か月の株価モメンタム、200日線との乖離、売上・利益成長率、アナリスト目標株価までの上昇余地、
予想PER（割安度）、ROE を使い、RSI(14) が80を超える過熱銘柄は減点します。

機械的なスクリーニング結果であり、投資助言ではありません。
