"""Read-only day-specific batch check; never mistake yesterday's output for today."""
import argparse
import csv
import json
from datetime import date
from pathlib import Path

from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.pipeline import _verified_manifest
from alpha_loop.runtime import status


def verify_files(directory, manifest_name):
    saved = read_json(directory / manifest_name)
    for name, sha in saved['artifacts'].items():
        file = (directory / name).resolve()
        if not file.is_relative_to(directory.resolve()) or digest(file.read_bytes()) != sha:
            raise RuntimeError('batch artifact hash mismatch')
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--session', default=now_iso()[:10])
    parser.add_argument('--save-proof', action='store_true')
    args = parser.parse_args()
    date.fromisoformat(args.session)
    root = args.root.resolve()
    state = status(root)
    result = state['latest_result'] or {}
    proof = dict(checked_at=state['checked_at'], requested_session=args.session,
                 running=state['running'], owner_unknown=state['owner_unknown'],
                 latest_completed_session=result.get('session'), verified_complete=False)
    if state['running'] or state['owner_unknown']:
        proof.update(status='RUNNING' if state['running'] else 'OWNER_UNKNOWN',
                     active_phases=[a['phase'] for a in state['active_attempts']],
                     collection_progress=state['collection_progress'])
    elif result.get('session') != args.session:
        proof['status'] = 'NO_COMPLETED_RESULT_FOR_REQUESTED_SESSION'
    else:
        directory = root / 'outputs' / result['run_id']
        manifest = _verified_manifest(directory)
        if manifest['target_session'] != args.session:
            raise RuntimeError('session differs from saved price run')
        hashes = {}
        for path, sha in result['service_artifacts'].items():
            file = Path(path).resolve()
            if not file.is_relative_to(root) or digest(file.read_bytes()) != sha:
                raise RuntimeError('service output hash mismatch')
            hashes[str(file)] = sha
        with Path(result['candidate_csv']).open(encoding='utf-8-sig', newline='') as stream:
            count = sum(1 for _ in csv.DictReader(stream))
        if count != result['candidate_count']:
            raise RuntimeError('candidate row count mismatch')
        evaluations = []
        for entry in result.get('evaluations', []):
            file = Path(entry['outcomes_csv'])
            saved = verify_files(file.parent, 'evaluation_manifest.json')
            prior = _verified_manifest(root / 'outputs' / entry['source_run_id'])
            if saved['source_run_id'] != prior['run_id'] or saved['through'] != args.session:
                raise RuntimeError('evaluation source/session mismatch')
            evaluations.append(dict(source_session=prior['target_session'], through=saved['through'], outcomes_csv=str(file)))
        weekly = result.get('weekly', {})
        if weekly.get('drafts_path'):
            verify_files(Path(weekly['drafts_path']).parent, 'manifest.json')
        materials = result.get('materials', {})
        if materials.get('documents_path'):
            verify_files(Path(materials['documents_path']).parent, 'manifest.json')
        proof.update(status=result['status'], candidate_count=count, missing_target_count=result['missing_target_count'],
                     run_id=result['run_id'], candidate_csv=result['candidate_csv'], evaluations=evaluations,
                     qwen_status=result.get('qwen_status'), weekly=weekly,
                     material_status=materials.get('status'), material_import=result.get('material_import'),
                     material_m5=result.get('material_m5'), m5=result.get('m5'),
                     side_failures=result.get('side_failures', []), artifact_hashes_verified=hashes,
                     supervisor_completed_at=(state['supervision'] or {}).get('completed_at'),
                     verified_complete=result['status'] in ('SUCCEEDED', 'SUCCEEDED_WITH_GAPS'))
    if args.save_proof:
        write_json(root / 'data/operations' / ('daily_check_' + args.session + '.json'), proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
