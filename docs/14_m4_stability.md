# M4 運用の安定化

更新: 2026-10-01。既存のHermesジョブ `8a4841ec482c` に反映済み。平日20:00 JST、uv管理、外部AI・発注・外部通知無効を維持する。数値計算のコードと登録済みM5実験の計算コードは変更していない。

実定時の確認: 10/1は20:00:56に発火。初回が20:50:56に3000秒上限で中断後、同じ監督内で20:50:57に自動再開し、21:04:56に全工程が完了した。3700件取得処理、候補40件、欠損77件、Qwen成功、M5はWAITING/HOLD。監督receiptは`data/operations/supervision/9fb9cee2533f4098a0403539292dc964/receipt.json`。再構成研究入力で予測性能の証拠ではない。材料・週次機能はその後配備したため、新機能の定時確認は10/2以降。手順は15。

## 今回の変更

- **自動再開**: 時間切れ、または実行中のworkerが異常終了したと確認できた場合、同じ呼出し内で最大3試行。1試行の上限3000秒、全体7100秒、子プロセス終了とGPU復元に180秒を確保する設計初期値。Hermes外側は7200秒。回数・残り時間を超えて再試行しない。
- **再開の固定**: 対象営業日、取得済みデータ、設定とソース版を固定する。再開で日付が変わる、入力・設定・ソースが変わる場合は拒否。取得途中は既存銘柄キャッシュを使い、価格判定後は封印済みrunを再利用する。
- **二重起動防止**: supervisorとworkerのOSロック、PIDとOSのプロセス作成識別子で確認する。ロックファイルが残っていても、プロセス終了でOSロックは解放される。既存の生存workerがあれば追加起動せず `SKIPPED_ALREADY_RUNNING` を保存する。
- **正確な状態**: workerの所有者、5秒間隔のheartbeat、処理段階、対象日、取得requestを保存。所有者が死亡していれば `INTERRUPTED`、判定不能なら `OWNER_UNKNOWN`。検出時刻を実際の終了時刻として捏造しない。古い記録にPIDがない場合は勝手に完了へ変えない。
- **終了と復元**: Windows Job Objectで管理し、時間切れ・監督プロセスの異常終了でも子プロセス群を終了させる。GPU切替前の専用2コンテナの状態を記録し、死亡workerの未復元記録だけを排他ロック下で復元する。Dockerに接続できない復元は失敗として残す。復元時に保証するのはコンテナ状態で、モデルAPIの準備完了ではない。
- **毎回の結果記録**: 完了、失敗、途中停止、重複のどれでも監督receiptを出力。変更のない再実行もstdoutに記録を返す。以前の「2回目stdoutは空」という仕様を置き換えた。設定欠落など、worker起動前の失敗も別途保存する。

429の1時間休止を維持し、休止中は `DEFERRED_COOLDOWN` として自動再開しない。入力不正、改変、AI応答不正、Qwen/M5の通常の失敗を無条件で繰り返さない。数値CSVを保持し、AI失敗は既存の `--retry-ai` で明示的に再試行する。

## 実行と状態確認

自動再開を含む通常の手動起動は既存ジョブを使う。

```powershell
hermes cron run 8a4841ec482c
hermes gateway status
hermes cron status
```

プロジェクト直下で処理状態を確認する。Gatewayの生存とAlpha Loopの処理中は別々に確認する。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operation-status
```

JSONの読み方:

| 項目 | 意味 |
|---|---|
| `running` | OSで所有者の生存を確認した処理がある |
| `owner_unknown` | 生存確認できない記録がある。停止・完了と断定しない |
| `active_attempts[].phase` | COLLECTING、SCREENING、EVALUATING、LOCAL_QWEN、M5_REPORTSなど |
| `collection_progress` | 生存workerに紐づく取得済み件数と母集団。処理済み件数と当日価格の欠損は別 |
| `latest_attempt` | 直近の試行。古い成功とは区別する |
| `latest_result` | 最後に完了した出力。現在進行中の結果と誤認しない |
| `supervision` | 自動再開の試行履歴、上限、ログ、失敗理由 |
| `cooldown.active` | 取得元の休止期間内か |

残り時間は、取得・起動・推論の遅延が変動するため推測値を出さない。全体の終了上限は監督receiptの `deadline_at` で確認できる。

`operate` を直接呼ぶ場合は1試行だけ。自動再開を使うにはHermesジョブまたは `integrations/hermes/alpha_loop_daily.py` を呼ぶ。新環境では[12の導入手順](12_hermes_operation.md)でuv環境を用意し、既存ジョブを確認してからラッパーを配置する。別PCでHermesの外側上限7200秒も設定する。

## 保存場所

10/2追加の指定日確認コマンド。昨日のlatest_serviceを今日の完了と誤認せず、現在の所有者・段階・取得件数、完了後の価格/評価/材料/週次成果物hashと候補行数を照合する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/check_daily_batch.py --session 2026-10-02 --save-proof
```

日付は確認したい営業日に置換する。`data/operations/daily_check_<日付>.json`に確認結果を保存する。実行中・所有者不明・対象日成果物なしはverified_complete=false。欠損がある完了はSUCCEEDED_WITH_GAPSで、取得品質の全件合格という意味ではない。

| 記録 | パス |
|---|---|
| 自動再開・各試行のstdout/stderr | `data/operations/supervision/<id>/receipt.json`、`<n>.stdout.log`、`<n>.stderr.log` |
| worker試行・heartbeat | `data/operations/service_attempts/`、`heartbeats/` |
| 封印済み価格判定の再開参照 | `data/operations/service_checkpoints/` |
| GPUの元状態・復元結果 | `data/operations/gpu_leases/` |
| 起動前の失敗 | `data/operations/startup_failures/` |
| 最新完了・数値CSV・原本 | 従来の `latest_service.json`、`outputs/`、`data/raw/` |

旧ラッパーは `data/operations/hermes_install_backup/alpha_loop_daily_before_m4_stability.py` に退避。新ラッパーの配備確認は `data/operations/m4_deployment.json`。

## 合成での受入確認

追加パッケージ・API・GPUを使わず、実際の子プロセスを中断して再開し、候補CSV・翌日結果CSV・replayを確認する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_m4.py
uv run --offline --managed-python --no-project --python 3.11 python -m unittest discover -s tests -v
```

全112試験成功（70.618秒）。M4追加22件は、時間切れ・異常終了の再開、回数/時間上限、429休止、二重起動、PID再利用、死亡所有者/不明所有者、heartbeat/進捗、入力・設定・ソース改変拒否、価格判定後の中断/再利用、起動失敗、GPU復元範囲、数値保存済みでも復元失敗を成功扱いしないことを検証。Windowsでは親が先に終了した場合の残存パイプ、監督プロセスの異常終了、子孫プロセス終了も実際のプロセスで確認した。GPU復元は模擬Docker応答の試験。

`scripts/verify_m4.py` の合成workerはAI待機を模擬して時間切れとなり、次の試行で同じ封印済みrunを使って完了。翌営業日の評価と同じ入力の再実行も成功。配備済みWindowsラッパーも隔離した合成入力で成功。証跡は `m4_acceptance.json`、`m4_wrapper_stdout.json`、`m4_tests_stderr.log`。この結果を実市場の品質やモデル精度として扱わない。

## 残る実機・継続確認

- M4安定化版の実定時・自動再開・全3700銘柄/Qwen/M5完了は10月1日に確認済み。材料・週次・材料M5追加版も10/2 21:06:44に完了し、候補39・欠損75・週次新案1・side_failuresなし。材料未提供/未登録の正常分岐を確認し、実材料品質と長期完了率は継続観測が必要。
- 実際のログアウト、PC電源断、スリープ復帰。ログイン起動項目と生存Gatewayは確認したが、検証のためにユーザーのPCを停止していない。Gateway停止・電源断をまたぐ処理の即時再開は保証せず、次のジョブ呼出しで死亡記録を整合し、保存済みデータを利用する。現在のHermesは定時発火時に次回予定を進めるため、処理中のPC終了後に同じ回が必ず再発火するとは扱わない。
- Docker/WSL自体の停止、実GPU OOM後の復旧。プロジェクトの処理異常と復元失敗は記録するが、Docker Desktopの再起動やPC復旧は自動化していない。
- 長期取得率、月末一覧によるIPO/上場廃止の欠落、実分割・訂正、処理時間/ピークVRAM。継続観測が必要で、今回の合成試験では合格扱いにしない。

PCは20時にログイン済み・起動中でスリープしていないこと。Qwen日報にはDocker Desktopを起動しておく。専用コンテナは停止状態でもラッパーが起動する。2026-09-30配備時はGateway稼働・次回10月1日20時を確認し、Docker APIは接続不能だったためQwenの実機再試験はしていない。
