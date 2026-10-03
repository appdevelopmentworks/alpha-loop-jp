# ランキングから急騰前を振り返る

更新: 2026-09-30。U02・U09の「当日急騰した株の前営業日までの状態を調べ、仮説を作る」工程を実装。合成・ファイル入力での研究用機能であり、実市場の予測性能は未検証。

## 処理と保存

1. ローカルの`ranking.csv`と`ranking_source.json`を読み、日付・時刻・銘柄ID・重複・欠損・有限値を検証する。`RankingProvider`の入力境界は交換可能。
2. 明示カレンダーからランキング日Dの前営業日を求める。休日を暦日で数えない。
3. 保存済みの前日run、または正規化された日足ファイルからD-1までの状態を計算する。D以降の日足は原本に保持して計算から取り除く。
4. Pythonで全母集団を判定し、ランキング掲載銘柄と照合する。前日母集団にない銘柄は`unmatched.csv`へ保存し、銘柄コードの不一致はエラー。非掲載を非急騰・失敗と決めつけない。
5. 掲載群・非掲載群の件数、区分、指標中央値を比較し、仮説の定型下書きを保存する。任意でローカルQwenによる説明・最大2件の未検証仮説を別ファイルに保存する。

`outputs/retrospectives/<study_id>/`へ、全母集団の`retrospective.csv`、掲載一致分の`ranked_prior.csv`、`unmatched.csv`、`features.json`、`disclosure_exclusions.json`、`comparison.json`、`hypothesis_drafts.json`、日本語`report.md`、hash付き`study_manifest.json`を出力する。原本は`data/raw/`、再計算用入力は`data/retrospectives/`、成功・失敗の実行記録は`data/operations/retrospective_attempts/`に残す。CSVはUTF-8 BOM。候補・掲載一致ゼロもヘッダーを保存する。入力・設定・数値コード・振り返りコードの同一版は同じstudyへ収束し、SQLiteで生成を排他する。

`--prior-run-id`は、保存済みrunのsnapshot・設定・情報締切をそのまま利用し、原本hashと当時の数値コード版を照合する。前日runのカレンダーにはランキング日Dも必要。前日のsnapshotや候補CSVを上書きしない。

`--input`は後日再構成用。実データは元のgradeがobservedでも本研究では`reconstructed`とし、実際の取得時刻を過去へ変更しない。価格はD-1まで、開示は公表時刻が情報締切までの版に限定する。取得が後日の開示は`published_only_reconstruction`として扱い、初見・取得時刻不明は不採用。提供元が明示した日足`available_at`が締切後ならDATA_HOLD。銘柄属性の有効日がD以降なら停止する。後日訂正、当時の母集団、調整基準の完全再現は保証しない。

全studyは`prediction_score_eligible=false`。ランキングの掲載騰落率は出典記録であり、翌日高値10%の正解ラベルや約定利益へ置き換えない。仮説の採用には未使用期間の全母集団と別の翌日結果評価が必要。

## 合成データで実行

PowerShellでプロジェクト直下から実行。既存のuv管理Pythonを使用し、追加パッケージやモデルは取得しない。新しい環境でPython未取得なら`--offline`を外してuvに管理させる。

```powershell
$env:PYTHONPATH = "src"
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".uv-cache"
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli ranking-fixture
$study = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective --ranking-input demo-ranking-input --input demo-ranking-input | ConvertFrom-Json
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective-replay --study-id $study.study_id
```

fixtureは9月18日の合成ランキングと9月17日までの日足。6銘柄の母集団、掲載一致2銘柄、母集団外1銘柄を意図的に含む。実サイトの株価・掲載結果ではない。

## 実ファイルの契約

ランキング入力ディレクトリに次の2ファイルを置く。取得・加工・保存が許される情報の記録形式であり、保存したHTMLをスクレイピングする機能ではない。手動転記自体が掲載情報の加工を許可するものではない。現在は利用可能な日足CSVからPythonでランキングを派生する経路も用意した（[11](11_csv_sources_and_plans.md)）。

`ranking.csv`の列順:

```text
instrument_id,symbol,rank,listed_return,source_url,source_updated_at,page
```

`instrument_id`と`symbol`は価格providerの銘柄一覧と明示的に対応付ける。先頭ゼロ・英字を保持。`listed_return`は小数比率（0.12=12%）、不明なら空欄。`rank`と`page`は正の整数。`source_updated_at`は表示された情報更新時刻で、通常の日付はDと一致し、取得時刻より後にならない。`derived_prices`の場合は実際の計算時刻を入れるためDより後も認める。同一銘柄の別サイト出典は併記、同じ出典・ページ・銘柄の一致行は重複除去し、矛盾行は拒否する。

`ranking_source.json`の例（日時は実際のものへ置換）:

```json
{
  "schema_version": 1,
  "ranking_session": "2026-09-29",
  "fetched_at": "2026-09-29T20:00:00+09:00",
  "data_grade": "observed",
  "acquisition_method": "manual",
  "coverage": "partial"
}
```

`coverage`は`partial`または`declared_complete`。1ページだけの記録はpartial。完全性の自己申告だけで正解母集団として採用しない。methodは`manual`、`synthetic`、`derived_prices`。価格からの派生入力はCLIがprovenance付きで作り、実履歴ならgradeはreconstructed。合成ならgradeとmethodを共にsyntheticとする。合成と実データの混在は拒否する。

前日の保存済みrunを使う例:

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective --ranking-input incoming/rankings/2026-09-29 --prior-run-id '<9月28日のrun_id>'
```

後日取得した履歴を使う例。価格ファイルの形式は[データ契約](03_data_contract.md)。カレンダーにはDとD-1、日足には最低60取引日、銘柄属性にはD-1時点の有効日が必要。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective --ranking-input incoming/rankings/2026-09-29 --input incoming/history --feature-as-of 2026-09-28T20:00:00+09:00
```

## 仮説台帳とローカルQwen

定型下書きの登録は任意。検証期間はランキング日より後を指定し、既存仮説の期間を上書きしない。DRAFTとして出典studyとgradeを記録する。採否基準・必要件数・実験分割の登録まで完了したことにはしない。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective-register --study-id $study.study_id --period-start 2026-10-01 --period-end 2026-12-31
```

Qwen説明は既存の固定モデル設定を使う。モデル切替・起動手順は[M4記録](09_m4_local_operations.md)。元の起動状態を確認し、GPU上は一方だけを起動する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective-report --study-id $study.study_id --runtime-config configs/qwen_runtime_rtx5090_v1.json --model-id Inferact/Qwen3.8-27B-NVFP4 --model-revision 6128240ebaf4eaa7bad2b3d1c72c37d677c5f462
```

集計値と最大20銘柄の前日状態はPythonがMarkdown表へ直接出力する。Qwenへは指標中央値の比較方向、grade、情報締切、制限と有限の条件カタログを渡す（上限12KB）。現行質問版は`ranking-plan-ja-v4`で、条件IDと理由コードのJSONを最大2案返させ、出力上限は1,024トークン。Pythonが条件名・単位・比較方法を確定する。未知ID・自由文・重複等と`finish_reason=length`は拒否し、応答原本と検証結果を試行履歴へ残す。AI案は人手確認が必要なDRAFTとして保存し、数値・候補・採用状態を変更しない。モデル・重みrevision・推論設定・質問版・入力hash・コードbundleでキャッシュする。AI案の台帳登録、OpenJev分類との自動連結、統計的改善は未実施。形式と制限の詳細は[11](11_csv_sources_and_plans.md)。

## 検証と未実施

2026-09-30にuv管理Pythonで全39件（振り返り追加13件）が成功。将来日足・当日公表資料の除外、後日の取得時刻保持と訂正版、公表締切、全母集団、不一致銘柄、ゼロ、重複、銘柄コード、休日、欠損、分割調整の比率不変性、原本/CSV/snapshotの改変検知、再実行・replay、検証期間の重複拒否、Qwen mock/cache/数値不変・途中応答拒否を確認。CLIでも合成CSVを生成。

実機でも`study-77b8fd65dc9429ec10f1`の合成CSV、replay、Qwen下書きを生成。v3応答は`finish_reason=stop`、キャッシュIDは`5d92d077a8817314af3ae3872fd0e6ff77a8f4057ab66f0ada6606aa25b65106`。Qwen停止後に同じcache IDで再実行し、数値CSVのSHA256不変を確認。開始時は両専用モデルが停止していたため、検証後も両方停止へ復帰した。

初回v1は512トークンで途中切れ、v2は掲載群の中央値を選出1銘柄の値として誤帰属した。v3ではPython数値表とAI仮説を別に生成するよう修正。v3文面にも売買代金の中央値を「出来高中央値」と呼ぶ誤記、具体的な比較条件を示さない提案があり、内容の品質は未合格。`qwen/quality_review.json`に確認記録を保存。診断時の旧版応答は追跡用に保持し、採用しない。

その後v4の構造化案に変更し、全49件と合成入力での実機応答に成功。旧v1〜v3の応答は診断記録として保持する。v4の形式合格を仮説の効果・採用の合格とは扱わない。最新の実行ID・ソース保存・CSV入口は[11](11_csv_sources_and_plans.md)。

実ランキングの継続収集、全銘柄の実履歴との結合、真正の当時母集団、実開示の正解標本、複数日・未使用期間の比較は未実施。サイト接続は[取得条件の記録](06_decisions_sources.md)に従う。株探の機械的取得は禁止。Yahoo!は公式時系列CSVの私的利用の案内と、それ以外の掲載情報の加工等の禁止を確認したため、ランキングのWeb自動取得は有効化していない。
