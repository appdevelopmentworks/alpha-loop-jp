# M3 長文材料の分割と抜粋shadow分析

2026-10-02追加。許諾確認済みUTF-8本文を、末尾まで保持した窓へ分割する。原本・資料版・取得時刻は15の取込み仕様を継承する。画像PDFのOCRは対象外。

## できること

- 原本SHAと文書record_hashを検証し、保存段落も原文から再生成して照合する。
- UTF-8 BOMを除いて復号した原文の文字位置を保存する。CRLF、空行、日本語・絵文字を保持する。位置はバイト数ではない。
- 改行・句点付近を優先して分割し、境界を重ねる。長い一段落も分割可能。上限で残った本文は原本に残し、未処理文字数とpartialを表示する。
- 各抜粋へ既存6問と根拠確認を適用し、原文のstart/endとPythonでコピーした引用を保存する。モデル・質問・原本・分割条件ごとに成功応答を再利用する。
- 通信失敗・不正応答・根拠不足はunknown。再呼出し前のpartial応答もattemptsに保持する。保存キャッシュ/公開成果物のhash不一致は拒否する。

窓単位のyes/noを平均・多数決で資料全体へ昇格しない。前半の契約と末尾の訂正、複数企業、否定・条件文を抜粋だけで統合すると誤るため、`document_answers=null`、`m5_eligible=false`を常に保持する。全窓成功でも`CHUNKS_COMPLETE_REVIEW_REQUIRED`。既存M5の資料分類や候補CSVへは接続しない。

## 実行

プロジェクト直下のPowerShell。Pythonはuv管理のものを使う。

```powershell
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:PYTHONPATH = 'src'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --managed-python --no-project --python 3.11 python -S scripts/verify_long_material.py
```

追加パッケージ・API・GPUなしで長文合成資料、候補CSV、翌日結果CSV、分割CSV、抜粋応答、replayを生成する。`data/operations/long_text_acceptance.json`に出力先を保存する。合成分類は実モデルの精度確認ではない。

実資料は15に従って`disclosures-import`した後、返されたdocument_idを指定する。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/analyze_long_material.py --document-id <document-id>
```

既定は`PLAN_ONLY`。原文分割のみでGPUを起動しない。初期値は6,000文字、重なり300文字、分割上限100、推論上限10。数値は実装初期値で、利用者確定値ではない。文字数はモデルのtoken数とは異なる。

許諾済み資料と封印runがある場合に限り、明示的にローカルOpenJevへ送る。

```powershell
uv run --offline --managed-python --no-project --python 3.11 python -S scripts/analyze_long_material.py --document-id <document-id> --run-id <run-id> --infer --window-chars 2000 --overlap-chars 200
```

本文が価格判定時点に利用可能で封印対象かを確認する。日次バッチ実行中/所有者不明は手動GPU分析を拒否し、既存GPUロックでOpenJevへ排他切替・元状態復元する。起動込み時間予算は既存materials_operations.jsonの600秒。providerのtimeoutは残り時間まで縮め、期限超過の回答はunknownとして次の窓へ進まない。接続確認と推論の複数HTTP処理・復元を含む厳密なプロセス終了時刻の保証ではない。文脈上限エラーはunavailable/不正応答として残し、勝手に本文を切り捨てない。実長文での適切な窓長・精度・token数は未確認。

## 検証記録

全184テスト成功（226.688秒、追加18件）。稼働中の10/2バッチに影響しない隔離コードで実施した。原文の全範囲、末尾の訂正、巨大段落、CRLF/BOM/Unicode、上限・未知、予算超過、キャッシュ/成果物改変、再試行履歴、矛盾窓・注入文の昇格防止を確認。注入文試験は模擬モデルの構造確認で、実モデルの耐性を証明しない。

`python -S scripts/verify_long_material.py`で18,421文字を4窓へ分割し、API/GPUなしで候補・翌日結果CSVと抜粋応答を生成。保存応答再利用、末尾までの原文位置、既定CLIの推論無効、価格CSV保持、replayを確認。証跡は`data/operations/long_text_acceptance.json`、`long_text_regression_stderr.log`。

保存先は`outputs/material_long_text/lts-*/`。`plan.json`、`chunks.csv`、`responses.json`、`summary.json`、`manifest.json`、`attempts/<hash>/`を保存する。キャッシュは`data/materials/long_text_cache/`。既存日次処理の自動分岐・定期登録は追加しない。

## 残る条件

資料全体の照合・訂正優先・複数発行者対応を独立した人手正解で検証してから、別版の統合器とM5接続を設計する。実許諾済み本文と人手標本は未提供。OCR、実長文のOpenJev品質、ACTIVE昇格、真正PIT/長期稼働の確認は残る。
