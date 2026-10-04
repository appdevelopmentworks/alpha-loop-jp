"""Hermes operation boundary: collect, screen, evaluate old runs, retrospective, local AI."""
from __future__ import annotations

import math
import os
import uuid
from contextlib import ExitStack
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from .common import JST, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .csv_input import derive_ranking
from .market import collect
from .operations import _daily_lock
from .pipeline import _code_hash, _verified_manifest, _verified_snapshot, evaluate_run, run
from .provenance import archive_code
from .provider import FileProvider
from .reporting import LocalQwen, NoHypothesisEvidence, qwen_report, qwen_retrospective_report
from .retrospective import retrospective
from .runtime import heartbeat, owner, reconcile

VERSION = "hermes-operation-v3"


def load_config(path: Path) -> dict:
    config = read_json(path)
    if config["schema_version"] != 1 or config["provider"] not in ("yfinance", "file"):
        raise ValueError("unsupported Hermes operation config")
    if config["cloud_enabled"] or config["orders_enabled"]:
        raise ValueError("external AI and orders must remain disabled")
    if config.get("weekly_research_enabled", False) not in (True, False):
        raise ValueError("weekly_research_enabled must be boolean")
    if config["provider"] == "yfinance":
        if config["purpose"] != "personal_research" or config["universe_basis"] != "latest_jpx_month_end_membership":
            raise ValueError("explicit research purpose and universe basis required")
        if not config["markets"] or len(set(config["markets"])) != len(config["markets"]) or any(m not in ("Prime", "Standard", "Growth") for m in config["markets"]):
            raise ValueError("explicit unique target markets required")
        if not 120 <= config["history_calendar_days"] <= 730 or not 1 <= config["download_workers"] <= 2 or not 20 <= config["session_ready_hour_jst"] <= 23:
            raise ValueError("invalid market collection bounds")
    return config


def _future(root: Path, run_id: str, data: dict, session: str, directory: Path) -> Path:
    previous = _verified_manifest(root / "outputs" / run_id)
    snapshot = _verified_snapshot(root, previous["snapshot_id"])
    prior_session = previous["target_session"]
    by_key = {(b["instrument_id"], b["session_date"]): b for b in data["bars"]}
    saved_bars = {(b["instrument_id"], b["session_date"]): b for b in snapshot["data"]["bars"]}
    events = {}
    for item in data["bars"]:
        if item.get("stock_split_event") and prior_session < item["session_date"] <= session:
            events.setdefault(item["instrument_id"], []).append((item["session_date"], item["stock_split_event"]))
    bars = []
    for row in data["bars"]:
        if row["session_date"] <= prior_session or row["session_date"] > session:
            continue
        factor = 1.0
        for day, split in events.get(row["instrument_id"], []):
            if day <= row["session_date"]:
                factor /= split
        old = saved_bars.get((row["instrument_id"], prior_session))
        current_prior = by_key.get((row["instrument_id"], prior_session))
        # Later corrections without a matching split event must not masquerade as a rebase.
        if not old or not current_prior or not old.get("adj_close") or not current_prior.get("adj_close") or not math.isclose(old["adj_close"] * factor, current_prior["adj_close"], rel_tol=1e-6, abs_tol=.01):
            factor = None
        bars.append({**row, "prior_close_rebase_factor": factor, "execution_status": "unknown"})
    write_json(directory / "future.json", {"calendar": data["calendar"], "bars": bars, "data_grade": data["source"]["data_grade"]})
    return directory


def _prior_runs(root: Path, session: str, grade: str) -> list[str]:
    runs = []
    for path in (root / "outputs").glob("run-*/run_manifest.json"):
        manifest = read_json(path)
        if manifest["run_kind"] != "baseline" or manifest["target_session"] >= session or manifest["data_grade"] != grade:
            continue
        if manifest["code_hash"] != _code_hash():
            continue
        # Once the one-session horizon has been evaluated, reuse that result.
        evaluations = path.parent / "evaluation"
        complete = False
        for evaluated in evaluations.glob("*/outcomes.json"):
            evaluation_manifest = read_json(evaluated.with_name("evaluation_manifest.json"))
            for name, hash_ in evaluation_manifest["artifacts"].items():
                if digest((evaluated.parent / name).read_bytes()) != hash_:
                    raise RuntimeError("previous evaluation artifact hash mismatch")
            outcomes = read_json(evaluated)
            if outcomes and all(o["outcome_status"] != "horizon_incomplete" for o in outcomes):
                complete = True
                break
        if complete:
            continue
        runs.append(manifest["run_id"])
    return sorted(runs)


def _qwen(root: Path, config: dict, run_id: str, study_id: str | None, learning_feedback=None, skip_hypotheses=False) -> dict:
    from .model_session import local_model
    if config.get('self_improvement_config'):
        from .self_improvement import learning_facts, protected
        m = _verified_manifest(root / 'outputs' / run_id)
        snap = _verified_snapshot(root, m['snapshot_id'])
        session = m['target_session']
        preceding = snap['data']['calendar'][snap['data']['calendar'].index(session) - 1]
        skip_hypotheses = skip_hypotheses or protected(root, session) or protected(root, preceding)
        if learning_feedback is None and not skip_hypotheses:
            learning_feedback = learning_facts(root, session)
    provider = LocalQwen("http://127.0.0.1:8000", config["qwen_model_id"], config["qwen_model_revision"],
                         read_json(root / config["qwen_runtime_config"]))
    # Daily text and hypothesis responses remain optional side outputs.
    with local_model(root, "qwen", 1200):
        result = {"daily_report": qwen_report(root, run_id, provider)}
        if study_id and not skip_hypotheses:
            try:
                result["hypotheses"] = qwen_retrospective_report(root, study_id, provider, learning_feedback, analysis_required=True)
            except NoHypothesisEvidence as error:
                result["hypotheses_status"] = "NOT_GENERATED"
                result["hypotheses_reason"] = str(error)
        return result


def market_summary(data: dict, rows: list[dict], session: str, threshold: float, markets: list[str]) -> list[dict]:
    index = data["calendar"].index(session)
    previous = data["calendar"][index - 1] if index else None
    by_key = {(b["instrument_id"], b["session_date"]): b for b in data["bars"]}
    by_id = {r["instrument_id"]: r for r in rows}
    result = []
    for market in markets:
        instruments = [i for i in data["instruments"] if i["market"] == market]
        known = gainers = 0
        for item in instruments:
            a, b = (by_key.get((item["instrument_id"], day)) for day in (previous, session))
            if a and b and a["bar_status"] == b["bar_status"] == "ok":
                known += 1
                gainers += Decimal(str(b["adj_close"])) / Decimal(str(a["adj_close"])) - 1 >= Decimal(str(threshold))
        result.append({"market": market, "universe_count": len(instruments),
                       "candidate_count": sum(by_id[i["instrument_id"]]["selected"] for i in instruments),
                       "known_price_pairs": known, "unknown_price_pairs": len(instruments) - known,
                       "gainers_close_threshold": gainers, "return_threshold": threshold,
                       "rate_among_known_prices": gainers / known if known else None})
    return result


def operate(root: Path, config_path: Path, session: str | None = None, limit: int | None = None,
            refresh: bool = False, no_ai: bool = False, retry_ai: bool = False) -> dict:
    attempt_id = uuid.uuid4().hex
    attempt_path = root / "data" / "operations" / "service_attempts" / (attempt_id + ".json")
    attempt = {"attempt_id": attempt_id, **owner(), "started_at": datetime.now(JST).isoformat(), "status": "RUNNING",
               "phase": "WAITING_LOCK", "supervision_id": os.environ.get("ALPHA_LOOP_SUPERVISION_ID"),
               "supervision_trial": int(os.environ.get("ALPHA_LOOP_SUPERVISION_TRIAL", "0")),
               "config_hash": digest(config_path.read_bytes())}
    write_json(attempt_path, attempt)
    resources = ExitStack()
    try:
        resources.enter_context(heartbeat(root / "data/operations/heartbeats" / (attempt_id + ".json")))
        config = load_config(config_path)
        if os.environ.get("ALPHA_LOOP_OPERATION_CONFIG_HASH") and attempt["config_hash"] != os.environ["ALPHA_LOOP_OPERATION_CONFIG_HASH"]:
            raise RuntimeError("operation config changed during supervision")
        if attempt["supervision_id"]:
            session = session or os.environ.get("ALPHA_LOOP_PINNED_SESSION")
        if limit is not None and limit < 1:
            raise ValueError("positive collection limit required")
        if retry_ai and (no_ai or not config["qwen_enabled"]):
            raise ValueError("AI retry requires enabled local Qwen")
        with _daily_lock(root):
            reconcile(root)
            resume_id = os.environ.get("ALPHA_LOOP_RESUME_ATTEMPT") if attempt["supervision_id"] else None
            resumed = read_json(root / "data/operations/service_attempts" / (resume_id + ".json")) if resume_id else None
            if resumed and (resumed.get("supervision_id") != attempt["supervision_id"] or resumed["config_hash"] != attempt["config_hash"]):
                raise ValueError("resume attempt ownership/config mismatch")
            if resumed and resumed.get("input_dir"):
                input_dir, session = Path(resumed["input_dir"]), resumed["session"]
                if resumed.get("input_hashes") and any(digest((input_dir / (name + ".json")).read_bytes()) != hash_ for name, hash_ in resumed["input_hashes"].items()):
                    raise RuntimeError("sealed resume input changed")
                if config["provider"] == "yfinance":
                    dataset = read_json(input_dir / "manifest.json")
                    if dataset["session"] != session:
                        raise RuntimeError("resume dataset session mismatch")
                    for name, hash_ in dataset["artifacts"].items():
                        if digest((input_dir / name).read_bytes()) != hash_:
                            raise RuntimeError("resume dataset artifact hash mismatch")
                attempt["resumed_from"] = resume_id
            elif config["provider"] == "yfinance":
                attempt["phase"] = "COLLECTING"
                write_json(attempt_path, attempt)
                def bind_progress(progress):
                    attempt.update(session=progress["target_session"], request_id=progress["request_id"])
                    write_json(attempt_path, attempt)
                dataset = collect(root, config, session, limit, refresh, on_progress=bind_progress)
                input_dir, session = Path(dataset["input_dir"]), dataset["session"]
            else:
                input_dir = root / config["input_dir"]
                supplied, _ = FileProvider(input_dir).load()
                session = session or max(b["session_date"] for b in supplied["bars"])
                if supplied["source"]["data_grade"] != "synthetic" and session != parse_time(now_iso()).astimezone(JST).date().isoformat():
                    raise ValueError("file input session is stale; explicitly use a historical research workflow")
            strategy = root / config["strategy_config"]
            if os.environ.get("ALPHA_LOOP_STRATEGY_CONFIG_HASH") and digest(strategy.read_bytes()) != os.environ["ALPHA_LOOP_STRATEGY_CONFIG_HASH"]:
                raise RuntimeError("strategy config changed during supervision")
            attempt.update(phase="SCREENING", session=session, input_dir=str(input_dir))
            write_json(attempt_path, attempt)
            # Preserve a supplied synthetic cutoff; real sources use the actual seal time.
            data, _ = FileProvider(input_dir).load()
            attempt["input_hashes"] = {name: digest((input_dir / (name + ".json")).read_bytes()) for name in
                                       ("calendar", "instruments", "bars", "source", "disclosures")}
            write_json(attempt_path, attempt)
            as_of = session + "T20:00:00+09:00" if data["source"]["data_grade"] == "synthetic" else now_iso()
            from .material_operations import import_pending, analyze_pending
            material_config = config.get("materials_config")
            attempt["phase"] = "MATERIAL_IMPORT"
            write_json(attempt_path, attempt)
            try:
                material_import = import_pending(root, material_config)
            except Exception as error:
                material_import = {"status": "FAILED", "error": str(error)[:500]}
            # Actual real cutoff follows original observation/import, never a supplied historical timestamp.
            if data["source"]["data_grade"] != "synthetic":
                as_of = now_iso()
            optional_hashes = {}
            loop_config = config.get('self_improvement_config')
            if loop_config:
                from .self_improvement import settings
                if settings(root / loop_config)['mode'] != 'shadow':
                    raise ValueError('native operations cannot use simulation policy')
                optional_hashes[loop_config] = digest((root / loop_config).read_bytes())
            if material_config and (root / material_config).exists():
                optional_hashes[material_config] = digest((root / material_config).read_bytes())
                try:
                    from .material_operations import load_settings
                    settings = load_settings(root, material_config)
                    for name in ("question_set", "runtime_config"):
                        if settings.get(name):
                            optional_hashes[settings[name]] = digest((root / settings[name]).read_bytes())
                except Exception as error:
                    material_import = {"status": "FAILED", "error": str(error)[:500]}
            if resumed and "optional_config_hashes" in resumed and resumed["optional_config_hashes"] != optional_hashes:
                raise RuntimeError("material configuration changed during supervision")
            attempt["optional_config_hashes"] = optional_hashes
            write_json(attempt_path, attempt)
            service_key = stable_id({name: digest((input_dir / (name + ".json")).read_bytes()) for name in
                                    ("calendar", "instruments", "bars", "source", "disclosures")},
                                   digest(strategy.read_bytes()), config, optional_hashes, session, VERSION, archive_code(root))
            job_path = root / "data" / "operations" / "service_jobs" / (service_key + ".json")
            prior = read_json(job_path) if job_path.exists() else None
            checkpoint_path = root / "data/operations/service_checkpoints" / (service_key + ".json")
            checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else None
            if not checkpoint:
                checkpoint = {"requested_as_of": as_of, "recorded_at": now_iso()}
                write_json(checkpoint_path, checkpoint)
            manifest = _verified_manifest(root / "outputs" / prior["run_id"]) if prior else _verified_manifest(root / "outputs" / checkpoint["run_id"]) if checkpoint.get("run_id") else run(root, input_dir, strategy, session, checkpoint["requested_as_of"])
            run_id = manifest["run_id"]
            _verified_snapshot(root, manifest["snapshot_id"])
            if checkpoint.get("run_id") and digest((root / "outputs" / run_id / "run_manifest.json").read_bytes()) != checkpoint["manifest_hash"]:
                raise RuntimeError("service checkpoint manifest hash mismatch")
            if not checkpoint.get("run_id"):
                write_json(checkpoint_path, {**checkpoint, "run_id": run_id, "manifest_hash": digest((root / "outputs" / run_id / "run_manifest.json").read_bytes()),
                                            "sealed_as_of": manifest["as_of"], "recorded_at": now_iso()})
            attempt.update(run_id=run_id, service_key=service_key)
            write_json(attempt_path, attempt)
            result = {"run_id": run_id, "session": session, "data_grade": manifest["data_grade"],
                      "history_only": manifest["history_only"], "prediction_eligible": False,
                      "candidate_count": manifest["candidate_count"], "candidate_csv": str(root / "outputs" / run_id / "candidates.csv"),
                      "input_dir": str(input_dir), "first_recorded": prior is None, "evaluations": [],
                      "universe_basis": data["source"].get("universe_basis", "file_declared"),
                      "missing_target_count": data["source"].get("missing_target_count", 0)}
            # Native non-PIT research inputs are intentionally excluded from prediction scoring.
            if prior:
                result = {**prior, "first_recorded": False}
                _verified_manifest(root / "outputs" / run_id)
                _verified_snapshot(root, manifest["snapshot_id"])
                for path, hash_ in result["service_artifacts"].items():
                    if digest(Path(path).read_bytes()) != hash_:
                        raise RuntimeError("service artifact hash mismatch")
            else:
                attempt.update(phase="EVALUATING", run_id=run_id)
                write_json(attempt_path, attempt)
                for earlier in _prior_runs(root, session, manifest["data_grade"]):
                    future_dir = root / "data" / "operations" / "evaluation_inputs" / service_key / earlier
                    future = _future(root, earlier, data, session, future_dir)
                    evaluation = evaluate_run(root, earlier, future, session)
                    result["evaluations"].append({"source_run_id": earlier, "outcomes_csv": str(Path(evaluation["path"]) / "outcomes.csv"),
                                                 "evaluation_id": evaluation['evaluation_id'], 'future_ref': str((future / 'future.json').relative_to(root))})
                ranking = derive_ranking(root, input_dir, session, config["ranking_return_min"], config["ranking_limit"])
                result["ranking_csv"] = str(Path(ranking["ranking_input"]) / "ranking.csv")
                result["ranking_count"] = ranking["ranking_count"]
                attempt["phase"] = "RETROSPECTIVE"
                write_json(attempt_path, attempt)
                study = retrospective(root, Path(ranking["ranking_input"]), input_dir, strategy)
                result["study_id"] = study["study_id"]
                result["retrospective_csv"] = str(Path(study["output_path"]) / "ranked_prior.csv")
                rows = read_json(root / "outputs" / run_id / "decisions.json")
                summaries = market_summary(data, rows, session, config["ranking_return_min"], config.get("markets", ["Standard", "Growth", "Prime"]))
                summary_path = root / "outputs" / run_id / "market_summary.csv"
                write_csv(summary_path, list(summaries[0]), summaries)
                result["market_summary_csv"] = str(summary_path)
                attempt["phase"] = "LOCAL_MATERIALS"
                write_json(attempt_path, attempt)
                try:
                    result["materials"] = analyze_pending(root, run_id, material_config, no_ai)
                    if result["materials"].get("documents_path"):
                        from .material_experiments import preview
                        result["material_experiments"] = preview(root, run_id, Path(result["materials"]["documents_path"]))
                except Exception as error:
                    result["materials"] = {"status": "FAILED", "error": str(error)[:500]}
                result["material_import"] = material_import
                learning_feedback, skip_hypotheses = None, False
                if loop_config:
                    try:
                        from .self_improvement import feedback, learning_facts, protected
                        entries = [{'run_id': e['source_run_id'], 'evaluation_id': e.get('evaluation_id', Path(e['outcomes_csv']).parent.name),
                                    'future_ref': e['future_ref']} for e in result['evaluations'] if e.get('future_ref')]
                        feedback(root, entries, session)
                        skip_hypotheses = protected(root, session) or protected(root, data['calendar'][data['calendar'].index(session) - 1])
                        learning_feedback = learning_facts(root, session)
                    except Exception as error:
                        result['self_improvement'] = {'status': 'FAILED', 'error': str(error)[:500]}
                if config["qwen_enabled"] and not no_ai:
                    attempt["phase"] = "LOCAL_QWEN"
                    write_json(attempt_path, attempt)
                    try:
                        result["qwen"] = _qwen(root, config, run_id, study["study_id"], learning_feedback, skip_hypotheses) if loop_config else _qwen(root, config, run_id, study["study_id"])
                        result["qwen_status"] = "SUCCEEDED"
                    except Exception as error:
                        result.update(qwen_status="FAILED", qwen_error=str(error)[:500])
                else:
                    result["qwen_status"] = "DISABLED"
                result["status"] = "SUCCEEDED_WITH_GAPS" if result["missing_target_count"] else "SUCCEEDED"
                if result["qwen_status"] == "FAILED":
                    result["status"] = "SUCCEEDED_WITH_AI_FAILURE"
                files = [result[key] for key in ("candidate_csv", "ranking_csv", "retrospective_csv", "market_summary_csv")]
                files.extend(e["outcomes_csv"] for e in result["evaluations"])
                result["service_artifacts"] = {path: digest(Path(path).read_bytes()) for path in files}
                write_json(job_path, result)
            if prior and config["qwen_enabled"] and not no_ai and (result["qwen_status"] == "DISABLED" or retry_ai and result["qwen_status"] == "FAILED"):
                attempt["phase"] = "LOCAL_QWEN_RETRY"
                write_json(attempt_path, attempt)
                try:
                    result["qwen"] = _qwen(root, config, run_id, result.get("study_id"))
                    result["qwen_status"] = "SUCCEEDED"
                    result.pop("qwen_error", None)
                    result["status"] = "SUCCEEDED_WITH_GAPS" if result["missing_target_count"] else "SUCCEEDED"
                except Exception as error:
                    result.update(qwen_status="FAILED", qwen_error=str(error)[:500])
                result["first_recorded"] = True
                result["ai_retried_at"] = now_iso()
                write_json(job_path, result)
            from .research import refresh as refresh_research
            if prior and retry_ai and material_config:
                attempt["phase"] = "LOCAL_MATERIALS_RETRY"
                write_json(attempt_path, attempt)
                try:
                    result["materials"] = analyze_pending(root, run_id, material_config, no_ai, retry_unavailable=True)
                except Exception as error:
                    result["materials"] = {"status": "FAILED", "error": str(error)[:500]}
            # Auxiliary results do not participate in the frozen next-session adoption decision.
            attempt["phase"] = "AUXILIARY_RESULTS"
            write_json(attempt_path, attempt)
            try:
                from .auxiliary import evaluate_auxiliary, completed as auxiliary_completed
                result["auxiliary"] = []
                for path in sorted((root / "outputs").glob("run-*/run_manifest.json")):
                    m = read_json(path)
                    if m["run_kind"] != "baseline" or m["data_grade"] != manifest["data_grade"] or m["target_session"] not in data["calendar"]:
                        continue
                    age = data["calendar"].index(session) - data["calendar"].index(m["target_session"])
                    if age >= 3 and not auxiliary_completed(root, m["run_id"]):
                        result["auxiliary"].append(evaluate_auxiliary(root, m["run_id"], input_dir, session))
            except Exception as error:
                result["auxiliary_status"] = "FAILED"
                result["auxiliary_error"] = str(error)[:500]
            attempt["phase"] = "M5_REPORTS"
            write_json(attempt_path, attempt)
            # M5 is a separate research side output; failure must preserve numerical CSVs.
            try:
                result["m5"] = refresh_research(root)
            except Exception as error:
                result["m5"] = {"status": "FAILED", "error": str(error)[:500], "experiments": []}
            if result["m5"]["status"] == "FAILED" and result["qwen_status"] != "FAILED":
                result["status"] = "SUCCEEDED_WITH_M5_FAILURE"
            elif result["status"] == "SUCCEEDED_WITH_M5_FAILURE":
                result["status"] = "SUCCEEDED_WITH_AI_FAILURE" if result["qwen_status"] == "FAILED" else "SUCCEEDED_WITH_GAPS" if result["missing_target_count"] else "SUCCEEDED"
            try:
                from .material_research import refresh as refresh_material_research
                result["material_m5"] = refresh_material_research(root, run_id)
            except Exception as error:
                result["material_m5"] = {"status": "FAILED", "error": str(error)[:500], "experiments": []}
            write_json(job_path, result)
            if loop_config:
                attempt['phase'] = 'SELF_IMPROVEMENT'
                write_json(attempt_path, attempt)
                try:
                    from .self_improvement import day as hypothesis_day, feedback, monitor, promote_ready
                    entries = [{'run_id': e['source_run_id'], 'evaluation_id': e.get('evaluation_id', Path(e['outcomes_csv']).parent.name),
                                'future_ref': e['future_ref']} for e in result['evaluations'] if e.get('future_ref')]
                    feedback(root, entries, session)
                    # On AI retry the first sealed forecast is reused; never replace its selections.
                    result['self_improvement_monitor'] = monitor(root, root / loop_config, session)
                    result['self_improvement_promotion'] = promote_ready(root, root / loop_config)
                    report = result.get('qwen', {}).get('hypotheses', {}).get('report_path') if result.get('qwen_status') == 'SUCCEEDED' else None
                    result['self_improvement'] = hypothesis_day(root, run_id, root / loop_config, Path(report) if report else None)
                    result['operational_candidate_csv'] = result['self_improvement']['operational_candidate_csv']
                except Exception as error:
                    result['self_improvement'] = {'status': 'FAILED', 'error': str(error)[:500]}
            if config.get("weekly_research_enabled", False):
                attempt["phase"] = "WEEKLY_DRAFTS"
                write_json(attempt_path, attempt)
                try:
                    from .research_workflow import weekly
                    result["weekly"] = weekly(root, session)
                except Exception as error:
                    result["weekly"] = {"status": "FAILED", "error": str(error)[:500]}
            side_failures = [name for name in ("materials", "material_import", "weekly", "material_m5", "self_improvement") if result.get(name, {}).get("status") == "FAILED"]
            if result.get("auxiliary_status") == "FAILED":
                side_failures.append("auxiliary")
            result["side_failures"] = side_failures
            if side_failures and result["qwen_status"] != "FAILED" and result["m5"]["status"] != "FAILED":
                result["status"] = "SUCCEEDED_WITH_SIDE_FAILURE"
            elif not side_failures and result['status'] == 'SUCCEEDED_WITH_SIDE_FAILURE':
                result['status'] = 'SUCCEEDED_WITH_AI_FAILURE' if result['qwen_status'] == 'FAILED' else 'SUCCEEDED_WITH_M5_FAILURE' if result['m5']['status'] == 'FAILED' else 'SUCCEEDED_WITH_GAPS' if result['missing_target_count'] else 'SUCCEEDED'
            write_json(job_path, result)
            write_json(root / "data" / "operations" / "latest_service.json", result)
            attempt.update(phase="COMPLETE", result=result, status=result["status"], completed_at=now_iso())
            write_json(attempt_path, attempt)
            return result
    except Exception as error:
        attempt.update(status="FAILED", error=str(error), error_type=type(error).__name__, completed_at=now_iso())
        write_json(attempt_path, attempt)
        raise
    finally:
        resources.close()
