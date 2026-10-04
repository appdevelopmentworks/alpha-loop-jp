from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fixture import create
from .operations import daily, doctor
from .models import model_status, switch_model
from .reporting import LocalQwen, qwen_report
from .pipeline import add_hypothesis, evaluate_run, list_hypotheses, replay, run
from .semantic import MockSemanticProvider, OpenJevProvider, import_document, shadow
from .common import now_iso, read_json, stable_id, write_json
from .retrospective import create_ranking_fixture, retrospective, replay_retrospective, register_hypothesis
from .reporting import qwen_retrospective_report
from .csv_input import create_csv_fixture, derive_ranking, import_csv
from .service import operate
from .research import register as register_experiment, compare as compare_experiment, prepare_batch, refresh as refresh_research, draft_from_qwen
from .research_fixture import create_research_fixture
from .runtime import status as operation_status


def main() -> None:
    p = argparse.ArgumentParser(prog="alpha-loop")
    p.add_argument("--root", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("operation-status")
    sub.add_parser('self-improve-status')
    sd = sub.add_parser('self-improve-day')
    sd.add_argument('--run-id', required=True)
    sd.add_argument('--policy', type=Path, default=Path('configs/self_improvement.json'))
    sd.add_argument('--report', type=Path)
    sf = sub.add_parser('self-improve-feedback')
    sf.add_argument('--batch', type=Path, required=True)
    sf.add_argument('--session', required=True)
    sa = sub.add_parser('self-improve-activate')
    sa.add_argument('--experiment-id', required=True)
    sa.add_argument('--policy', type=Path, default=Path('configs/self_improvement.json'))
    sa.add_argument('--reviewer', required=True)
    sa.add_argument('--human-reviewed', action='store_true')
    sm = sub.add_parser('self-improve-monitor')
    sm.add_argument('--session', required=True)
    sm.add_argument('--policy', type=Path, default=Path('configs/self_improvement.json'))
    sb = sub.add_parser('self-improve-rollback')
    sb.add_argument('--reason', required=True)
    sb.add_argument('--policy', type=Path, default=Path('configs/self_improvement.json'))
    mm = sub.add_parser("m5-material-demo")
    mm.add_argument("--out", type=Path, required=True)
    mm.add_argument("--baseline-config", type=Path, default=Path("configs/baseline.json"))
    mm.add_argument("--question-set", type=Path, default=Path("configs/questions_v1.json"))
    mmr = sub.add_parser("m5-material-register")
    mmr.add_argument("--plan", type=Path, required=True)
    mmb = sub.add_parser("m5-material-batch")
    mmb.add_argument("--experiment-id", required=True)
    mmc = sub.add_parser("m5-material-compare")
    mmc.add_argument("--experiment-id", required=True)
    mmc.add_argument("--batch", type=Path)
    sub.add_parser("m5-material-refresh")
    mmv = sub.add_parser("m5-material-review")
    mmv.add_argument("--experiment-id", required=True)
    mmv.add_argument("--arm", choices=("B1", "B2"), required=True)
    mmv.add_argument("--decision", choices=("HOLD", "REJECT", "APPROVE_SHADOW"), required=True)
    mmv.add_argument("--reviewer", required=True)
    mmv.add_argument("--reason", required=True)
    mmv.add_argument("--human-reviewed", action="store_true")
    di = sub.add_parser("disclosures-import")
    di.add_argument("--manifest", type=Path, required=True)
    mat = sub.add_parser("materials-batch")
    mat.add_argument("--run-id", required=True)
    mat.add_argument("--materials-config", default="configs/materials_operations.json")
    mat.add_argument("--no-ai", action="store_true")
    mat.add_argument("--retry-unavailable", action="store_true")
    gold = sub.add_parser("materials-labels-register")
    gold.add_argument("--labels", type=Path, required=True)
    gold.add_argument("--question-set", type=Path, default=Path("configs/questions_v1.json"))
    quality = sub.add_parser("materials-quality")
    quality.add_argument("--gold-id", required=True)
    quality.add_argument("--documents", type=Path, required=True)
    preview = sub.add_parser("materials-preview")
    preview.add_argument("--run-id", required=True)
    preview.add_argument("--documents", type=Path, required=True)
    aux = sub.add_parser("auxiliary-evaluate")
    aux.add_argument("--run-id", required=True)
    aux.add_argument("--input", type=Path, required=True)
    aux.add_argument("--through", required=True)
    week = sub.add_parser("m5-weekly")
    week.add_argument("--session", required=True)
    week.add_argument("--preview", action="store_true")
    review = sub.add_parser("m5-review")
    review.add_argument("--experiment-id", required=True)
    review.add_argument("--decision", choices=("HOLD", "REJECT", "APPROVE_SHADOW"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--human-reviewed", action="store_true")
    mf = sub.add_parser("m5-fixture")
    mf.add_argument("--out", type=Path, default=Path("demo-m5"))
    mf.add_argument("--baseline-config", type=Path, default=Path("configs/baseline.json"))
    mp = sub.add_parser("m5-plan")
    mp.add_argument("--qwen-report", type=Path, required=True)
    mp.add_argument("--calendar", type=Path, required=True)
    mp.add_argument("--out", type=Path, default=Path("configs/m5_drafts"))
    mp.add_argument("--baseline-config", type=Path, default=Path("configs/baseline.json"))
    mp.add_argument("--tuning-days", type=int, default=10)
    mp.add_argument("--holdout-days", type=int, default=30)
    mr = sub.add_parser("m5-register")
    mr.add_argument("--plan", type=Path, required=True)
    mb = sub.add_parser("m5-batch")
    mb.add_argument("--experiment-id", required=True)
    mc = sub.add_parser("m5-compare")
    mc.add_argument("--experiment-id", required=True)
    mc.add_argument("--batch", type=Path, required=True)
    sub.add_parser("m5-refresh")
    f = sub.add_parser("fixture")
    f.add_argument("--out", type=Path, default=Path("demo-input"))
    f.add_argument("--without-turnover", action="store_true")
    rf = sub.add_parser("ranking-fixture")
    rf.add_argument("--out", type=Path, default=Path("demo-ranking-input"))
    cf = sub.add_parser("csv-fixture")
    cf.add_argument("--out", type=Path, default=Path("demo-csv-input"))
    cf.add_argument("--without-turnover", action="store_true")
    ci = sub.add_parser("csv-import")
    ci.add_argument("--spec", type=Path, required=True)
    ci.add_argument("--session", required=True)
    dr = sub.add_parser("derive-ranking")
    dr.add_argument("--input", type=Path, required=True)
    dr.add_argument("--session", required=True)
    dr.add_argument("--return-min", type=float, default=.10)
    dr.add_argument("--limit", type=int, default=50)
    rs = sub.add_parser("retrospective")
    rs.add_argument("--ranking-input", type=Path, required=True)
    prior = rs.add_mutually_exclusive_group(required=True)
    prior.add_argument("--input", type=Path)
    prior.add_argument("--prior-run-id")
    rs.add_argument("--config", type=Path, default=Path("configs/baseline.json"))
    rs.add_argument("--feature-as-of")
    rr = sub.add_parser("retrospective-replay")
    rr.add_argument("--study-id", required=True)
    rh = sub.add_parser("retrospective-register")
    rh.add_argument("--study-id", required=True)
    rh.add_argument("--period-start", required=True)
    rh.add_argument("--period-end", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--input", type=Path, default=Path("incoming/current"))
    d.add_argument("--save", action="store_true")
    day = sub.add_parser("daily")
    day.add_argument("--input", type=Path, default=Path("incoming/current"))
    day.add_argument("--config", type=Path, default=Path("configs/baseline.json"))
    day.add_argument("--session", required=True)
    day.add_argument("--as-of", required=True)
    day.add_argument("--evaluate-run-id")
    day.add_argument("--through")
    operation = sub.add_parser("operate")
    operation.add_argument("--operation-config", type=Path, default=Path("configs/hermes_operations.json"))
    operation.add_argument("--session")
    operation.add_argument("--limit", type=int)
    operation.add_argument("--refresh", action="store_true")
    operation.add_argument("--no-ai", action="store_true")
    operation.add_argument("--retry-ai", action="store_true")
    sub.add_parser("model-status")
    sw = sub.add_parser("model-switch")
    sw.add_argument("--to", choices=("openjev", "qwen"), required=True)
    sw.add_argument("--timeout-seconds", type=float, default=1200)
    r = sub.add_parser("run")
    r.add_argument("--input", type=Path, default=Path("demo-input"))
    r.add_argument("--config", type=Path, default=Path("configs/baseline.json"))
    r.add_argument("--session", required=True)
    r.add_argument("--as-of", required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("--run-id", required=True)
    ev = sub.add_parser("evaluate")
    ev.add_argument("--run-id", required=True)
    ev.add_argument("--future", type=Path, default=Path("demo-input"))
    ev.add_argument("--through", required=True)
    h = sub.add_parser("hypothesis-add")
    for name in ("id", "thesis", "required-data", "comparison", "period-start", "period-end"):
        h.add_argument("--" + name, required=True)
    sub.add_parser("hypothesis-list")
    doc = sub.add_parser("semantic-import")
    doc.add_argument("--file", type=Path, required=True)
    doc.add_argument("--instrument-id", required=True)
    doc.add_argument("--disclosure-id", required=True)
    doc.add_argument("--revision-id", required=True)
    doc.add_argument("--published-at", required=True)
    doc.add_argument("--synthetic-first-seen-at")
    sem = sub.add_parser("semantic-shadow")
    sem.add_argument("--run-id", required=True)
    sem.add_argument("--document-id", required=True)
    sem.add_argument("--provider", choices=("mock", "openjev"), required=True)
    sem.add_argument("--mock-response", type=Path)
    sem.add_argument("--question-set", type=Path, default=Path("configs/questions_v1.json"))
    sem.add_argument("--evidence-id", action="append", default=[])
    sem.add_argument("--base-url", default="http://127.0.0.1:8080")
    sem.add_argument("--model-id", default="openjev-0.1")
    sem.add_argument("--model-revision")
    sem.add_argument("--server-commit")
    sem.add_argument("--quantization-id")
    sem.add_argument("--runtime-config", type=Path)
    sem.add_argument("--timeout-seconds", type=float, default=120)
    qr = sub.add_parser("qwen-report")
    qr.add_argument("--run-id", required=True)
    qr.add_argument("--runtime-config", type=Path, required=True)
    qr.add_argument("--model-id", required=True)
    qr.add_argument("--model-revision", required=True)
    qr.add_argument("--base-url", default="http://127.0.0.1:8000")
    qr.add_argument("--timeout-seconds", type=float, default=300)
    sr = sub.add_parser("retrospective-report")
    sr.add_argument("--study-id", required=True)
    sr.add_argument("--runtime-config", type=Path, required=True)
    sr.add_argument("--model-id", required=True)
    sr.add_argument("--model-revision", required=True)
    sr.add_argument("--base-url", default="http://127.0.0.1:8000")
    sr.add_argument("--timeout-seconds", type=float, default=300)
    args = p.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "operation-status":
            result = operation_status(root)
        elif args.command.startswith('self-improve-'):
            from . import self_improvement as loop
            if args.command == 'self-improve-status':
                result = loop.status(root)
            elif args.command == 'self-improve-day':
                result = loop.day(root, args.run_id, root / args.policy, root / args.report if args.report else None)
            elif args.command == 'self-improve-feedback':
                result = {'feedback': loop.feedback(root, read_json(root / args.batch)['entries'], args.session)}
            elif args.command == 'self-improve-activate':
                result = loop.activate(root, args.experiment_id, root / args.policy, reviewer=args.reviewer, human_reviewed=args.human_reviewed)
            elif args.command == 'self-improve-monitor':
                result = loop.monitor(root, root / args.policy, args.session)
            else:
                result = loop.rollback(root, root / args.policy, args.reason)
        elif args.command == "m5-material-demo":
            from .material_research_fixture import demo
            result = demo(root / args.out, root / args.baseline_config, root / args.question_set)
        elif args.command == "m5-material-register":
            from .material_research import register
            result = register(root, root / args.plan)
        elif args.command == "m5-material-batch":
            from .material_research import prepare_batch
            result = {"batch_path": str(prepare_batch(root, args.experiment_id))}
        elif args.command == "m5-material-compare":
            from .material_research import compare
            result = compare(root, args.experiment_id, root / args.batch if args.batch else None)
        elif args.command == "m5-material-refresh":
            from .material_research import refresh
            result = refresh(root)
        elif args.command == "m5-material-review":
            from .material_research import review
            result = review(root, args.experiment_id, args.arm, args.decision, args.reviewer, args.reason, args.human_reviewed)
        elif args.command == "disclosures-import":
            from .materials import ingest
            result = ingest(root, root / args.manifest)
        elif args.command == "materials-batch":
            from .material_operations import analyze_pending
            result = analyze_pending(root, args.run_id, args.materials_config, args.no_ai, args.retry_unavailable)
        elif args.command == "materials-labels-register":
            from .material_quality import register_labels
            result = register_labels(root, root / args.labels, root / args.question_set)
        elif args.command == "materials-quality":
            from .material_quality import evaluate_quality
            result = evaluate_quality(root, args.gold_id, root / args.documents)
        elif args.command == "materials-preview":
            from .material_experiments import preview
            result = preview(root, args.run_id, root / args.documents)
        elif args.command == "auxiliary-evaluate":
            from .auxiliary import evaluate_auxiliary
            result = evaluate_auxiliary(root, args.run_id, root / args.input, args.through)
        elif args.command == "m5-weekly":
            from .research_workflow import weekly
            result = weekly(root, args.session, force_draft=args.preview)
        elif args.command == "m5-review":
            from .research_workflow import record_review
            result = record_review(root, args.experiment_id, args.decision, args.reviewer, args.reason, args.human_reviewed)
        elif args.command == "m5-fixture":
            result = create_research_fixture(root / args.out, root / args.baseline_config)
        elif args.command == "m5-plan":
            result = draft_from_qwen(root, root / args.qwen_report, root / args.calendar, root / args.out,
                                     root / args.baseline_config, args.tuning_days, args.holdout_days)
        elif args.command == "m5-register":
            result = register_experiment(root, root / args.plan)
        elif args.command == "m5-batch":
            result = {"batch_path": str(prepare_batch(root, args.experiment_id))}
        elif args.command == "m5-compare":
            result = compare_experiment(root, args.experiment_id, root / args.batch)
        elif args.command == "m5-refresh":
            result = refresh_research(root)
        elif args.command == "fixture":
            result = create(root / args.out, without_turnover=args.without_turnover)
        elif args.command == "ranking-fixture":
            result = create_ranking_fixture(root / args.out)
        elif args.command == "csv-fixture":
            result = create_csv_fixture(root / args.out, without_turnover=args.without_turnover)
        elif args.command == "csv-import":
            result = import_csv(root, root / args.spec, args.session)
        elif args.command == "derive-ranking":
            result = derive_ranking(root, root / args.input, args.session, args.return_min, args.limit)
        elif args.command == "retrospective":
            result = retrospective(root, root / args.ranking_input,
                                   root / args.input if args.input else None, root / args.config,
                                   args.prior_run_id, args.feature_as_of)
        elif args.command == "retrospective-replay":
            result = replay_retrospective(root, args.study_id)
        elif args.command == "retrospective-register":
            result = register_hypothesis(root, args.study_id, args.period_start, args.period_end)
        elif args.command == "doctor":
            result = doctor(root, root / args.input)
            if args.save:
                path = root / "data" / "diagnostics" / f"doctor-{stable_id(now_iso(), result)}.json"
                write_json(path, result)
                result["saved_to"] = str(path)
        elif args.command == "daily":
            result = daily(root, root / args.input, root / args.config, args.session,
                           args.as_of, args.evaluate_run_id, args.through)
        elif args.command == "operate":
            result = operate(root, root / args.operation_config, args.session, args.limit, args.refresh, args.no_ai, args.retry_ai)
        elif args.command == "model-status":
            result = model_status()
        elif args.command == "model-switch":
            result = switch_model(root, args.to, args.timeout_seconds)
        elif args.command == "run":
            result = run(root, root / args.input, root / args.config, args.session, args.as_of)
        elif args.command == "replay":
            result = replay(root, args.run_id)
        elif args.command == "evaluate":
            result = evaluate_run(root, args.run_id, root / args.future, args.through)
        elif args.command == "hypothesis-add":
            add_hypothesis(root, args.id, args.thesis, args.required_data, args.comparison,
                           args.period_start, args.period_end)
            result = {"hypothesis_id": args.id, "state": "DRAFT"}
        elif args.command == "semantic-import":
            result = import_document(root, args.file, args.instrument_id, args.disclosure_id,
                                     args.revision_id, args.published_at, args.synthetic_first_seen_at)
        elif args.command == "semantic-shadow":
            if args.provider == "mock":
                if args.mock_response is None:
                    raise ValueError("--mock-response required for mock provider")
                provider = MockSemanticProvider(read_json(args.mock_response))
            else:
                if args.runtime_config is None:
                    raise ValueError("--runtime-config required for OpenJev")
                provider = OpenJevProvider(args.base_url, args.model_id, args.model_revision,
                                           args.server_commit, args.quantization_id,
                                           read_json(root / args.runtime_config), args.timeout_seconds)
            result = shadow(root, args.run_id, args.document_id, provider, root / args.question_set, args.evidence_id)
        elif args.command == "qwen-report":
            provider = LocalQwen(args.base_url, args.model_id, args.model_revision,
                                 read_json(root / args.runtime_config), args.timeout_seconds)
            result = qwen_report(root, args.run_id, provider)
        elif args.command == "retrospective-report":
            provider = LocalQwen(args.base_url, args.model_id, args.model_revision,
                                 read_json(root / args.runtime_config), args.timeout_seconds)
            result = qwen_retrospective_report(root, args.study_id, provider, analysis_required=True)
        else:
            result = list_hypotheses(root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
