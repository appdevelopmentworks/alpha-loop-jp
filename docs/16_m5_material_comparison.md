# M5 材料比較・採否エンジン

更新: 2026-10-02。実装上の初期値を事前登録して、保存済み材料応答と翌営業日の全母集団結果をPythonで比較する。既存の価格基準1.1.0と登録済み価格M5の2実験は別の登録・出力として保持する。

## API・GPUなしの実行

プロジェクト直下のPowerShellで実行する。Pythonはuv管理、追加パッケージ不要。初回のPython取得が必要なら`--offline`を外す。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/verify_material_m5.py
uv run --offline --managed-python --no-project --python 3.11 python -m unittest discover -s tests -v
```

毎回新しい`data/material_m5_demo/<id>/`に7営業日の価格・材料・候補CSV・翌日結果CSVを生成する。比較報告はその配下の`outputs/material_comparisons/mcmp-*/report.md`、全母集団の選択と結果は`samples.csv`、日別比較と費用感度は`daily_metrics.csv`・`cost_sensitivity.csv`。最新受入証跡は`data/operations/material_m5_acceptance.json`。合成では統計判定が採用候補でも最終採否はHOLDとなる。

## 比較の定義（設計初期値）

| 経路 | 事前固定の選択 | 同一の予算・母集団 |
|---|---|---|
| A | 元のEARLY/PREMOVE上位K | 元の価格基準。WATCHはA主評価に含めない |
| B1 | Aのうち材料条件を満たす銘柄、補充なし | Aの部分集合。候補保持率と取りこぼしも比較 |
| B2 | AにWATCH/NONEの材料適合銘柄をスコア順で最大固定枠追加 | 総上限2K。超過時はAの下位順位を置換。価格不良・過熱・対象外は追加しない |

初期材料条件は正式契約・希薄化確率0.5未満。スコアは本業、反映時期、金額明記の重み付き平均（既定各1/3）、複数資料は全資料が根拠付き正常応答のときのみ、適合資料の最大スコアを使う。追加枠は既定5。スコアは上昇確率ではない。未知は陰性やゼロに補完しない。

04のB2「材料経路から候補追加」に従う。15で先行実装した既存価格適合群内のB2順位プレビューは、そのまま別の旧プレビューとして残す。新エンジンのB2追加比較とは区別する。

主比較はEARLY/PREMOVE合算のALLでA対B1、A対B2を別計算する。追加候補の元区分はbaseline_stageで保存し、価格PREMOVEへ読み替えない。両経路の上限は同一だが、実際の候補数は異なり得る。登録済み価格M5の区分別主比較は変更しない。

## 登録・時点・品質

合成環境の`material_plan.json`が全項目を含む例。実研究には仮説ID・版・family・thesis、discovery/tuning/holdout、明示営業日カレンダー、基準設定、質問、モデル/サーバー/量子化/runtime/抽出器/根拠方針、資料上限、材料条件・重み・追加枠、統計基準、品質証跡を指定する。原本JSON、設定とコードbundleを登録時に保存し、SQLiteの別テーブルでhashを固定する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-material-register --plan <material_plan.json>
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-material-batch --experiment-id <mexp-id>
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-material-compare --experiment-id <mexp-id>
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-material-refresh
```

modeはsynthetic / research / prospective。研究は品質証跡なしでも進められるがHOLD。prospectiveは登録日の翌日以降に始まる未使用holdout、同じモデル・質問の実資料人手正解品質、レビュー記録が必須。採用対象providerはユーザー指定のrazorback16/openjevで、mockは人手レビューでも昇格不可。`quality_evidence`は`report_path`、`documents_path`、`human_reviewed`、`reviewer`、`reason`。15の実品質基準を満たすREADY_FOR_HUMAN_REVIEWとhuman正解だけが対象で、資料・正解・報告の時刻がholdout開始前であることを確認する。品質側でも本文から段落を再生成し、発行者/推論文脈・引用・モデル・キャッシュとの一致を検証して原本を凍結保存する。人手正解の独立性・利用許諾の実確認はレビュー責任である。

初回完了価格run、指定版・資料上限の初回材料receipt、初回の完了翌日評価を機械的に選ぶ。材料のretriesを良い応答へ差し替えない。原本・issuer対応・資料時刻・snapshot・キャッシュ・質問/モデル・引用を検証。翌営業日9時以降の材料応答は不明。選択は結果を渡さず計算し、価格/材料入力とともに封印する。後日投入による既存選択の変更は拒否する。

全holdoutと翌営業日20時を待ち、不足時はWAITINGで途中成績を出さない。全母集団の価格unknown率を判定し、材料unknown率は全母集団の診断値と実際に各経路が使う入力で保存する。B1はA候補、B2は追加対象WATCH/NONEで資料が提供された銘柄の不明率を品質判定に使い、対象入力ゼロはHOLD。資料未提供を「材料なし」とは扱わず、B2の検証対象を固定された提供資料の範囲に限定する。日数・有効件数・独立銘柄/クラスタ、候補保持率・capture低下も判定する。5営業日purge、区分間の同一材料イベント・重複原本を除外し、同一銘柄と共有イベントを保守的にまとめる。対応日×イベントクラスタbootstrapを固定seedで行い、事前試行上限（B1/B2で最低2）により区間を補正する。

holdoutは一度だけ消費し、同一入力は同じ報告を再利用する。登録条件や結果を変えて再採点できない。採否はHOLD / REJECT / ADOPTION_CANDIDATE、品質未合格・合成・再構成価格は必ずHOLD。未知ラベルの上下限、費用0/10/30/50bpsの感度、未約定・停止・約定不明の除外を保存する。実際の約定収益は算出しない。

## Hermesと人手採否

既存日次処理の最後に、登録された材料実験の選択保存・比較更新を追加。未登録はNOT_REGISTERED。定時ジョブを増設せず、材料AIの呼び出しや基準条件を変更しない。失敗はside failureとして記録し、価格CSVは保持する。外部AI・発注は無効。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli m5-material-review --experiment-id <mexp-id> --arm B1 --decision HOLD --reviewer <名前> --reason <理由> --human-reviewed
```

完了報告に対して経路ごとにHOLD / REJECT / APPROVE_SHADOWを台帳へ記録する。APPROVE_SHADOWは品質合格のprospective採用候補だけで、別版のlive_enabled=falseを作る。本番ACTIVE昇格は実装していない。

## 検証と残条件

2026-10-02 08:51 JST確認: 最終全166試験成功（169.705秒、材料M5追加28件）。`python -S scripts/verify_material_m5.py`で追加依存・API/GPUなしの7営業日一括合成実行に成功。配備済みHermesラッパーも隔離合成入力で候補3件とmaterial_m5=NOT_REGISTEREDを保存し、新しい日次接続を確認。`material_m5_deployment.json`では既存価格数値コード・価格M5エンジン・基準設定・登録済み2実験・実成果物・平日20時設定の保持、稼働中バッチなしを確認した。

同日夜の実20時ジョブは21:06:44完了。新しい材料M5日次境界はNOT_REGISTEREDで正常終了、side_failures=[]、価格2実験はWAITING。実材料の登録・分類・採否は未実施で、資料未提供と品質/期間不足が理由。`data/operations/daily_check_2026-10-02.json`と04§18に実定時証跡を記録。

証跡は`data/operations/material_m5_regression_stderr.log`、`material_m5_acceptance.json`、`material_m5_wrapper_acceptance.json`、`material_m5_deployment.json`。最終の合成出力は`data/material_m5_demo/aaa5f96d9d55/`、比較報告はその配下の`outputs/material_comparisons/mcmp-cbc3210115e0d605a906/report.md`。B1の統計判定はADOPTION_CANDIDATE、B2はREJECTだが、合成であるため両方の最終採否はHOLD。これらを実成績・利用者による承認と扱わない。

追加の材料比較試験は手計算、B2追加/上限制御、元区分保持、費用/未約定、不明/候補ゼロ、休日、期間未完了/覗き見拒否、入力/モデル/根拠改変、同一再実行、後日補完拒否、イベントpurge/クラスタ、版固定/試行数/再利用期間、人手採否を対象とする。最終の件数・実行証跡は04と受入記録へ追記する。

実許諾済み材料、人手正解、真正PITの母集団、未使用期間の実成績は未提供・未完成。したがって実市場の材料改善効果・本番採用は未検証。長文分割・抜粋分析は[17](17_long_materials.md)で追加し、全文統合/OCR、品質合格後の実prospective登録、ACTIVE昇格は残る。今回の合成実装成功を実モデル品質・利益の検証としない。
