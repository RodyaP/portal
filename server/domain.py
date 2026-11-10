from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from fastapi import HTTPException
TZ = ZoneInfo('Asia/Yekaterinburg')

def now(): return datetime.now(TZ)
def fail(text, code=422): raise HTTPException(code, text)
def user_ref(u): return {k:u.get(k,'') for k in ('id','name','email','department')}
def can_edit(b,u):
    return u.get('admin',False) or b['organizer']['id']==u['id'] or (u.get('delegate',False) and b['creator']['id']==u['id'])
def can_view(b,u):
    return can_edit(b,u) or any(p['id']==u['id'] for p in b['participants'])
def serialize(b,u):
    view=can_view(b,u)
    result={k:b[k] for k in ('id','series_id','room','starts','ends','status','version','kind')}
    result['organizer']={'id':b['organizer']['id'],'name':b['organizer']['name']}
    result.update(can_view=view,can_edit=can_edit(b,u), title=b['title'] if view else ('Недоступно' if b['kind']=='block' else 'Занято'))
    if view: result.update({k:b[k] for k in ('purpose','creator','participants')})
    return result

def validate_interval(start,end,kind='meeting',allow_started=False,clock=None):
    clock=clock or now()
    if start.tzinfo is None or end.tzinfo is None: fail('Часовой пояс обязателен.')
    start,end=start.astimezone(TZ),end.astimezone(TZ)
    if start.date()!=end.date(): fail('Встреча должна завершиться в тот же день.')
    if start.hour<7 or end.hour>21 or (end.hour==21 and (end.minute or end.second)): fail('Рабочее время: 07:00–21:00.')
    if any(t.minute%15 or t.second or t.microsecond for t in (start,end)): fail('Шаг времени — 15 минут.')
    minutes=(end-start).total_seconds()/60
    if not 15<=minutes<=(840 if kind=='block' else 240): fail('Длительность встречи — от 15 минут до 4 часов.')
    if not allow_started and start<clock: fail('Нельзя забронировать прошедшее время.')
    if start.date()>clock.date()+timedelta(days=60): fail('Горизонт бронирования — 60 дней.')

def occurrences(payload,clock=None):
    clock=clock or now()
    try:
        day=date.fromisoformat(payload['date']); end_day=date.fromisoformat(payload.get('until') or payload['date'])
        st=datetime.fromisoformat(f"{day}T{payload['start']}:00+05:00")
        en=datetime.fromisoformat(f"{day}T{payload['end']}:00+05:00")
    except (ValueError,KeyError): fail('Проверьте дату и время.')
    rule=payload.get('repeat','none')
    if rule not in ('none','daily','weekly'): fail('Неизвестное повторение.')
    if rule=='none': end_day=day
    if end_day<day or end_day>clock.date()+timedelta(days=60): fail('Окончание серии должно быть в пределах 60 дней.')
    weekdays=payload.get('weekdays',[])
    if rule=='weekly' and (not weekdays or any(type(d)!=int or d<0 or d>6 for d in weekdays)): fail('Выберите дни недели.')
    excluded=set(payload.get('exclude',[]))
    dates=[]
    for n in range((end_day-day).days+1):
        d=day+timedelta(days=n)
        if rule=='weekly' and d.weekday() not in weekdays: continue
        if d.isoformat() in excluded: continue
        s,e=st+timedelta(days=n),en+timedelta(days=n)
        validate_interval(s,e,payload.get('kind','meeting'),clock=clock)
        dates.append((s,e))
    if not dates: fail('В серии не осталось встреч.')
    return dates

def make_ics(b,origin):
    def stamp(t): return t.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')
    def esc(s): return s.replace('\\','\\\\').replace('\n','\\n').replace(';','\\;').replace(',','\\,').replace('\r','')
    # Neutral title and no agenda or attendees; URL remains access controlled.
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//UGMK//Meeting Portal//RU','CALSCALE:GREGORIAN',
           'METHOD:PUBLISH','BEGIN:VEVENT',f"UID:{b['id']}@peregovornii.ugmkstroy.ru",f"SEQUENCE:{b['version']}",
           f'DTSTAMP:{stamp(now())}',f"DTSTART:{stamp(b['starts'])}",f"DTEND:{stamp(b['ends'])}",
           'SUMMARY:'+esc('Встреча в переговорной'),f"LOCATION:{esc('Переговорная '+str(b['room'])+' этаж')}",
           'URL:'+origin+'/?meeting='+str(b['id']),
           'DESCRIPTION:'+esc('Подробности: '+origin+'/?meeting='+str(b['id'])+'\nОбновления в Outlook вносятся вручную.'),
           'STATUS:'+('CANCELLED' if b['status']=='cancelled' else 'CONFIRMED'),'END:VEVENT','END:VCALENDAR']
    # RFC 5545 line folding counts UTF-8 octets without splitting a character.
    out=[]
    for line in lines:
        part=''
        for char in line:
            if len((part+char).encode())>73: out.append(part); part=' '+char
            else: part+=char
        out.append(part)
    return '\r\n'.join(out)+'\r\n'
