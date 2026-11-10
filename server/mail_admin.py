import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from .auth import administrator, audit, cipher
from .storage import db
from . import mail

router = APIRouter()


class MailSettings(BaseModel):
    enabled: bool = False
    host: str = Field(default='',max_length=253,pattern=r'^(?:[a-zA-Z0-9][a-zA-Z0-9.-]*)?$')
    port: int = Field(default=587,ge=1,le=65535)
    security: Literal['starttls','ssl','none'] = 'starttls'
    sender: str = Field(default='',max_length=254)
    username: str = Field(default='',max_length=256)
    password: str = Field(default='',max_length=512)

    @model_validator(mode='after')
    def validate_transport(self):
        if self.enabled and (not self.host or not mail.mailbox(self.sender)):
            raise ValueError('Укажите SMTP-сервер и корректный адрес отправителя.')
        if self.security == 'none' and (self.username or self.password):
            raise ValueError('Вход с паролем требует STARTTLS или SSL/TLS.')
        return self


def candidate(data):
    values = data.model_dump()
    with db() as c:
        old = mail.settings(c)
    values['password'] = values['password'] or (old.get('password','') if values['username']==old.get('username') else '')
    if not values['username']:
        values['password'] = ''
    if values['enabled'] and values['username'] and not values['password']:
        raise HTTPException(422,'Введите пароль учётной записи SMTP.')
    return values


@router.get('/api/admin/mail')
def get_settings(user=Depends(administrator)):
    with db() as c:
        values = mail.settings(c)
    return {**{k:v for k,v in values.items() if k!='password'},'password_set':bool(values.get('password'))}


@router.put('/api/admin/mail')
def save_settings(data:MailSettings,user=Depends(administrator)):
    values = candidate(data)
    with db() as c:
        c.execute('INSERT INTO portal_mail_settings(id,payload) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                  (cipher().encrypt(json.dumps(values).encode()).decode(),))
        audit(c,user,'Обновлены настройки почтовых уведомлений',{'enabled':values['enabled'],'host':values['host']})
    return {'ok':True}


class TestMail(MailSettings):
    recipient: str = Field(max_length=254)


@router.post('/api/admin/mail/test')
def test_mail(data:TestMail,user=Depends(administrator)):
    address = mail.mailbox(data.recipient)
    if not address or not data.host or not mail.mailbox(data.sender):
        raise HTTPException(422,'Укажите SMTP-сервер, отправителя и адрес для тестового письма.')
    # Explicit test uses form values, not necessarily saved settings.
    values = candidate(MailSettings(**data.model_dump()))
    try:
        mail.send(values,address,{'subject':'Проверка почты портала переговорных','text':'SMTP настроен: тестовое письмо портала переговорных.'},'test-'+str(uuid4()))
    except Exception as exc:
        raise HTTPException(422,'SMTP не принял тестовое письмо ('+type(exc).__name__+'). Проверьте сервер, TLS, доступ и учётную запись.')
    with db() as c:
        audit(c,user,'Отправлено тестовое письмо',{'recipient':address})
    return {'ok':True}


@router.get('/api/admin/mail/queue')
def queue(user=Depends(administrator)):
    with db() as c:
        counts = c.execute('SELECT state,count(*) AS count FROM portal_mail_outbox GROUP BY state').fetchall()
        rows = c.execute('SELECT id,booking_id,version,event,person_name,recipient,state,attempts,last_error,created_at,sent_at FROM portal_mail_outbox ORDER BY id DESC LIMIT 100').fetchall()
    return {'counts':{row['state']:row['count'] for row in counts},'items':rows}


@router.post('/api/admin/mail/queue/{identifier}/retry')
def retry(identifier:int,user=Depends(administrator)):
    with db() as c:
        row = c.execute("UPDATE portal_mail_outbox o SET state='pending',attempts=0,last_error='',next_attempt=now() WHERE id=%s AND state='failed' AND NOT EXISTS(SELECT 1 FROM portal_mail_outbox newer WHERE newer.booking_id=o.booking_id AND newer.version>o.version) RETURNING id",(identifier,)).fetchone()
        if not row:
            raise HTTPException(409,'Повтор доступен только для ошибки актуальной версии встречи.')
        audit(c,user,'Повтор отправки почтового уведомления',{'mail_id':identifier})
    return {'ok':True}
