"""Transactional mail outbox. SMTP never runs in the booking transaction."""
import asyncio
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formatdate
import json
import logging
import os
import re
import smtplib
import ssl

from psycopg.types.json import Jsonb
from .domain import TZ, make_ics
from .storage import db

EVENTS = {'created':'Встреча забронирована', 'updated':'Встреча изменена',
          'cancelled':'Встреча отменена', 'finished':'Встреча завершена досрочно',
          'extended':'Встреча продлена', 'removed':'Вы исключены из встречи'}


def mailbox(value):
    value = str(value or '').strip()
    # AD mail is a single SMTP address, never a display-name/recipient list.
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        return ''
    return value


def settings(c):
    from .auth import cipher
    row = c.execute('SELECT payload FROM portal_mail_settings WHERE id=1').fetchone()
    return json.loads(cipher().decrypt(row['payload'].encode())) if row else {'enabled':False}


def people(booking):
    return {str(p['id']): p for p in [booking['organizer'], *booking['participants']]}


def enqueue(c, booking, event, before=None):
    if booking['kind'] != 'meeting':
        return
    # A newer version replaces stale unsent notifications, including messages to
    # people removed from the meeting. It cannot retract already accepted mail.
    c.execute("UPDATE portal_mail_outbox SET state='skipped',last_error='replaced_by_newer_event' WHERE booking_id=%s AND state IN ('pending','failed')", (booking['id'],))
    if not settings(c).get('enabled'):
        return
    current = people(booking)
    removed = {k:v for k,v in people(before).items() if k not in current} if before else {}
    recipients = [(p,event,booking) for p in current.values()]
    recipients += [(p,'removed',dict(before, status='cancelled', version=booking['version'])) for p in removed.values()]
    seen = set()
    origin = os.environ.get('PUBLIC_ORIGIN','https://peregovornii.ugmkstroy.ru').rstrip('/')
    for person, kind, source in recipients:
        address = mailbox(person.get('email'))
        key = address.casefold() if address else 'missing:' + str(person['id'])
        if key in seen:
            continue
        seen.add(key)
        start, end = source['starts'].astimezone(TZ), source['ends'].astimezone(TZ)
        lines = [EVENTS[kind], '', source['title'],
                 f"Переговорная: {source['room']} этаж",
                 f"Дата: {start:%d.%m.%Y}", f"Время: {start:%H:%M}–{end:%H:%M} (Екатеринбург, UTC+5)"]
        if kind != 'removed':
            lines += [f"Организатор: {source['organizer']['name']}",
                      'Участники: ' + ', '.join(p['name'] for p in source['participants'])]
            if source.get('purpose'):
                lines += ['Повестка: ' + source['purpose']]
            lines += ['', 'Открыть встречу: ' + origin + '/?meeting=' + str(booking['id'])]
        lines += ['', 'Во вложении .ics для ручного импорта в Outlook. Автоматической синхронизации календаря нет.']
        payload = {'subject':EVENTS[kind] + ': ' + source['title'], 'text':'\n'.join(lines),
                   'ics':make_ics(source,origin)}
        c.execute("""INSERT INTO portal_mail_outbox(booking_id,version,event,person_id,person_name,recipient,payload,state,last_error)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(booking_id,version,person_id,event) DO NOTHING""",
                  (booking['id'],booking['version'],kind,str(person['id']),person['name'],address,Jsonb(payload),
                   'pending' if address else 'skipped','' if address else 'missing_or_invalid_ad_mail'))


@contextmanager
def smtp(config):
    context = ssl.create_default_context()
    ca = os.getenv('SMTP_CA_FILE') or os.getenv('AD_CA_FILE')
    if ca:
        context.load_verify_locations(cafile=ca)
    if config['security'] == 'ssl':
        client = smtplib.SMTP_SSL(config['host'],config['port'],timeout=10,context=context)
    else:
        client = smtplib.SMTP(config['host'],config['port'],timeout=10)
    try:
        client.ehlo()
        if config['security'] == 'starttls':
            client.starttls(context=context)
            client.ehlo()
        if config.get('username'):
            client.login(config['username'],config['password'])
        yield client
    finally:
        # A successful DATA response is the acceptance boundary; a failed QUIT
        # must not cause a second delivery of an already accepted message.
        client.close()


def send(config, recipient, payload, identifier):
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = recipient
    message['Subject'] = payload['subject'].replace('\r',' ').replace('\n',' ')
    message['Date'] = formatdate(localtime=False)
    domain = config['sender'].split('@',1)[1]
    message['Message-ID'] = f'<portal-{identifier}@{domain}>'
    message.set_content(payload['text'])
    if payload.get('ics'):
        message.add_attachment(payload['ics'].encode('utf-8'),maintype='text',subtype='calendar',
                               filename='meeting.ics',params={'charset':'UTF-8','method':'PUBLISH'})
    with smtp(config) as client:
        refused = client.send_message(message,from_addr=config['sender'],to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def deliver_one():
    with db() as c:
        if not c.execute('SELECT pg_try_advisory_xact_lock(6071104) AS locked').fetchone()['locked']:
            return False
        config = settings(c)
        if not config.get('enabled'):
            return False
        row = c.execute("SELECT * FROM portal_mail_outbox WHERE state='pending' AND next_attempt<=now() ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
        if not row:
            return False
        attempt = row['attempts'] + 1
        try:
            send(config,row['recipient'],row['payload'],row['id'])
        except Exception as exc:
            # SMTP diagnostics may contain addresses/credentials; persist only
            # exception type plus numerical SMTP code, never a raw server reply.
            error = type(exc).__name__ + (':' + str(exc.smtp_code) if hasattr(exc,'smtp_code') else '')
            c.execute("UPDATE portal_mail_outbox SET attempts=%s,state=%s,last_error=%s,next_attempt=now()+(%s * interval '1 second') WHERE id=%s",
                      (attempt,'failed' if attempt>=10 else 'pending',error,min(3600,30*2**min(attempt-1,7)),row['id']))
        else:
            c.execute("UPDATE portal_mail_outbox SET state='sent',attempts=%s,sent_at=now(),last_error='' WHERE id=%s",(attempt,row['id']))
        return True


async def worker(stop):
    while not stop.is_set():
        try:
            worked = await asyncio.to_thread(deliver_one)
        except Exception as exc:
            logging.error('Mail worker failed: %s',type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
