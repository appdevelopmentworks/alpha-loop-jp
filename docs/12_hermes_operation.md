# Hermesでの実データ研究運用

2026-10-03整理: 新規環境向けの[導入マニュアル](18_setup.md)と[運用マニュアル](19_operations.md)を現行の入口とする。本書のジョブ/run IDと実行経過は元PCの記録で、新しいcloneには対応する台帳・成果物・ジョブは含まれない。10/2の追加版の定時完了は04§18に記録。

2026-10-01追記: [材料・週次研究手順](15_materials_and_weekly.md)を追加。既存20時ジョブへ開示ファイル取込み・OpenJev shadow・3/5日補助結果と金曜の週次案を接続し、配備済みWindowsラッパーの隔離合成実行を確認。資料未提供時は不明を保存してOpenJevを起動しない。新機能の定時実行は次回10/2、実資料品質は検証待ち。既存の価格基準と2比較実験は継続する。

更新: 2026-09-30。Hermesの既存no-agentジョブをuv管理のPythonへ接続した。取得、候補・選外、以前のrunの翌日評価、急騰前日比較、ローカルQwenの構造化出力まで実行する。数値、日報本文、仮説の説明はPythonが確定する。Hermes本体の対話モデルは変更していない。

2026-10-01追記: M4の運用安定化を配備。時間切れ・異常終了の有限自動再開、封印済みrun再利用、所有者/heartbeat、二重起動防止、Windows子孫プロセス終了、GPU元状態の復元記録を追加。[14_m4_stability.md](14_m4_stability.md)を現在の障害対応・状態確認手順とする。

## このPCでの操作

既存ジョブ`8a4841ec482c`（Alpha Loop JP daily）は平日20:00 JSTに実行する。出力・失敗記録はHermes内のlocal保存。Windowsタスクの登録は管理者権限なしでは失敗したため、Hermes公式のStartupフォルダー代替登録でgatewayをログイン時に起動する。管理者操作は不要。

20時にPCが起動・ログイン済みでスリープしていないこと、Qwen日報にはDocker Desktopが起動していることが必要。APIキーや追加CSVサンプルは不要。

```powershell
hermes cron run 8a4841ec482c
hermes cron status
hermes gateway status
```

同じ入力の再実行は成果物hashを確認して再利用する。新版ラッパーは2回目も完了/失敗/重複の監督receiptをstdoutへ返す。AI失敗後は`operate --retry-ai`で同じ数値CSVを保ちながらAIだけを再試行できる。

## 新しい環境・手動実行

プロジェクト直下のPowerShell:

```powershell
uv sync --managed-python --python 3.11 --extra market-data
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operate
```

`--no-ai`ならDocker・GPUなしで取得と数値処理ができる。`--limit 3 --no-ai`は部分銘柄の疎通試験。`--refresh`は原本更新が必要な場合だけ使う。

合成確認は`fixture --without-turnover`に続いて`operate --operation-config configs/hermes_demo.json`。候補・前日分析が生成される。翌日結果CSVまでの最短手順はルートREADMEの`evaluate`を使う。追加パッケージなし・APIキーなし・GPUなしで合成処理を実行できる。

別PCでHermesへ接続する場合は`integrations/hermes/alpha_loop_daily.py`をHermesのscripts、`integrations/hermes/alpha-loop-jp/SKILL.md`をskills/alpha-loop-jpへ配置し、`ALPHA_LOOP_ROOT`に実際のプロジェクトを設定する。既存ジョブを確認してから登録する。このPCの旧ラッパーは`data/operations/hermes_install_backup/`へ退避済み。ラッパーはHermesのVIRTUAL_ENVを引き継がず、プロジェクトのuv環境を明示する。

## 入力仕様と設計初期値

- `configs/hermes_operations.json`: JPX普通株をスタンダード→グロース→プライムの順で取得。365日履歴、1並列、20時確定、終値比10%以上/上位50件は設計初期値。ユーザーの「uv管理」「取得できる情報で推論」「Hermes運用」と区別する。
- [JPX公式一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)の今回の基準日は2026-08-31。優先株・社債型種類株の5桁コード7件は理由付きで除外。4桁英数字コードは保持。月末以降のIPO、上場廃止・市場変更、当時の母集団は完全には保証しない。
- [yfinance](https://github.com/ranaroussi/yfinance)は非公式のYahoo Finance取得ライブラリで、個人研究として先行する。包括的な利用許諾や契約データ品質が確認されたものとして扱わない。Yahoo! JAPAN・株探のランキングページをスクレイピングする実装はない。
- [履歴仕様](https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html)に合わせauto_adjust=False、actions=True、repair=Falseを固定。YahooネイティブOHLCVの分割調整を保持し、二重調整しない。Adj Closeの配当調整を分割係数と誤認しない。売買代金はnull、終値×出来高で代入しない。
- XTKSカレンダーの完了営業日だけを取得。20時前、休日は直前営業日を使う。取得時刻を過去へ戻さない。入力はreconstructed、prediction_eligible=false。実開示がないので材料判定は未評価。
- 急騰一覧は価格から自前生成し、D-1の特徴だけで全母集団と比較する。非掲載を陰性ラベルと扱わない。仮説はDRAFTで自動採用しない。
- 翌日高値による値上がりラベルと実売買収益を区別する。約定はunknown、費用はnullで実収益は算出しない。分割と前日価格の整合が取れない補正はadjustment_unknown。翌1営業日の評価が完了したrunは毎日作り直さない。

## 保存と障害対応

| 内容 | 保存先 |
|---|---|
| JPX原本・返却株価CSV | `data/raw/<sha256>` |
| JPX一覧と除外理由 | `data/market/jpx_universe.json` |
| 銘柄別キャッシュ・再開 | `data/market/histories/<session>/` |
| 入力と原本参照 | `data/market/datasets/market-*/` |
| 進捗・休止 | `data/market/progress/`、`cooldown.json` |
| 試行・重複識別・最新結果 | `data/operations/service_attempts/`、`service_jobs/`、`latest_service.json` |
| 候補・選外・市場別分母 | `outputs/run-*/candidates.csv`、`decisions.csv`、`market_summary.csv` |
| 翌日結果 | `outputs/run-*/evaluation/*/outcomes.csv` |
| 前日比較・仮説 | `outputs/retrospectives/study-*/` |
| Python日報・Qwen確認事項 | `outputs/run-*/qwen/*.md`、`*.json` |

株価原本はライブラリの返却DataFrame CSVであり、Yahoo HTTP応答原文ではない。パラメータ、ライブラリ版、実取得時刻、入力hash、条件版、数値コードhash、ソースbundleを保存する。

429では1時間休止し、アクセス制限を回避しない。保存済みデータは利用可能。部分欠損はDATA_HOLDとSUCCEEDED_WITH_GAPS、全価格取得不能はBLOCKED_DATA。ネイティブ取得は別プロセスで60秒、最大2試行。新版ラッパーは全体7100秒（Hermesの`cron.script_timeout_seconds=7200`）、1試行3000秒・最大3回、終了/復元用180秒を確保し、時間切れ・確認できた異常終了を同じ対象日/入力から再開する。429休止中は再試行しない。このHermes設定はcronスクリプト共通で、現在の有効ジョブはこの1件。別環境でも外側の上限を設定すること。Windowsの一時ファイルロックは短時間再試行する。具体的な状態と失敗理由は14を参照。

Qwen失敗はSUCCEEDED_WITH_AI_FAILUREで数値を保持する。専用モデル2コンテナだけを排他的に切り替え、実行前の状態へ戻す。外部AI・自動発注・外部通知は無効。

M5の登録済み実験は候補・翌日結果保存後に日次更新する。評価期間が揃うまではWAITINGで、途中成績は出さない。M5の障害はSUCCEEDED_WITH_M5_FAILUREとHermesの失敗ログへ保存し、数値CSVを保持する。[M5の期間・操作・採否基準](13_m5_comparison.md)を参照。既存ジョブの予定時刻は変更していない。

## 検証と残る確認

全71件成功（19.411秒）。追加試験は休日/確定時刻、分割/配当、原本とsnapshot改変、429休止、子プロセス終了、一時ロック、部分/前日欠損、翌日不明約定、分割と事後訂正、重複/AI再試行、市場別分母、再構成の予測評価拒否、Qwen自由文/架空コード/確認事項不足の拒否。JSON全体を囲むコードブロックだけは正規化し、前後の自由文は拒否する。

Hermes実機では9月29日分に母集団3700、価格欠損8、候補40（EARLY 20 / PREMOVE 20）、終値比10%以上10件、D-1比較と構造化Qwen日報・仮説を保存。市場別の既知価格ペアStandard 1548、Growth 596、Prime 1548に対し上昇件数3/5/2。単日・月末母集団の記述的結果で、一般的な市場優劣や予測精度の証明ではない。

旧daily-draft v2は区分別件数を誤帰属し不採用。v3はAIが確認事項コードだけを返し、数値・本文はPythonが生成する。

`scripts/verify_hermes_research.py`は保存済みD-1再構成入力から翌日結果CSVを検証する補助スクリプト。事前予測の成績ではない。検証記録は`data/operations/hermes_research_evaluation.json`。

未実施: 定時実行から全工程完了までの連続成功、再ログイン・スリープ復帰、長期取得率と実分割/訂正事例、月次一覧の欠落対策、利用条件の継続確認、実開示正解標本、真正point-in-timeの独立期間比較。理由は運用開始直後で観測期間・当時資料・正解標本がないため。

### 2026-09-30 定時起動・時間上限への対応

20:00予定のジョブは20:00:58に自動dispatchされ、20:00:59に処理開始。3,603/3,700銘柄まで進んだ後、20:59:19にラッパーの旧3500秒上限で終了した。Hermesの失敗ログと取得キャッシュを保存し、残っていたattemptのRUNNING表示を証跡付きTIMED_OUTへ補正。21:06:16に既存ジョブを手動再開した。取得並列数や429対応は変更していない。定時起動は確認済みだが、この回の自動実行は完了成功ではない。実行上限を上記の2時間へ変更し、Windows側のラッパーとのSHA256一致、uvのPythonで構文を確認した。

21:08:29に3,700銘柄の取得処理が完了（当日価格欠損64、SUCCEEDED_WITH_GAPS）。21:11:07に再開した処理の全工程が完了し、21:11:08にHermesの成功出力を保存した。`run-a521c7451dd53d0dede7`に9月30日の候補40件、過去run 2件の翌日評価、急騰16件の前日比較、ローカルQwenの日報と仮説案を保存。研究入力のdata_gradeはreconstructed、prediction_eligible=false。21:28の実機確認ではAlpha Loopの処理プロセスはなく、Qwen・OpenJevは両方停止していた。定時起動後に手動再開して完了した記録であり、変更後の2時間上限での次回定時実行は未検証。

- 候補CSV: `outputs/run-a521c7451dd53d0dede7/candidates.csv`
- 9月29日候補の翌日結果: `outputs/run-8196002d62f81fc2f425/evaluation/66c5886eabf5b2247302/outcomes.csv`
- 日報: `outputs/run-a521c7451dd53d0dede7/qwen/bf9691191165a542eb4c358017870482ebe01f58da090e20acf01d2806d99f9b.md`
- 前日比較: `outputs/retrospectives/study-ac61ff5603997d8cd124/ranked_prior.csv`


## 9月29日時点の完了記録

最終のHermes実行は`run-8196002d62f81fc2f425`、前日比較は`study-57b7818ff32b5278e394`。ローカルQwenの日報と構造化仮説2案を保存し、同じ入力の反復は再利用（first_recorded=false）を確認。終了時は両専用GPUコンテナを停止状態へ戻した。検証記録は`data/operations/hermes_acceptance.json`。

- 候補CSV（ローカル証跡）: `outputs/run-8196002d62f81fc2f425/candidates.csv`
- 研究用の9/28→9/29結果CSV（ローカル証跡）: `outputs/run-5d8f0af2404090d3445f/evaluation/50c336568918cf69e5bc/outcomes.csv`
- Python日報（ローカル証跡）: `outputs/run-8196002d62f81fc2f425/qwen/979424d65343a7e039737ee0fdcf3d5a6efee81100578fe8064689a9e1033ed3.md`

合成検証は`run-e5d64397899ea9318206`、候補3件、翌日結果6件、replay成功。追加依存を無効化した実行記録は`data/operations/hermes_synthetic_acceptance.json`。対話型HermesのローカルQwen接続は32K/64Kゲートが残るため未接続。今回の定時処理はno-agent方式で、外部AIを呼ばない。
