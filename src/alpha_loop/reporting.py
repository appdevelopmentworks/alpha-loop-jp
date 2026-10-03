"""Optional local Qwen draft; numerical screening remains sealed Python output."""
from __future__ import annotations

import csv
import json
import urllib.request
import uuid
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from .common import atomic_bytes, canonical, digest, now_iso, read_json, write_json
from .pipeline import _verified_manifest
from .hypothesis_plans import CATALOG, CATALOG_VERSION, REASONS, render_plans, validate_plans
from .provenance import archive_code


PROMPT_VERSION = "daily-notes-ja-v3"
DAILY_NOTES = {
    "synthetic": "合成データによる動作確認です。実市場の成績ではありません。",
    "reconstructed": "実市場から後日取得した履歴の研究です。当時の予測成功を示しません。",
    "unvalidated": "候補条件と仮説の性能は未検証です。",
    "missing_quotes": "取得できない価格や履歴を保留として残しています。",
    "turnover_missing": "売買代金は不明です。流動性による足切りを既定では行いません。",
    "execution_unknown": "約定可否は不明です。実現した値上がりを売買利益と扱いません。",
    "cost_unset": "売買費用は未設定です。純損益は計算していません。",
}


class NoHypothesisEvidence(ValueError):
    pass


def json_content(content: str) -> str:
    """Accept only a whole JSON code fence as a transport envelope, never prose."""
    envelope = re.fullmatch(r"\s*```json\s*\n(\{.*\})\s*\n```\s*", content, re.DOTALL)
    return envelope.group(1) if envelope else content


class LocalQwen:
    def __init__(self, base_url: str, model_id: str, model_revision: str,
                 runtime_config: dict, timeout_seconds: float = 300):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
            raise ValueError("Qwen endpoint must be loopback HTTP")
        if not model_id or "latest" in model_id or not model_revision:
            raise ValueError("pinned Qwen identity required")
        if not isinstance(runtime_config, dict) or not runtime_config.get("runtime_config_version"):
            raise ValueError("versioned Qwen runtime config required")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.model_revision = model_revision
        self.runtime_config = runtime_config
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(self.base_url + path,
                                         data=canonical(body) if body is not None else None,
                                         headers={"Content-Type": "application/json"} if body is not None else {},
                                         method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read(4_000_000))

    def ask(self, facts: dict) -> dict:
        models = self._request("/v1/models")
        if self.model_id not in {item.get("id") for item in models.get("data", [])}:
            raise ValueError("pinned Qwen model absent from /v1/models")
        instruction = '日報の数値と文章はPythonが作ります。与えたavailable_note_codesから確認事項を選び、必須のrequired_note_codesを全て含めてください。重複は不要です。返すのは {"note_codes":["reconstructed","unvalidated","execution_unknown"]} 形式のJSONのみ。説明、銘柄名、数値、自由文は不要です。'
        max_tokens = 512
        if facts.get("report_kind") == "ranking_retrospective":
            max_tokens = 1024
            instruction = '条件カタログから検証するAND条件の仮説を1〜2件選んでください。各仮説の条件IDは1〜3個。しきい値は設計上の仮定であり、性能・因果関係は未検証です。非掲載を非急騰と扱わないでください。同じ特徴の同方向の条件を重ねないでください。返すのは次の形式のJSONのみ。自由文、Markdown、説明、カタログ外の条件は不要です。{"hypotheses":[{"condition_ids":["volume_ge_1_5","near_high_ge_minus_0_05"],"reason_code":"volume_expansion"}]} reason_codeはallowed_reason_codesから選び、各仮説の条件の組合せを変えてください。'
            if facts.get('learning_feedback') is not None:
                instruction += ' learning_feedbackの既存仮説・的中・誤検出・見逃しを参考に次の仮説を選んでください。不明は陰性にせず、的中率と捕捉率を両方考慮し、改善が見込めなければ既存条件を再提案して構いません。'
        return self._request("/v1/chat/completions", {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)}],
            "temperature": 0, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}})


def qwen_report(root: Path, run_id: str, provider: LocalQwen) -> dict:
    output = root / "outputs" / run_id
    manifest = _verified_manifest(output)
    if manifest is None:
        raise FileNotFoundError(run_id)
    with (output / "candidates.csv").open(encoding="utf-8-sig", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    if len(candidates) > 200:
        raise ValueError("too many candidates for bounded Qwen report")
    available = ["unvalidated", "execution_unknown"]
    if manifest["data_grade"] in ("synthetic", "reconstructed"):
        available.insert(0, manifest["data_grade"])
    if manifest["stage_counts"].get("DATA_HOLD"):
        available.append("missing_quotes")
    if any(row["turnover_status"] == "missing" for row in candidates):
        available.append("turnover_missing")
    if manifest["config"]["roundtrip_cost_bps"] is None:
        available.append("cost_unset")
    facts = {"report_kind": "daily_constraints", "available_note_codes": available,
             "required_note_codes": available, "note_catalog": {code: DAILY_NOTES[code] for code in available}}
    if len(canonical(facts)) > 12000:
        raise ValueError("Qwen report facts exceed size limit")
    identity = {"prompt_version": PROMPT_VERSION, "manifest_hash": digest((output / "run_manifest.json").read_bytes()),
                "candidate_hash": digest((output / "candidates.csv").read_bytes()),
                "model_id": provider.model_id, "model_revision": provider.model_revision,
                "runtime_config": provider.runtime_config, "generation": {"temperature": 0, "max_tokens": 512}}
    key = digest(canonical(identity))
    cache = root / "data" / "qwen_cache" / f"{key}.json"
    if cache.exists():
        saved = read_json(cache)
        if saved["identity"] != identity:
            raise RuntimeError("Qwen cache identity mismatch")
        response = saved["response"]
    else:
        response = provider.ask(facts)
    choices = response.get("choices") if isinstance(response, dict) else None
    draft = choices[0].get("message", {}).get("content") if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else None
    if not isinstance(response, dict) or response.get("model") != provider.model_id or not isinstance(draft, str) or not draft.strip() or len(draft) > 8000:
        raise ValueError("invalid Qwen response")
    if choices[0].get("finish_reason") == "length":
        raise ValueError("truncated Qwen response")
    try:
        plan = json.loads(json_content(draft))
    except ValueError as error:
        raise ValueError("Qwen daily response must contain only note-code JSON") from error
    codes = plan.get("note_codes") if isinstance(plan, dict) and set(plan) == {"note_codes"} else None
    if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes) or len(codes) != len(set(codes)) or set(codes) != set(available):
        raise ValueError("invalid Qwen daily note codes")
    selected_counts = dict(sorted(Counter(row["stage"] for row in candidates).items()))
    draft = (f"# 日次研究レポート\n\n対象営業日: {manifest['target_session']} / データ: {manifest['data_grade']}\n\n"
             f"母集団: {manifest['universe_count']} / 選出候補: {manifest['candidate_count']}\n\n"
             "選出候補の内訳: " + ", ".join(f"{key}: {value}" for key, value in selected_counts.items()) + "\n\n"
             + "\n".join("- " + DAILY_NOTES[code] for code in available) + "\n")
    if not cache.exists():
        write_json(cache, {"identity": identity, "facts": facts, "response": response})
    destination = output / "qwen" / f"{key}.json"
    write_json(destination, {"run_id": run_id, "cache_key": key, "model_id": provider.model_id,
                             "model_revision": provider.model_revision, "draft": draft,
                             "selected_stage_counts": selected_counts, "note_codes": codes, "text_rendered_by": "Python",
                             "data_grade": manifest["data_grade"], "history_only": manifest["history_only"],
                             "price_csv_unchanged": True})
    markdown = destination.with_suffix(".md")
    atomic_bytes(markdown, draft.encode("utf-8"))
    return {"run_id": run_id, "report_path": str(destination), "draft_path": str(markdown), "cache_key": key}


def qwen_retrospective_report(root: Path, study_id: str, provider: LocalQwen, learning_feedback: dict | None = None) -> dict:
    from .retrospective import FEATURE_FIELDS, _verify
    output, manifest, _ = _verify(root, study_id)
    rows = read_json(output / "decisions.json")
    ranked = [r for r in rows if r["in_ranking"]]
    comparison = read_json(output / "comparison.json")
    groups = comparison["groups"]
    if not groups["listed"]["feature_valid_eligible_count"] or not groups["not_listed"]["feature_valid_eligible_count"]:
        raise NoHypothesisEvidence("matched and comparison features required for hypothesis plans")
    directions = {}
    for field in FEATURE_FIELDS:
        listed, other = (groups[key]["feature_medians"][field] for key in ("listed", "not_listed"))
        directions[field] = "unknown" if listed is None or other is None else "higher" if listed > other else "lower" if listed < other else "equal"
    facts = {"report_kind": "ranking_retrospective", "study_id": study_id,
             "ranking_session": manifest["ranking_session"], "feature_session": manifest["feature_session"],
             "feature_cutoff": manifest["feature_cutoff"], "data_grade": manifest["data_grade"],
             "availability_basis": manifest["provenance"]["availability_basis"],
             "prediction_score_eligible": False, "material_semantics": "未評価。event_idsは開示メタデータのみ。",
             "comparison_scope": "掲載群と非掲載群の、履歴有効・対象内銘柄の指標中央値。非掲載は陰性ラベルではない。",
             "comparison_directions_listed_vs_not_listed": directions,
             "feature_observed_counts": {key: groups[key]["feature_observed_counts"] for key in ("listed", "not_listed")},
             "liquidity_filter_mode": "required" if any(r["liquidity_filter_status"] in ("enabled", "missing") for r in rows) else "disabled" if any(r["liquidity_filter_status"] == "disabled" for r in rows) else "not_evaluated",
             "limitations": comparison["limitations"], "sample_quality": "少数標本、性能・因果関係は未検証",
             "hypothesis_seed": "前営業日の出来高増加と20日高値への接近が翌営業日の急騰捕捉に役立つか。"}
    facts.update(condition_catalog=CATALOG, catalog_version=CATALOG_VERSION, allowed_reason_codes=list(REASONS))
    if learning_feedback is not None:
        if learning_feedback['cutoff_session'] != manifest['ranking_session']:
            raise ValueError('learning feedback cutoff differs from discovery day')
        facts['learning_feedback'] = learning_feedback
    if len(canonical(facts)) > 12000:
        raise ValueError("retrospective report facts exceed size limit")
    identity = {"prompt_version": "ranking-plan-feedback-ja-v1" if learning_feedback is not None else "ranking-plan-ja-v4", "manifest_hash": digest((output / "study_manifest.json").read_bytes()),
                "code_bundle_id": archive_code(root),
                "facts_hash": digest(canonical(facts)), "model_id": provider.model_id,
                "model_revision": provider.model_revision, "runtime_config": provider.runtime_config,
                "generation": {"temperature": 0, "max_tokens": 1024}}
    key = digest(canonical(identity))
    cache = root / "data" / "qwen_cache" / (key + ".json")
    attempt_path = None
    if cache.exists():
        saved = read_json(cache)
        if saved["identity"] != identity or saved["facts"] != facts:
            raise RuntimeError("retrospective Qwen cache identity mismatch")
        response = saved["response"]
    else:
        attempt_path = root / "data" / "operations" / "qwen_attempts" / (uuid.uuid4().hex + ".json")
        attempt = {"cache_key": key, "identity": identity, "facts": facts, "started_at": now_iso(), "status": "REQUESTING"}
        write_json(attempt_path, attempt)
        try:
            response = provider.ask(facts)
            attempt.update(status="RECEIVED", response=response)
            write_json(attempt_path, attempt)
        except Exception as error:
            attempt.update(status="FAILED", error=str(error), completed_at=now_iso())
            write_json(attempt_path, attempt)
            raise
    try:
        choices = response.get("choices") if isinstance(response, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else None
        draft = message.get("content") if isinstance(message, dict) else None
        if not isinstance(response, dict) or response.get("model") != provider.model_id or not isinstance(draft, str) or not draft.strip() or len(draft) > 8000:
            raise ValueError("invalid retrospective Qwen response")
        if choices[0].get("finish_reason") == "length":
            raise ValueError("incomplete retrospective Qwen response: token limit reached")
        plans = validate_plans(json_content(draft), study_id, manifest["data_grade"])
    except Exception as error:
        if attempt_path:
            attempt.update(status="INVALID_RESPONSE", error=str(error), completed_at=now_iso())
            write_json(attempt_path, attempt)
        raise
    if attempt_path:
        attempt.update(status="VALIDATED", completed_at=now_iso())
        write_json(attempt_path, attempt)
    draft = render_plans(plans)
    if not cache.exists():
        write_json(cache, {"identity": identity, "facts": facts, "response": response})
    destination = output / "qwen" / (key + ".json")
    write_json(destination, {"study_id": study_id, "cache_key": key, "model_id": provider.model_id,
                             "model_revision": provider.model_revision, "draft": draft, "needs_review": True,
                             "hypothesis_plans": plans, "structure_validated": True,
                             "data_grade": manifest["data_grade"], "prediction_score_eligible": False,
                             "numerical_artifacts_unchanged": True})
    draft_path = destination.with_suffix(".md")
    summary = ["# 急騰前の振り返り\n", f"grade: {manifest['data_grade']} / 特徴日: {manifest['feature_session']} / ランキング日: {manifest['ranking_session']}\n",
               "## Pythonによる事実の集計\n", "| 項目 | 掲載群 | 非掲載群 |", "|---|---:|---:|"]
    for label, metric_key in (("全件数", "count"), ("有効・対象内件数", "feature_valid_eligible_count"), ("選出件数", "selected_count")):
        summary.append(f"| {label} | {groups['listed'][metric_key]} | {groups['not_listed'][metric_key]} |")
    for field in FEATURE_FIELDS:
        a, b = (groups[key]['feature_medians'][field] for key in ("listed", "not_listed"))
        summary.append(f"| {field}（有効・対象内の中央値） | {a if a is not None else '不明'} | {b if b is not None else '不明'} |")
    summary.extend(["", "売買代金の取得済み件数（掲載群/非掲載群）: " + "/".join(str(groups[key]['feature_observed_counts']['turnover_median_20d_jpy']) for key in ("listed", "not_listed")),
                    "流動性フィルター: " + facts["liquidity_filter_mode"] + "。未取得の売買代金を推測で補わない。"])
    summary.extend(["", "## 掲載銘柄の前営業日状態（Python計算）\n", "| 銘柄ID | 区分 | 選出 | 相対出来高 | 5日騰落率 |", "|---|---|---|---:|---:|"])
    for row in ranked[:20]:
        summary.append(f"| {row['instrument_id']} | {row['stage']} | {row['selected']} | {row['rel_volume_20d']} | {row['ret_5d']} |")
    summary.extend(["", f"表示{min(len(ranked),20)}件 / 一致総数{len(ranked)}件。", "", "## AIによる未検証仮説（人手確認が必要）\n", draft,
                    "", "本出力は事例研究です。合成データは市場の証拠ではありません。非掲載を非急騰とせず、勝率・利益を示しません。"])
    atomic_bytes(draft_path, ("\n".join(summary) + "\n").encode("utf-8"))
    return {"study_id": study_id, "report_path": str(destination), "draft_path": str(draft_path), "cache_key": key}
