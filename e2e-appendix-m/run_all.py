"""Run every step script in order and print a summary of out/results.jsonl.

    python run_all.py            # continue from out/state.json
    python run_all.py --fresh    # start a new run (clears out/); use with a freshly migrated + seeded database
    python run_all.py --from s07 # resume from a given script
    python run_all.py --fresh --reset-db   # also wipe, migrate and seed the throwaway database fundlok_e2e

Use the backend's own virtualenv (it has httpx, Pillow, pypdf, PyYAML and the app's dependencies).
"""
import collections
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
args = sys.argv[1:]
if "--fresh" in args and os.path.isdir(OUT):
    shutil.rmtree(OUT)
start = args[args.index("--from") + 1] if "--from" in args else "s00"
if "--reset-db" in args:
    BACKEND = os.path.abspath(os.path.join(HERE, ".."))
    db_url = os.environ.get("E2E_DATABASE_URL", "postgresql+asyncpg://fundlok:fundlok@localhost:5433/fundlok_e2e")
    if not db_url.endswith("/fundlok_e2e"):
        sys.exit("--reset-db only resets fundlok_e2e")
    pg = os.environ.get("E2E_PG_CONTAINER", "repo-postgres-1")
    subprocess.run(["docker", "exec", pg, "psql", "-U", "fundlok", "-d", "fundlok_e2e", "-qc",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"], check=True)
    subprocess.run([os.path.join(os.path.dirname(sys.executable), "alembic"), "upgrade", "head"], cwd=BACKEND, check=True,
                   env=dict(os.environ, DATABASE_URL=db_url.replace("+asyncpg", "+psycopg2")), stdout=subprocess.DEVNULL)
    with open(os.path.join(BACKEND, "scripts", "seed_data.sql"), "rb") as seed:
        subprocess.run(["docker", "exec", "-i", pg, "psql", "-U", "fundlok", "-d", "fundlok_e2e", "-v", "ON_ERROR_STOP=1",
                        "-q"], stdin=seed, check=True, stdout=subprocess.DEVNULL)
    print("fundlok_e2e wiped, migrated and seeded")

scripts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "s[0-9][0-9]_*.py")))
env = dict(os.environ, PYTHONIOENCODING="utf-8")
for s in scripts:
    if s < start:
        continue
    if not s.startswith("s00"):
        # admin access tokens last 30 minutes: refresh before every script
        subprocess.run([sys.executable, os.path.join(HERE, "s00_staff_sessions.py")], env=env, check=True,
                       stdout=subprocess.DEVNULL)
    print(f"\n===== {s} =====", flush=True)
    rc = subprocess.run([sys.executable, os.path.join(HERE, s)], env=env).returncode
    if rc != 0:
        print(f"!! {s} exited with {rc}; fix and resume with: python run_all.py --from {s.split('_')[0]}")
        sys.exit(rc)

results = os.path.join(OUT, "results.jsonl")
latest = {}
with open(results, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        latest[r["step"]] = r
print("\n===== summary =====")
print(dict(collections.Counter(r["result"] for r in latest.values())))
for r in latest.values():
    if r["result"] != "PASS":
        print(f"{r['result']:<9} {r['step']}")
