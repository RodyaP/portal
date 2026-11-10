from datetime import datetime,timedelta
import pytest
from fastapi import HTTPException
from server.domain import *
CLOCK=datetime(2026,11,10,10,0,tzinfo=TZ)
def p(**kw): return dict(dict(date='2026-11-11',start='10:00',end='11:00'),**kw)
def test_daily_weekly_exclusions():
 assert len(occurrences(p(repeat='daily',until='2026-11-17'),CLOCK))==7
 rows=occurrences(p(repeat='weekly',until='2026-11-17',weekdays=[0,2,4],exclude=['2026-11-13']),CLOCK)
 assert [s.date().isoformat() for s,e in rows]==['2026-11-11','2026-11-16']
@pytest.mark.parametrize('params',[{'start':'06:45'},{'end':'21:15'},{'end':'10:00'},{'start':'10:01'},{'end':'14:15'},{'date':'2026-11-09'},{'date':'2027-01-10'},{'until':'2027-01-10','repeat':'daily'},{'repeat':'weekly','weekdays':[]},{'exclude':['2026-11-11']},{'repeat':'invalid'}])
def test_rejected(params):
 with pytest.raises(HTTPException):occurrences(p(**params),CLOCK)
@pytest.mark.parametrize('start,end',[('07:00','07:15'),('17:00','21:00'),('20:45','21:00')])
def test_accepted(start,end):assert len(occurrences(p(start=start,end=end),CLOCK))==1

def test_horizon_inclusive():
 end=(CLOCK.date()+timedelta(days=60)).isoformat()
 assert len(occurrences(p(date=end),CLOCK))==1

def test_privacy_role_changes():
 owner={'id':'a','name':'А'};part={'id':'b','name':'Б'};creator={'id':'c','name':'В'}
 b=dict(id='book',series_id=None,room=1,starts=CLOCK,ends=CLOCK+timedelta(hours=1),status='active',version=1,kind='meeting',organizer=owner,creator=creator,participants=[part],title='Секрет',purpose='Повестка')
 outsider=serialize(b,{'id':'x'})
 assert outsider['title']=='Занято' and 'purpose' not in outsider and 'participants' not in outsider
 assert can_view(b,part) and not can_edit(b,part)
 assert can_edit(b,dict(creator,delegate=True))
 assert not can_view(b,dict(creator,delegate=False))
 assert can_edit(b,{'id':'x','admin':True})
 b['participants']=[];assert not can_view(b,part)

def test_ics_private_fields_fold_timezone():
 b=dict(id='123',starts=CLOCK,ends=CLOCK+timedelta(hours=1),room=1,version=2,status='cancelled',title='Secret',purpose='Private',participants=[{'name':'Hidden'}])
 s=make_ics(b,'https://peregovornii.ugmkstroy.ru')
 assert 'T050000Z' in s and 'T060000Z' in s and 'SEQUENCE:2' in s
 assert 'STATUS:CANCELLED' in s and 'Secret' not in s and 'Private' not in s and 'Hidden' not in s
 assert max(len(x.encode()) for x in s.splitlines())<=75
