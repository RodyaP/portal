"""Integration tests against a disposable PostgreSQL database. Never use production DB."""
import os
from pathlib import Path
from uuid import uuid4
from datetime import timedelta,datetime
from concurrent.futures import ThreadPoolExecutor
import pytest, psycopg
from fastapi.testclient import TestClient
from server import app as m
from server.domain import now, TZ, make_ics

USERS=[{'id':str(uuid4()),'name':n,'email':f'test{i}@example.invalid','department':'Тест','admin':i==3,'delegate':i==4} for i,n in enumerate(['Организатор','Участник','Посторонний','Администратор','Секретарь'])]
BASE=(now()+timedelta(days=2)).date().isoformat()

def payload(**kw):
 return dict(dict(request_id=str(uuid4()),room=1,date=BASE,start='10:00',end='11:00',title='Секретное название',purpose='Закрытая цель',participants=[USERS[1]['id']]),**kw)

@pytest.fixture(autouse=True)
def setup(monkeypatch):
 if not os.getenv('TEST_DATABASE_URL'): pytest.skip('Requires disposable PostgreSQL TEST_DATABASE_URL')
 monkeypatch.setenv('DATABASE_URL',os.environ['TEST_DATABASE_URL'])
 with m.db() as c:
  c.execute(Path('server/schema.sql').read_text())
  c.execute('TRUNCATE bookings,audit,requests,portal_mail_settings,portal_mail_outbox RESTART IDENTITY')
 monkeypatch.setattr(m.directory,'person',lambda id:next(u for u in USERS if u['id']==id))
 m.app.dependency_overrides[m.current_user]=lambda:USERS[0]
 yield
 m.app.dependency_overrides.clear()

client=TestClient(m.app)
def as_user(i):m.app.dependency_overrides[m.current_user]=lambda:USERS[i]
def send(method,url,data):return client.request(method,url,json=data,headers={'Origin':m.ORIGIN})
def create(**kw):
 p=payload();p.update(kw);r=send('POST','/api/bookings',p);assert r.status_code==200,r.text;return r.json()['ids'],p

def test_parallel_one_winner():
 def call(_):return send('POST','/api/bookings',payload()).status_code
 with ThreadPoolExecutor(max_workers=2) as ex: assert sorted(ex.map(call,range(2)))==[200,409]
 with m.db() as c: assert c.execute('SELECT count(*) FROM bookings').fetchone()['count']==1

def test_database_constraint_without_application():
 ids,p=create()
 with pytest.raises(psycopg.errors.ExclusionViolation):
  with m.db() as c:
   b=m.get_booking(c,ids[0]);b['id']=uuid4();m.insert(c,b)

def test_idempotency_and_different_payload():
 ids,p=create();r=send('POST','/api/bookings',p);assert r.json()['ids']==ids
 p['title']='Изменено';assert send('POST','/api/bookings',p).status_code==409
 with m.db() as c: assert c.execute('SELECT count(*) FROM bookings').fetchone()['count']==1

def test_adjacent_and_different_rooms():
 create();create(start='11:00',end='12:00');create(room=2)
 assert send('POST','/api/bookings',payload(start='09:30')).status_code==409

def test_privacy_and_participant_revocation():
 ids,p=create();id=ids[0];as_user(2)
 r=client.get('/api/bookings',params={'start':BASE+'T00:00:00+05:00','end':BASE+'T23:59:00+05:00'})
 assert r.status_code==200
 row=r.json()[0];assert row['title']=='Занято' and 'purpose' not in row and 'participants' not in row
 assert 'Секретное' not in r.text and 'Закрытая цель' not in r.text
 assert client.get(f'/api/bookings/{id}').status_code==403
 assert client.get(f'/api/bookings/{id}/ics').status_code==403
 as_user(1);assert client.get(f'/api/bookings/{id}').status_code==200
 assert send('POST',f'/api/bookings/{id}/cancel',{'version':1}).status_code==403
 as_user(0);p.update(request_id=str(uuid4()),version=1,participants=[])
 assert send('PUT',f'/api/bookings/{id}',p).status_code==200
 as_user(1);assert client.get(f'/api/bookings/{id}').status_code==403

def test_conflicting_series_atomic_and_exclusion():
 tomorrow=(datetime.fromisoformat(BASE)+timedelta(days=1)).date().isoformat();create(date=tomorrow)
 p=payload(repeat='daily',until=(datetime.fromisoformat(BASE)+timedelta(days=2)).date().isoformat())
 r=send('POST','/api/bookings',p);assert r.status_code==409
 assert r.json()['detail']['conflicts'][0]['date']==tomorrow
 with m.db() as c:assert c.execute('SELECT count(*) FROM bookings').fetchone()['count']==1
 p['exclude']=[tomorrow];r=send('POST','/api/bookings',p);assert len(r.json()['ids'])==2

def test_series_edit_rolls_back():
 until=(datetime.fromisoformat(BASE)+timedelta(days=2)).date().isoformat()
 ids,p=create(repeat='daily',until=until)
 create(date=until,start='12:00',end='13:00')
 v=client.get(f'/api/bookings/{ids[0]}/series').json()
 p.update(request_id=str(uuid4()),start='12:00',end='13:00',scope='future',version=1,versions=v)
 assert send('PUT',f'/api/bookings/{ids[0]}',p).status_code==409
 for id in ids:assert client.get('/api/bookings/'+id).json()['version']==1
 # Move whole series to free time, then reject stale versions.
 p.update(start='15:00',end='16:00');assert send('PUT',f'/api/bookings/{ids[0]}',p).status_code==200
 p['request_id']=str(uuid4());assert send('PUT',f'/api/bookings/{ids[0]}',p).status_code==409

def test_stale_edit_and_failed_move_preserve():
 ids,p=create();id=ids[0];create(start='12:00',end='13:00')
 p.update(version=1,request_id=str(uuid4()),start='12:00',end='13:00')
 assert send('PUT',f'/api/bookings/{id}',p).status_code==409
 assert client.get(f'/api/bookings/{id}').json()['version']==1
 p.update(start='14:00',end='15:00');assert send('PUT',f'/api/bookings/{id}',p).status_code==200
 p['request_id']=str(uuid4());assert send('PUT',f'/api/bookings/{id}',p).status_code==409

def test_delegation_admin_and_revocation():
 p=payload(organizer=USERS[2]['id'])
 assert send('POST','/api/bookings',p).status_code==403
 as_user(3);assert send('POST','/api/bookings',p).status_code==403
 as_user(4);r=send('POST','/api/bookings',p);assert r.status_code==200;id=r.json()['ids'][0]
 assert client.get('/api/bookings/'+id).json()['creator']['id']==USERS[4]['id']
 m.app.dependency_overrides[m.current_user]=lambda:dict(USERS[4],delegate=False)
 assert client.get('/api/bookings/'+id).status_code==403
 as_user(2);assert client.get('/api/bookings/'+id).json()['can_edit']
 as_user(3);assert send('POST',f'/api/bookings/{id}/cancel',{'version':1}).status_code==422
 assert send('POST',f'/api/bookings/{id}/cancel',{'version':1,'reason':'Технические работы'}).status_code==200

@pytest.mark.parametrize('change',[{'start':'06:45'},{'end':'21:15'},{'start':'10:01'},{'end':'14:15'},{'end':'10:00'},{'date':(now()-timedelta(days=1)).date().isoformat()},{'date':(now()+timedelta(days=61)).date().isoformat()},{'repeat':'weekly','until':BASE,'weekdays':[]}])
def test_limits(change):
 p=payload();p.update(change);assert send('POST','/api/bookings',p).status_code==422

def test_block_and_cancel_release():
 as_user(3);ids,p=create(kind='block',reason='Ремонт',start='07:00',end='21:00');as_user(0)
 assert send('POST','/api/bookings',payload()).status_code==409
 as_user(3);assert send('POST',f'/api/bookings/{ids[0]}/cancel',{'version':1,'reason':'Ремонт окончен'}).status_code==200
 as_user(0);create()

def test_neutral_ics_and_csrf():
 ids,p=create();r=client.get(f'/api/bookings/{ids[0]}/ics');assert r.status_code==200
 assert 'Секретное' not in r.text and 'Закрытая цель' not in r.text and 'ATTENDEE' not in r.text
 assert 'T050000Z' in r.text and 'T060000Z' in r.text
 assert max(len(line.encode()) for line in r.text.splitlines())<=75
 assert client.post('/api/bookings',json=payload()).status_code==403

def test_auth_and_real_config(monkeypatch):
 m.app.dependency_overrides.clear();assert client.get('/api/me').status_code==401
 assert 'demo:false' in client.get('/config.js').text
 monkeypatch.setenv('PROXY_SECRET','x'*32)
 assert client.get('/api/me',headers={'X-Remote-User':'admin@EKATCITY.EKAT-CITY.COM'}).status_code==401

def test_ongoing_finish_extend():
 ids,p=create()
 # Set an ongoing interval at a deterministic clock, independent of test execution hour.
 clock=datetime.fromisoformat(BASE+'T10:30:00+05:00')
 original=m.now;m.now=lambda:clock
 try:
  r=send('POST',f'/api/bookings/{ids[0]}/extend',{'version':1,'end':BASE+'T11:15:00+05:00'});assert r.status_code==200,r.text
  r=send('POST',f'/api/bookings/{ids[0]}/finish',{'version':2});assert r.status_code==200,r.text
  assert datetime.fromisoformat(client.get('/api/bookings/'+ids[0]).json()['ends'])==clock
 finally:m.now=original

def test_my_list_range():
 create();r=client.get('/api/bookings',params={'start':(now()-timedelta(days=365)).isoformat(),'end':(now()+timedelta(days=61)).isoformat(),'mine':'true'});assert r.status_code==200
