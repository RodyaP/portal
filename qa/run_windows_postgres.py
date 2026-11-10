"""Run tests with an operator-supplied EDB binary ZIP, without a Windows service.

Usage: python qa/run_windows_postgres.py .qa-runtime/postgres.zip
All runtime files stay under .qa-runtime, and the server is stopped afterwards.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parent.parent
runtime = root / '.qa-runtime'
runtime.mkdir(exist_ok=True)
with zipfile.ZipFile(sys.argv[1]) as archive:
    for entry in archive.infolist():
        if not entry.filename.startswith(('pgsql/bin/', 'pgsql/share/', 'pgsql/lib/')):
            continue
        target = (runtime / entry.filename).resolve()
        if not target.is_relative_to(runtime.resolve()):
            raise ValueError('Unsafe archive path')
        if not target.exists():
            archive.extract(entry, runtime)
bin_dir = runtime / 'pgsql/bin'
data = runtime / 'integration-data'
if not data.exists():
    subprocess.run([str(bin_dir / 'initdb.exe'), '-D', str(data), '-U', 'tester', '-A', 'trust', '--encoding=UTF8', '--locale=C'], check=True)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
pg_ctl = str(bin_dir / 'pg_ctl.exe')
subprocess.run([pg_ctl, '-D', str(data), '-l', str(runtime / 'postgres.log'), '-o', f'-h 127.0.0.1 -p {port}', '-w', 'start'], check=True)
try:
    import psycopg
    from psycopg import sql
    database = 'portal_test_' + str(os.getpid())
    with psycopg.connect(host='127.0.0.1', port=port, user='tester', dbname='postgres', autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    env = dict(os.environ, TEST_DATABASE_URL=f'postgresql://tester@127.0.0.1:{port}/{database}')
    result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q'], cwd=root, env=env)
    if '--preview' in sys.argv and result.returncode == 0:
        from cryptography.fernet import Fernet
        import time
        with psycopg.connect(env['TEST_DATABASE_URL']) as c:
            c.execute('TRUNCATE bookings,audit,requests,portal_local,portal_roles,portal_settings,portal_sessions,portal_attempts')
        stop_file = runtime / 'stop-preview'
        if stop_file.exists():
            raise RuntimeError('Remove the previous stop-preview file before requesting another preview.')
        preview_env = dict(env, DATABASE_URL=env['TEST_DATABASE_URL'], PUBLIC_ORIGIN='http://localhost:8080',
                           ALLOW_INSECURE_LOCALHOST='true', COOKIE_SECURE='false', SETTINGS_KEY=Fernet.generate_key().decode(),
                           BOOTSTRAP_TOKEN='qa-bootstrap-token-for-browser-preview-only-2026')
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', '8080'], cwd=root, env=preview_env)
        try:
            print('QA PREVIEW: http://localhost:8080 (up to 5 minutes)', flush=True)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and not stop_file.exists() and process.poll() is None:
                time.sleep(1)
        finally:
            process.terminate()
            process.wait(timeout=15)
finally:
    subprocess.run([pg_ctl, '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True)
raise SystemExit(result.returncode)
