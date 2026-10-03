# Alpha Loop JP

日本株の未上昇・初動候補を抽出し、候補と選外理由をCSVへ保存して、翌営業日の結果を再現可能な方法で評価する研究用システムです。

数値計算・分類・評価はPythonで実行します。原本、取得・利用可能時刻、判定時点、条件版、ソース版、選外理由を保存します。Python環境は**uv管理**です。

## 現在できること

| 機能 | 状態 |
|---|---|
| ファイル・合成データ入力、候補/選外CSV、翌営業日評価、replay | 実装済み |
| yfinanceによる株価履歴収集、急騰銘柄の前日状態との比較 | 個人研究用に接続済み |
| Windows Hermesの平日20時実行、中断再開、状態確認 | 実機確認済み。新規環境では別途導入・登録が必要 |
| ローカルQwen日報・週次仮説案 | 接続済み。GPU環境は任意の追加構成 |
| OpenJev材料shadow、根拠保存・正解比較、長文抜粋分析 | 実装済み。実資料品質は未検証 |
| M5事前登録、未使用期間の比較・採否報告、人手レビュー | 実装済み。本番条件への自動反映は未実装 |

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

既存機能の確認記録は2026-10-02時点で184テスト成功。[検証仕様・実施記録](docs/04_validation.md)に合成/実機/未実施の区別を記載しています。

## マニュアル

| 目的 | 文書 |
|---|---|
| 新しいPCへの導入、合成/実データ/GPU/Hermesの準備 | [導入マニュアル](docs/18_setup.md) |
| 毎日の開始条件、終了確認、再実行、障害対応、バックアップ | [運用マニュアル](docs/19_operations.md) |
| 初回Git公開、除外確認、commit/push、環境移行 | [Git公開手順](docs/20_git_publish.md) |
| 要求・設計・データ契約・検証仕様 | [設計文書の索引](docs/README.md) |
| 価格M5比較 / 材料M5比較 / 長文材料 | [価格比較](docs/13_m5_comparison.md) / [材料比較](docs/16_m5_material_comparison.md) / [長文分析](docs/17_long_materials.md) |

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
- 実材料の許諾済み本文・独立した人手正解、長文全体の統合・OCR、本番条件の切替、長期運用受入は残っています。
- `configs/`の閾値・品質基準は設計初期値です。利用者の採用承認と区別します。
- ソース・設定はハッシュで識別するため、Gitの改行自動変換を`.gitattributes`で無効にしています。

本リポジトリに第三者データ・モデルは同梱しません。配布ライセンスはまだ設定していません。データ・モデル・外部ソフトウェアの利用条件はそれぞれ確認してください。
