# Offline QA adapter only: PostgreSQL single-user process, no network service.
import subprocess,json,re,threading,sys
from pathlib import Path
from datetime import datetime
from psycopg.adapt import Transformer
import psycopg
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from server import app as m
from uuid import uuid4
from fastapi import HTTPException
LOCK=threading.RLock()
import os
BIN=os.environ['POSTGRES_BIN']
DATA=os.environ['POSTGRES_TEST_DATA']
class Cursor:
 def __init__(self,rows):self.rows=rows
 def fetchone(self):return self.rows[0] if self.rows else None
 def fetchall(self):return self.rows
class Conn:
 def __enter__(self):
  LOCK.acquire()
  self.p=subprocess.Popen([BIN,'--single','-D',DATA,'-c','log_error_verbosity=verbose','postgres'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
  self.execute('BEGIN');return self
 def __exit__(self,typ,val,tb):
  try:self.execute('ROLLBACK' if typ else 'COMMIT')
  finally:
   self.p.stdin.close();self.p.wait();LOCK.release()
 def execute(self,sql,args=()):
  t=Transformer();pieces=sql.split('%s');query=pieces[0]
  for v,part in zip(args,pieces[1:]):query+=t.as_literal(v).decode()+part
  query=query.strip().rstrip(';')
  select=query.lstrip().upper().startswith('SELECT')
  if select:query="SELECT coalesce(json_agg(row_to_json(q)), '[]'::json)::text AS result FROM ("+query+") q"
  sentinel='S'+uuid4().hex
  self.p.stdin.write(query.replace('\n',' ')+';\n'+f"SELECT '{sentinel}' AS sentinel;\n");self.p.stdin.flush()
  lines=[]
  while True:
   line=self.p.stdout.readline()
   if not line:raise RuntimeError('Postgres process exited')
   if f'sentinel = "{sentinel}"' in line:break
   lines.append(line)
  output=''.join(lines)
  if 'ERROR:' in output:
   if '23P01' in output:raise psycopg.errors.ExclusionViolation(output)
   raise RuntimeError(output)
  match=re.search(r'result = "(.*)"\s*\(typeid',output)
  rows=json.loads(match.group(1)) if match else []
  for row in rows:
   for k in ['starts','ends','created_at','updated_at','at']:
    if isinstance(row.get(k),str):row[k]=datetime.fromisoformat(row[k])
  return Cursor(rows)
m.db=lambda:Conn()

if __name__=='__main__':
 import os,pytest
 os.environ['TEST_DATABASE_URL']='offline-single-user-adapter'
 # Exclude concurrency: a single-user process cannot validate independent DB sessions.
 raise SystemExit(pytest.main(['tests/test_portal.py','-q','-k','not parallel_one_winner']))
