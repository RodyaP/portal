"""Explicit operator commands. Env vars must be supplied by the service/operator."""
import argparse
import getpass
from pathlib import Path
from .storage import db
p=argparse.ArgumentParser();p.add_argument('command',choices=['migrate','prune','reset-local-password']);args=p.parse_args()
with db() as c:
 if args.command=='migrate':c.execute(Path(__file__).with_name('schema.sql').read_text())
 elif args.command=='reset-local-password':
  from .auth import password_hash, audit, LOCAL_ID
  password=getpass.getpass('New local administrator password (12+ characters): ')
  if len(password)<12 or password!=getpass.getpass('Repeat password: '):
   raise SystemExit('Passwords must match and contain at least 12 characters.')
  row=c.execute('UPDATE portal_local SET password_hash=%s WHERE id=1 RETURNING login',(password_hash(password),)).fetchone()
  if not row:raise SystemExit('Complete initial setup first.')
  c.execute('DELETE FROM portal_sessions WHERE local=true')
  audit(c,{'id':LOCAL_ID,'name':row['login']},'Пароль локального администратора сброшен оператором сервера')
 else:
  # Only data older than the agreed 12 calendar months; future series remain intact.
  c.execute("DELETE FROM bookings WHERE ends < now() - interval '12 months'")
  c.execute("DELETE FROM audit WHERE at < now() - interval '12 months'")
  c.execute("DELETE FROM requests WHERE at < now() - interval '12 months'")
  c.execute('DELETE FROM portal_sessions WHERE expires<now()')
  c.execute("DELETE FROM portal_attempts WHERE started<now()-interval '15 minutes'")
print('Completed:',args.command)
