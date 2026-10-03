# M0〜M2 実装記録

2026-09-28 / 実装版 0.1.0

2026-09-30補足: ユーザーU10により基準戦略は1.1.0へ更新。売買代金は任意、既定の足切り・順位付けは無効。株価・出来高による算定、候補保存、翌日評価は継続する。旧1.0.0設定は`configs/baseline_turnover_v1.json`、変更前コードはbundle `af6f189a6d72a08287d365e6d7af4306f6975f174e251ef031b9a8f785be7840`へ保存。現在の数値コードで旧runの結果を上書きしない。最新手順と検証は[11](11_csv_sources_and_plans.md)。

## 入出力と実行

手順は [ルートREADME](../README.md)。`src/alpha_loop/provider.py` の `MarketDataProvider` / `DisclosureProvider` は交換境界で、現在は `FileProvider` のみ。`calendar.json` は昇順の営業日配列、`instruments.json` は当時の銘柄属性、`bars.json` は03の正規化日足、`source.json` は `data_grade, fetched_at, available_at`、任意の `disclosures.json` は版付き開示メタデータです。評価専用の `future.json` は別ファイルで、判定snapshotに入りません。外部AI・発注は設定をtrueにすると実行を拒否します。

原本はSHA-256名で `data/raw/`、封印済み正規化入力は `data/snapshots/`、実行・試行・仮説は `data/state.sqlite` に保存します。`run_id` は同じ入力・設定で一意に収束し、replayは別runに `replay_of` を保存します。成果物は一時ディレクトリから確定し、ハッシュ照合後だけ既存の完了runを再利用します。中断時の作業用ディレクトリは排他取得後に再生成します。

合成fixtureの日付は2026-09-17、翌営業日は2026-09-18。実行例で候補3件（EARLY/PREMOVE/WATCH）、全判定6件、翌日結果6件を生成しました。保存場所: `outputs/run-050290209878c3a137f4/`、評価IDは `c71896474ae8e85486e1`。完了が翌営業日開始後なので `history_only=true`。合成データの的中率や濃縮率は動作試験の数値であり、実市場の性能を示しません。

## 評価の扱い

翌営業日の調整後高値と判定日終値を同じ株式数基準で比較します。`future.json` の `prior_close_rebase_factor` は、判定日の終値を評価日基準へ直す評価専用係数です。これが不明なら正解はunknown。分割と配当込み総収益は混同しません。始値→終値のproxyはストップ高・停止・未約定・約定不明で算出せず、費用未設定ならnetはnullです。`metrics.json` には区分別的中率、未知を全失敗/全成功とした範囲、母集団率、捕捉率、濃縮率、誤検出数を保存します。単一日の集計であり、統計的な優位性は判断できません。

仮説台帳は `hypothesis-add` と `hypothesis-list` でDRAFTを登録・閲覧できます。例: `uv run --managed-python --no-project --python 3.11 python -m alpha_loop.cli hypothesis-add --id H001 --thesis '出来高増加' --required-data '日足' --comparison '価格基準A' --period-start 2026-10-01 --period-end 2026-12-31`。仮説の採用・条件変更・研究期間分割はM5で扱います。

## 残課題

- 実データ契約・当日全銘柄の配信完了時刻と許諾の確認、実API adapterは未実施。合成データを実データの検証結果として扱わない。
- 分割係数は評価ファイルから受け取る。実データの企業行動・現金配当・併合の正規化と真正の約定可能性は未検証。
- 3/5営業日評価、複数日比較、イベント群を跨ぐ期間分割とpurging、信頼区間、前向きの統計的採否はM5。現在の評価は翌1営業日だけ。
- 開示本文・OpenJev・Qwen・Hermes・GPU・定期実行はM3以降。開示providerがない場合はsemantic_status=unavailableで、材料が存在しなかったという判定にはしない。
