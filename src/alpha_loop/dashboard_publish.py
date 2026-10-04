"""Publish exactly the allowlisted static site using an isolated checkout."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .common import digest, read_json, write_json
from .dashboard import enforce_publication

FILES = ("index.html", "style.css", "app.js", ".nojekyll", "data/dashboard.json")
REMOTE = "https://github.com/appdevelopmentworks/alpha-loop-jp.git"


def publish(site: Path, root: Path, policy: dict, *, push: bool = False) -> dict:
    if policy.get("remote_url") != REMOTE or policy.get("branch") != "gh-pages":
        raise ValueError("publication target must be this project's gh-pages branch")
    enforce_publication(read_json(site / "data/dashboard.json"), policy)
    present = {p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()}
    if present != set(FILES) or any(p.is_symlink() for p in site.rglob("*")):
        raise ValueError("publication directory contains unexpected files")
    hashes = {name: digest((site / name).read_bytes()) for name in FILES}
    if not push:
        return {"status": "DRY_RUN", "branch": "gh-pages", "files": hashes, "network_used": False}
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # Do not reset/switch/stage the user's repository or force push any branch.
    with tempfile.TemporaryDirectory(prefix="alpha-loop-pages-") as temporary:
        checkout = Path(temporary)
        def git(*args):
            completed = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=checkout,
                                       env=env, capture_output=True, timeout=120)
            if completed.returncode:
                # Credential manager output can contain private values: don't log it.
                raise RuntimeError("GitHub Pages Git operation failed: " + args[0])
            return completed.stdout.decode("utf-8").strip()
        git("init", "--initial-branch=gh-pages")
        git("remote", "add", "origin", REMOTE)
        existing = git("ls-remote", "--heads", "origin", "refs/heads/gh-pages")
        if existing:
            git("fetch", "--depth=1", "origin", "gh-pages")
            git("reset", "--hard", "FETCH_HEAD")  # Isolated temporary checkout only.
            tracked = set(git("ls-files").splitlines())
            if tracked - set(FILES):
                raise ValueError("existing gh-pages contains unrelated files; review before publishing")
        for name in FILES:
            target = checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((site / name).read_bytes())
        git("add", "--", *FILES)
        if not git("diff", "--cached", "--name-only"):
            result = {"status": "UNCHANGED", "branch": "gh-pages", "network_used": True}
        else:
            git("-c", "user.name=Alpha Loop Dashboard", "-c", "user.email=dashboard@users.noreply.github.com",
                "commit", "-m", "Update read-only dashboard")
            git("push", "origin", "HEAD:refs/heads/gh-pages")
            result = {"status": "PUSHED", "branch": "gh-pages", "commit": git("rev-parse", "HEAD"),
                      "network_used": True}
    write_json(root / "data/operations/dashboard/publication.json", {**result, "files": hashes})
    return result
