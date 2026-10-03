# 導入マニュアル

更新: 2026-10-03。Windows / PowerShellを確認環境とする。Pythonはuvで管理し、システムPythonへ依存しない。[日常運用](19_operations.md) / [Git公開・移行](20_git_publish.md)。

## 1. 導入する範囲

| 構成 | 必要なもの | 新規cloneで利用できる範囲 |
|---|---|---|
| 合成・ファイル入力 | Git、uv、uv管理Python 3.11 | 候補CSV・翌日結果CSV・replay・材料/M5合成確認 |
| 株価の研究用収集 | 上記＋market-data追加依存、ネット接続 | yfinance / JPX月末一覧から数値処理。GPU不要 |
| ローカルAI | 上記＋Docker Desktop、対応NVIDIA GPU、固定モデル/コンテナ | Qwen日報、OpenJev材料shadow。別途セットアップ |
| 定時実行 | 上記＋WindowsネイティブHermes、Gateway、no-agentジョブ | 平日20時の日次運用。別途登録 |

Hermesはこの構成ではWindows側で動く。Docker内のHermesを前提としない。DockerはQwen/OpenJev用で、cloneやDocker Desktop起動だけではジョブは登録されない。

## 2. 新規環境の合成確認

Gitとuvを準備し、プロジェクトをcloneしてそのディレクトリへ移動する。以下は初回だけ実行する。既存`.venv`を作り直す手順ではない。

Windowsでは`C:\src\alpha-loop-jp`等の短い配置先を推奨する。原本・コード版・推論応答のhash付き保存パスが深くなるため、長い配置先ではOSのパス長制限で`FileNotFoundError`になる場合がある。今回の合成確認も短い隔離配置先で実施した。

```powershell
uv venv --managed-python --python 3.11
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --managed-python --no-project --python 3.11 python -S scripts/verify_long_material.py
```

初回はuvが管理Pythonを取得する場合がある。準備済みなら`uv run`へ`--offline`を追加する。`--no-project`でビルド依存の導入を避け、`-S`で追加パッケージを無効にして合成確認を行う。

成功条件:

- 出力JSONに`candidate_csv`と`outcomes_csv`が表示され、実際にファイルが生成される。
- `price_candidates_unchanged`と`saved_response_reused`がtrue、`replay`が成功する。
- `data_grade=synthetic`。これは実株価・実開示の品質検証ではない。

生成物は`data/long_text_demo/<id>/`、証跡は`data/operations/long_text_acceptance.json`に保存する。

追加の合成確認と全テスト:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_material_m5.py
uv run --offline --managed-python --no-project --python 3.11 python -m unittest discover -s tests -v
```

`verify_*_deployment.py`、`verify_hermes_research.py`等は既存運用データ・インストール状態を検証する補助であり、新規clone向けの必須手順ではない。

## 3. 合成候補と翌日評価を個別に実行する

実日次の台帳と混ぜず、隔離したrootを使う。上記の環境変数を設定してから実行する。

```powershell
$demoRoot = Join-Path (Get-Location) ('data/manual_demo/' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$inputPath = Join-Path $demoRoot 'input'
$baselinePath = Join-Path (Get-Location) 'configs/baseline.json'
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli fixture --out $inputPath --without-turnover
$run = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root $demoRoot run --input $inputPath --config $baselinePath --session 2026-09-17 --as-of 2026-09-17T20:00:00+09:00 | ConvertFrom-Json
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root $demoRoot evaluate --run-id $run.run_id --future $inputPath --through 2026-09-18
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root $demoRoot replay --run-id $run.run_id
```

この固定日は合成fixtureの営業日。実データの公表・取得時刻をこの日へ戻すための指定ではない。

## 4. 株価の研究用収集

プロジェクト直下で依存lockに従って準備する。

```powershell
uv sync --locked --managed-python --python 3.11 --extra market-data
```

`uv.lock`を再解決せず使う。ビルド・依存パッケージの初回取得にはネット接続が必要。環境変数は2と同じ。価格のみの部分疎通試験:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operate --limit 3 --no-ai
```

`--offline`はuvのPython/依存取得を止める指定で、yfinanceの通信を止める指定ではない。`--limit 3`は部分試験であり全母集団処理ではない。全件の価格のみ処理は`--limit 3`を外す。

`configs/hermes_operations.json`はStandard / Growthを先に取得し、Primeも含む設計初期値。売買代金は任意。入力はreconstructed、現在のJPX月末一覧は当時母集団を保証しない。非公式yfinanceの個人研究用途と第三者データの条件を守り、429休止を回避しない。Yahoo! JAPAN・株探ページの自動スクレイピングは行わない。

共通設定は`configs/`に追跡する。個人用の変更は`configs/hermes_operations.local.json`など`.local.json`へ保存し、手動CLIの`--operation-config`またはHermesの`ALPHA_LOOP_CONFIG`で指定する。参照する材料設定にも独自の変更があれば対応するlocalファイルへ分離する。外部AI/発注はfalseのまま。

## 5. 任意のローカルGPU構成

合成・価格のみ運用では不要。導入済み試験機はRTX 5090 32GB、Qwenは固定revisionのNVFP4、OpenJevは**razorback16/openjev**。別環境でのGPU適合・VRAM・長文品質は別途確認する。

- 固定構成: `configs/qwen_runtime_rtx5090_v1.json`、`configs/openjev_runtime_rtx5090_v1.json`。
- OpenJevイメージ/専用volume/作成コマンド: [M3実機手順](08_m3_semantic.md)。モデル重みは別途取得する工程で、Gitには同梱しない。
- Qwenの構成・接続実績と制限: [ローカル実行仕様](05_local_runtime.md)、[M4実機記録](09_m4_local_operations.md)。モデル/推論イメージを実設定のrevision/digestへ合わせる。
- 必要な専用コンテナ名は`alpha-loop-qwen`と`alpha-loop-openjev`。単なる名前だけでなくAPIの固定モデルIDまで照合する。Qwenは127.0.0.1:8000、OpenJevは127.0.0.1:8080。外部公開しない。

既存コンテナの確認:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli model-status
```

双方を同時に起動しない。日次コントローラーが排他的に切り替え、元の状態へ復元する。資料未提供の場合はOpenJevを起動しない。初回モデル取得・コンテナ作成・実応答は追加の手動導入工程であり、本書の合成確認はそれを実行しない。

## 6. 任意のHermes定時実行

WindowsネイティブHermesを別途導入し、`hermes --version`、`hermes cron create --help`、`hermes gateway --help`で対応CLIを確認する。以下は確認済みCLIの例。既存ジョブがあるPCでは作り直さない。

```powershell
$projectDir = (Get-Location).Path
$hermesDir = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
New-Item -ItemType Directory -Path (Join-Path $hermesDir 'scripts') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $hermesDir 'skills/alpha-loop-jp') -Force | Out-Null
Copy-Item integrations/hermes/alpha_loop_daily.py (Join-Path $hermesDir 'scripts/alpha_loop_daily.py')
Copy-Item integrations/hermes/alpha-loop-jp/SKILL.md (Join-Path $hermesDir 'skills/alpha-loop-jp/SKILL.md')
$env:ALPHA_LOOP_ROOT = $projectDir
[Environment]::SetEnvironmentVariable('ALPHA_LOOP_ROOT', $projectDir, 'User')
hermes config set cron.script_timeout_seconds 7200
hermes cron list
```

ルートを移設したら`ALPHA_LOOP_ROOT`を更新する。起動済みGatewayには後から設定した環境変数が届かないため、設定後の新しい環境からGatewayを起動する。local設定を使う場合は`ALPHA_LOOP_CONFIG`も同様に指定する。認証情報やHermesのconfig/.env全体をリポジトリへコピーしない。

既存の同用途ジョブがない場合だけ、停止状態で登録する。Windowsのタイムゾーンが日本時間であることを確認する。

```powershell
hermes cron create '0 20 * * 1-5' --name 'Alpha Loop JP daily' --script alpha_loop_daily.py --workdir $projectDir --no-agent --deliver local --paused
```

作成されたIDを記録する。Gatewayが動く環境で手動確認を行い、成功後に有効化する。

```powershell
$jobId = '作成されたジョブID'
hermes gateway status
hermes cron run $jobId
hermes cron list
hermes cron resume $jobId
```

`cron run`はscheduler tickでの実行要求。即時完了とは扱わず、[運用マニュアル](19_operations.md)でプロジェクト側の結果を確認する。Gatewayのログイン時起動はHermes導入環境の手順で設定し、`hermes gateway status`と次回予定を確認する。Dockerだけの起動ではGatewayは起動しない。

Hermesのno-agentは対話モデルを呼ばない方式。日報用ローカルQwenはPythonから別途呼ぶ。全体7100秒、外側7200秒、1試行3000秒・最大3回は現行初期値。共通cron timeout設定は他のHermesジョブにも影響するため、既存環境では現設定を確認する。

この文書整備では新しいジョブ登録・有効化、モデルのダウンロードは実行していない。
