"""Opt-in long-text side output; default only prepares windows and uses no GPU."""
import argparse
import json
import re
import time
from pathlib import Path

from alpha_loop.common import read_json
from alpha_loop.material_long_text import analyze
from alpha_loop.material_operations import load_settings
from alpha_loop.materials import seal
from alpha_loop.model_session import local_model
from alpha_loop.runtime import status
from alpha_loop.semantic import OpenJevProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--document-id', required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--config', default='configs/materials_operations.json')
    parser.add_argument('--infer', action='store_true')
    parser.add_argument('--window-chars', type=int, default=6000)
    parser.add_argument('--overlap-chars', type=int, default=300)
    parser.add_argument('--max-chunks', type=int, default=100)
    parser.add_argument('--max-infer-chunks', type=int, default=10)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]+', args.document_id):
        parser.error('invalid document ID')
    root = args.root.resolve()
    settings = load_settings(root, args.config)
    document = root / 'data/materials/documents' / (args.document_id + '.json')
    options = dict(window_chars=args.window_chars, overlap_chars=args.overlap_chars,
                   max_chunks=args.max_chunks, max_infer_chunks=args.max_infer_chunks)
    if not args.infer:
        result = analyze(root, document, root / settings['question_set'], **options)
    else:
        if not args.run_id or settings['provider'] != 'openjev':
            parser.error('--infer requires --run-id and local OpenJev settings')
        live = status(root)
        if live['running'] or live['owner_unknown']:
            raise RuntimeError('daily batch is active; run manual GPU analysis after it finishes')
        snapshot = seal(root, args.run_id)
        if args.document_id not in {d['document_id'] for d in snapshot['documents']}:
            raise ValueError('document not eligible at the sealed price cutoff')
        runtime = read_json(root / settings['runtime_config'])
        provider = OpenJevProvider(settings['base_url'], settings['model_id'], runtime['model_revision'],
                                  runtime['base_image_digest'] + '+topk50', 'NVFP4-marlin', runtime, 30)
        began = time.monotonic()
        with local_model(root, 'openjev', min(180, settings['budget_seconds'])):
            remaining = settings['budget_seconds'] - (time.monotonic() - began)
            if remaining <= 0:
                raise TimeoutError('long-text model startup exhausted budget')
            result = analyze(root, document, root / settings['question_set'], provider,
                             budget_seconds=remaining, **options)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
