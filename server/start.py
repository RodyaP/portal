"""Apply additive migrations before accepting requests."""
from pathlib import Path
import uvicorn
from .storage import db

with db() as c:
    c.execute('SELECT pg_advisory_xact_lock(6071103)')
    c.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))

uvicorn.run('server.app:app', host='0.0.0.0', port=8000, proxy_headers=True, forwarded_allow_ips='*')
