# 日足CSVからの急騰研究と構造化仮説

更新: 2026-09-30。U10を反映し基準戦略1.1.0へ更新。売買代金を必須にせず、取得できた株価・出来高で分析する。実市場データでの検証は未実施。

## 入手方法と残る入力

Yahoo!ファイナンスの[公式利用案内](https://support.yahoo-net.jp/PccFinance/s/article/H000011281)は、VIPの時系列CSVを私的利用する場合について案内し、それ以外の掲載情報の加工・転載・商用利用を禁止している。ランキングページを自動収集する許諾として扱わない。手動転記に変えただけで加工・保存の許可が得られるものでもない。[VIPのCSV説明](https://finance.yahoo.co.jp/feature/promotion/vip/information)にある日付・始値・高値・安値・終値・出来高・調整後終値は、日足の入力候補になる。購入・契約・ログイン・ダウンロードは本実装では行っていない。

株探の機械的取得は禁止されている（[出典S16](06_decisions_sources.md)）。Yahoo!も[掲載情報のスクレイピング禁止](https://support.yahoo-net.jp/SccFinance/s/article/H000011276)を明示している。両ランキングサイトへのHTTP取得機能はない。[銘柄詳細](https://finance.yahoo.co.jp/quote/7203.T)には当日の売買代金があるが、途中時刻の値は日足確定値・20日履歴の代用にしない。ユーザー添付の四季報ランキング画像の金額列は時価総額。利用・加工・保存可能な公式CSV等を入力し、Pythonで上昇銘柄を抽出できるようにした。サイト独自の掲載ランキングと完全一致することは保証しない。

売買代金は任意。CSVの列がない/空欄、JSONで省略/nullでも株価・出来高の算定を継続する。終値×出来高を実売買代金欄へ代入しない。基準戦略の売買代金足切りを取り除いたため、取得不能な売買代金の提出は必要ない。

株価調整の確認は引き続き必要。[公式の調整説明](https://support.yahoo-net.jp/PccFinance/s/article/H000006679)には分割・併合以外の権利落ち等もあるため、調整後終値÷終値を無条件に分割係数として採用しない。現在の汎用CSV取込みは明確なsplit_factorを要する。未知の企業行動を推測で補わない。別ファイルの補足データ自動結合・権利落ち対応adapterは未実装。

[yfinance](https://github.com/ranaroussi/yfinance)は日足OHLCVや分割・配当イベントを得る別取得経路で、Python側でCSVを作れる。手動CSV提出を必須とする必要はない。ただし今回までの実装にyfinance接続はなく、uvへの追加依存も未導入。Yahoo! Finance側の利用条件、標準日足の調整意味・欠損・取得制限を確認して専用adapterで接続する必要がある。`auto_adjust=False`だけで完全な未調整原値と断定せず、提供元が既に行った調整を二重適用しない。

## ローカルCSVの契約

`csv-import --spec <import.json> --session <D>`が列名を正規化し、既存のファイルproviderへ渡す。実履歴は`reconstructed`、合成値は`synthetic`。取得・利用可能時刻を実際の値で保持する。銘柄一覧と営業日カレンダーは[03のJSON契約](03_data_contract.md)で明示し、価格CSVだけから全市場の母集団や休日を推測しない。

設定例。値・ファイル名・列名・時刻は実際の入力へ置換し、提供元で確認できない調整範囲を宣言しない。

```json
{
  "schema_version": 1,
  "provider_id": "your_export_provider",
  "source_url": "https://example.invalid/provider-documentation",
  "usage_basis": "提供元の利用・加工・保存条件と確認日を記録",
  "data_grade": "reconstructed",
  "acquisition_method": "licensed_export",
  "fetched_at": "2026-09-30T19:45:00+09:00",
  "available_at": "2026-09-30T19:00:00+09:00",
  "adjustment_basis_id": "provider-split-basis-2026-09-30",
  "corporate_action_scope": "split_and_consolidation_only",
  "universe_coverage": "partial",
  "calendar_file": "calendar.json",
  "instruments_file": "instruments.json",
  "files": [{
    "instrument_id": "TSE:0001",
    "file": "0001.csv",
    "encoding": "utf-8-sig",
    "columns": {
      "session_date": "日付", "open": "始値", "high": "高値",
      "low": "安値", "close": "終値", "volume": "出来高",
      "split_factor": "分割調整係数",
      "bar_status": "状態"
    }
  }]
}
```

`acquisition_method`は実入力なら`official_csv_export`または`licensed_export`。`encoding`はUTF-8 BOMが既定で、明示したcp932等にも対応。日付は`YYYY-MM-DD`または`YYYY/MM/DD`、カンマ付き数値はCSVで引用する。価格・数量は有限の非負値、分割係数は正数。調整価格=原価格×係数、調整出来高=原出来高÷係数、売買代金は実値のまま。任意の`adj_close`列は係数との一致を確認する。

実売買代金がある場合だけ列対応へ`"turnover_jpy": "売買代金"`を追加する。列を対応させた場合も空欄はnull。D-20〜D-1の全20日に実値が揃う場合だけ20日中央値を計算する。足りない場合は中央値null、取得済み日数とmissing/observed_partialを保存。基準戦略1.1.0の`turnover_filter_mode=disabled`は観測値があっても選外・順位に使わず、相対出来高→銘柄IDで並べる。流動性や約定可能性を確認できたことにはしない。

旧条件を明示比較する場合は`--config configs/baseline_turnover_v1.json`（戦略1.0.0、1億円以上）を使う。新設定なら`turnover_filter_mode=required`と有限の非負閾値を指定。実値20日が不足した銘柄はDATA_HOLD、取得済みで閾値未満ならINELIGIBLE。比較は同じ母集団で行い、欠損件数も報告する。旧run/studyの完全再現には旧コードbundleと当時の設定を使う。

`bar_status`を省くと`ok`、他に`halted`・`no_trade`・`missing`を認め、価格不明として保存する。銘柄ID・日付の重複、未知銘柄、列欠損、対象ディレクトリ外の参照は拒否する。Dより後の行は計算から除き、CSV原本には保持する。銘柄一覧の全対象に必要な履歴がなければ、既存のDATA_HOLDと未知扱いになる。最低60営業日の履歴を用意する。

`data/imports/<import_id>`に正規化入力、`data/raw`に原本、`data/operations/import_attempts`に成功・失敗履歴を保存。同じ入力・設定・対象日・取込みコードは同じIDを使い、改変された保存入力を再利用しない。

## uvで合成CSVを一巡させる

プロジェクト直下のPowerShellで実行。GPU・APIキー・追加依存なし。uv管理Pythonが未取得の新環境では`--offline`を外してuvに取得させる。

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "src"
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".uv-cache"
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli csv-fixture --without-turnover
$import = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli csv-import --spec demo-csv-input/import.json --session 2026-09-18 | ConvertFrom-Json
$ranking = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli derive-ranking --input $import.input_dir --session 2026-09-18 | ConvertFrom-Json
$study = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective --ranking-input $ranking.ranking_input --input $import.input_dir | ConvertFrom-Json
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective-replay --study-id $study.study_id
```

`derive-ranking`は調整後終値/前営業日調整後終値−1が`--return-min`以上の銘柄を降順抽出。既定は10%、上限`--limit 50`で、どちらも設計初期値。閾値境界は十進比較。対象は入力母集団の上場普通株。`data/derived_rankings/<ranking_id>/`にCSV、原出典をたどれるmanifestと`unknown_prices.json`を保存。対象欠損・上限による切捨てがあればcoverageはpartial。実入力の出力methodは`derived_prices`、生成時刻は実際の時刻。合成はsyntheticのまま。

この**終値騰落率**は評価器の「翌営業日高値/前営業日終値−1が10%以上」というラベルと異なる。非掲載を非急騰と決めつけず、実現値上がりを約定収益とも呼ばない。前日分析にはD-1までだけを渡し、studyは常に予測成績非適格。候補CSV・翌日結果CSVの生成は[根READMEのM0〜M2手順](../README.md)を併用する。

## Qwenの構造化提案

起動と排他的なモデル切替は[09](09_m4_local_operations.md)、任意の`retrospective-report`コマンドは[10](10_ranking_retrospective.md)を参照。質問版は`ranking-plan-ja-v4`。AIには比較方向と有限の条件カタログを渡し、次のJSONだけを受け付ける。

```json
{"hypotheses":[{"condition_ids":["not_breakout_le_0","volume_ge_2"],"reason_code":"near_previous_high"}]}
```

条件IDは相対出来高1.5/2倍以上、20日高値からの距離−5%以上/0以下、5日騰落率5/12%以下、25日移動平均乖離率15%以下。最大2案・各1〜3条件のAND。Pythonが特徴量・単位・閾値・日本語名・比較方法を決める。未知ID、自由文、重複、同じ方向の冗長条件、理由と特徴量の不一致、NaN、途中応答を拒否する。応答原本と失敗理由は`data/operations/qwen_attempts/`に残す。

各案は「同じ未使用期間の全母集団で基準Aと提案条件のAND選別を比較」と明示し、的中率・捕捉率・候補数・未知率を記録する計画にする。閾値は測定から得た最適値ではなく設計初期値。自由な仮説発見機能ではなく、既知条件の組合せ提案である。`structure_validated=true`は形式・語彙の合格であり、経済的根拠や統計的改善の合格ではない。検証期間・必要件数・採否基準は未設定のDRAFT、`needs_review=true`、自動採用なし。

AI案は`qwen/*.json`の`hypothesis_plans`に保存する。現行の`retrospective-register`が台帳へ登録するのはPythonの定型下書きであり、このAI案ではない。AI案の台帳登録と実験実行は未実装。OpenJevの開示分類との自動連結も未実施。

## 保存した版での再現

Git未導入のため、新規取込み・studyは全Pythonソースを`data/code_bundles/<code_bundle_id>/src/alpha_loop/`に保存し、manifestでhashを照合する。Qwen cacheにもコードbundle IDを含める。旧studyを将来のコード更新後に再計算する場合は、study_manifestの`code_bundle_id`を使う。

```powershell
$studyId = "study-521002032e9d1ec8d057"
$manifest = Get-Content "outputs/retrospectives/$studyId/study_manifest.json" -Raw | ConvertFrom-Json
$env:PYTHONPATH = "data/code_bundles/$($manifest.code_bundle_id)/src"
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli retrospective-replay --study-id $studyId
$env:PYTHONPATH = "src"
```

作業ディレクトリは元プロジェクトのまま。原本・snapshot・設定・成果物も必要。bundle保存導入前の旧studyは当時コードを自動復元できない。ソースの保存はPython実行環境全体の固定ではない。

## 今回の確認と未実施

uv管理Pythonで全49件成功。CSVの欠損列・時刻・調整・重複・ゼロ・閾値境界・原本/コード改変を確認し、構造化AI案の正常/異常応答も試験した。合成CSVの実CLIでは6銘柄から上昇2件、価格不明1件を保存し、前日study・replayまで成功。

実機Qwenが2案をJSONで返し、Pythonが条件名と比較方法を生成した。成果物は`outputs/retrospectives/study-521002032e9d1ec8d057/`、Qwen cache IDは`7270cb7ee64a2a8c0e2f210ebd8c416eafb913b097c8593febc6004e0af604aa`。モデル停止後もcacheを再利用し、数値CSVのSHA256不変を確認。検証後はOpenJev・Qwenの両専用コンテナを開始時と同じ停止状態へ戻した。

上記49件・Qwen実機応答は売買代金任意化前の記録。その後、売買代金なしの株価・出来高入力について全54件成功（12.593秒）。候補/順位/翌日評価の継続、空欄保存、部分履歴の件数、旧流動性設定、欠損によらない順位、Qwenへのunknown伝達と構造化提案を確認。今回はQwen mockの接続試験で、欠損入力による新しい実機推論は未実施。

実CLIでも`fixture --out data/demos/no-turnover --without-turnover`から候補3件・全判定6件・翌日結果6件を生成し、replay成功。runは`run-1bd96db9cdc0f94a8358`、翌日結果は`outputs/run-1bd96db9cdc0f94a8358/evaluation/7b02ff86cd00ec1da3d3/outcomes.csv`。売買代金なしCSVの取込み→上昇2件→前日分析のstudyは`study-99afbb06aac8dbcc4059`でreplay成功。記録は`data/operations/no_turnover_validation.json`。生成オプションは既存fixtureへ上書きできるので、別ディレクトリ指定で比較するとよい。

売買代金なしの候補・翌日結果を再生成する場合、上のUTF-8とuv設定後に実行する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli fixture --out data/demos/no-turnover --without-turnover
$run = uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli run --input data/demos/no-turnover --session 2026-09-17 --as-of 2026-09-17T20:00:00+09:00 | ConvertFrom-Json
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli evaluate --run-id $run.run_id --future data/demos/no-turnover --through 2026-09-18
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli replay --run-id $run.run_id
```

未実施は、yfinance等の実接続・調整仕様照合、全銘柄の実履歴と企業行動、真正の当時母集団、実開示の人手正解、未使用期間の性能比較・費用/約定評価、AI案の実験登録。取得adapter・正解標本・実験条件が未確定のため。売買代金の入手は開発・分析を止める条件から外した。合成成功を実データの検証結果として扱わない。
