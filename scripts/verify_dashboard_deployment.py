"""Back up/sync the existing Hermes wrapper, verify synthetic export, preserve job state."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT / "src"))
from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.pipeline import _code_hash
from alpha_loop.runtime import exclusive, status


def main():
    log=(PROJECT / "data/operations/dashboard_regression_stderr.log").read_text(encoding="utf-8-sig")
    match=re.search(r"Ran (\d+) tests in ([\d.]+)s",log)
    if not match or int(match[1])<254 or not log.rstrip().endswith("OK"):
        raise RuntimeError("full regression must pass before deployment")
    hermes=Path.home() / "AppData/Local/hermes"
    sources=[(PROJECT / "integrations/hermes/alpha_loop_daily.py",hermes / "scripts/alpha_loop_daily.py"),
             (PROJECT / "integrations/hermes/alpha-loop-jp/SKILL.md",hermes / "skills/alpha-loop-jp/SKILL.md")]
    root=PROJECT / "data/dw" / uuid.uuid4().hex[:8]
    root.mkdir(parents=True)
    empty=root / "git_config";empty.write_bytes(b"")
    git_env={**os.environ,"GIT_CONFIG_GLOBAL":str(empty),"GIT_CONFIG_NOSYSTEM":"1"}
    for source,target in sources:
        relative=source.relative_to(PROJECT).as_posix()
        committed=subprocess.run(["git","-c","core.excludesFile="+str(empty),"show","HEAD:"+relative],
                                 cwd=PROJECT,env=git_env,capture_output=True,check=True).stdout
        if committed.replace(b"\r\n",b"\n").strip()!=target.read_bytes().replace(b"\r\n",b"\n").strip():
            raise RuntimeError("installed Hermes source differs from committed version; preserve custom changes")
    with exclusive(PROJECT / "data/operations/.supervisor.lock"):
        live=status(PROJECT)
        if live["running"] or live["owner_unknown"]:
            raise RuntimeError("daily operation must be idle before synchronization")
        protected=[hermes / "cron/jobs.json", PROJECT / "data/operations/latest_service.json", PROJECT / "configs/baseline.json"]
        protected += list((PROJECT / "outputs").glob("run-*/candidates.csv"))
        hashes={p:digest(p.read_bytes()) for p in protected}
        numerical=_code_hash()
        shutil.copytree(PROJECT / "src/alpha_loop",root / "src/alpha_loop",ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(PROJECT / "dashboard",root / "dashboard")
        (root / "configs").mkdir()
        shutil.copy2(PROJECT / "configs/baseline.json",root / "configs/baseline.json")
        fixture=create(root / "input",without_turnover=True)
        write_json(root / "input/calendar.json",read_json(root / "input/future.json")["calendar"])
        config=read_json(PROJECT / "configs/hermes_demo.json");config["input_dir"]="input"
        write_json(root / "configs/operation.json",config)
        env={**os.environ,"ALPHA_LOOP_ROOT":str(root),"ALPHA_LOOP_CONFIG":"configs/operation.json",
             "ALPHA_LOOP_SESSION":fixture["target_session"],"ALPHA_LOOP_NO_AI":"1"}
        env.pop("ALPHA_LOOP_LIMIT",None)
        backups=[]
        for source,target in sources:
            backup=root / ("old_"+target.name);shutil.copy2(target,backup);backups.append((backup,target))
        try:
            for source,target in sources:shutil.copy2(source,target)
            completed=subprocess.run([sys.executable,str(sources[0][1])],cwd=root,env=env,
                                     capture_output=True,text=True,encoding="utf-8",timeout=180)
            (root / "wrapper_stdout.json").write_text(completed.stdout,encoding="utf-8")
            (root / "wrapper_stderr.log").write_text(completed.stderr,encoding="utf-8")
            if completed.returncode:raise RuntimeError("isolated installed wrapper failed")
            receipt=json.loads(completed.stdout)
            local=read_json(root / "outputs/dashboard/local/data/dashboard.json")
            export=read_json(root / "data/operations/dashboard/latest.json")
            assert receipt["result"]["candidate_count"]==3
            assert len(local["days"])==1 and local["days"][0]["stats"]["candidates"]==3
            assert export["publication"]=="DISABLED" and export["network_used"] is False
            assert all(digest(p.read_bytes())==hash_ for p,hash_ in hashes.items())
            assert _code_hash()==numerical
            assert all(digest(a.read_bytes())==digest(b.read_bytes()) for a,b in sources)
        except BaseException:
            for backup,target in backups:shutil.copy2(backup,target)
            raise
        proof={"status":"SUCCEEDED","verified_at":now_iso(),"tests":int(match[1]),"wrapper_root":str(root),
               "installed_sources_synced":True,"installed_wrapper_synthetic_export":True,"publication_enabled":False,
               "daily_schedule_preserved":True,"real_receipt_and_candidate_files_preserved":True,
               "numerical_code_hash_preserved":True,"new_live_scheduled_run_verified":False}
        write_json(PROJECT / "data/operations/dashboard_deployment.json",proof)
    print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
