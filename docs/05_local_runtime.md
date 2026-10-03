# ローカル運用・GPU設計

版: 0.3 / 更新日: 2026-09-29 / OpenJevとQwenの単独応答、Hermes no-agentの手動実行を確認。定時自動実行と実データ評価は未実施

## 1. 確定した構成と未確認事項

ユーザー指定はRTX 5090・VRAM 32GB、[Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)、[razorback16/openjev](https://github.com/razorback16/openjev)。OpenJevはTypeSafeと独立したサーバーであり、openjev.comのSemIfや別の同名モデルへ置き換えない。

WindowsのプロジェクトパスとDocker Desktop上のRTX 5090を確認し、OpenJevとQwenの単独起動・応答を試験した。固定設定と残る品質試験は[08_m3_semantic.md](08_m3_semantic.md)および[09_m4_local_operations.md](09_m4_local_operations.md)を参照。Hermesのno-agentジョブは手動実行済みで、自動実行は停止状態。

## 2. 確認できた提供元の情報

| 情報 | 設計への反映 |
|---|---|
| Qwen公式カードは27Bモデル、標準262,144 tokenの文脈長を記載 | モデルの最大能力と、このGPUで設定可能な文脈長を区別する |
| vLLM公式recipeに1枚のRTX 5090でNVFP4・32,768 context・CUDA graphs無効化の例がある | 初期の実機検証候補。ユーザー機での成功保証ではない |
| OpenJev READMEはDiffusionGemma 26B-A4BのNVFP4に24GB以上のGPUを案内 | 32GBで単独稼働を試す。同時常駐の根拠にはしない |
| Hermes FAQはローカルOpenAI互換endpointと64,000以上のcontext設定を案内 | Qwen単体32K試験の成功をHermes本体への接続成功と同一視しない |
| HermesにはLLMなしのscript-only cronがある | GPUモデルが停止していても定型ジョブを開始できる構成にする |

出典: [Qwen公式カード](https://huggingface.co/Qwen/Qwen3.8-27B)、[vLLM recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)、[OpenJev README](https://github.com/razorback16/openjev)、[Hermes FAQ](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/faq.md)、[script-only cron](https://hermes-agent.nousresearch.com/docs/guides/cron-script-only/)。いずれも更新されるため導入時の版を固定する。

## 3. 初期実行方式

Python本体、Hermes、Qwen推論サーバー、OpenJevサーバーを別環境にする。NVIDIA GPUを使うサーバーはLinux系コンテナ/WSL2を第一検証候補にするが、既存環境を確認してから決定する。HermesのWindowsネイティブ対応は、すべてのGPU依存ライブラリのWindowsネイティブ対応を意味しない。[Hermes Windows資料](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/windows-native.md)

常駐させるのはHermes gatewayとCPU側コントローラーを基本とする。GPU推論サーバーはフェーズ単位で占有させる。

1. Hermesのno-agentジョブがPythonコントローラーを開始。
2. 株価・開示を取得、入力を封印し、価格条件を計算。
3. GPUロック取得後、OpenJevを起動・health確認し、未処理資料をまとめて判定。
4. OpenJev停止と対象プロセス終了・GPU解放を確認し、判定とCSVを保存。
5. 日報が必要ならQwenを起動。保存した指標・根拠から限定した入力で作成。
6. Qwenを停止し、runを完了。次回はキャッシュを再利用。

制御にはLLMを使わない。Qwen自身に「自分を停止してOpenJevを起動し、その後自分に返答せよ」と指示しない。起動中の対話・背景メモリー処理もGPU利用者となるため、切替フェーズ中の新規推論を待機させる。利用者が別用途でGPUを使用中なら、勝手にそのプロセスを停止せず待機または期限付き中止。

ロックには所有者run_id・PID・取得時刻・heartbeatを持たせる。クラッシュ後の回収は所有者の終了確認後に行う。OOM時は同一条件で無制限再試行せず、失敗情報を保存してモデル処理だけを保留する。

## 4. Qwenの導入ゲート

元の27B重みを16bitで読む場合、重みだけの概算は約54GB（27B×2byte）で32GBを超える。4bit単純換算の約13.5GBも、実際のGPU使用量ではない。量子化メタデータ、未量子化部分、KV cache、演算用領域、画面出力等が加わる。

初期候補はQwen3.8-27B由来のNVFP4等の量子化版。vLLM recipeにある`Inferact/Qwen3.8-27B-NVFP4`を試験候補とし、Qwen公式元モデルと配布主体が違うことを記録する。別のNVFP4/GGUFを使う場合も、派生元・revision・hash・精度低下を再確認する。

まず並列1、短い資料からPythonによる直接呼出しを試す。32,768 context、FP8 KV、`enforce-eager`はrecipeに基づく候補であり、確定設定ではない。必要な入力量・出力上限・推論深度を固定して、日本語材料の品質とピークVRAMを測る。起動成功だけでは合格としない。

次にHermesエージェント本体をQwenへ接続する場合、採用するHermes版が求めるcontextと、サーバーが実際に提供するcontextを一致させる。64,000等を設定欄に書くだけでは拡張できない。32Kしか安定しない場合、まずHermesはno-agentの定時実行に使い、Qwen分析はPythonから直接呼ぶ構成で運用可能性を確認する。Hermes本体のQwen対話運用は別ゲートとして未達を明示する。

## 5. OpenJevの導入ゲート

固定するものはリポジトリcommit、コンテナdigest、内部のvLLM/CUDA版、使用モデル・重みrevision、推論パラメーター。READMEの`openjev-latest`等を実験記録の唯一の識別子にしない。

TypeSafe互換の`POST /v1/systemone`へ接続するadapterを作り、`GET /v1/models`で提供モデルを照合する。noul・choice・scoreを個別に試験し、確率の範囲・分布・回答候補・欠落・異常応答を検証する。入力本文と質問の長さ、バッチ、再読取り設定を測定する。表示されるusageだけで実演算量を推定せず、wall_time・再試行・GPUピークも記録する。

まず主モデルの単独稼働を検証する。READMEの小型モデルを使う場合は別のモデル選択実験として扱い、無断の品質代替をしない。日本語開示での性能は本プロジェクトの正解標本で確認する。

## 6. Hermesとの接続

初期の配信先はローカルファイルのみ。メール・チャットへの外部送信は設定が決まった後の機能とする。日次ジョブと週次研究ジョブを分け、どちらも日付・対象・終了状態を明示する。

script-only cronの実行入口はHermesの管理するscriptsディレクトリ内に小さなラッパーを置き、本プロジェクトのPython実体・作業ディレクトリ・設定を明示して起動する。HermesのPython環境へ分析用依存を混在させない。ジョブは親プロセスの認証情報が自動継承されると仮定せず、必要な資格情報だけを専用設定から読む。キーはログに出さない。

定時実行にはgatewayの稼働を確認する。PCがスリープした場合の再開時に、過去ジョブを最新予測として発行しない。期限を過ぎたジョブは履歴補完かskipとして記録する。[cron公式資料](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/)

ローカルendpointは可能なら127.0.0.1に限定する。Windows/WSL/コンテナ間ではlocalhostが同じとは限らないため、通信方向と実アドレスを接続テストする。OpenJevとQwenのポートは設定値にし、競合を診断する。

## 7. 実機受入チェック

| ID | 合格条件 |
|---|---|
| R01 | OS、RAM、空き容量、driver、GPU実体、実行環境と各版を記録できる |
| R02 | 両モデルをそれぞれ単独で起動し、代表的な日本語入力を処理できる |
| R03 | OpenJev→停止→Qwen→停止を反復し、メモリ残留・競合・孤児プロセスを診断できる |
| R04 | 異常終了・OOM・起動待ち期限超過でも、原本と完了済み価格判定が壊れない |
| R05 | サーバーcontextとクライアント設定が一致し、超過を黙って切り捨てない |
| R06 | no-agentの定時処理がモデル非起動から開始でき、完了/失敗をローカルに残す |
| R07 | 推論品質、処理時間、最大VRAM、モデル切替時間が所定の日次処理枠内に収まる |

処理枠・対象件数・並列数は代表データで測定して設定する。全市場件数を仮定した秒数や「32GBなら確実に動く」という保証を置かない。外部Astra等への経路は既定無効で、利用時は別の接続・費用・品質試験を行う。
