# データ契約と出力仕様

版: 0.1 / 2026-09-27 / 以下は内部の正規化形式。外部APIの仕様ではない。

## 1. 型・単位・識別子

- 日付は`YYYY-MM-DD`、日時はタイムゾーン付きISO 8601。内部日時をUTCで保存してもよいが、画面・日報はAsia/Tokyo。
- 価格・売買代金はJPY。騰落率は小数比率（0.05=5%）。bpsは1bp=0.0001。
- 銘柄コードは文字列。英字・先頭ゼロを保持し、整数変換しない。providerの5桁コード等はraw_codeに残して明示的に対応付ける。
- `instrument_id`は取引所と証券を識別する安定ID。名称変更、上場区分変更、廃止・再上場を最新名称だけで結合しない。
- 不明値はnull。ゼロ・false・該当なしと区別する。JSONはNaN/Infinity禁止、CSVの空欄はnull。
- 行と列の並び、丸め、同順位の扱いを固定する。内部判定は丸め前、CSV表示の丸めとは分離する。

## 2. 保存するエンティティ

| 名称 | 主キー・必須情報 |
|---|---|
| raw_object | content_hash、provider、source_urlまたはファイル参照、fetched_at、media_type、保存先 |
| instrument_snapshot | snapshot_id+instrument_id、raw_code、名称、市場、証券種類、上場状態、属性の有効日・取得時刻 |
| trading_calendar | venue+session_date、is_trading_day、取得元・取得時刻 |
| daily_bar | snapshot_id+instrument_id+session_date、raw OHLCV、任意のturnover_jpy、調整OHLCVまたは計算可能な調整情報、状態 |
| disclosure_revision | disclosure_id+revision_id、公表者、関連銘柄、published_at、first_seen_at、本文hash、段落、原本参照、訂正・取消関係 |
| input_snapshot | snapshot_id、as_of、対象D、原本・正規化データのhash一覧、data_grade、利用可能範囲 |
| run | run_id、run_kind、開始・終了・as_of、対象D、snapshot_id、コード・設定・ルール版、状態、warning、replay_of |
| feature_row | run_id+instrument_id、各指標、算定窓、データ品質、利用した日足参照 |
| semantic_judgment | judgement_id、資料・質問・モデル・実行設定参照、応答全体、証拠段落、状態、推論時刻 |
| decision | run_id+instrument_id+strategy_version、区分、各条件の真偽、順位、採否、理由コード、根拠ID |
| outcome | decision_id+horizon+evaluation_version、評価可能日、将来結果、仮想損益、約定不確実性、使用価格参照 |
| hypothesis / experiment | 仮説ID・版、対象母集団、条件、評価期間、比較基準、結果、状態、変更履歴 |

長い本文・大量の応答を各CSVへ複製しない。IDで原本と台帳へ辿れるようにする。DB変更にはschema_versionと移行履歴を持たせる。

daily_barの内部列名は`open,high,low,close,volume,turnover_jpy,adj_open,adj_high,adj_low,adj_close,adj_volume,adjustment_basis,bar_status`とする。未調整と調整済みを同名へ上書きしない。providerが提供しない調整項目は調整情報から一度だけ計算し、その方法と元の値を追跡可能にする。

2026-09-30のU10により`turnover_jpy`は任意。JSONの省略/null、CSVの未対応列/空欄は不明であり0にしない。終値×出来高をこの実値欄へ入れない。D-20〜D-1の20営業日すべてに実値がある場合だけ中央値を計算する。少数日の中央値を20日中央値と呼ばない。必要なOHLCVの履歴・調整が不明な場合のDATA_HOLDは維持する。

decisionには`decision_id`を発行し、run_id・instrument_id・strategy_versionの組に一意に対応させる。CSVやoutcomeからこのIDへ結合する。公開日が異なる訂正記事を同じ原文hashだけで消さず、内容重複と出来事の識別は別に扱う。

## 3. 時点整合性

`run_started_at`は開始時刻、`as_of`は入力を封印した情報締切、`completed_at`は出力完成時刻。銘柄情報・価格・開示の提供元の更新時刻と取得時刻を混同しない。

実際の当日運用では、資料のpublished_atとfirst_seen_atがas_of以前であり、価格等もas_ofまでに取得・検証できたものだけを使用する。公表日しか分からない開示は同日夜の材料として自動採用せず、時刻不明を記録する。

過去履歴を今取得した場合に、first_seen_atを過去の日へ書き換えてはならない。以下のgradeで分ける。

| grade | 意味と利用 |
|---|---|
| observed | 実際にその時点で保存した原本・属性・判定がある |
| vendor_pit | 当時の版と利用可能時刻をproviderが保証し、その情報を保存できる |
| reconstructed | 現在の履歴から再構成したもの。訂正・収録遅延・母集団の完全再現に限界がある |
| synthetic | 合成fixture。動作確認専用で成績報告へ混ぜない |

gradeごとに結果を分ける。最初のバックテストがreconstructedでも開発は進められるが、当時その通り運用できた証明にはしない。過去ニュースの銘柄名からモデルが後年の知識を補う問題もあるため、根拠を渡した当時資料に限定し、前向きのshadow評価を別に行う。

訂正前の開示を上書きしない。修正後に再判定するときは新しいrevisionと新しいrunを作る。結果ラベルの訂正もevaluation_versionを更新する。

## 4. 日足の検証

OHLCの大小関係、非負の出来高・代金、重複キー、対象日、母集団との対応を検証する。providerのnull日足は売買なし・停止等の状態情報で説明する。存在しない行と売買のない行は異なる。

価格調整は分割・併合の基準を揃え、出来高も対応させる。売買代金そのものに分割係数を二重適用しない。配当込み総収益と単なる分割調整価格を混同しない。調整不能な企業行動を跨ぐ指標・評価はDATA_HOLD等で別扱いにする。

母集団の期待行が欠け、理由を説明できないrunはBLOCKED_DATA。必要窓の個別欠損は銘柄単位DATA_HOLDとし、理由を保存する。全体障害を個別除外で隠さない。母集団の急変・日付ずれは診断報告に出す。

## 5. CSV契約

保存先は`outputs/<run_id>/`、UTF-8 BOM、ヘッダーあり、列順固定、改行は標準CSVとして読み取り可能にする。銘柄コードは読込側でも文字列指定する。表計算ソフト向けの文字列保護と数値型保持を区別し、文章・銘柄名などに数式として実行される文字列があれば表示用コピーでエスケープする。負の数値を文字列へ変えない。

### candidates.csv

```text
decision_id,run_id,target_session,as_of,instrument_id,symbol,name,strategy_id,strategy_version,stage,rank,ret_1d,ret_5d,rel_volume_20d,high_distance_20d,ma25_gap,turnover_median_20d_jpy,semantic_status,event_ids,evidence_ids,reason_codes,data_grade,turnover_status,turnover_observed_sessions_20d,liquidity_filter_status
```

EARLY / PREMOVE / WATCHを区分ごとに出力する。WATCHは価格初動と区別する。イベントや根拠が複数ならJSON配列文字列をCSVの規則に従ってquoteする。正常な候補ゼロでもヘッダー付きCSVを作る。

末尾3列は戦略1.1.0で追加。`turnover_status`はobserved_complete/observed_partial/missing/not_evaluated、観測日数は20日窓内の実値件数。`liquidity_filter_status`はdisabled/enabled/missing/not_evaluated。売買代金なしでも既定の株価・出来高判定は継続する。集計の`feature_observed_counts`は各特徴量の非null件数を示す。売買代金0件のAI比較方向はunknown。

### decisions.csv

対象母集団の全行。candidates.csvと同じ情報に`selected,is_eligible,rule_flags,quality_status,exclusion_codes`を追加する。rankは選外ではnull。モデルによる「分類確率」を「急騰確率」という列名にしない。

### outcomes.csv

```text
decision_id,source_run_id,instrument_id,horizon_sessions,evaluation_version,evaluated_at,outcome_status,discovery_return,discovery_hit,open_close_proxy_return,roundtrip_cost_bps,open_close_net_proxy_return,execution_status,forward_max_return,forward_min_return,price_basis,data_grade
```

discovery_returnはD終値からD+1高値への比率（horizon=1）。open_close_proxyは翌営業日始値→終値の価格上の仮想成績で、実約定の証明ではない。3/5日評価の具体的な出口と期間はevaluation_version内に定義し、異なる評価を同じ列へ黙って混ぜない。

売買コストは設定必須。未設定ならgross proxyだけを出し、netをnull、状態をcost_unsetにする。ゼロコストを暗黙採用しない。参考感応度として0/10/30bps等を使う場合も仮定値と明示する。

### run_manifest.json / report.md

manifestは状態、入力hash、各版、対象件数、区分別件数、取得・推論時間、警告、生成物hashを持つ。reportは成功・欠損・保留を日本語で説明し、抽出銘柄の推奨購入や断定的な急騰予測へ言い換えない。

## 6. AI応答と費用

`provider_id, model_id, model_revision, server_commit, quantization_id, inference_config_hash, question_set_version, source_hashes`を保存する。`latest`等しか使えない場合は再現性が限定されると記録し、運用前に解決したモデル実体を固定する。

`semantic_status`は`ok / needs_review / missing_evidence / unavailable / invalid_response`。材料不存在という内容上の判定と、読取・サービス障害を区別する。モデルの確信度が高くても根拠不一致ならneeds_review。

利用量はrequest件数、入力・出力トークン（providerが返す場合）、wall_time、再試行、GPUピーク、外部費用を保存する。未知の利用量をゼロと扱わない。ローカルAPI料金ゼロと機材・電力・保守コストを分ける。

## 7. ランキング振り返りの追加契約

`ranking.csv`は銘柄ID・文字列コード・順位・掲載騰落率・出典URL・表示更新時刻・ページ、`ranking_source.json`は対象取引日・実取得時刻・grade・取得方法・掲載範囲を保存する。`ranking_session`と`feature_session`（前営業日）を別列にし、原本の取得時刻を特徴日の時刻へ変更しない。後日再構成には別の`availability_basis`を記録する。

studyの主キーは入力snapshot・設定・コード版に由来する`study_id`。全母集団の`retrospective.csv`、掲載一致の`ranked_prior.csv`、母集団外の`unmatched.csv`、記述的な比較、仮説DRAFTを保存。各出力と原本のhashを検証して再利用する。順位・掲載騰落率は特徴量や前日分類へ入力しない。`prediction_score_eligible=false`を固定し、非掲載を陰性にしない。取得方法・CLI・列定義は[10_ranking_retrospective.md](10_ranking_retrospective.md)を正とする。

## yfinance研究入力とHermes運用の追加契約

market.pyは交換可能な取得adapterで、標準のFileProvider契約へJSONを書き出す。sourceにprovider_id/source_url、reconstructed、実取得時刻、利用可能時刻、月末母集団の基準日、原本kind/hash、取得パラメータ、yfinance版、未知件数を保存する。raw_kind=yfinance_returned_dataframe_csvはライブラリ返却値でありHTTP原文ではない。ネイティブOHLCVの分割調整を保持し、配当調整Adj Closeや終値×出来高をそれぞれ分割係数・売買代金へ代入しない。欠損銘柄もtarget bar_status=missingで母集団に残す。D-1欠損も明示してDATA_HOLDにする。

実約定はunknown、コスト初期値はnull。prediction_score_eligibleはobserved/vendor_pitかつ期限前に限り、reconstructedを除外する。service_jobsは入力・設定・コードbundleで重複識別、service_attemptsは各試行、latest_serviceは最新保存結果を示す。Qwenのdaily-notes-ja-v3は有限の確認事項IDだけを返し、Pythonが候補区分別件数・本文を確定する。詳細は[12](12_hermes_operation.md)。
