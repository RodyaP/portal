import os, json, hashlib, secrets, logging
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from contextlib import asynccontextmanager
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import directory
from .domain import *
from .auth import router as auth_router, current_user
from .storage import db
from . import mail
from .mail_admin import router as mail_router
ROOT=Path(__file__).resolve().parent.parent
ORIGIN=os.getenv('PUBLIC_ORIGIN','https://peregovornii.ugmkstroy.ru').rstrip('/')

@asynccontextmanager
async def lifespan(app):
    required=['DATABASE_URL','SETTINGS_KEY','BOOTSTRAP_TOKEN']
    if any(not os.getenv(k) for k in required) or len(os.getenv('BOOTSTRAP_TOKEN',''))<32:
        raise RuntimeError('Configure DATABASE_URL, SETTINGS_KEY and a BOOTSTRAP_TOKEN of at least 32 characters.')
    from .auth import cipher
    cipher()
    local_dev = os.getenv('ALLOW_INSECURE_LOCALHOST') == 'true' and ORIGIN in ('http://localhost:8080','http://127.0.0.1:8080')
    if not ORIGIN.startswith('https://') and not local_dev: raise RuntimeError('HTTPS PUBLIC_ORIGIN required.')
    if os.getenv('COOKIE_SECURE','true') != 'true' and not local_dev: raise RuntimeError('Secure session cookies required.')
    stop = asyncio.Event()
    task = asyncio.create_task(mail.worker(stop))
    try:
        yield
    finally:
        stop.set()
        await task

app=FastAPI(title='Переговорные УГМК',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
app.include_router(auth_router)
app.include_router(mail_router)

@app.get('/health')
def health():
    with db() as c: c.execute('SELECT 1')
    return {'ok':True}

@app.middleware('http')
async def safety(request,call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('origin')!=ORIGIN:
            return JSONResponse({'detail':'Запрос должен исходить с портала.'},status_code=403)
        try: length=int(request.headers.get('content-length','0'))
        except ValueError: return JSONResponse({'detail':'Некорректная длина запроса.'},status_code=400)
        if length>65536:
            return JSONResponse({'detail':'Слишком большой запрос.'},status_code=413)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin',
       'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
    return response

@app.exception_handler(psycopg.Error)
async def database_error(request,exc):
    logging.error('Database request failed: %s',type(exc).__name__)
    return JSONResponse({'detail':'Хранилище недоступно или бронь уже занята. Обновите расписание и повторите проверку.'},status_code=503)

class BookingInput(BaseModel):
    request_id: UUID = Field(default_factory=uuid4)
    room: int
    date: str
    start: str
    end: str
    title: str = Field(min_length=1,max_length=160)
    purpose: str = Field(default='',max_length=4000)
    organizer: str|None = None
    participants: list[str] = Field(default_factory=list,max_length=100)
    repeat: str='none'
    until: str|None=None
    weekdays: list[int]=Field(default_factory=list)
    exclude: list[str]=Field(default_factory=list,max_length=61)
    kind: str='meeting'
    reason: str=Field(default='',max_length=1000)
    scope: str='one'
    version: int|None=None
    versions: dict[str,int]=Field(default_factory=dict)

class ActionInput(BaseModel):
    request_id: UUID=Field(default_factory=uuid4)
    version: int
    versions: dict[str,int]=Field(default_factory=dict)
    scope: str='one'
    reason: str=Field(default='',max_length=1000)
    end: str|None=None

def get_booking(c,id):
    b=c.execute('SELECT * FROM bookings WHERE id=%s',(id,)).fetchone()
    if not b: fail('Встреча не найдена.',404)
    return b

def lock(c):
    # Low-load single application: serialize writes, including series and request deduplication.
    c.execute('SELECT pg_advisory_xact_lock(6071101)')

def audited(c,u,action,b,reason='',before=None):
    changes={'after':{k:str(b[k]) for k in ('room','starts','ends','status','version')}}
    if before: changes['before']={k:str(before[k]) for k in ('room','starts','ends','status','version')}
    c.execute('INSERT INTO audit(actor,action,booking_id,reason,changes) VALUES(%s,%s,%s,%s,%s)',
              (Jsonb(user_ref(u)),action,b['id'],reason,Jsonb(changes)))

def idempotent(c,u,p,action,fn):
    lock(c)
    data=p.model_dump(mode='json'); fingerprint=hashlib.sha256(json.dumps([action,data],sort_keys=True).encode()).hexdigest()
    cached=c.execute('SELECT * FROM requests WHERE actor=%s AND request_id=%s',(u['id'],p.request_id)).fetchone()
    if cached:
        if cached['fingerprint']!=fingerprint: fail('Ключ запроса уже использован. Откройте форму заново.',409)
        return cached['response']
    result=fn()
    c.execute('INSERT INTO requests(actor,request_id,fingerprint,response) VALUES(%s,%s,%s,%s)',(u['id'],p.request_id,fingerprint,Jsonb(result)))
    return result

def normalize(p,u):
    if p.room not in (1,2): fail('Выберите переговорную.')
    if p.kind not in ('meeting','block'): fail('Некорректный вид бронирования.')
    if not p.title.strip(): fail('Введите название встречи.')
    if p.kind=='block' and not u.get('admin'): fail('Блокировка доступна администратору.',403)
    if p.kind=='block' and not p.reason.strip(): fail('Укажите причину блокировки.')
    organizer=u['id'] if not p.organizer else p.organizer
    if organizer!=u['id'] and not u.get('delegate'): fail('Бронировать от имени другого может только группа secretary.',403)
    return {'title':p.title.strip(),'purpose':p.purpose.strip(),'room':p.room,'kind':p.kind,
            'organizer':user_ref(u) if organizer==u['id'] else directory.person(organizer),'participants':[directory.person(i) for i in dict.fromkeys(p.participants)]}

def overlaps(c,room,starts,ends,ignore=()):
    return c.execute("SELECT id FROM bookings WHERE room=%s AND status='active' AND starts<%s AND ends>%s AND NOT(id=ANY(%s::uuid[]))",(room,ends,starts,list(ignore))).fetchone() is not None

def check_conflicts(c,rows,ignore=()):
    bad=[]
    for b in rows:
        if overlaps(c,b['room'],b['starts'],b['ends'],ignore):
            bad.append({'date':b['starts'].astimezone(TZ).date().isoformat(),'start':b['starts'].astimezone(TZ).strftime('%H:%M'),'end':b['ends'].astimezone(TZ).strftime('%H:%M')})
    for i,b in enumerate(rows):
        if any(a['room']==b['room'] and a['starts']<b['ends'] and a['ends']>b['starts'] for a in rows[:i]): fail('Экземпляры серии пересекаются.')
    return bad

def prepared(p,u):
    info=normalize(p,u)
    dates=occurrences(p.model_dump())
    series=uuid4() if p.repeat!='none' else None
    return [dict(info,id=uuid4(),series_id=series,starts=s,ends=e,creator=user_ref(u),status='active',version=1) for s,e in dates]

def insert(c,b):
    fields=('id','series_id','room','starts','ends','title','purpose','organizer','creator','participants','kind','status','version')
    c.execute('INSERT INTO bookings('+','.join(fields)+') VALUES('+','.join(['%s']*len(fields))+')',tuple(Jsonb(b[k]) if k in ('organizer','creator','participants') else b[k] for k in fields))

def update(c,b):
    fields=('room','starts','ends','title','purpose','organizer','participants','status','version')
    c.execute('UPDATE bookings SET '+','.join(k+'=%s' for k in fields)+',updated_at=now() WHERE id=%s',tuple(Jsonb(b[k]) if k in ('organizer','participants') else b[k] for k in fields)+(b['id'],))

def targets(c,b,p,u):
    if p.scope not in ('one','future'): fail('Неизвестная область изменения.')
    if not can_edit(b,u): fail('Нет права изменять встречу.',403)
    if b['ends']<=now() or b['status']!='active': fail('Эта встреча доступна только для просмотра.',409)
    if p.scope=='future':
        if not b['series_id']: fail('Встреча не входит в серию.')
        rows=c.execute("SELECT * FROM bookings WHERE series_id=%s AND starts>=%s AND status='active' ORDER BY starts",(b['series_id'],max(b['starts'],now()))).fetchall()
    else: rows=[b]
    if not rows: fail('Нет будущих встреч.')
    for row in rows:
        if not can_edit(row,u): fail('Нет права изменять всю серию.',403)
        if u.get('admin') and row['organizer']['id']!=u['id'] and not p.reason.strip(): fail('Укажите причину административного изменения.')
        expected=p.versions.get(str(row['id'])) if p.scope=='future' else p.version
        if expected!=row['version']: fail('Встреча изменена другим пользователем. Обновите карточку.',409)
    return rows

@app.get('/config.js')
def config(): return Response('window.PORTAL_CONFIG={demo:false};',media_type='application/javascript')
@app.get('/api/me')
def me(u=Depends(current_user)): return u
@app.get('/api/people')
def people(q:str='',u=Depends(current_user)): return directory.people(q)
@app.get('/api/bookings')
def bookings(start:str,end:str,mine:bool=False,u=Depends(current_user)):
    try: s=datetime.fromisoformat(start); e=datetime.fromisoformat(end)
    except ValueError: fail('Некорректный период.')
    if s.tzinfo is None or e.tzinfo is None or not timedelta(0)<e-s<=timedelta(days=430): fail('Период не должен превышать 430 дней.')
    with db() as c:
        rows=c.execute('SELECT * FROM bookings WHERE starts<%s AND ends>%s ORDER BY starts',(e,s)).fetchall()
    if mine: rows=[b for b in rows if b['organizer']['id']==u['id'] or any(p['id']==u['id'] for p in b['participants']) or (u.get('delegate') and b['creator']['id']==u['id'])]
    return [serialize(b,u) for b in rows]
@app.get('/api/bookings/{id}')
def detail(id:UUID,u=Depends(current_user)):
    with db() as c: b=get_booking(c,id)
    if not can_view(b,u): fail('Доступны только время, комната и организатор.',403)
    return serialize(b,u)
@app.get('/api/bookings/{id}/series')
def series(id:UUID,u=Depends(current_user)):
    with db() as c:
        b=get_booking(c,id)
        if not can_edit(b,u): fail('Нет доступа.',403)
        rows=c.execute("SELECT * FROM bookings WHERE series_id=%s AND starts>=%s AND status='active' ORDER BY starts",(b['series_id'],max(b['starts'],now()))).fetchall()
    if any(not can_edit(r,u) for r in rows): fail('Нет доступа ко всей серии.',403)
    return {str(r['id']):r['version'] for r in rows}
@app.post('/api/bookings/preview')
def preview(p:BookingInput,u=Depends(current_user)):
    rows=prepared(p,u)
    with db() as c: conflicts=check_conflicts(c,rows)
    return {'dates':[r['starts'].astimezone(TZ).date().isoformat() for r in rows],'conflicts':conflicts}
@app.post('/api/bookings')
def create(p:BookingInput,u=Depends(current_user)):
    with db() as c:
        def work():
            rows=prepared(p,u); conflicts=check_conflicts(c,rows)
            if conflicts: raise HTTPException(409,{'message':'Время уже занято. Измените время или явно исключите даты.','conflicts':conflicts})
            for b in rows:
                insert(c,b); audited(c,u,'Создание блокировки' if b['kind']=='block' else 'Создание',b,p.reason)
                mail.enqueue(c,b,'created')
            return {'ids':[str(b['id']) for b in rows]}
        return idempotent(c,u,p,'create',work)
@app.put('/api/bookings/{id}')
def edit(id:UUID,p:BookingInput,u=Depends(current_user)):
    with db() as c:
        def work():
            base=get_booking(c,id); old=targets(c,base,p,u)
            if any(b['starts']<now() for b in old): fail('Встреча уже началась. Можно завершить или продлить её.',409)
            if p.kind!=base['kind']: fail('Нельзя менять вид бронирования.')
            # Existing organizer may be retained by an administrator without delegation rights.
            if p.organizer and p.organizer!=base['organizer']['id'] and not u.get('delegate'): fail('Изменение организатора доступно только secretary.',403)
            local=p.model_copy(update={'organizer':u['id']})
            info=normalize(local,u)
            info['organizer']=base['organizer'] if not p.organizer or p.organizer==base['organizer']['id'] else directory.person(p.organizer)
            intervals=occurrences(p.model_copy(update={'repeat':'none'}).model_dump())
            s,e=intervals[0]; rows=[]
            if p.scope=='future' and s.date()!=base['starts'].astimezone(TZ).date(): fail('Для всей серии меняйте время; дату экземпляра меняйте отдельно.')
            for b in old:
                ns=s if p.scope=='one' else datetime.combine(b['starts'].astimezone(TZ).date(),s.timetz())
                ne=ns+(e-s); validate_interval(ns,ne,b['kind'])
                rows.append(dict(b,**info,starts=ns,ends=ne,version=b['version']+1))
            conflicts=check_conflicts(c,rows,[b['id'] for b in old])
            if conflicts: raise HTTPException(409,{'message':'Перенос пересекается с занятой бронью. Исходное время сохранено.','conflicts':conflicts})
            for b,o in zip(rows,old):
                update(c,b); audited(c,u,'Изменение',b,p.reason,o)
                mail.enqueue(c,b,'updated',o)
            return {'ids':[str(b['id']) for b in rows]}
        return idempotent(c,u,p,'edit:'+str(id),work)
@app.post('/api/bookings/{id}/{action}')
def act(id:UUID,action:str,p:ActionInput,u=Depends(current_user)):
    if action not in ('cancel','finish','extend'): fail('Неизвестное действие.',404)
    with db() as c:
        def work():
            base=get_booking(c,id); old=targets(c,base,p,u); rows=[]
            for o in old:
                b=dict(o,version=o['version']+1)
                if action=='cancel':
                    if o['starts']<=now(): fail('Начавшуюся встречу можно завершить досрочно.')
                    b['status']='cancelled'
                else:
                    if p.scope!='one' or not o['starts']<now()<o['ends']: fail('Действие доступно только для текущей встречи.')
                    if action=='finish': b['ends']=now()
                    else:
                        try: b['ends']=datetime.fromisoformat(p.end or '')
                        except ValueError: fail('Укажите новое время окончания.')
                        validate_interval(b['starts'],b['ends'],b['kind'],allow_started=True)
                        if b['ends']<=o['ends']: fail('Новое окончание должно быть позже текущего.')
                        if overlaps(c,b['room'],b['starts'],b['ends'],[b['id']]): fail('Следующее время занято.',409)
                rows.append(b)
            for b,o in zip(rows,old):
                update(c,b); audited(c,u,{'cancel':'Отмена','finish':'Досрочное завершение','extend':'Продление'}[action],b,p.reason,o)
                mail.enqueue(c,b,{'cancel':'cancelled','finish':'finished','extend':'extended'}[action],o)
            return {'ids':[str(b['id']) for b in rows]}
        return idempotent(c,u,p,action+str(id),work)
@app.get('/api/bookings/{id}/ics')
def ics(id:UUID,u=Depends(current_user)):
    with db() as c: b=get_booking(c,id)
    if not can_view(b,u): fail('Нет доступа к экспорту.',403)
    return Response(make_ics(b,ORIGIN),media_type='text/calendar',headers={'Content-Disposition':f'attachment; filename="meeting-{id}.ics"'})
@app.get('/api/audit')
def audit(u=Depends(current_user)):
    if not u.get('admin'): fail('Журнал доступен администратору.',403)
    with db() as c: return c.execute('SELECT * FROM audit ORDER BY at DESC LIMIT 500').fetchall()
app.mount('/',StaticFiles(directory=ROOT/'dist',html=True),name='ui')
