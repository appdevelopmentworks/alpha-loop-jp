# 日次ダッシュボードとGitHub Pages

2026-10-04。ユーザー確定: 現在の `appdevelopmentworks/alpha-loop-jp` を使ってGitHub Pagesで公開する。新規リポジトリーとCloudflareは使わない。

**合成デモ公開済み:** [Alpha Loop JP ダッシュボード](https://appdevelopmentworks.github.io/alpha-loop-jp/)。2026-10-04にsyntheticだけの5ファイルを `gh-pages` へPushし、既存の公開元 `gh-pages` / `/(root)` とビルド完了、公開画面を確認した。公開commitは `a0ed2b01f811bf7f2b948b9dc1a4ea7d3a3dcb0b`。表示数値は架空のデモで、実市場の予測成績ではない。実結果の自動Pushは無効のまま。

## 表示内容

- 最新候補数、候補日、翌営業日、結果CSVの保存済み/評価待ち、更新日時、欠損数。
- 1週間/2週間/1か月/指定期間の件数、日別グラフ、日次一覧、銘柄検索、表示項目のCSV保存。
- 対象は日次サービスが保存した**価格基準候補**。自己改善のshadow/ACTIVE候補や保護された比較期間の途中成績は取得しない。画面に条件版・データ区分を表示する。
- 的中は保存済みの `discovery_hit`（基準版では翌営業日の分割調整高値が前日終値比10%以上）。終値騰落率や売買収益とは区別する。
- 終値騰落率は「翌営業日の分割調整終値 ÷ (候補日の分割調整終値 × 翌日のprior_close_rebase_factor) − 1」。日次receiptの評価参照から原本を読み、保存評価のfuture_hashと一致する場合だけPython/Decimalで算出。古いreceiptに参照がなければ該当runの日次評価原本保存先だけからhash一致を探す。実験/保護期間の入力や現在の市場データは検索しない。判定条件・元の評価CSVは変更しない。原本/終値/分割調整がない場合は「—」とし、値を推定しない。
- 「不的中のうち終値下落」件数、最大下落率、日別内訳、銘柄別の符号付き騰落率を表示。下落銘柄は赤い表示にし、「不的中・下落のみ」で下落率の大きい順に絞り込める。終値不明件数は別表示。高値で的中して終値で下落した銘柄も終値欄には表示し、不的中・下落件数へは含めない。
- 最大下落は不的中・下落候補の終値騰落率の最小値で、日中の安値や実現損益ではない。表示用結果CSVにも終値騰落率（比率値）と下落区分を追加。これはdashboard-v2の表示拡張であり、M5の条件・採否・収益評価は変更しない。
- 候補のdecision_idだけを全母集団の結果へ結合する。未評価/欠損/調整不明は分母から除外。期間の的中率は延べ的中数÷延べ評価可能候補数。日別率の平均は使わない。
- 同一候補日・データ区分・条件版・閾値では最初に封印された実行を採用。replayや単独の研究実行は日次集計へ入れない。異なる版・データ区分・事前予測算入可否は画面で別集計する。
- CSVボタンは表示用の項目を出力する。元の候補/結果CSV、根拠・原本はローカルへ保存したままで、Webに同梱しない。候補ゼロでもヘッダー付きCSVを保存可能。

## 最短の確認（追加パッケージ・API・Docker・GPU不要）

プロジェクト直下のPowerShellで、既存のuv環境を使う。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/export_dashboard.py --demo --public
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/serve_dashboard.py --site outputs/dashboard/demo
```

`http://127.0.0.1:8765/`を開く。サーバーの終了はCtrl+C。新規環境でPythonをまだ取得していなければ `--offline` を外す。HTMLをダブルクリックする `file://` ではJSON取得が失敗するためHTTPで表示する。プレビューはloopbackのみ、画面の5ファイルだけを配信し、プロジェクト全体は配信しない。

合成デモは22営業日分の架空データで、候補ゼロ・欠損・直近の評価待ちを含む。デモ用カレンダーであり、実市場の成績や正式営業日カレンダーの検証結果ではない。

## 実結果をローカル表示する

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/export_dashboard.py
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/serve_dashboard.py
```

`outputs/dashboard/local/`はGit除外。実行済みの日だけを読み、1か月分に足りない日を合成で補わない。yfinanceの `reconstructed` は**事後の参考集計**と明示する。原本や評価のハッシュ検証が失敗すると出力を更新しない。

## GitHub Pagesへ初回公開する

この実装には送信用コマンドを用意したが、初期設定では自動Pushしない。公開準備は合成デモで確認できる。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/publish_dashboard.py --site outputs/dashboard/demo
```

既定はDRY_RUN。ネットワーク接続・Git変更なしで、送信ファイルを確認する。公開する場合はGitの認証を済ませ、同じコマンドへ `--push` を付ける。対象は固定された既存リポジトリーの `gh-pages` だけ。ユーザーのmain、作業ツリー、indexを変更せず、隔離した一時checkoutから通常Pushする。強制Pushはしない。既存の公開ブランチに無関係なファイルがあれば停止し、競合も失敗として記録する。

初回Push後、GitHubのリポジトリー画面で:

1. Settings → Pages。
2. Sourceを **Deploy from a branch**。
3. Branchを **gh-pages**、フォルダーを **/(root)** にしてSave。
4. デプロイ完了を確認し `https://appdevelopmentworks.github.io/alpha-loop-jp/` を開く。

GitHub CLIから設定する場合は `gh auth login` が別途必要。2026-10-04の確認時点ではCLIと作業用ブラウザーは未認証だったが、既存Git認証でPush成功。同じ認証をメモリ内だけで使った公式API確認により、公開元設定済み・対象commitのビルド成功を確認した。認証情報は保存・表示していない。GitのPush認証とCLI認証は別なので、CLI未認証だけでGitの認証状態は判断しない。新規環境ではブラウザーのSettings → Pagesから設定する方法も使える。

GitHub Freeでは公開リポジトリーが必要。設定したフォルダーはWeb配信範囲であり、公開リポジトリー内の別ファイルを非公開にする機能ではない。

公開ファイルは `index.html`、`style.css`、`app.js`、`.nojekyll`、`data/dashboard.json` の5つ。原本/台帳/内部パス/LLM応答は出力しない。許可していない余分なフィールドやファイル、symlinkがある場合は送信を拒否する。

## 日次自動更新

新版の `integrations/hermes/alpha_loop_daily.py` は日次処理成功後、ローカル画面を生成する。2026-10-04に既存Hermesラッパー/スキルを退避して同期し、隔離合成・no-aiで候補3件→画面生成→公開無効を確認した。既存ジョブの設定と実結果は保持した。次回の実定時全工程は未確認。新規環境では[導入](18_setup.md)の既存ラッパー配置手順を使い、ジョブは追加しない。`alpha_loop.cli operate` の直接実行ではこの後処理は走らないので、必要なら出力CLIを実行する。

実データの公開を有効にする前に、そのデータと派生結果の公開条件を確認する。yfinanceの取得成功・個人研究利用は公開権限を意味しない。次のローカル設定はGit除外:

```powershell
Copy-Item configs/dashboard_publication.example.json configs/dashboard_publication.local.json
```

- `allowed_data_grades`: 公開を確認したデータ区分だけを指定。初期値はsyntheticのみ。
- `rights_confirmed`: 実データの公開条件を確認した場合のみtrue。
- `rights_note`: 確認した契約・利用条件と公開可能な項目を記録する。ソフトウェアは権利を自動判定しない。
- `auto_publish`: 上記確認と初回公開・認証後にtrueへ変更すると、日次終了時に公開データ生成→gh-pagesへのPushを実行する。

ローカル出力/公開処理の成功記録は `data/operations/dashboard/latest.json`。公開失敗や権限不足は `data/operations/dashboard/failure.json` に記録し、ラッパーは非ゼロで終了する。日次の数値成果物は保持する。再実行で同一内容ならUNCHANGEDで追加commitを作らない。データの更新は原子的で、Pushに失敗した場合は公開サイトの前回版が残る。

ローカルPC/Docker停止中も公開済み画面は閲覧可能。新しい結果の生成・送信にはローカル日次処理とネットワークが必要。表示する最終更新日時で鮮度を確認する。認証情報はファイルへ書かずGitの認証管理を使う。

## 検証と未実施

`python -S scripts/verify_dashboard.py`は合成22日分の画面、実結果のローカル出力、公開DRY_RUN、元の候補CSV・数値コード保持を確認する。証跡は `data/operations/dashboard_acceptance.json`。既存Hermesへの同期確認は `scripts/verify_dashboard_deployment.py`、証跡は `data/operations/dashboard_deployment.json`。[検証記録](04_validation.md)を参照。

GitHub初回Push・公開元・ビルドと公開画面の表示/期間切替は確認済み。証跡は `data/operations/dashboard/publication.json`、`data/operations/dashboard/pages_setup.json`、`data/operations/dashboard_published.png`。終値騰落率の拡張はPython23件、画面側18検査、合成22日/ローカル実結果4日の生成、元の候補CSV23ファイルと数値コード保持を確認。拡張版の公開commitは `87d9c52159b6976b4e84caa25ecc35cf45775036`。実データの公開権利確認、新版ラッパー配置後の実定時更新、自己改善版比較の可視化は残条件。CSV生成処理は画面側テストで確認済みだが、公開画面の保存操作では作業用ブラウザーのdownload完了通知を取得できず、ファイル保存の実確認は未了。

## 公式資料

- [GitHub Pagesの公開元設定](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
- [yfinanceのデータ利用上の注意](https://github.com/ranaroussi/yfinance)
