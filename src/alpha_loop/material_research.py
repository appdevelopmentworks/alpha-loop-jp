"""M5 material comparison: preregistered A/B1/B2, paired results, human review only."""
from __future__ import annotations
from datetime import date
from pathlib import Path
from .common import JST, atomic_bytes, canonical, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .pipeline import _code_hash, _db, _verified_manifest, config_load
from .provenance import archive_code, verify_code
from .research import _criteria, _id, _read_entry, _ready, _split
from .research_metrics import comparison_metrics, decide
from .material_research_inputs import MODEL_FIELDS, canonical_entries, load_materials, model_identity, select_arms, verify_artifacts, verify_saved_response
from .runtime import exclusive

VERSION = 'm5-material-paired-v1'
DEFAULT_POLICY = {'material_type': 'contract', 'contract_stage': 'formal_contract', 'dilution_max': .5,
                  'score_weights': {'core_business': 1/3, 'guidance_timing': 1/3, 'explicit_amount': 1/3},
                  'augmentation_slots': 5}


def engine_hash():
    names = ('material_research.py', 'material_research_inputs.py', 'materials.py', 'material_quality.py',
             'semantic.py', 'research.py', 'research_metrics.py')
    return digest(b''.join((Path(__file__).parent / n).read_bytes() for n in names))


def _store(root):
    con = _db(root)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS material_m5_experiments(
      experiment_id TEXT PRIMARY KEY, hypothesis_id TEXT, version INTEGER, family_id TEXT,
      registration_hash TEXT, consumed_hash TEXT, report_id TEXT, UNIQUE(hypothesis_id,version));
    CREATE TABLE IF NOT EXISTS material_m5_reports(report_id TEXT PRIMARY KEY, manifest_hash TEXT);
    CREATE TABLE IF NOT EXISTS material_m5_selections(
      experiment_id TEXT, run_id TEXT, selection_hash TEXT, PRIMARY KEY(experiment_id,run_id));
    ''')
    con.commit()
    return con


def _load(root, experiment_id, con=None):
    _id(experiment_id, 'mexp-')
    own = con is None
    con = con or _store(root)
    try:
        expected = con.execute('SELECT registration_hash FROM material_m5_experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
        path = root / 'data/research/material_experiments' / (experiment_id + '.json')
        if not expected or digest(path.read_bytes()) != expected[0]:
            raise RuntimeError('material registration hash mismatch')
        spec = read_json(path)
        verify_code(root, spec['code_bundle_id'])
        if digest((root / spec['plan_raw_ref']).read_bytes()) != spec['plan_hash']:
            raise RuntimeError('material plan raw hash mismatch')
        return spec
    finally:
        if own:
            con.close()


def _quality(root, value, question_set, identity, periods):
    """Freeze evidence before holdout, bind it to the exact model and human gold."""
    if value is None:
        return {'status': 'NOT_VALIDATED', 'qualified': False, 'reason': 'human_material_quality_not_supplied'}
    if set(value) != {'report_path', 'documents_path', 'human_reviewed', 'reviewer', 'reason'}:
        raise ValueError('invalid material quality evidence')
    report_path, documents_path = root / value['report_path'], root / value['documents_path']
    verify_artifacts(report_path.parent, ('report.json', 'provenance.json', 'samples.csv'))
    report = read_json(report_path)
    from .material_quality import evaluate_quality
    if evaluate_quality(root, report['gold_id'], documents_path) != report:
        raise RuntimeError('quality report differs from recomputation')
    gold = read_json(root / 'data/materials/gold' / (report['gold_id'] + '.json'))
    provenance = read_json(report_path.parent / 'provenance.json')
    responses = read_json(documents_path)
    if gold['question_set'] != question_set or not responses or any(
            r.get('identity') and model_identity(r['identity']) != identity for r in responses):
        raise ValueError('quality model/question identity mismatch')
    labelled_ids = {r['document_id'] for r in gold['label_set']['labels']}
    predictions = {r['document_id']: r for r in responses}
    if not any(predictions.get(i, {}).get('identity') for i in labelled_ids):
        raise ValueError('quality model identity missing for labelled documents')
    latest = max(parse_time(provenance['created_at']), parse_time(gold['registered_at']))
    if latest.astimezone(JST).date().isoformat() >= periods['holdout']['start']:
        raise ValueError('material quality evidence must precede holdout')
    from .materials import verified_document
    for doc_id in labelled_ids:
        doc = verified_document(root, root / 'data/materials/documents' / (doc_id + '.json'))
        prediction = predictions.get(doc_id)
        if prediction:
            verify_saved_response(root, doc, prediction, question_set, identity)
            if prediction.get('inferred_at') and parse_time(prediction['inferred_at']) > latest:
                raise ValueError('quality prediction must precede quality report')
        if (max(parse_time(doc[k]) for k in ('published_at', 'first_seen_at', 'available_at', 'fetched_at')) > latest or
                parse_time(doc['published_at']).astimezone(JST).date().isoformat() >= periods['holdout']['start']):
            raise ValueError('quality source must be available before quality report and holdout')
    qualified = (report['status'] == 'READY_FOR_HUMAN_REVIEW' and gold['label_set']['label_origin'] == 'human'
                 and value['human_reviewed'] is True and bool(value['reviewer']) and bool(value['reason']))
    proof_paths = [root / 'data/materials/gold' / (report['gold_id'] + '.json')]
    for directory in (report_path.parent, documents_path.parent):
        manifest = read_json(directory / 'manifest.json')
        proof_paths += [directory / 'manifest.json', *[directory / n for n in manifest['artifacts']]]
    for doc_id in labelled_ids:
        path = root / 'data/materials/documents' / (doc_id + '.json')
        proof_paths += [path, root / read_json(path)['raw_ref']]
        prediction = predictions.get(doc_id, {})
        if prediction.get('cache_id'):
            proof_paths.append(root / 'data/materials/cache' / (prediction['cache_id'] + '.json'))
    proof_objects = []
    for path in sorted(set(proof_paths)):
        raw = path.read_bytes()
        ref = 'data/raw/' + digest(raw)
        atomic_bytes(root / ref, raw)
        proof_objects.append({'source': str(path), 'raw_ref': ref, 'hash': digest(raw)})
    return {'status': report['status'], 'qualified': qualified, 'review': value,
            'model_identity': identity, 'gold_id': report['gold_id'], 'label_document_ids': sorted(labelled_ids),
            'gold_hash': digest((root / 'data/materials/gold' / (report['gold_id'] + '.json')).read_bytes()),
            'report_hash': digest(report_path.read_bytes()), 'documents_hash': digest(documents_path.read_bytes()),
            'created_at': provenance['created_at'], 'criteria': report['criteria'], 'proof_objects': proof_objects}


def register(root, plan_path):
    value = read_json(plan_path)
    fields = {'schema_version', 'hypothesis_id', 'version', 'family_id', 'mode', 'periods',
              'baseline_config_path', 'calendar_path', 'question_set_path', 'model_identity',
              'max_documents', 'policy', 'criteria', 'quality_evidence', 'thesis'}
    if set(value) != fields or value['schema_version'] != 1 or value['mode'] not in ('synthetic', 'research', 'prospective'):
        raise ValueError('explicit material comparison plan required')
    for key in ('hypothesis_id', 'family_id'):
        _id(value[key])
    if type(value['version']) is not int or value['version'] < 1 or not value['thesis']:
        raise ValueError('positive version and thesis required')
    calendar = read_json(root / value['calendar_path'])
    if not isinstance(calendar, list) or not calendar or calendar != sorted(set(calendar)):
        raise ValueError('sorted unique explicit exchange calendar required')
    for d in calendar:
        if date.fromisoformat(d).isoformat() != d:
            raise ValueError('invalid session date')
    periods = value['periods']
    if set(periods) != {'discovery', 'tuning', 'holdout'}:
        raise ValueError('three explicit periods required')
    previous = ''
    for key in ('discovery', 'tuning', 'holdout'):
        p = periods[key]
        if set(p) != {'start', 'end'} or p['start'] not in calendar or p['end'] not in calendar or not previous < p['start'] <= p['end']:
            raise ValueError('periods must be ordered nonoverlapping exchange sessions')
        previous = p['end']
    if calendar.index(previous) + 5 >= len(calendar):
        raise ValueError('calendar needs five sessions beyond holdout')
    baseline = config_load(root / value['baseline_config_path'])
    criteria = _criteria(value['criteria'], baseline)
    if criteria['stages'] != ['EARLY', 'PREMOVE'] or criteria['max_trials'] < 2:
        raise ValueError('material family compares both price stages and reserves two trials for B1/B2')
    if sum(periods['holdout']['start'] <= d <= periods['holdout']['end'] for d in calendar) < criteria['min_days']:
        raise ValueError('holdout shorter than frozen minimum')
    questions_path = root / value['question_set_path']
    questions = read_json(questions_path)
    from .materials import VERSION as material_version
    identity = value['model_identity']
    if set(identity) != set(MODEL_FIELDS) or identity['version'] != material_version or identity['extractor_version'] != 'utf8-paragraphs-v2' or identity['evidence_policy'] != 'source-paragraph-v2':
        raise ValueError('explicit supported material model/extractor identity required')
    if identity['question_hash'] != digest(canonical(questions)) or any(not identity[k] for k in ('provider', 'model', 'revision', 'server_commit', 'quantization')):
        raise ValueError('question hash or frozen model revision missing')
    policy = value['policy']
    if set(policy) != set(DEFAULT_POLICY) or policy['material_type'] not in questions['questions']['material_type']['criteria'] or policy['contract_stage'] not in questions['questions']['contract_stage']['criteria']:
        raise ValueError('invalid finite material policy')
    import math
    numbers = [policy['dilution_max'], *policy['score_weights'].values()]
    if set(policy['score_weights']) != set(DEFAULT_POLICY['score_weights']) or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in numbers) or abs(sum(policy['score_weights'].values()) - 1) > 1e-12:
        raise ValueError('finite normalized material score weights required')
    if type(policy['augmentation_slots']) is not int or not 1 <= policy['augmentation_slots'] <= 2 * criteria['k_per_stage']:
        raise ValueError('invalid fixed augmentation slots')
    if type(value['max_documents']) is not int or not 1 <= value['max_documents'] <= 100:
        raise ValueError('invalid frozen document cap')
    quality = _quality(root, value['quality_evidence'], questions, identity, periods)
    raw = plan_path.read_bytes()
    raw_ref = 'data/raw/' + digest(raw)
    payload = {k: value[k] for k in ('hypothesis_id', 'version', 'family_id', 'mode', 'periods', 'model_identity', 'max_documents', 'policy', 'thesis')}
    payload.update(schema_version=1, engine_version=VERSION, engine_hash=engine_hash(), numerical_code_hash=_code_hash(),
                   calendar=calendar, baseline_config=baseline, baseline_config_hash=digest(canonical(baseline)), criteria=criteria,
                   question_set=questions, question_file_hash=digest(questions_path.read_bytes()), quality=quality,
                   plan_hash=digest(raw), plan_raw_ref=raw_ref, arms=['B1', 'B2'],
                   primary_metric='paired daily ALL hit-rate delta; B1 and B2 separate, two comparisons',
                   selection_policy='first baseline / first configured original material receipt / first complete evaluation',
                   thresholds_are_design_defaults=True, automatic_adoption=False)
    experiment_id = 'mexp-' + stable_id(payload)
    with exclusive(root / 'data/research/.material_m5.lock'):
        con = _store(root)
        try:
            con.execute('BEGIN IMMEDIATE')
            prior = con.execute('SELECT experiment_id FROM material_m5_experiments WHERE hypothesis_id=? AND version=?', (value['hypothesis_id'], value['version'])).fetchone()
            if prior:
                if prior[0] != experiment_id:
                    raise ValueError('material hypothesis version is immutable')
                return _load(root, experiment_id, con)
            at = now_iso()
            if value['mode'] == 'prospective' and periods['holdout']['start'] <= parse_time(at).astimezone(JST).date().isoformat():
                raise ValueError('prospective holdout must start after registration')
            if value['mode'] == 'prospective' and not quality['qualified']:
                raise ValueError('prospective materials need model-matched human-reviewed quality evidence')
            if value['mode'] == 'prospective' and identity['provider'] != 'razorback16/openjev':
                raise ValueError('prospective material adoption requires the selected local OpenJev provider; mocks remain research only')
            members = con.execute('SELECT experiment_id,consumed_hash FROM material_m5_experiments WHERE family_id=?', (value['family_id'],)).fetchall()
            if 2 * (len(members) + 1) > criteria['max_trials'] or any(m[1] for m in members):
                raise ValueError('material family trial budget exhausted or holdout revealed')
            for member, _ in members:
                other = _load(root, member, con)
                if any(other[k] != payload[k] for k in ('periods', 'calendar', 'criteria', 'baseline_config_hash', 'mode', 'model_identity', 'question_file_hash', 'max_documents', 'quality')):
                    raise ValueError('material family definitions must match')
            # Revealed periods cannot be recycled across price and material experiments.
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            revealed = [_load(root, r[0], con) for r in con.execute('SELECT experiment_id FROM material_m5_experiments WHERE consumed_hash IS NOT NULL')]
            if 'm5_experiments' in tables:
                from .research import _load as price_load
                revealed += [price_load(root, r[0], con) for r in con.execute('SELECT experiment_id FROM m5_experiments WHERE consumed_hash IS NOT NULL')]
            for other in revealed:
                a, b = periods['holdout'], other['periods']['holdout']
                if a['start'] <= b['end'] and b['start'] <= a['end']:
                    raise ValueError('holdout overlaps revealed period')
            if con.execute('SELECT 1 FROM hypotheses WHERE hypothesis_id=? AND version=?', (value['hypothesis_id'], value['version'])).fetchone():
                raise ValueError('hypothesis ledger version occupied')
            spec = {**payload, 'experiment_id': experiment_id, 'registered_at': at, 'code_bundle_id': archive_code(root)}
            atomic_bytes(root / raw_ref, raw)
            path = root / 'data/research/material_experiments' / (experiment_id + '.json')
            write_json(path, spec)
            con.execute('INSERT INTO material_m5_experiments VALUES(?,?,?,?,?,?,?)', (experiment_id, value['hypothesis_id'], value['version'], value['family_id'], digest(path.read_bytes()), None, None))
            con.execute('INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?)', (value['hypothesis_id'], value['version'], value['thesis'], 'sealed materials and full-universe next-session outcomes', 'A vs B1 filter / B2 augmentation', periods['holdout']['start'], periods['holdout']['end'], 'REGISTERED', at))
            con.execute('INSERT INTO hypothesis_events VALUES(?,?,?,?,?)', (value['hypothesis_id'], value['version'], at, 'MATERIAL_PREREGISTER', experiment_id))
            con.commit()
            return spec
        finally:
            con.close()


def freeze_selection(root, spec, run_id):
    """Seal choices before joining labels; SQLite anchor prevents receipt replacement."""
    manifest = _verified_manifest(root / 'outputs' / run_id)
    if manifest['config_hash'] != spec['baseline_config_hash'] or manifest['code_hash'] != spec['numerical_code_hash']:
        raise ValueError('material baseline version mismatch')
    responses, docs, evidence = load_materials(root, run_id, spec)
    decisions = read_json(root / 'outputs' / run_id / 'decisions.json')
    rows = select_arms(decisions, responses, spec)
    groups = {}
    for doc in docs:
        # Same source event (including revisions) AND duplicate content link clusters.
        groups.setdefault(doc['instrument_id'], set()).update(('event:' + doc['disclosure_id'], 'content:' + doc['content_hash']))
    result = {'experiment_id': spec['experiment_id'], 'run_id': run_id, 'session': manifest['target_session'],
              'as_of': manifest['as_of'], 'decision_hash': manifest['artifacts']['decisions.json'],
              'rows': rows, 'events': {k: sorted(v) for k, v in groups.items()}, 'material_evidence': evidence}
    path = root / 'data/research/material_selections' / spec['experiment_id'] / (run_id + '.json')
    con = _store(root)
    try:
        con.execute('BEGIN IMMEDIATE')
        prior = con.execute('SELECT selection_hash FROM material_m5_selections WHERE experiment_id=? AND run_id=?', (spec['experiment_id'], run_id)).fetchone()
        if prior:
            if digest(path.read_bytes()) != prior[0] or read_json(path) != result:
                raise RuntimeError('frozen material selection/input changed; cannot backfill')
        else:
            write_json(path, result)
            con.execute('INSERT INTO material_m5_selections VALUES(?,?,?)', (spec['experiment_id'], run_id, digest(path.read_bytes())))
        con.commit()
    finally:
        con.close()
    return result


def _purge_cluster(rows, spec):
    earlier = set()
    for split, following in (('discovery', 'tuning'), ('tuning', 'holdout'), ('holdout', None)):
        group = [r for r in rows if r['split'] == split]
        events = {e for r in group for e in r['event_groups']}
        for row in group:
            if earlier.intersection(row['event_groups']):
                row['exclusion_reason'] = 'material_event_crosses_partition'
            if following:
                i = spec['calendar'].index(row['session']) + spec['criteria']['purge_sessions']
                if i >= len(spec['calendar']) or spec['calendar'][i] >= spec['periods'][following]['start']:
                    row['exclusion_reason'] = 'five_session_horizon_crosses_partition'
        earlier.update(events)
    parent = {r['instrument_id']: r['instrument_id'] for r in rows}
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owners = {}
    for row in rows:
        for event in row['event_groups']:
            if event in owners:
                a, b = find(row['instrument_id']), find(owners[event])
                parent[max(a, b)] = min(a, b)
            owners[event] = row['instrument_id']
    for row in rows:
        row['cluster_id'] = find(row['instrument_id'])


def _verified_report(root, report_id, con):
    path = root / 'outputs/material_comparisons' / report_id
    expected = con.execute('SELECT manifest_hash FROM material_m5_reports WHERE report_id=?', (report_id,)).fetchone()
    if not expected or digest((path / 'manifest.json').read_bytes()) != expected[0]:
        raise RuntimeError('material comparison manifest anchor mismatch')
    verify_artifacts(path, ('summary.json', 'metrics.json', 'samples.json', 'samples.csv', 'report.md', 'inputs.json'))
    return read_json(path / 'summary.json')


def compare(root, experiment_id, batch_path=None):
    with exclusive(root / 'data/research/.material_m5.lock'):
        spec = _load(root, experiment_id)
        if spec['engine_hash'] != engine_hash() or spec['numerical_code_hash'] != _code_hash():
            raise RuntimeError('material engine changed; use frozen source bundle')
        for proof in spec['quality'].get('proof_objects', []):
            if digest((root / proof['raw_ref']).read_bytes()) != proof['hash']:
                raise RuntimeError('frozen material quality proof hash mismatch')
        if not _ready(spec):
            return {'experiment_id': experiment_id, 'status': 'WAITING', 'recommendation': 'HOLD',
                    'reason': 'holdout_not_finished_no_interim_peeking', 'automatic_adoption': False}
        entries = canonical_entries(root, spec)
        if batch_path is not None:
            batch = read_json(batch_path)
            if set(batch) != {'schema_version', 'entries'} or batch['schema_version'] != 1 or sorted(batch['entries'], key=canonical) != sorted(entries, key=canonical):
                raise ValueError('comparison must use canonical first runs/evaluations, no subset or replacement')
        days = [d for d in spec['calendar'] if spec['periods']['holdout']['start'] <= d <= spec['periods']['holdout']['end']]
        observed = {_verified_manifest(root / 'outputs' / e['run_id'])['target_session'] for e in entries}
        if not set(days) <= observed:
            return {'experiment_id': experiment_id, 'status': 'WAITING', 'recommendation': 'HOLD',
                    'reason': 'missing_holdout_sessions', 'missing_sessions': sorted(set(days) - observed), 'automatic_adoption': False}
        rows, evidence = [], []
        for entry in entries:
            selection = freeze_selection(root, spec, entry['run_id'])
            # Decisions and material choices above never receive any outcome values.
            manifest, decisions, outcomes, instruments, price_events, receipt = _read_entry(root, entry, spec)
            receipt['material_evidence'] = selection['material_evidence']
            receipt['selection_hash'] = digest(canonical(selection))
            evidence.append(receipt)
            selected = {r['instrument_id']: r for r in selection['rows']}
            for decision in decisions:
                iid = decision['instrument_id']
                outcome, choice = outcomes[iid], selected[iid]
                rows.append({**choice, 'split': _split(manifest['target_session'], spec['periods']),
                             'session': manifest['target_session'], 'source_run_id': entry['run_id'], 'decision_id': decision['decision_id'],
                             'market': instruments[iid]['market'], 'liquidity': decision['turnover_status'], 'regime': 'unknown',
                             'stage': 'ALL', 'event_groups': sorted(set(selection['events'].get(iid, [])) | {'event:' + e for e in price_events[iid]}),
                             'discovery_hit': outcome['discovery_hit'], 'discovery_return': outcome['discovery_return'],
                             'execution_status': outcome['execution_status'], 'outcome_status': outcome['outcome_status'],
                             'gross_proxy_return': outcome['open_close_proxy_return'] if outcome['execution_status'] in ('proxy_only', 'cost_unset') else None,
                             'exclusion_reason': None if decision['is_eligible'] else 'outside_universe'})
        _purge_cluster(rows, spec)
        quality_reasons = []
        if spec['mode'] != 'prospective':
            quality_reasons.append('synthetic_or_research_not_prospective')
        if not spec['quality']['qualified']:
            quality_reasons.append('human_material_quality_not_validated')
        if any(not e['prediction_score_eligible'] or not e['evaluation_raw_verified'] for e in evidence if _split(e['session'], spec['periods']) == 'holdout'):
            quality_reasons.append('price_point_in_time_or_raw_evaluation_unverified')
        for split in ('discovery', 'tuning'):
            required = {d for d in spec['calendar'] if spec['periods'][split]['start'] <= d <= spec['periods'][split]['end']}
            if not required <= observed:
                quality_reasons.append(split + '_coverage_unverified')
        gold_ids = set(spec['quality'].get('label_document_ids', []))
        if any(gold_ids.intersection(e['material_evidence'].get('document_record_hashes', {})) for e in evidence if _split(e['session'], spec['periods']) == 'holdout'):
            quality_reasons.append('quality_labels_overlap_holdout')
        final = [r for r in rows if r['split'] == 'holdout' and r['exclusion_reason'] is None]
        if not final or sum(r['discovery_hit'] is None for r in final) / len(final) > spec['criteria']['max_unknown_rate']:
            quality_reasons.append('universe_outcome_unknown_rate_exceeded')
        # Preserve whole-universe coverage, but gate only the inputs each arm uses.
        # B2 retains A without interpreting A's missing material as negative.
        material_unknown_rate = sum(not r['material_known'] for r in final) / len(final) if final else None
        material_populations = {'B1': [r for r in final if r['A_selected']],
                                'B2': [r for r in final if r['eligible'] and r['baseline_stage'] in ('WATCH', 'NONE') and r['document_count']]}
        material_input_unknown_rates = {arm: sum(not r['material_known'] for r in population) / len(population) if population else None
                                        for arm, population in material_populations.items()}
        criteria = {**spec['criteria'], 'stages': ['ALL']}
        metrics, recommendations = {}, {}
        for arm in ('B1', 'B2'):
            paired = [{**r, 'B_selected': r[arm + '_selected']} for r in final]
            metrics[arm] = comparison_metrics(paired, days, criteria)
            arm_quality = list(quality_reasons)
            if material_input_unknown_rates[arm] is None:
                arm_quality.append('material_inputs_unavailable')
            elif material_input_unknown_rates[arm] > spec['criteria']['max_unknown_rate']:
                arm_quality.append('material_unknown_rate_exceeded')
            recommendations[arm] = decide(metrics[arm], criteria, arm_quality)
        input_hash = digest(canonical({'evidence': evidence, 'spec': spec['experiment_id'], 'engine': spec['engine_hash']}))
        report_id = 'mcmp-' + stable_id(experiment_id, input_hash)
        con = _store(root)
        try:
            con.execute('BEGIN IMMEDIATE')
            consumed, prior = con.execute('SELECT consumed_hash,report_id FROM material_m5_experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if consumed:
                if consumed != input_hash:
                    raise RuntimeError('holdout already consumed with different inputs')
                return _verified_report(root, prior, con)
            path = root / 'outputs/material_comparisons' / report_id
            result = {'experiment_id': experiment_id, 'report_id': report_id, 'status': 'COMPLETE',
                      'recommendation': {k: v['recommendation'] for k, v in recommendations.items()}, 'arms': recommendations,
                      'material_unknown_rate': material_unknown_rate, 'data_mode': spec['mode'],
                      'material_input_unknown_rates': material_input_unknown_rates,
                      'quality_reasons': sorted(set(quality_reasons + [r for a in recommendations.values() for r in a['reason_codes'] if r.startswith('material_')])), 'holdout_sessions': days,
                      'input_hash': input_hash, 'automatic_adoption': False, 'live_enabled': False,
                      'performance_claim_allowed': False, 'report_path': str(path / 'report.md')}
            write_json(path / 'summary.json', result)
            write_json(path / 'metrics.json', metrics)
            write_json(path / 'samples.json', rows)
            write_csv(path / 'samples.csv', list(rows[0]) if rows else ['instrument_id'], rows)
            write_json(path / 'inputs.json', {'registration': spec, 'evidence': evidence, 'input_hash': input_hash})
            lines = ['# M5 材料比較・採否', '', 'A: 価格EARLY/PREMOVE上位K。B1: Aの材料フィルター（補充なし）。',
                     'B2: WATCH/NONEの材料適合銘柄を固定枠追加。価格の下位順位を置換し総上限2Kを保持。',
                     '主比較はALL。元の価格区分はsamples.csvへ保存。B1/B2は別比較、事前試行上限で区間補正。', '',
                     '| 比較 | A候補/有効/的中 | B候補/有効/的中 | 日平均的中率差と対応区間 | 採否 |', '|---|---|---|---|---|']
            daily, costs = [], []
            for arm, m in metrics.items():
                a, b, interval = m['overall']['A'], m['overall']['B'], m['by_stage']['ALL']['paired']
                lines.append(f"| A/{arm} | {a['selected']}/{a['valid']}/{a['hits']} | {b['selected']}/{b['valid']}/{b['hits']} | {interval['delta_daily_mean_hit_rate']} [{interval['lower']}, {interval['upper']}] | {recommendations[arm]['recommendation']} |")
                for name in ('A', 'B'):
                    daily += [{'comparison': arm, 'arm': name, **r} for r in m['overall'][name]['daily']]
                    costs += [{'comparison': arm, 'arm': name, **r} for r in m['overall'][name]['cost_sensitivity']]
                lines += ['', arm + '理由: ' + ', '.join(recommendations[arm]['reason_codes'])]
            lines += ['', '不明は陰性に変換しない。未約定・停止・約定不明は収益proxyから除外。費用感度は実売買収益ではない。',
                      '原本/時刻/質問/モデル/品質/条件/期間は固定。訂正・重複原本イベントを区分間purgeし、銘柄と共通イベントでクラスタ化。',
                      '合成・再構成・品質未合格はHOLD。本番ACTIVE・自動発注は無効。採用候補も人手確認が必要。']
            atomic_bytes(path / 'report.md', ('\n'.join(lines) + '\n').encode('utf-8'))
            write_csv(path / 'daily_metrics.csv', ['comparison', 'arm', 'session', 'selected', 'valid', 'hits', 'hit_rate'], daily)
            write_csv(path / 'cost_sensitivity.csv', ['comparison', 'arm', 'roundtrip_cost_bps', 'net_proxy_count', 'mean_net_proxy_return'], costs)
            files = ('summary.json', 'metrics.json', 'samples.json', 'samples.csv', 'report.md', 'inputs.json', 'daily_metrics.csv', 'cost_sensitivity.csv')
            write_json(path / 'manifest.json', {'report_id': report_id, 'artifacts': {n: digest((path / n).read_bytes()) for n in files}})
            con.execute('INSERT INTO material_m5_reports VALUES(?,?)', (report_id, digest((path / 'manifest.json').read_bytes())))
            con.execute('UPDATE material_m5_experiments SET consumed_hash=?,report_id=? WHERE experiment_id=?', (input_hash, report_id, experiment_id))
            states = set(result['recommendation'].values())
            state = 'ADOPTION_CANDIDATE' if 'ADOPTION_CANDIDATE' in states else 'REJECT' if states == {'REJECT'} else 'HOLD'
            con.execute('UPDATE hypotheses SET state=? WHERE hypothesis_id=? AND version=?', (state, spec['hypothesis_id'], spec['version']))
            con.execute('INSERT INTO hypothesis_events VALUES(?,?,?,?,?)', (spec['hypothesis_id'], spec['version'], now_iso(), 'MATERIAL_COMPARE', report_id))
            con.commit()
            return result
        finally:
            con.close()


def prepare_batch(root, experiment_id):
    spec = _load(root, experiment_id)
    path = root / 'data/research/material_batches' / (experiment_id + '.json')
    write_json(path, {'schema_version': 1, 'entries': canonical_entries(root, spec)})
    return path


def refresh(root, run_id=None):
    con = _store(root)
    try:
        ids = [r[0] for r in con.execute('SELECT experiment_id FROM material_m5_experiments ORDER BY experiment_id')]
    finally:
        con.close()
    results = []
    for experiment_id in ids:
        try:
            if run_id:
                spec = _load(root, experiment_id)
                manifest = _verified_manifest(root / 'outputs' / run_id)
                if (_split(manifest['target_session'], spec['periods']) and
                        (manifest['data_grade'] == 'synthetic') == (spec['mode'] == 'synthetic') and
                        manifest['config_hash'] == spec['baseline_config_hash'] and manifest['code_hash'] == spec['numerical_code_hash']):
                    freeze_selection(root, spec, run_id)
            results.append(compare(root, experiment_id))
        except Exception as error:
            results.append({'experiment_id': experiment_id, 'status': 'FAILED', 'error': str(error)[:500], 'automatic_adoption': False})
    return {'status': 'FAILED' if any(r['status'] == 'FAILED' for r in results) else 'OK' if ids else 'NOT_REGISTERED', 'experiments': results}


def review(root, experiment_id, arm, decision, reviewer, reason, human_reviewed=False):
    """Record a human decision; APPROVE_SHADOW creates an inactive release only."""
    if arm not in ('B1', 'B2') or decision not in ('HOLD', 'REJECT', 'APPROVE_SHADOW') or not human_reviewed or not reviewer.strip() or not reason.strip():
        raise ValueError('explicit human review, arm, reviewer and reason required')
    with exclusive(root / 'data/research/.material_m5.lock'):
        con = _store(root)
        try:
            spec = _load(root, experiment_id, con)
            row = con.execute('SELECT report_id FROM material_m5_experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if not row or not row[0]:
                raise ValueError('completed material comparison required')
            report = _verified_report(root, row[0], con)
            if decision == 'APPROVE_SHADOW' and (spec['mode'] != 'prospective' or report['arms'][arm]['recommendation'] != 'ADOPTION_CANDIDATE'):
                raise ValueError('only validated prospective adoption candidates can enter shadow review')
            value = {'experiment_id': experiment_id, 'report_id': row[0], 'arm': arm, 'decision': decision,
                     'reviewer': reviewer, 'reason': reason, 'human_reviewed': True,
                     'report_manifest_hash': digest((root / 'outputs/material_comparisons' / row[0] / 'manifest.json').read_bytes()),
                     'automatic_adoption': False, 'live_enabled': False}
            review_id = 'mreview-' + stable_id(value)
            path = root / 'data/research/material_reviews' / (review_id + '.json')
            value.update(review_id=review_id)
            con.execute('BEGIN IMMEDIATE')
            prior = con.execute("SELECT 1 FROM hypothesis_events WHERE hypothesis_id=? AND version=? AND action='MATERIAL_REVIEW' AND reason=?", (spec['hypothesis_id'], spec['version'], review_id)).fetchone()
            if prior:
                if read_json(path) != value:
                    raise RuntimeError('material review receipt changed')
                return value
            write_json(path, value)
            if decision == 'APPROVE_SHADOW':
                write_json(root / 'data/research/material_releases' / (review_id + '.json'), {**value, 'state': 'SHADOW_ONLY', 'policy': spec['policy'], 'model_identity': spec['model_identity']})
            con.execute('INSERT INTO hypothesis_events VALUES(?,?,?,?,?)', (spec['hypothesis_id'], spec['version'], now_iso(), 'MATERIAL_REVIEW', review_id))
            con.commit()
            return value
        finally:
            con.close()
