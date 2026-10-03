---
name: alpha-loop-jp
description: Alpha Loop JPの日本株研究を取得、候補抽出、翌日評価、前日分析、ローカルQwen日報の順に実行する。
---

# Alpha Loop JP

Project: the directory specified by `ALPHA_LOOP_ROOT`. Set it explicitly when moving or cloning the project; the wrapper fallback is the current user's `Desktop/alpha-loop-jp`.

Read `docs/18_setup.md`, `docs/19_operations.md` and `data/operations/latest_service.json` for setup and current state. `docs/12_hermes_operation.md` contains the original machine's operation history. Run from the project directory. Python is managed with uv; do not install system Python.
Read `docs/15_materials_and_weekly.md` for file disclosures, material quality, auxiliary results and weekly review. The existing daily job imports permission-asserted local text, preserves actual first observation, and runs materials in shadow. Missing sources are unknown and require no OpenJev startup. Friday completion produces weekly drafts. Never use protected holdout dates for proposals or claim material previews are validated. Human reviews require an explicit human declaration; do not act as the human approver or enable ACTIVE rules.

```powershell
Set-Location $env:ALPHA_LOOP_ROOT
$env:PYTHONPATH = 'src'
$env:VIRTUAL_ENV = Join-Path (Get-Location) '.venv'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONIOENCODING = 'utf-8'
uv run --offline --managed-python --no-project --python 3.11 python -m alpha_loop.cli operate
```

Use `hermes cron list` to identify the existing Alpha Loop JP daily job, then `hermes cron run <job-id>` when the user asks to run it. Job IDs are local to each installation. Do not create a duplicate job. It is a no-agent job: deterministic Python performs calculations, the standalone local Qwen drafts text. Hermes's conversational provider settings are not changed.

- Default universe: JPX monthly ordinary shares, Standard / Growth first, Prime included. Never claim current monthly membership is a historical point-in-time universe.
- yfinance is an unofficial personal research source. Do not scrape finance.yahoo.co.jp or kabutan.jp. Preserve provider cooldown; do not bypass access limits.
- At 20:00 JST use the latest completed XTKS session. Before that use the preceding session. Report the actual session date.
- Outputs are research data (`reconstructed`), not verified prediction results. Mention `SUCCEEDED_WITH_GAPS`, missing counts and coverage in any summary. Non-listed ranking stocks are not negative examples.
- Turnover is optional and missing remains null. Do not invent turnover, execution, costs, disclosures or stock split adjustments.
- External AI and orders must remain disabled. Use only the existing local model containers, exclusively; no model downloads or external message delivery.
- If Qwen fails, report the saved numerical CSVs and error. Once Docker is available, `operate --retry-ai` retries only the failed AI side output using the same saved input.
- Repeated unchanged input reuses verified outputs. Do not force refresh to generate new results. `--limit` is only a partial smoke test and must be identified as such.
- AI hypotheses are drafts. Never adopt a strategy or claim performance without a preregistered comparison and unused evaluation period.

Report the paths of candidate, retrospective and outcome CSVs, counts, actual data grade and remaining gaps. Do not alter job schedules or activate external notifications unless requested.
