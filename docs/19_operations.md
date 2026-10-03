# 運用マニュアル

更新: 2026-10-03。[初回導入](18_setup.md) / [Git公開・移行](20_git_publish.md)。以下のuvコマンドはプロジェクト直下で実行する。

## 1. 毎日の開始条件

平日20:00 JSTのHermesジョブを運用する場合、PCが起動・ログイン済みでスリープしていないこと、Gateway稼働、日次ジョブ有効を確認する。ローカルAIを使う場合はDocker Desktopも起動する。専用GPUコンテナは停止状態でよい。

```powershell
hermes gateway status
hermes cron list
```

実行日は取引所カレンダーで決める。通常の土日は定期実行対象外で、Alpha LoopのためにDockerを起動し続ける必要はない。祝日に平日ジョブが動いても直近の完了営業日を使い、同じ入力は再利用する。

初回は[18の環境変数](18_setup.md)を設定する。新しいPowerShellを開くたびに必要な設定:

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
```

## 2. 実行中か確認する

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operation-status
```

見る項目:

- `running` / `owner_unknown`: 現在のOS所有者と監督の状態。
- `active_attempts[].phase`: 取得、判定、AI、補助評価、週次などの現在段階。
- `collection_progress`: 取得処理済み件数/予定件数と実際の対象営業日。
- `latest_result.session`: 最後に完了した日。実行中の日とは別の場合がある。
- `supervision`: 試行回数、時間切れ、自動再開、終了状態。

RUNNINGファイルがあるだけで実プロセスの生存を断定しない。`OWNER_UNKNOWN`は完了でも停止でもない。取得件数が全件になっても、評価・AI・集計・復元が続いていれば全工程は未完了。

## 3. 対象日の終了を確認する

`--session`には実際の対象営業日を指定する。休日は最後に処理した営業日を確認する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/check_daily_batch.py --session 2026-10-02 --save-proof
```

保存先は`data/operations/daily_check_<対象日>.json`。対象日一致、現在の所有者、価格/評価/材料/週次成果物hash、候補CSV行数を確認する。

| 状態 | 判断・対応 |
|---|---|
| `verified_complete=true` + `SUCCEEDED` | 保存成果物を検証した完了 |
| `SUCCEEDED_WITH_GAPS` | 完了したが欠損あり。件数と対象範囲を確認する |
| `RUNNING` / `OWNER_UNKNOWN` | 終了とは扱わず、監督・heartbeatを確認する |
| `NO_COMPLETED_RESULT_FOR_REQUESTED_SESSION` | その日の完了結果なし。昨日の成功を流用しない |
| `SUCCEEDED_WITH_AI_FAILURE` / `WITH_M5_FAILURE` / `WITH_SIDE_FAILURE` | 数値CSVは保持されるが全工程正常ではない。失敗理由を確認する |
| `BLOCKED_DATA` / `FAILED` | 未完了。入力・休止・取得・起動ログを確認する |

材料未提供の`NO_AVAILABLE_DOCUMENTS`、実験未登録の`NOT_REGISTERED`、期間待ちの`WAITING`はそれぞれ正常な保留分岐。実資料の品質合格・実験完了を意味しない。

AI後のGPU復元は`data/operations/gpu_leases/`で`RESTORED`を確認する。Hermes側も`cron list`で結果と次回予定を確認する。20時は開始予定であり終了予定ではない。最新の実確認は2026-10-02、20:00開始→時間切れ後自動再開→21:06:44完了、候補39・欠損75だった。[検証記録](04_validation.md)を参照。

## 4. 出力の見方

| 内容 | 保存先 |
|---|---|
| 候補CSV | `outputs/<run_id>/candidates.csv` |
| 全銘柄の分類・選外理由 | `outputs/<run_id>/decisions.csv`・`decisions.json` |
| 判定根拠・条件/入力/ソース版 | `run_manifest.json`、`data/snapshots/`、`data/code_bundles/` |
| 前のrunの翌営業日結果 | `outputs/<前のrun>/evaluation/<id>/outcomes.csv` |
| 急騰銘柄の前日状態比較 | `outputs/retrospectives/study-*/` |
| Qwen日報 | `outputs/<run>/qwen/` |
| 材料・引用・正解比較 | `outputs/<run>/materials/`、`outputs/material_quality/` |
| 週次仮説案 | `outputs/weekly_research/completed/<週>/` |
| M5比較 | `outputs/research/<experiment_id>/<report_id>/`、`outputs/material_comparisons/` |

日付Dの候補は次営業日向けで、翌日上昇を保証しない。金曜なら通常は月曜向け。翌営業日の処理でD候補を評価する。高値騰落と売買可能な収益は別。約定不明・費用未設定は実利益へ変換しない。

CSVはUTF-8 BOM付き。候補ゼロでもヘッダー付きCSVが生成される。空欄/unknownはゼロ・陰性・材料なしではない。売買代金の欠損は推計で埋めない。

## 5. 手動再実行と障害対応

まず2の状態確認を行い、生存workerと二重起動しない。既存ジョブのIDは`hermes cron list`で確認する。

```powershell
$jobId = '既存のAlpha Loop JP dailyのID'
hermes cron run $jobId
```

Gateway停止・PC電源断をまたいだ同じ回の即時再発火は保証しない。20時を逃した場合もDocker起動だけで再実行を保証しない。既存ジョブまたは手動CLIで実際の開始・対象営業日を確認する。取得時刻/as_ofは実時刻で、20時へ戻さない。

数値だけを処理する場合:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operate --no-ai
```

同じ入力は検証して再利用する。Qwen失敗後、Docker復旧・worker停止を確認した上でAIだけを再試行する場合:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operate --retry-ai
```

local設定運用では同じ`--operation-config`も指定する。`--refresh`は原本更新が必要な場合だけ。429休止や整合性エラーを回避するために付けない。

| 事象 | 確認先・対応 |
|---|---|
| 時間切れ | `supervision/<id>/receipt.json`と試行ログ。対象日・入力を固定して有限回数だけ自動再開 |
| 429休止 | `data/market/cooldown.json`。休止終了を待ち、並列増加や別経路で回避しない |
| Docker/WSL停止 | 数値結果とAI失敗を確認。Docker復旧後に再試行。OS復旧自体は自動化していない |
| hash/設定/ソース不一致 | 実行中の更新・原本変更を調べる。元の固定版へ戻すか別版として再登録する |
| 所有者不明 | 古いロックを即削除せずOSプロセスと監督記録を確認する |

詳細は[運用安定化仕様](14_m4_stability.md)。本番稼働中に`git pull`、設定変更、Python環境更新をしない。

## 6. 材料・改善研究

材料を使う場合は許諾済み本文とmanifestを`incoming/disclosures/`へ置く。[取込み・人手正解](15_materials_and_weekly.md)、[長文](17_long_materials.md)を参照。実初見時刻を過去へ戻さない。入力がない場合も価格候補処理は継続する。

M5は仮説・条件・期間・モデル/質問を事前登録し、未使用期間終了まで保留する。新規cloneには元PCの登録実験は存在しない。元PCの2研究実験は2026-12-01以降、全翌日ラベルが揃ってから比較可能だが、reconstructedのため本番採用の証拠にはしない。

週次新案は下書き。`APPROVE_SHADOW`は従来の別版準備だけ。日次の価格仮説のACTIVE・自動監視/復帰は[21の自己改善経路](21_hypothesis_loop.md)で実装し、初回は明示的人手確認が必要。材料B1/B2のACTIVEは別の残工程。採用した人間の名前をAIが代行しない。[価格M5](13_m5_comparison.md) / [材料M5](16_m5_material_comparison.md)。

自己改善付きのHermesラッパーは`configs/hermes_self_improving.json`を既定とする。`ALPHA_LOOP_CONFIG`で従来/local設定を指定している場合はその値を使う。日次JSONの`self_improvement`・`self_improvement_monitor`・`self_improvement_promotion`を確認する。実験WAITING/品質HOLDや未採用は正常な保留。運用候補のファイルは`operational_candidate_csv`で確認し、基準CSVと全仮説版を併記したshadow CSVを取り違えない。

## 7. バックアップ・更新・移行

Gitはコードの保存であり、運用状態のバックアップではない。少なくとも`data/`（原本・snapshot・固定ソース・SQLite・実験・キャッシュ）、`outputs/`、`incoming/`、実際に使う`configs/`を一緒に保管する。

**worker/監督が停止していることを確認してから**コピーする。SQLite本体だけを稼働中にコピーしない。既存スクリプトには絶対パスも記録されるため、移設は元のパスで復元するのが最も確実。別パス移設では成果物/台帳の参照先を確認し、保存JSONを直接書換えてhashを破壊しない。

PowerShellでプロジェクト内のローカルバックアップを作る例:

```powershell
$backupDirectory = Join-Path (Get-Location) ('backups/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
@('data', 'outputs', 'incoming', 'configs') | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object {
    Copy-Item -LiteralPath $_ -Destination $backupDirectory -Recurse
}
```

同じディスク内のコピーだけではディスク故障対策にならないため、必要に応じて外部保管する。ローカル設定・本文・正解・株価の公開可否はコードと別。バックアップはGit対象外。

Hermesのジョブ/config/起動設定、Dockerの固定イメージ・モデルvolumeも別管理。Hermes設定は認証情報を含み得るため公開しない。移行後は合成確認→設定/旧版ソース照合→保存成果物/replay確認→Gateway/ジョブ確認の順に検証する。移行確認のための`--refresh`で過去原本を差し替えない。
