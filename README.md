# Alpha Loop JP

日本株の未上昇・初動候補を抽出し、候補と選外理由をCSVへ保存して、翌営業日の結果を再現可能な方法で評価する研究用システムです。

数値計算・分類・評価はPythonで実行します。原本、取得・利用可能時刻、判定時点、条件版、ソース版、選外理由を保存します。Python環境は**uv管理**です。

## 現在できること

| 機能 | 状態 |
|---|---|
| ファイル・合成データ入力、候補/選外CSV、翌営業日評価、replay | 実装済み |
| 候補・翌日結果の静的ダッシュボード、GitHub Pages向け出力 | [合成デモ公開済み](https://appdevelopmentworks.github.io/alpha-loop-jp/)。的中・終値下落の件数/率/銘柄別表示。実結果はローカル表示。自動Pushは無効、実データの公開条件は未確認 |
| yfinanceによる株価履歴収集、急騰銘柄の前日状態との比較 | 個人研究用に接続済み |
| Windows Hermesの平日20時実行、中断再開、状態確認 | 実機確認済み。新規環境では別途導入・登録が必要 |
| ローカルQwen日報・仮説前の分析/推論・日次/週次仮説案 | 接続済み。根拠と分析を別保存。GPU環境は任意の追加構成 |
| OpenJev材料shadow、根拠保存・正解比較、長文抜粋分析 | 実装済み。実資料品質は未検証 |
| M5事前登録、比較・採否、日次仮説候補・結果のフィードバック、採用・監視・復帰 | 実装済み。初回本番採用は人手確認、実データ採用は品質・未使用期間待ち |

毎日の急騰前状態から仮説を作り、仮説による翌営業日候補と結果・見逃しを次の改善へつなぎます。価格基準への有限条件のANDフィルターを別版で試し、未使用期間の比較を通った版だけを運用へ反映します。[自己改善ループの範囲と実行](docs/21_hypothesis_loop.md)を参照してください。

外部AIへの送信と発注は既定で無効です。合成試験や実機接続の成功は、実市場の予測精度・収益の証明にはしません。現在のyfinance入力は`reconstructed`で、真正point-in-timeの独立検証は未了です。

## 最短の動作確認

必要なのはGit、uv、およびuv管理のPython 3.11です。以下の合成確認にはAPIキー、Docker、GPU、Hermes、追加Pythonパッケージは不要です。初回のuv/Python取得にはネットワークを使う場合があります。

新しくcloneしたプロジェクト直下で、PowerShellから実行します。

Windowsでは短い配置先（例: `C:\src\alpha-loop-jp`）を推奨します。hash付き成果物の保存パスが深くなるためです。[導入マニュアル](docs/18_setup.md)に注意点を記載しています。

```powershell
uv venv --managed-python --python 3.11
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --managed-python --no-project --python 3.11 python -S scripts/verify_long_material.py
```

`data/long_text_demo/<id>/`に隔離した合成入力、候補CSV、翌営業日結果CSV、長文分割・応答を生成し、replayと保存応答の再利用を確認します。具体的な出力先は標準出力と`data/operations/long_text_acceptance.json`に保存します。既存の実日次データを置き換えません。

Pythonが準備済みなら`uv run`に`--offline`を追加できます。既存環境では`.venv`を作り直さず、環境変数の設定から始めてください。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m unittest discover -s tests -v
```

分析工程追加時点で全240テスト成功。仮説前分析のローカルQwen実応答と、API/GPUなしの合成ループも確認済み。[検証仕様・実施記録](docs/04_validation.md)に合成/実機/未実施の区別を記載しています。

ダッシュボード追加後は全体回帰254件と公開経路の追加2件、画面側の検査9件が成功。実結果4日分のローカル表示と合成22日分のデモを生成し、既存Hermesへの日次出力の配置も合成で確認しました。2026-10-04に合成デモをGitHub Pagesへ公開し、表示と期間切替を確認済みです。実データの公開条件確認と新版の実定時更新は未実施です。

自己改善の合成確認（API/GPU・追加パッケージ不要）:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_self_improvement.py
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_inference.py
```

## マニュアル

| 目的 | 文書 |
|---|---|
| 新しいPCへの導入、合成/実データ/GPU/Hermesの準備 | [導入マニュアル](docs/18_setup.md) |
| 毎日の開始条件、終了確認、再実行、障害対応、バックアップ | [運用マニュアル](docs/19_operations.md) |
| 初回Git公開、除外確認、commit/push、環境移行 | [Git公開手順](docs/20_git_publish.md) |
| 要求・設計・データ契約・検証仕様 | [設計文書の索引](docs/README.md) |
| 価格M5比較 / 材料M5比較 / 長文材料 | [価格比較](docs/13_m5_comparison.md) / [材料比較](docs/16_m5_material_comparison.md) / [長文分析](docs/17_long_materials.md) |
| 仮説を立てる前の分析・推論、根拠・代替説明・反証条件 | [分析工程](docs/22_pre_hypothesis_inference.md) |
| ダッシュボードの表示、GitHub Pages公開、日次更新 | [ダッシュボード手順](docs/23_dashboard_pages.md) |

## リポジトリ構成

```text
src/alpha_loop/       確定的な計算・保存・評価と外部接続境界
configs/             版付きの共通設定・質問・ローカルGPU構成
tests/               合成データによる契約・数値・運用テスト
scripts/             合成受入確認、指定日確認、材料分析の補助CLI
integrations/hermes/ Hermesラッパーとスキルの配置用ソース
docker/              固定OpenJevイメージ用パッチ
docs/                導入・運用・設計・検証・決定記録
uv.lock              依存関係の固定（Gitへ含める）
```

`data/`、`outputs/`、`incoming/`、仮想環境、認証情報、モデル重みはGitへ含めません。cloneだけでは既存の原本、実験台帳、実行履歴、Docker volume、Hermesジョブは復元されません。必要な状態は[運用マニュアル](docs/19_operations.md)に従って別途バックアップします。

## 制限と残作業

- 売買代金は任意で、欠損を終値×出来高などで埋めません。費用未設定は純損益proxyがnull、約定不明は実収益として集計しません。
- Yahoo! JAPAN・株探ランキングページの自動スクレイピングは実装していません。価格から自前の急騰一覧を作ります。
- 実材料の許諾済み本文・独立した人手正解、長文全体の統合・OCR、実未使用期間による本番採用、長期運用受入は残っています。
- `configs/`の閾値・品質基準は設計初期値です。利用者の採用承認と区別します。
- ソース・設定はハッシュで識別するため、Gitの改行自動変換を`.gitattributes`で無効にしています。

本リポジトリに第三者データ・モデルは同梱しません。配布ライセンスはまだ設定していません。データ・モデル・外部ソフトウェアの利用条件はそれぞれ確認してください。
