# M4 ローカル日次運用

2026-10-01追記: 自動再開・状態表示・中断/終了記録の安定化を配備。[14_m4_stability.md](14_m4_stability.md)を参照。以下のpaused・Gateway停止・2回目stdout空は当時の記録で、現在の運用とは異なる。

2026-09-30追記: 現在はyfinanceの個人研究用取得とHermesの平日20時運用を接続済み。最新手順・制限は[12_hermes_operation.md](12_hermes_operation.md)。以下はファイル入力段階での記録。

更新: 2026-09-29。契約済みの実データはないため、ファイル入力と合成データで運用経路を検証する。合成結果を実市場の成績と扱わない。

## 実装済み

- `daily` は M0〜M2 の封印済み価格判定を実行し、任意で過去 run の翌日評価を続ける。毎回 `data/operations/attempts/` に成功・失敗を記録する。
- 入力ファイル・設定・コード版・手動資料のハッシュで同じ日次ジョブを識別する。再実行は保存済み manifest と成果物を検証して再利用し、`receipts/` に1件の受領記録を持つ。変更された入力は別ジョブとする。
- `doctor --save` はOS、RAM、空き容量、GPU、uv・Docker・Hermesの実行ファイルの所在をプロジェクト内JSONへ保存する。認証情報は読まない。
- [Hermesラッパー](../integrations/hermes/alpha_loop_daily.py) はuv管理のPythonだけを呼び、同じ入力で2回目のstdoutを空にする。外部送信、発注、LLM呼出しはない。
- `qwen-report` はローカルQwenの応答を価格CSVとは別の `outputs/<run_id>/qwen/` に保存する。元の数値・分類・CSVを変更しない。モデル・重みrevision・推論設定をキャッシュIDに含める。
- `model-status` と `model-switch --to openjev|qwen` は専用コンテナ2件だけを検査・切替し、API準備を確認する。切替履歴は `data/operations/model_switches/` に残す。同時起動が既に起きていれば停止せず失敗する。

## ファイル入力での実行

`incoming/current/` に `calendar.json`、`instruments.json`、`bars.json`、`source.json` を置く。必要なら `disclosures.json`、翌日評価には `future.json` も置く。形式は[データ契約](03_data_contract.md)と合成fixtureを参照する。実データの `source.json` には実際の取得時刻・利用可能時刻と適切な `data_grade` を記録する。カレンダーには次営業日を含める。供給元の利用・保存条件を確認せずに観測済みと宣言しない。

PowerShellで手動実行する例。`--session` と `--as-of` は実際のファイルに合わせる。翌日評価は対象日の後に、`--evaluate-run-id` と `--through` を両方指定する。

```powershell
$env:PYTHONPATH = "src"
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli doctor --input incoming/current --save
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli daily --input incoming/current --session 2026-09-29 --as-of 2026-09-29T20:00:00+09:00
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli daily --input incoming/current --session 2026-09-30 --as-of 2026-09-30T20:00:00+09:00 --evaluate-run-id '<前日のrun_id>' --through 2026-09-30
```

合成入力の疎通試験は `uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli fixture` の後、`daily --input demo-input --session 2026-09-17 --as-of 2026-09-17T20:00:00+09:00`。合成・期限後は `prediction_eligible=false`。実機でも `run-b98a0fdb548252603de2` に候補CSVと翌日結果CSV（評価ID `bfe142df5369bcf3714a`）を生成した。合成の数値は市場性能の証拠ではない。

## Hermes

公式の[no-agentジョブ仕様](https://hermes-agent.nousresearch.com/docs/guides/cron-script-only/)に従い、ラッパーを `%LOCALAPPDATA%\hermes\scripts\alpha_loop_daily.py` に配置した。ジョブ `8a4841ec482c`（`Alpha Loop JP daily`）を平日20:00・`--deliver local`・`--no-agent`・**paused** で登録した。合成入力を指定した手動 `hermes cron run` は成功した。現在gatewayは停止しており、自動実行は有効化していない。実入力と検証が揃ってから `hermes cron resume 8a4841ec482c`、必要に応じて `hermes gateway install` を実行する。

## Qwen

ユーザー指定の `Qwen/Qwen3.8-27B` の量子化派生である `Inferact/Qwen3.8-27B-NVFP4` を、[vLLMの単一RTX 5090 recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)に基づく試験候補とした。重みrevisionと推論設定は [`qwen_runtime_rtx5090_v1.json`](../configs/qwen_runtime_rtx5090_v1.json) に固定。重みは `alpha-loop-qwen-hf-cache` volumeへ取得した。固定版OpenJev派生イメージ内のvLLMをQwenサーバーにも使い、ホストのPythonはuv管理を維持する。

OpenJev停止後、次でQwen専用コンテナを作成した。以後の切替は `model-switch` CLIを使い、両コンテナを同時起動しない。

```powershell
docker run -d --name alpha-loop-qwen --gpus all --ipc host -p 127.0.0.1:8000:8000 -v alpha-loop-qwen-hf-cache:/root/.cache/huggingface -e HF_HUB_OFFLINE=1 -e VLLM_NO_USAGE_STATS=1 -e MAX_JOBS=2 --entrypoint vllm alpha-loop-openjev:rtx5090-profile-fix serve Inferact/Qwen3.8-27B-NVFP4 --revision 6128240ebaf4eaa7bad2b3d1c72c37d677c5f462 --served-model-name Inferact/Qwen3.8-27B-NVFP4 --tensor-parallel-size 1 --max-model-len 32768 --kv-cache-dtype fp8 --enforce-eager --language-model-only --max-num-seqs 1 --reasoning-parser qwen3 --host 0.0.0.0 --port 8000
```

初回起動はFP4のFlashInferカーネルを2並列でコンパイルした。ログ上の重み読込みは23.3GiB、KV cacheは122,576トークン。`/v1/models`で固定IDを照合し、合成runから日本語日報を生成した。応答後に観測したGPU使用量は31,926MiB/32,607MiBで、これは測定期間のピーク値を保証するものではない。`outputs/run-b98a0fdb548252603de2/qwen/` の保存済み応答から、Qwen停止中にも同じキャッシュIDで再実行できた。候補CSVのハッシュは不変。報告文はAI下書きであり、人手確認なしに配信しない。

```powershell
$env:PYTHONPATH = "src"
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli qwen-report --run-id run-b98a0fdb548252603de2 --runtime-config configs/qwen_runtime_rtx5090_v1.json --model-id Inferact/Qwen3.8-27B-NVFP4 --model-revision 6128240ebaf4eaa7bad2b3d1c72c37d677c5f462
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli model-status
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli model-switch --to openjev
```

OpenJevとQwenはGPUを排他的に使う。Hermes本体の対話モデル接続は、現在の32K contextで要件の64Kを満たしていないため行わない。FP8 KVのスケール係数が未較正というvLLM警告があり、実用文面の品質評価は保留する。

## 残るゲート

ランキング起点の振り返り（U09）は2026-09-30にファイル入力で実装。[10_ranking_retrospective.md](10_ranking_retrospective.md)に実行・時点・保存・検証仕様を記録。Yahoo!の継続的な自動取得・保存の許諾は未確認。株探は公式案内で機械的取得を禁止している。サイト自動取得と全母集団の実履歴接続は未実施。ランキングだけでは前日の60取引日履歴や全母集団が得られず、予測性能を検証できない。

実データ供給元と保存許諾、当日の完全性、手動入力の運用、実開示の人手正解標本、長時間運用、処理時間とピークVRAM、Qwen文面の人手確認が未了。OpenJev→停止→Qwen→停止→OpenJevを手動と切替CLIで各1回確認し、切替後の実応答も得た。キャッシュ済みコンテナの2回目の切替記録はQwen準備が約38秒、OpenJev復帰が約59秒。自動発注と外部AI送信は無効のまま。日次ジョブの自動有効化は、入力経路と締切を確認してから行う。
