"""Dry run by default. --push sends only reviewed static files to gh-pages."""
import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import read_json
from alpha_loop.dashboard_publish import publish

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=PROJECT / "outputs/dashboard/demo")
    parser.add_argument("--push", action="store_true")
    options = parser.parse_args()
    local = PROJECT / "configs/dashboard_publication.local.json"
    policy = read_json(local if local.exists() else PROJECT / "configs/dashboard_publication.example.json")
    print(json.dumps(publish(options.site, PROJECT, policy, push=options.push), ensure_ascii=False))
