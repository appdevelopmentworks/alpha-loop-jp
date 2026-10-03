# M5 比較・採否報告

2026-10-01追記: 金曜の日次終了後の週次仮説案、最終期間からの提案抑制、登録済み条件の重複除外、比較後の明示的人手採否記録、非ACTIVE版の準備、3/5日補助結果を追加。[操作と残条件](15_materials_and_weekly.md)。材料B1/B2は保存応答によるプレビューまでで統計採否は未実装。本番価格条件と登録済み実験の数値エンジンは不変。全138試験と合成HOLD記録・配備確認が成功。以下の90件の記述は初回比較エンジン実装時の記録。

実保存データでの週次previewも確認した。9/30・10/1の2日分を参照し、条件2組はすでに登録済みなので正常なNO_NEW_HYPOTHESISとなった。追加の株価取得・モデル呼出し・実験登録は行っていない。保存先: `outputs/weekly_research/previews/2026-W40/2026-10-01/`。

更新: 2026-09-30。実装範囲は仮説の事前登録、固定した条件の対応比較、採否用報告、Hermes日次処理からの更新。本番条件の自動変更・採用は実装していない。

## 現在のPCでの運用

9月30日の保存済みQwen仮説2案を研究実験として登録した。設定はユーザー確定値ではなく設計初期値。元の価格基準1.1.0、AIの日報・原本は変更していない。

| 項目 | 登録値 |
|---|---|
| 発見期間 | 2026-09-29〜09-30 |
| 調整期間 | 2026-10-01〜10-15（10営業日） |
| 最終評価期間 | 2026-10-16〜11-30（30営業日） |
| 最終評価可能時刻 | 2026-12-01 20:00 JST以降、全日分の翌日評価が揃った時点 |
| 実験 | `exp-3d24fd6ce03039954a50`、`exp-2f7cae1492753993ea90` |
| 現在の状態 | WAITING / HOLD。最終期間の途中成績は非公開 |
| 入力 | yfinanceのreconstructed。研究用途で、本番採用の証拠にはしない |

登録記録は`data/operations/m5_research_registration.json`、凍結仕様は`data/research/experiments/exp-*.json`。元のAI案・取引所カレンダーと生成元の版は`data/research/plans/study-ac61ff5603997d8cd124/`。

既存のHermes平日20時ジョブが、候補・翌日結果保存後にM5の登録済み実験を更新する。新しい定期ジョブは作っていない。M5の障害は候補CSVを保持しつつ、`SUCCEEDED_WITH_M5_FAILURE`とHermesの失敗記録に残す。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-refresh
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli hypothesis-list
```

追加のユーザー操作は不要。通常の日次運用を続ければ対象期間の保存データが蓄積する。期間内に未実行・未取得の日があればWAITINGを継続し、期間やラベルを勝手に補わない。翌日評価・日報は最終期間の終了を待たず通常どおり毎日保存する。

## 新しい環境での合成デモ

Pythonはuv管理。合成デモには追加パッケージ、APIキー、GPU、Docker、Hermesは不要。プロジェクト直下のPowerShellで環境変数を上記のとおり設定し、次を実行する。

```powershell
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-fixture --out demo-m5
$registered = uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root demo-m5 m5-register --plan plan.json | ConvertFrom-Json
$experimentId = $registered.experiment_id
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root demo-m5 m5-compare --experiment-id $experimentId --batch batch.json
uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli --root demo-m5 m5-refresh
```

同じ入力の再実行は同じ比較IDに収束し、保存物をhash検証する。コード変更後は旧版のソースbundleを使用するか、新しいデモ出力先を指定する。新しい仮説版に結果を見た最終期間を再利用できない。

より短いオフライン確認は`uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_m5.py`。site-packagesを無効にしてレポートを生成し、`data/operations/m5_acceptance.json`へ記録する。

## 仮説を登録する

`m5-register --plan <JSON>`に、仮説ID・版・実験群ID、mode、基準設定、明示取引所カレンダー、発見/調整/最終期間、条件ID・理由コード、全採否基準を指定する。modeはsynthetic / research / prospective。prospectiveは実登録日より後の最終期間だけ受け付ける。未指定基準、重複/逆転期間、休日境界、未知条件、非有限数値は拒否する。

`m5-plan --qwen-report <保存JSON> --calendar <JSON> --out <フォルダー>`はQwenの有限条件案を明示設定のDRAFTへ変換する。自動登録はしない。既存の実機案からの準備用スクリプトは`python scripts/prepare_m5_research.py`。`--register`を付けた時だけ台帳を凍結する。この補助スクリプトはmarket-data追加依存のexchange_calendarsを使い、株価・モデルを再取得しない。

| 採否項目 | 設計初期値と意味 |
|---|---|
| 区分・候補上限 | EARLY / PREMOVE、各20。WATCH・材料経路の選別は比較対象外 |
| 最低標本 | 区分ごと対応日30、A/B各有効候補200、異なる銘柄50、保守的イベント/銘柄群50 |
| 最小改善 | 日平均的中率差 B−A が0.03（3ポイント）以上、補正区間の下限が0を超える |
| 欠損上限 | 選抜正解不明率・全母集団の正解不明率とも20%以下 |
| 捕捉率・件数 | 捕捉率の低下5ポイント以内、B候補数/A候補数が50%以上 |
| 不確実性 | 日×イベント群の積重みで対応bootstrap、固定seed、2,000回、95%水準。仮説の事前上限10と区分数で区間の両側確率を補正 |
| 費用感度 | 往復0/10/30/50bps。実費の確定値ではない |
| 境界purging | 保守的に5営業日。暦日加算やランダム分割は使わない |

件数や閾値は最適値・ユーザー確定値ではない。合成デモは小標本用に基準を下げた別設定で、本番条件へ転用しない。

## 比較方法と保護

- Aは保存済み価格基準の区分別上位K。Bは同じA候補へ凍結AND条件を適用し、落とした候補を補充しない。K、元の数値コード、価格基準、正解・費用規則を揃える。これは価格条件のフィルター比較で、OpenJevの材料B1/B2の実証ではない。
- 日次全母集団を保持し、候補外の急騰とunknownも保存する。基準run・snapshot・原本・翌日結果・元の評価入力をhash検証し、元の入力がある時は同じPython評価器でラベルを再計算する。評価原本がない場合は採用候補にしない。
- 毎日最初に完成した基準runと、最初の完了した翌1営業日評価を使う。同日に都合のよい別run/訂正評価を選んだ手動batchは拒否する。
- 発見→調整、調整→最終境界をまたぐ5営業日の標本と、区間をまたぐ同一材料イベント群を除外する。除外は両群共通。監査CSVに全行・理由を残す。
- 同じ銘柄の連日標本と、複数銘柄に共通する材料群を保守的クラスタにまとめる。材料IDがない場合の銘柄単位proxyは独立性の保証ではない。期間別の材料履歴が不足すれば保留。
- 最終期間の翌日結果が全部揃うまで集計を公開しない。最終評価は1回だけ。既評価期間への新規仮説登録や、評価後の別入力への差し替えは拒否する。元の入力による再計算は認める。
- 最終期間のdata_gradeを混ぜない。synthetic / reconstructed、遅延完成、件数不足、品質未達はHOLD。統計上の候補と運用上の採否を別に保存する。

## 保存物と判定

`outputs/research/<experiment_id>/<comparison_id>/`へ以下を保存する。

| ファイル | 内容 |
|---|---|
| `report.md`、`summary.json` | 採用候補 / HOLD / REJECT、理由と人手確認状態 |
| `metrics.json` | 区分別・日別・全体の的中率、捕捉率、濃縮率、誤検出、未知の全失敗/全成功範囲、対応区間、市場・流動性別集計 |
| `samples.csv`、`samples.json` | 共通母集団、A/B選抜、元run、条件・選外・purging理由、正解、約定状態 |
| `daily_metrics.csv` | 区分別の選抜/有効/的中、空日とnullの率 |
| `cost_sensitivity.csv` | 仮想売買proxyの費用感度。未約定・停止・約定不明は収益へ入れない |
| `registration.json`、`inputs.json`、`manifest.json` | 凍結設定、原本参照・hash、時点、分割・除外表、完了hash |

`ADOPTION_CANDIDATE`は固定基準を満たした人手確認対象であり、採用済みではない。`HOLD`は品質/件数不足・区間不確実、`REJECT`は標本が十分な時の改善不足・捕捉率/件数悪化。研究データに品質保留があれば、統計判定が候補/棄却でも最終recommendationはHOLD。本番設定を書き換えるコマンドはない。

## 検証結果と残項目

全90テスト成功（60.180秒）。M5追加18件＋日次接続障害1件。独立した合成実行はA選抜100・的中50、B選抜50・的中50を確認し、費用・未知約定を分離した。統計上の候補でもsyntheticの最終判定はHOLD。同一入力の再実行一致、原本/台帳/報告改変、境界purging、同一イベント、途中公開禁止、試行上限、既評価期間再利用拒否、途中書込み/台帳commit直前中断からの復帰を確認。

実データの未使用期間比較は未実施（開始前で期間が終了していない）。実開示による分類精度、当時母集団の完全性、真の独立イベント、人手採用証跡は未検証（データ/正解標本が不足）。市場局面は当時の指標入力がないためunknownとして集計。3・5営業日の補助成績、材料モデルB1/B2比較、週次の新仮説スケジュール、本番自動採用は今回の比較実装に含めていない。主評価は翌1営業日。区間推定は小標本の確定的証明ではない。

試行上限・区間補正は登録済みの同じfamily内に適用する。別familyを多数試す場合の全体多重性は未検証であり、個々の報告から全探索の改善を断定しない。現在の2案は同じfamily。本番採用にはこの点も人が確認する。

Windows側ラッパーとのhash一致、隔離合成入力で候補とM5を同時保存、実データlatest_serviceと価格基準/数値コードの不変を`data/operations/m5_deployment.json`へ記録した。修正したラッパーの次回定時実行そのものは未確認（次回10月1日20時）。
