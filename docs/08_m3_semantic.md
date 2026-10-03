# M3 材料 shadow 実装・実機記録

2026-10-01追記: 現行のファイル開示・日次接続・項目別根拠・正解比較は[15_materials_and_weekly.md](15_materials_and_weekly.md)。全138試験、追加パッケージ/API/GPUなしの一括合成実行、配備済みHermesラッパーの合成実行を確認。実OpenJevも合成本文へ分類・根拠IDを応答し、78.08秒で起動〜復元まで完了。一部根拠不足をmissing_evidenceとして保持した。接続合格であり実開示品質の合格ではない。証跡: remaining_acceptance.json、remaining_gpu_acceptance.json、remaining_deployment.json（data/operations）。以下は以前の手動経路の記録。

更新: 2026-09-29。OpenJev のローカル実応答まで確認。日本語開示の分類精度と投資成績は未検証。

## 実装

- UTF-8資料を手動取込みし、原本SHA-256、公表・初見・取得時刻、段落ID、抽出状態を保存。文字化け・空本文は `needs_review`。
- 版付き日本語質問6件を `configs/questions_v1.json` に固定。Pythonで `noul`、`choice`、`score` の形式と数値を検証。
- `razorback16/openjev` のローカル adapter が `/v1/models` でモデルIDを照合し、`/v1/systemone` に送信。外部URL、`latest`、固定版情報欠落を拒否。
- 原本・抽出器・質問版・モデル・量子化・推論設定をキャッシュIDに含める。通信失敗は恒久キャッシュせず、12,000文字超の本文は切り捨てず保留。
- 価格候補CSVを変更しない shadow 経路。根拠段落欠落は `missing_evidence` とし、材料不存在と読取不能を区別。

`PYTHONPATH=src` で `uv run --managed-python --no-project --python 3.11 --offline python -m unittest discover -s tests -q` を実行し、M0〜M3の17件が成功した。

合成資料の取込み例（PowerShell、先にREADMEの合成runを実行して `$runId` を設定）:

```powershell
$env:PYTHONPATH = "src"
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli semantic-import --file tests/fixtures/manual_notice.txt --instrument-id TSE:0003 --disclosure-id event-watch --revision-id 1 --published-at 2026-09-17T16:00:00+09:00 --synthetic-first-seen-at 2026-09-17T16:02:00+09:00
$documentId = "直前のJSONのdocument_id"
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli semantic-shadow --run-id $runId --document-id $documentId --provider mock --mock-response tests/fixtures/mock_semantic_response.json --evidence-id p001
```

`--synthetic-first-seen-at` を使った資料は合成扱いで、実際の観測証拠にしない。通常の手動取込みでは現在時刻を初回観測時刻とし、過去のrunに混入させない。

## RTX 5090での起動

初回の `razorback16/openjev:0.5.0` 起動では、重み読込み後にDocker APIが500/タイムアウトになった。診断トレースはvLLMのKV cache計測中にFlashInfer fused MoEのJITが多数の `nvcc` を起動していたことを示した。Docker/WSLのメモリ使用量も上限付近だった。`MAX_JOBS=2` と `--moe-backend marlin` を設定した派生イメージで起動・healthが成功した。二つの設定を同時に変えたため寄与は個別には確定していない。経過ログは `data/diagnostics/` に保存。

固定ベースイメージdigestは `sha256:53741f5005c81e4d19a8a8d66e717213c173bb11a46aac1147eea1dc9244b854`、重みrevisionは `ec4ff3df205028f4e81c954c2227f9312b3ec2ea`。派生イメージの1行パッチは[RTX 5090のvLLM報告](https://github.com/vllm-project/vllm/issues/42987)に基づくが、パッチ単独では停止を解決しなかった。[FlashInferの並列ビルド問題](https://github.com/flashinfer-ai/flashinfer/issues/3634)を参考に `MAX_JOBS` を制限した。実行設定は [`openjev_runtime_rtx5090_v1.json`](../configs/openjev_runtime_rtx5090_v1.json) に記録し、キャッシュIDにも含める。

初回作成時のみ、Docker Desktop起動後にプロジェクト直下から次を実行。モデル重みは専用volumeに約18GB保存される。

```powershell
docker build -t alpha-loop-openjev:rtx5090-profile-fix docker/openjev-rtx5090
docker volume create alpha-loop-openjev-hf-cache
docker volume create alpha-loop-openjev-flashinfer-cache
docker run -d --name alpha-loop-openjev --gpus all --ipc host -p 127.0.0.1:8080:8080 -v alpha-loop-openjev-hf-cache:/root/.cache/huggingface -v alpha-loop-openjev-flashinfer-cache:/root/.cache/flashinfer -e MAX_JOBS=2 -e OPENJEV_GPU_UTIL=0.72 -e OPENJEV_MAX_MODEL_LEN=8192 -e OPENJEV_MAX_NUM_SEQS=1 -e OPENJEV_MAX_INFLIGHT=1 -e OPENJEV_MAX_IMAGES=0 -e OPENJEV_WARMUP=0 -e 'OPENJEV_VLLM_ARGS=--enforce-eager --moe-backend marlin' alpha-loop-openjev:rtx5090-profile-fix
docker inspect alpha-loop-openjev --format '{{.State.Status}} {{.State.Health.Status}}'
```

作成済みコンテナは `docker start alpha-loop-openjev` で再開、`docker stop alpha-loop-openjev` でVRAMを解放。2026-09-29の実機では `running healthy`、`/v1/models` は `openjev-0.1` を返し、稼働時のGPU使用量は約26.5GBだった。

合成日本語資料への6問で `semantic_status=ok`。`noul`、`choice`、`score` の実応答を検証した。再実行は同じキャッシュIDへ収束し、基準候補CSVのSHA-256 `AE4E19979804DF3C82233287B335296920E4BA4F0917F1DC610F9CC6D7F0E954` は不変。応答は `data/semantic_cache/`、判定は `outputs/<run_id>/semantic/` に保存。

実応答の再現例（保存済み合成runと資料を使用）:

```powershell
$env:PYTHONPATH = "src"
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli semantic-shadow --run-id run-f310b7685b3b67bc53ec --document-id 920993d346572f9d2190 --provider openjev --model-id openjev-0.1 --model-revision ec4ff3df205028f4e81c954c2227f9312b3ec2ea --server-commit 'sha256:53741f5005c81e4d19a8a8d66e717213c173bb11a46aac1147eea1dc9244b854+topk50' --quantization-id NVFP4-marlin --runtime-config configs/openjev_runtime_rtx5090_v1.json --timeout-seconds 300 --evidence-id p001 --evidence-id p002
```

## 残る受入条件

合成資料への応答は接続と形式の証拠であり、実開示での精度や株価予測の証拠ではない。[記入用CSV](gold_labels_template.csv)へ、保存許可のある日本語資料の人手正解と根拠段落を記録し、肯定・否定・曖昧、訂正、重複、資料欠損、文書内命令を含む標本で検証する。実サーバーの異常応答、タイムアウト、長文、ピークVRAM、反復起動も未検証。材料判定を候補順位へ反映するのは、これらの試験と評価基準を定めた後。Hermes定期実行とQwen接続はM4。
