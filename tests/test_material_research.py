import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from alpha_loop.common import canonical, digest, read_json, write_json
from alpha_loop.material_research import (_load, _purge_cluster, _quality, compare, freeze_selection,
                                         prepare_batch, refresh, register, review)
from alpha_loop.material_research_fixture import create
from alpha_loop.material_research_inputs import load_materials, receipt_key, select_arms
from alpha_loop.pipeline import list_hypotheses
from alpha_loop.research_metrics import comparison_metrics, decide

PROJECT = Path(__file__).resolve().parents[1]


class MaterialResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name) / 'base'
        cls.receipt = create(cls.base, PROJECT / 'configs/baseline.json', PROJECT / 'configs/questions_v1.json')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'demo'
        shutil.copytree(self.base, self.root)
        self.exp = self.receipt['experiment_id']
        self.spec = _load(self.root, self.exp)
        self.entries = read_json(self.root / 'batch.json')['entries']
        self.run_id = self.entries[-1]['run_id']
        self.directory = self.root / 'outputs' / self.run_id / 'materials' / receipt_key(self.run_id, self.spec)

    def tearDown(self):
        self.temp.cleanup()

    def change_plan(self, fn):
        value = read_json(self.root / 'material_plan.json')
        fn(value)
        write_json(self.root / 'material_plan.json', value)
        return register(self.root, self.root / 'material_plan.json')

    def rehash(self, directory):
        m = read_json(directory / 'manifest.json')
        for name in m['artifacts']:
            m['artifacts'][name] = digest((directory / name).read_bytes())
        write_json(directory / 'manifest.json', m)

    def test_hand_counts_augmentation_costs_and_synthetic_hold(self):
        result = compare(self.root, self.exp)
        path = Path(result['report_path']).parent
        metrics = read_json(path / 'metrics.json')
        a, b = metrics['B1']['overall']['A'], metrics['B1']['overall']['B']
        self.assertEqual((a['selected'], a['valid'], a['hits']), (100, 100, 50))
        self.assertEqual((b['selected'], b['hits']), (50, 50))
        self.assertEqual(b['trade_proxy_count'], 40)  # unfilled and unknown excluded
        self.assertAlmostEqual(b['cost_sensitivity'][2]['mean_net_proxy_return'], .007)
        self.assertIsNone(b['actual_trading_return'])
        self.assertEqual(metrics['B2']['overall']['B']['selected'], 105)  # price NONE added, same cap 40/day
        samples = read_json(path / 'samples.json')
        added = [r for r in samples if r['instrument_id'] == 'TSE:0021']
        self.assertTrue(all(r['B2_selected'] and not r['A_selected'] and r['baseline_stage'] == 'NONE' for r in added))
        self.assertEqual(result['recommendation'], {'B1': 'HOLD', 'B2': 'HOLD'})
        self.assertEqual(result['arms']['B1']['statistical_decision'], 'ADOPTION_CANDIDATE')
        self.assertFalse(result['live_enabled'])
        self.assertEqual(list_hypotheses(self.root)[0]['state'], 'HOLD')
        self.assertEqual(len(samples), 154)

    def test_identical_reexecution_and_manifest_anchor(self):
        first = compare(self.root, self.exp)
        self.assertEqual(first, compare(self.root, self.exp, prepare_batch(self.root, self.exp)))
        directory = Path(first['report_path']).parent
        write_json(directory / 'metrics.json', {})
        self.rehash(directory)
        with self.assertRaisesRegex(RuntimeError, 'anchor mismatch'):
            compare(self.root, self.exp)

    def test_no_peeking_before_next_exchange_session_twenty(self):
        with patch('alpha_loop.research.now_iso', return_value='2026-09-24T19:59:59+09:00'), patch('alpha_loop.material_research.canonical_entries', side_effect=AssertionError('peek')):
            result = compare(self.root, self.exp, self.root / 'nonexistent.json')
        self.assertEqual(result['status'], 'WAITING')  # Sep21-23 holidays, 24th next session
        self.assertNotIn('arms', result)

    def test_incomplete_holdout_waits_without_partial_metrics(self):
        shutil.rmtree(self.root / 'outputs' / self.run_id / 'evaluation')
        result = compare(self.root, self.exp)
        self.assertEqual(result['status'], 'WAITING')
        self.assertNotIn('arms', result)

    def test_subsets_duplicate_runs_and_label_cherry_picking_rejected(self):
        for entries in (self.entries[:-1], self.entries + [self.entries[-1]]):
            write_json(self.root / 'changed_batch.json', {'schema_version': 1, 'entries': entries})
            with self.assertRaisesRegex(ValueError, 'canonical'):
                compare(self.root, self.exp, self.root / 'changed_batch.json')

    def test_freeze_reads_no_labels_and_detects_late_backfill(self):
        with patch('alpha_loop.material_research._read_entry', side_effect=AssertionError('future read')):
            first = freeze_selection(self.root, self.spec, self.run_id)
        future = self.root / self.entries[-1]['future_ref']
        write_json(future, {'fake_future': True})
        self.assertEqual(first, freeze_selection(self.root, self.spec, self.run_id))
        shutil.rmtree(self.directory)
        with self.assertRaisesRegex(RuntimeError, 'cannot backfill'):
            freeze_selection(self.root, self.spec, self.run_id)

    def test_unknown_material_never_false_label_or_successful_adoption(self):
        for e in self.entries:
            shutil.rmtree(self.root / 'outputs' / e['run_id'] / 'materials')
        result = compare(self.root, self.exp)
        rows = read_json(Path(result['report_path']).parent / 'samples.json')
        self.assertTrue(all(r['material_pass'] is None for r in rows))
        self.assertTrue(any(r['discovery_hit'] is True for r in rows))
        self.assertIn('material_unknown_rate_exceeded', result['quality_reasons'])
        self.assertEqual(result['arms']['B1']['statistical_decision'], 'HOLD')

    def test_late_original_receipt_unknown_retry_does_not_replace(self):
        receipt = read_json(self.directory / 'manifest.json')
        receipt['completed_at'] = '2026-09-24T09:00:00+09:00'
        write_json(self.directory / 'manifest.json', receipt)
        responses, _, evidence = load_materials(self.root, self.run_id, self.spec)
        self.assertEqual(evidence['status'], 'late_response')
        self.assertTrue(all(r['semantic_status'] == 'late_response' for r in responses))
        shutil.copytree(self.directory, self.directory / 'retries' / 'later')
        self.assertFalse(any(r['material_known'] for r in freeze_selection(self.root, self.spec, self.run_id)['rows']))

    def test_raw_doc_and_response_hash_tampering_rejected(self):
        (self.directory / 'documents.json').write_text('[]')
        with self.assertRaisesRegex(RuntimeError, 'artifact hash mismatch'):
            load_materials(self.root, self.run_id, self.spec)

    def test_model_question_mismatch_rejected_even_with_rehashed_receipt(self):
        docs = read_json(self.directory / 'documents.json')
        docs[0]['identity']['revision'] = 'different'
        write_json(self.directory / 'documents.json', docs)
        self.rehash(self.directory)
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            load_materials(self.root, self.run_id, self.spec)

    def test_evidence_is_validated_against_cache_and_raw_source(self):
        docs = read_json(self.directory / 'documents.json')
        row = docs[0]
        cache_path = self.root / 'data/materials/cache' / (row['cache_id'] + '.json')
        cache = read_json(cache_path)
        row['evidence']['material_type']['quote'] = 'invented quote'
        cache['evidence'] = row['evidence']
        cache['checksum'] = digest(canonical({k: v for k, v in cache.items() if k != 'checksum'}))
        write_json(cache_path, cache)
        write_json(self.directory / 'documents.json', docs)
        self.rehash(self.directory)
        with self.assertRaisesRegex(RuntimeError, 'evidence/source'):
            load_materials(self.root, self.run_id, self.spec)

    def test_original_snapshot_mutation_is_not_accepted(self):
        sealed = read_json(self.directory / 'snapshot.json')
        sealed['documents'][0]['available_at'] = '2026-09-24T12:00:00+09:00'
        write_json(self.directory / 'snapshot.json', sealed)
        self.rehash(self.directory)
        with self.assertRaisesRegex(RuntimeError, 'snapshot identity'):
            load_materials(self.root, self.run_id, self.spec)

    def test_registration_immutable_and_finite_rules(self):
        self.assertEqual(register(self.root, self.root / 'material_plan.json')['experiment_id'], self.exp)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.change_plan(lambda p: p['policy'].update(augmentation_slots=4))
        with self.assertRaisesRegex(ValueError, 'finite normalized'):
            self.change_plan(lambda p: p['policy']['score_weights'].update(core_business=.8))

    def test_two_trials_and_calendar_constraints(self):
        with self.assertRaisesRegex(ValueError, 'two trials'):
            self.change_plan(lambda p: p['criteria'].update(max_trials=1))
        with self.assertRaisesRegex(ValueError, 'exchange sessions'):
            self.change_plan(lambda p: p['periods']['holdout'].update(start='2026-09-13'))

    def test_prospective_requires_unused_period_and_human_quality(self):
        with patch('alpha_loop.material_research.now_iso', return_value='2026-08-01T20:00:00+09:00'):
            with self.assertRaisesRegex(ValueError, 'human-reviewed quality'):
                self.change_plan(lambda p: p.update(mode='prospective', hypothesis_id='H-prospective', family_id='F-prospective'))
        with self.assertRaisesRegex(ValueError, 'after registration'):
            self.change_plan(lambda p: p.update(mode='prospective'))

    def test_family_budget_and_consumed_window_cannot_be_recycled(self):
        with self.assertRaisesRegex(ValueError, 'trial budget'):
            self.change_plan(lambda p: p.update(hypothesis_id='H-second', version=2))
        compare(self.root, self.exp)
        with self.assertRaisesRegex(ValueError, 'revealed period'):
            self.change_plan(lambda p: p.update(family_id='F-new', hypothesis_id='H-new', version=3))

    def test_augmentation_fixed_capacity_deterministic_and_no_bad_price_quality(self):
        responses, _, _ = load_materials(self.root, self.run_id, self.spec)
        decisions = read_json(self.root / 'outputs' / self.run_id / 'decisions.json')
        spec = copy.deepcopy(self.spec)
        spec['criteria']['k_per_stage'] = 10
        rows = select_arms(decisions, responses, spec)
        self.assertEqual(sum(r['B2_selected'] for r in rows), 20)
        self.assertTrue(next(r for r in rows if r['instrument_id'] == 'TSE:0021')['B2_selected'])
        self.assertFalse(next(r for r in rows if r['instrument_id'] == 'TSE:0022')['B2_selected'])
        self.assertEqual(rows, select_arms(decisions, list(reversed(responses)), spec))

    def test_material_events_and_duplicate_content_purged_and_clustered(self):
        rows = [{'split': 'discovery', 'session': '2026-08-24', 'instrument_id': 'TSE:0001', 'event_groups': ['shared'], 'exclusion_reason': None},
                {'split': 'holdout', 'session': '2026-09-14', 'instrument_id': 'TSE:0002', 'event_groups': ['shared'], 'exclusion_reason': None}]
        _purge_cluster(rows, self.spec)
        self.assertEqual(rows[1]['exclusion_reason'], 'material_event_crosses_partition')
        self.assertEqual(rows[0]['cluster_id'], rows[1]['cluster_id'])

    def test_human_review_hold_idempotent_no_synthetic_approval(self):
        compare(self.root, self.exp)
        receipt = review(self.root, self.exp, 'B1', 'HOLD', 'fixture reviewer', 'synthetic evidence only', True)
        self.assertEqual(receipt, review(self.root, self.exp, 'B1', 'HOLD', 'fixture reviewer', 'synthetic evidence only', True))
        with self.assertRaisesRegex(ValueError, 'prospective adoption'):
            review(self.root, self.exp, 'B1', 'APPROVE_SHADOW', 'reviewer', 'try', True)
        self.assertFalse(receipt['live_enabled'])

    def test_daily_refresh_has_no_model_calls_and_saves_selection(self):
        result = refresh(self.root, self.run_id)
        self.assertEqual(result['status'], 'OK')
        self.assertTrue((self.root / 'data/research/material_selections' / self.exp / (self.run_id + '.json')).exists())

    def test_empty_arms_unknown_outcomes_hold_not_zero_rate(self):
        criteria = {**self.spec['criteria'], 'stages': ['ALL']}
        metrics = comparison_metrics([], ['2026-09-14'], criteria)
        self.assertIsNone(metrics['overall']['A']['hit_rate'])
        self.assertEqual(decide(metrics, criteria, [])['recommendation'], 'HOLD')

    def quality_fixture(self):
        from alpha_loop.material_quality import register_labels, evaluate_quality
        snapshot = read_json(self.directory / 'snapshot.json')
        labels = []
        for doc in snapshot['documents']:
            i = int(doc['issuer_code'])
            positive = i <= 5 or 11 <= i <= 15 or i == 21
            answers = {'material_type': 'contract' if positive else 'financing',
                       'contract_stage': 'formal_contract' if positive else 'not_applicable',
                       'core_business': positive, 'guidance_timing': positive, 'explicit_amount': positive, 'dilution': not positive}
            labels.append({'document_id': doc['document_id'], 'content_hash': doc['content_hash'],
                           'annotator': 'independent synthetic fixture author', 'permission_confirmed': True,
                           'case_tags': ['positive' if positive else 'negative'], 'answers': answers,
                           'evidence_ids': {k: ['p001'] for k in answers}})
        write_json(self.root / 'gold_labels.json', {'schema_version': 1, 'label_origin': 'synthetic_fixture', 'labels': labels})
        gold = register_labels(self.root, self.root / 'gold_labels.json', self.root / 'questions.json')
        report = evaluate_quality(self.root, gold['gold_id'], self.directory / 'documents.json')
        return {'report_path': str(Path(report['report_path']).relative_to(self.root)),
                'documents_path': str((self.directory / 'documents.json').relative_to(self.root)),
                'human_reviewed': True, 'reviewer': 'fixture reviewer', 'reason': 'test only'}

    def test_quality_binds_model_and_synthetic_labels_never_validate_real_selection(self):
        proof = self.quality_fixture()
        periods = copy.deepcopy(self.spec['periods'])
        periods['holdout']['start'] = '2027-01-01'
        result = _quality(self.root, proof, self.spec['question_set'], self.spec['model_identity'], periods)
        self.assertFalse(result['qualified'])
        self.assertEqual(result['status'], 'SYNTHETIC_ONLY')
        self.assertTrue(result['proof_objects'])
        changed = {**self.spec['model_identity'], 'revision': 'new-model'}
        with self.assertRaisesRegex(ValueError, 'quality model/question'):
            _quality(self.root, proof, self.spec['question_set'], changed, periods)
        with self.assertRaisesRegex(ValueError, 'precede holdout'):
            _quality(self.root, proof, self.spec['question_set'], self.spec['model_identity'], self.spec['periods'])

    def test_five_session_overlap_excludes_both_arms_before_statistics(self):
        row = {'split': 'tuning', 'session': '2026-09-11', 'instrument_id': 'TSE:0001',
               'event_groups': [], 'exclusion_reason': None, 'A_selected': True, 'B1_selected': True}
        _purge_cluster([row], self.spec)
        self.assertEqual(row['exclusion_reason'], 'five_session_horizon_crosses_partition')

    def test_comparison_publish_crash_recovers_without_duplicate_ledger_events(self):
        from alpha_loop.material_research import _store
        real = write_json
        interrupted = []
        def fail_after_publish(path, value):
            real(path, value)
            if path.name == 'manifest.json' and path.parent.name.startswith('mcmp-') and not interrupted:
                interrupted.append(True)
                raise RuntimeError('simulated interruption after file publication')
        with patch('alpha_loop.material_research.write_json', side_effect=fail_after_publish):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                compare(self.root, self.exp)
        result = compare(self.root, self.exp)
        self.assertEqual(result['status'], 'COMPLETE')
        con = _store(self.root)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM hypothesis_events WHERE action='MATERIAL_COMPARE'").fetchone()[0], 1)
        finally:
            con.close()

    def test_daily_material_comparison_failure_keeps_price_csv(self):
        from alpha_loop.fixture import create as daily_fixture
        from alpha_loop.service import operate
        daily_fixture(self.root / 'daily_input', without_turnover=True)
        config = read_json(PROJECT / 'configs/hermes_demo.json')
        config.update(input_dir='daily_input', strategy_config=str(PROJECT / 'configs/baseline.json'),
                      materials_config=None, weekly_research_enabled=False)
        write_json(self.root / 'daily_operation.json', config)
        with patch('alpha_loop.material_research.refresh', side_effect=RuntimeError('comparison failure')):
            result = operate(self.root, self.root / 'daily_operation.json', no_ai=True)
        self.assertEqual(result['status'], 'SUCCEEDED_WITH_SIDE_FAILURE')
        self.assertIn('material_m5', result['side_failures'])
        self.assertEqual(result['candidate_count'], 3)
        self.assertTrue((self.root / 'outputs' / result['run_id'] / 'candidates.csv').exists())

    def test_issuer_context_is_bound_to_exact_inference_state(self):
        docs = read_json(self.directory / 'documents.json')
        docs[0]['identity']['state_hash'] = digest(b'different issuer context')
        write_json(self.directory / 'documents.json', docs)
        self.rehash(self.directory)
        with self.assertRaisesRegex(RuntimeError, 'issuer/text/state'):
            load_materials(self.root, self.run_id, self.spec)

    def test_b2_unknown_gate_uses_supplied_addition_inputs_separately_from_b1(self):
        from alpha_loop.material_fixture import FixtureMaterialProvider
        from alpha_loop.materials import batch
        day = read_json(self.root / 'outputs' / self.run_id / 'run_manifest.json')['target_session']
        # Authored input case: only a NONE-stage issuer supplied a disclosure.
        for path in (self.root / 'data/materials/documents').glob('*.json'):
            doc = read_json(path)
            if doc['published_at'].startswith(day) and doc['instrument_id'] != 'TSE:0021':
                path.unlink()
        shutil.rmtree(self.directory)
        with patch('alpha_loop.materials.now_iso', return_value=day + 'T20:01:00+09:00'):
            batch(self.root, self.run_id, FixtureMaterialProvider(), self.root / 'questions.json', max_documents=100)
        def strict_policy(plan):
            plan.update(hypothesis_id='H-STRICT-MATERIAL', family_id='F-STRICT-MATERIAL')
            plan['criteria']['max_unknown_rate'] = .1
        strict = self.change_plan(strict_policy)
        result = compare(self.root, strict['experiment_id'])
        self.assertIn('material_unknown_rate_exceeded', result['arms']['B1']['reason_codes'])
        self.assertNotIn('material_unknown_rate_exceeded', result['arms']['B2']['reason_codes'])
        rows = read_json(Path(result['report_path']).parent / 'samples.json')
        missing = [r for r in rows if r['session'] == day and r['A_selected']]
        self.assertTrue(all(r['material_pass'] is None for r in missing))

    def test_mock_provider_cannot_be_promoted_by_a_quality_review(self):
        with patch('alpha_loop.material_research._quality', return_value={'qualified': True}), patch('alpha_loop.material_research.now_iso', return_value='2026-08-01T20:00:00+09:00'):
            with self.assertRaisesRegex(ValueError, 'selected local OpenJev'):
                self.change_plan(lambda p: p.update(mode='prospective', hypothesis_id='H-PROVIDER', family_id='F-PROVIDER'))


if __name__ == '__main__':
    unittest.main()
