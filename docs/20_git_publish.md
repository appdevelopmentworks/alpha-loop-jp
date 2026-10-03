# Git公開・移行手順

更新: 2026-10-03。コード・共通設定・文書を保存する。実株価/本文・台帳・応答・履歴・モデルは別途保管する。[導入](18_setup.md) / [運用・バックアップ](19_operations.md)。

## 1. 公開するもの / 除外するもの

| Gitへ含める | Gitへ含めない |
|---|---|
| `src/`、`tests/`、`scripts/`、`docker/` | `data/`、`outputs/`、`incoming/`、生成demo |
| 共通`configs/*.json`、質問・版付きGPU構成 | `*.local.json`、`configs/local/`、未確定M5下書き |
| README、docs、作成済み人手ラベル記入用CSV | 実本文・人手正解・株価CSV・SQLite台帳 |
| `pyproject.toml`、**`uv.lock`** | `.venv/`、uvキャッシュ、モデル重み、第三者ダウンロード |
| `.gitignore`、`.gitattributes`、Hermes配置用ソース | `.env`、鍵、Hermesの個人config/cron記録、ローカルagent状態 |

CSVは原則除外し、`docs/*.csv`と`tests/fixtures/*.csv`だけを例外として追跡する。そのディレクトリにも実データを置かない。モデルのrevision/digestやlocalhostの構成値は共通設定へ残す。認証値は残さない。

`.gitattributes`の`* -text`はGitによる改行自動変換を止める。計算コード/設定/原本はバイト列のSHA-256で固定しているため。エディタの一括改行変換・保存時整形も登録済み実験への影響を確認する。

`.gitignore`は既に追跡したファイルには効かない。本プロジェクトは2026-10-03確認時点では未初期化だった。既存repoへ移す場合は`git ls-files`も確認し、追跡済みデータをインデックスから外す。過去のcommitに認証情報があれば、現在の除外だけで解消したとは扱わない。

## 2. 初回commitまで

まずREADMEのuv環境で公開ファイルの確認を実行する。

```powershell
uv run --managed-python --no-project --python 3.11 python -S scripts/check_publish.py --export-clean
```

Gitの隔離インデックスで除外規則・限定的な秘密情報パターン・文書リンク・差分の空白を確認し、公開対象だけを`data/publication_audit/<ID>/checkout/`へ書き出す。元ファイルとのバイト一致も確認する。証跡は`data/operations/publish_check.json`。実リポジトリの初期化・ステージ変更・remote接続は行わない。秘密情報の確認は限定的なので、後段のステージ内容の目視確認も行う。

プロジェクト直下で実行する。既存repoの場合は`git init`を省く。運用中のコード/設定更新はworker停止後に行う。

```powershell
git init -b main
git status --short --untracked-files=all
git add --dry-run .
```

一覧に`data/`、`outputs/`、`.venv/`、実CSV、モデル、秘密情報がないことを確認する。除外ルールの個別確認:

```powershell
git check-ignore -v --no-index data/state.sqlite outputs/example/candidates.csv incoming/disclosures/manifest.json .env configs/hermes_operations.local.json models/example.safetensors
```

準備ができたら明示的なディレクトリだけをステージする。

```powershell
git add .gitignore .gitattributes README.md pyproject.toml uv.lock src tests scripts configs docker docs integrations
git diff --cached --stat
git diff --cached --check
git diff --cached
```

名前・内容を確認してからcommitする。Gitの作者名/メールは自分の既存設定またはリポジトリローカル設定を使い、例の他人の値をコピーしない。

```powershell
git commit -m "Initial Alpha Loop JP research MVP"
```

この作業ではリポジトリ初期化・commit・remote追加・pushは実行していない。

## 3. 初回Push

送信先に空のリポジトリを準備し、公開範囲を選ぶ。現在LICENSEは未設定。公開配布ライセンスを付ける場合は権利者の選択を反映し、第三者モデル/データの許諾と区別する。

```powershell
git remote -v
git remote add origin <自分の空リポジトリのURL>
git push -u origin main
```

`<...>`は実際の値へ置き換える。URLにトークンを埋め込まない。既存originがある場合は重複追加せず送信先を確認する。送信先に履歴がある場合はfetchして内容を確認し、force pushで上書きしない。

## 4. Push後の確認

新しいディレクトリへcloneして、[18の最短合成確認](18_setup.md)を実行する。既存PCの`.venv`、`data`、`outputs`をコピーしなくても候補CSVと翌日結果CSVが生成されることを確認する。

新規cloneでは原本、登録実験、人手採否、HermesのジョブID、Dockerコンテナ/volume、環境変数は復元されない。それらは[19のバックアップ](19_operations.md)と導入手順で復元/設定する。元PCの記録にあるrun/実験/ジョブIDは新環境の値として使わない。

`docs/01`〜`17`は仕様・実装経過も含む。そこに記載された`data/...`や`outputs/...`の証跡は元環境のローカル保存先で、リポジトリから配布されるファイルではない。現在の導入・運用入口は18/19とする。
