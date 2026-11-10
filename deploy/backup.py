"""Portable binary-safe PostgreSQL dump; run from any working directory."""
from pathlib import Path
import os
import subprocess
import sys

if len(sys.argv) != 2:
    raise SystemExit('Usage: python deploy/backup.py /secure/path/portal.dump')
target = Path(sys.argv[1]).resolve()
root = Path(__file__).resolve().parent.parent
with target.open('xb') as output:
    os.chmod(target, 0o600)
    result = subprocess.run(['docker', 'compose', 'exec', '-T', 'db', 'pg_dump', '-U', 'portal', '-d', 'portal', '-Fc'], cwd=root, stdout=output)
if result.returncode:
    raise SystemExit('Backup failed; the output file is incomplete. Inspect logs before using it.')
print('Backup written:', target)
