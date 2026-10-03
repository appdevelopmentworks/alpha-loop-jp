# 材料分析・週次研究の運用

2026-10-03追記: 日次の価格仮説ループと価格版のACTIVE・監視・復帰は[21](21_hypothesis_loop.md)を参照。ここで説明する材料のshadow品質と週次レビューは別機能で、材料B1/B2の本番昇格は未実装のまま。

更新: 2026-10-01。M3の開示ファイル入力・日次材料shadow・正解比較、M5の週次仮説案・人手採否、3/5営業日の補助価格結果を追加した。価格基準1.1.0と登録済みM5数値エンジンは保持する。実装の成功と実市場の採用条件を分ける。

2026-10-02追記: 材料の統計採否エンジンは[16](16_m5_material_comparison.md)へ追加。本書のB2順位プレビューと、新エンジンのB2候補追加は別定義で保存する。

確認済み: 全138試験（84.373秒）、追加パッケージ/API/GPUなしの一括合成確認、配備済みWindows Hermesラッパーの合成実行、実保存データ2日からの週次preview。OpenJevの合成実応答・元コンテナ状態の復元も78.08秒で確認したが、一部根拠不足はmissing_evidenceであり実開示品質未合格。証跡はdata/operationsのremaining_acceptance.json、remaining_gpu_acceptance.json、remaining_deployment.json、remaining_tests_stderr.log。

10/2追記: 実20時ジョブは21:06:44に終了し、金曜の週次新案1件を保存、登録済み条件2件の重複を抑制した。材料未提供はNO_AVAILABLE_DOCUMENTSで、実分類の成功とはしない。次の既存定時予定は10/5 20:00 JST。証跡はdaily_check_2026-10-02.json。長文追加後の全184テストと手順は17。

## API・GPUなしの合成確認

新環境はuvを用意し、プロジェクト直下のPowerShellで実行する。追加パッケージ・APIキー・GPU・Hermesは不要。Pythonがなければuv管理下で取得される。既にuv管理Python 3.11があれば`uv run`へ`--offline`を追加できる。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --managed-python --no-project --python 3.11 python -S scripts/verify_remaining.py
uv run --offline --managed-python --no-project --python 3.11 python -m unittest discover -s tests -v
```

`data/operations/remaining_acceptance.json`に隔離合成環境と出力先を保存する。候補CSV、翌日結果CSV、材料CSV、正解比較、B1/B2プレビュー、M5比較・HOLD記録、replayまで生成する。合成の人手操作試験は利用者の採用承認ではない。実日次の最新結果を変更しない。

## 許可された開示本文の投入

`incoming/disclosures/manifest.json`とUTF-8本文TXTを同じディレクトリ内に置く。空行で段落を区切る。source_urlは出典記録で、自動取得指示ではない。既存日次分類では画像PDF、空文・文字化け、12,000文字超は切り捨てずneeds_reviewとする。10/2追加の長文分割・抜粋shadow手順は[17](17_long_materials.md)。資料全体の統合分類とOCRは未実装。

```json
{
  "schema_version": 1,
  "data_grade": "observed",
  "documents": [{
    "file": "disclosure.txt",
    "instrument_id": "TSE:1234",
    "issuer_code": "1234",
    "disclosure_id": "issuer-event-001",
    "revision_id": "1",
    "source_url": "https://issuer.example/disclosure/001",
    "published_at": "2026-10-02T16:00:00+09:00",
    "permission_confirmed": true,
    "permission_reference": "実際に確認した保存・解析許諾の根拠"
  }]
}
```

銘柄・URL・許諾は実際に確認したものへ置換する。発行者対応は入力メタデータの明示指定で、モデル推定ではない。複数企業を含む本文は人手で発行者・根拠を確認する。

実資料のfirst_seen_at / available_at / fetched_atは初回取込み実時刻。古いavailable_atを入力してもasserted_available_atへ記録するだけで、取得時刻を戻さない。同じイベント・版・原本は初見時刻を保持して重複除去する。本文が変われば新revision_idを要求し、訂正前も保持する。公表・初見等が価格as_of以前で対象営業日の資料だけを封印する。観測済み材料を再構成価格と組み合わせても、価格をPITへ昇格させない。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli disclosures-import --manifest incoming/disclosures/manifest.json
```

既存の平日20時Hermesジョブでも同じ入力を取り込む。資料未提供はUNAVAILABLE_SOURCE / NO_AVAILABLE_DOCUMENTS、銘柄別unavailableを保存してOpenJevを起動しない。価格・出来高の日次候補は継続する。

## 材料shadow・正解比較

`configs/materials_operations.json`はlocal shadow、最大5資料、起動を含む600秒が設計初期値。GPUロックを推論・復元終了まで保持してOpenJev/Qwenを排他切替し、元の状態をjournalへ保存・復元する。外部AI・発注は無効。

6問の分類後、項目ごとに根拠段落IDを選ぶ。引用はPythonが原文からコピーする。unsupportedや分類unknownは不明へ戻す。段落存在・引用の一致と、判断の意味的な正しさは別で、後者は人手正解との比較が必要。新規性は過去事業資料がないためunknown。金額の大小・利益換算は行わない。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli materials-batch --run-id <run-id>
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli materials-batch --run-id <run-id> --retry-unavailable
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli materials-preview --run-id <run-id> --documents <documents.jsonのパス>
```

同一run・質問・モデルは封印資料と保存済み応答を再利用する。明示再試行は元snapshotを保ち、以前のreceiptを残してretriesへ出力。通信失敗は永続キャッシュしない。原本、抽出器、質問、モデルrevision、量子化、runtime、根拠質問方針をキャッシュIDへ含める。

B1は価格候補Aの材料フィルター（補充なし）、B2は既存のEARLY/PREMOVE価格適合群の材料順位上位K。初期条件は正式契約・希薄化なし、本業/反映時期/金額明記の平均スコア。いずれも未検証プレビューで、価格候補CSVを変更しない。価格条件外の資料もmaterials.csvに残す。モデル確信度・B2スコアは急騰確率ではない。

正解JSONの例は合成受入環境のsynthetic_labels.json。実資料はlabel_origin=human、資料ID・content_hash・annotator・case_tags・各質問の正解とevidence_idsを記入する。二値はtrue / false / unknown、Choiceは質問定義の選択肢。既知ラベルは根拠必須。AI生成ラベルを正解とする経路はない。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli materials-labels-register --labels <labels.json>
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli materials-quality --gold-id <gold-id> --documents <documents.json>
```

正解・質問版・原本hash・初期基準を登録時に凍結し、質問別混同行列、precision/recall、全既知/回答済みaccuracy、二値Brier、根拠一致、不明・不正率を保存する。初期基準は100資料、質問別既知50件、8ケース各5件、accuracy/根拠一致90%以上、不正応答5%以下。最適値や利用者確定値ではない。合成は必ずSYNTHETIC_ONLY、実標本不足はHOLD。達成しても人手の独立性確認前に選別を有効化しない。

## 週次仮説・人手採否・補助評価

毎週金曜、既存日次処理終了後に週次報告を保存する。追加ジョブは作らない。保存済みQwen振り返りから有限条件を検証し、登録済み条件を除いて最大2案を提示する。登録済み最終評価期間は新提案に使用せず、除外日を保存。新しい未使用期間と基準をm5-planで明示し、m5-registerした実験だけを比較する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-weekly --session 2026-10-02
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-weekly --session 2026-10-01 --preview
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-review --experiment-id <exp-id> --decision HOLD --reviewer <名前> --reason <理由> --human-reviewed
```

金曜以外はNOT_DUE。previewは別保存で金曜報告を早く凍結しない。新案なしは正常なNO_NEW_HYPOTHESIS。完成した比較へHOLD / REJECT / APPROVE_SHADOWを記録する。APPROVE_SHADOWはprospectiveのADOPTION_CANDIDATEだけ。研究・合成・HOLDは承認不可。承認後もlive_enabled=falseの別版を用意するだけで、本番条件を変更しない。最初のACTIVEは02の設計どおり利用者確認後の別工程。

3/5営業日の補助CSVは期間が終わったrunへ日次追加する。欠損・分割不明はunknown、未経過はhorizon_incomplete。高値最大・終了日終値の騰落を表示し、約定収益は算出しない。主評価・登録済みM5採否は翌1営業日のまま。

| 内容 | 保存先 |
|---|---|
| 開示原本・版・時点・応答・正解 | data/raw/、data/materials/documents/・imports/・snapshots/・cache/・gold/ |
| 材料CSV・文書応答 | outputs/<run>/materials/<id>/ |
| B1/B2・3/5日補助 | outputs/<run>/material_experiments/、auxiliary/ |
| 材料品質 | outputs/material_quality/<id>/ |
| 週次案・preview | outputs/weekly_research/completed/、previews/ |
| 人手採否・非ACTIVE版 | data/research/reviews/、releases/、SQLite仮説イベント |

## 未解決の条件

実開示の許諾済み本文・人手正解は未提供。用意できた時点で上記取込みへ進める。価格運用には追加CSVは不要。既存2実験は調整10/1〜10/15・最終10/16〜11/30、12/1の翌日結果が揃ってから比較可能。

材料B1/B2の統計採否エンジンは16で実装し、合成データで先行。実品質合格・人手正解・凍結した未使用期間が揃うまで実採用は保留。長文の分割・抜粋分析は17で追加し、資料全体の統合/OCRとACTIVE昇格は残る。真正PIT母集団・提供元完全性、実開示品質、長期稼働、実GPU OOM、電源断/再ログイン/スリープ復帰も未確認。実機接続・合成成功をこれらの合格に読み替えない。
