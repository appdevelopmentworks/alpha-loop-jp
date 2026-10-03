"""Check public files with an isolated Git index; never initialize the real repo."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
REQUIRED = {'.gitignore', '.gitattributes', 'README.md', 'pyproject.toml', 'uv.lock',
            'docs/18_setup.md', 'docs/19_operations.md', 'docs/20_git_publish.md',
            'configs/baseline.json', 'tests/fixtures/manual_notice.txt', 'docs/gold_labels_template.csv',
            'src/alpha_loop/cli.py', 'integrations/hermes/alpha_loop_daily.py'}
PRIVATE_ROOTS = {'.venv', '.uv-cache', '.uv-python', 'data', 'outputs', 'incoming',
                 'input', 'inputs', 'backups', 'logs', 'models', 'model-cache', 'vendor'}
IGNORED_PROBES = ['data/state.sqlite', 'outputs/example/candidates.csv', 'incoming/disclosures/manifest.json',
                  '.env', '.env.production', 'configs/hermes_operations.local.json', 'configs/local/private.json',
                  'configs/m5_drafts/example.json', '.venv/pyvenv.cfg', '.uv-cache/private.json',
                  'demo-m5/plan.json', 'models/example.safetensors', 'example.csv', 'secrets.json',
                  'credentials.json', 'example.gguf', 'pytorch_model.bin', 'private.pem']
SECRET_PATTERNS = {
    'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'github_token': r'\bgh[pousr]_[A-Za-z0-9]{30,}\b',
    'huggingface_token': r'\bhf_[A-Za-z0-9]{20,}\b',
    'openai_token': r'\bsk-[A-Za-z0-9_-]{20,}\b',
    'aws_access_key': r'\bAKIA[A-Z0-9]{16}\b',
    'credential_assignment': r'"(?:api_key|access_token|password|client_secret)"\s*:\s*"[^"\s]{8,}"',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-clean', action='store_true', help='Export only publishable files for a fresh-environment smoke test')
    args = parser.parse_args()
    audit = PROJECT / 'data/publication_audit' / uuid.uuid4().hex[:12]
    audit.mkdir(parents=True)
    git_dir = audit / 'index.git'
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

    def git(*arguments, input_=None):
        return subprocess.run(['git', '-c', 'core.excludesFile=' + os.devnull, '-c', 'core.autocrlf=false',
            '--git-dir=' + str(git_dir), '--work-tree=' + str(PROJECT), *arguments],
            cwd=PROJECT, env=env, input=input_, capture_output=True, check=True).stdout

    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(git_dir)],
                   env=env, capture_output=True, check=True)
    files = {p.decode('utf-8') for p in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if p}
    errors = ['required public file missing: ' + p for p in sorted(REQUIRED - files)]
    ignored = {p.decode('utf-8') for p in git('check-ignore', '--no-index', '-z', '--stdin',
        input_=('\0'.join(IGNORED_PROBES) + '\0').encode()).split(b'\0') if p}
    errors.extend('private probe not ignored: ' + p for p in IGNORED_PROBES if p not in ignored)
    links = 0
    for name in sorted(files):
        path = PROJECT / name
        if Path(name).parts[0] in PRIVATE_ROOTS:
            errors.append('private root included: ' + name)
        if path.stat().st_size > 2_000_000:
            errors.append('unexpected large public file: ' + name)
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            errors.append('unexpected binary public file: ' + name)
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                errors.append('possible ' + kind + ': ' + name)  # Never print token values.
        if path.suffix == '.md':
            for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)', text):
                target = target.strip().strip('<>')
                parsed = urlsplit(target)
                if parsed.scheme or target.startswith('#'):
                    continue
                relative = unquote(parsed.path)
                if not relative:
                    continue
                linked = (path.parent / relative).resolve()
                links += 1
                if not linked.is_relative_to(PROJECT):
                    errors.append('link outside repository: ' + name)
                elif linked.relative_to(PROJECT).as_posix() not in files:
                    errors.append('link target not published: ' + name + ' -> ' + target)
    attributes = git('check-attr', 'text', '--', 'src/alpha_loop/pipeline.py', 'configs/baseline.json').decode()
    if attributes.count('text: unset') != 2:
        errors.append('source/config newline conversion is not disabled')
    if errors:
        print(json.dumps({'status': 'FAILED', 'errors': errors}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    checkout = None
    if args.export_clean:
        checkout = audit / 'checkout'
        checkout.mkdir()
        git('add', '--', *sorted(files))
        git('diff', '--cached', '--check')
        git('checkout-index', '--all', '--prefix=' + checkout.as_posix() + '/')
        import hashlib
        for name in files:
            if hashlib.sha256((checkout / name).read_bytes()).digest() != hashlib.sha256((PROJECT / name).read_bytes()).digest():
                raise RuntimeError('Git export changed source bytes: ' + name)
    result = {'status': 'SUCCEEDED', 'public_file_count': len(files), 'public_files': sorted(files),
              'total_bytes': sum((PROJECT / p).stat().st_size for p in files), 'local_links_checked': links,
              'ignored_probes': sorted(ignored), 'source_bytes_preserved': True if args.export_clean else None,
              'secret_scan': 'limited pattern check; manual staged-content review remains required',
              'checkout': str(checkout) if checkout else None,
              'real_repository_initialized': False, 'remote_contacted': False}
    output = PROJECT / 'data/operations/publish_check.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'public_files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
