"""Cookie sessions, single-use bootstrap and explicit portal roles."""
import hashlib
import json
import os
import secrets
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from .storage import db
from . import directory

router = APIRouter()
LOCAL_ID = '00000000-0000-0000-0000-000000000001'
COOKIE = 'portal_session'


def cipher():
    return Fernet(os.environ['SETTINGS_KEY'].encode())


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + value


def password_valid(password, encoded):
    return secrets.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def audit(c, actor, action, changes=None):
    c.execute('INSERT INTO audit(actor,action,changes) VALUES(%s,%s,%s)',
              (Jsonb(actor), action, Jsonb(changes or {})))


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, 'Войдите в портал.')
    with db() as c:
        row = c.execute('SELECT * FROM portal_sessions WHERE token_hash=%s AND expires>now()', (digest(token),)).fetchone()
        if not row:
            raise HTTPException(401, 'Сеанс завершён. Войдите снова.')
        if row['local']:
            local = c.execute('SELECT login FROM portal_local WHERE id=1').fetchone()
            if not local:
                raise HTTPException(401, 'Войдите снова.')
            return dict(id=LOCAL_ID, name=local['login'], email='', department='Локальный администратор', admin=True, delegate=False, local=True)
        # Re-read AD state and portal roles on every request: disabling an account or
        # removing privileges takes effect even for an existing browser session.
        try:
            person = directory.person(row['person_id'])
        except HTTPException as exc:
            if exc.status_code == 422:
                raise HTTPException(401, 'Учётная запись отключена или удалена из разрешённой области AD.')
            raise
        role = c.execute('SELECT admin,delegate FROM portal_roles WHERE person_id=%s', (person['id'],)).fetchone()
        return dict(person, **(role or {'admin': False, 'delegate': False}), local=False)


def administrator(user=Depends(current_user)):
    if not user['admin']:
        raise HTTPException(403, 'Требуются права администратора портала.')
    return user


def session(c, response, person_id, local):
    token = secrets.token_urlsafe(48)
    c.execute('DELETE FROM portal_sessions WHERE expires<now()')
    c.execute("INSERT INTO portal_sessions(token_hash,person_id,local,expires) VALUES(%s,%s,%s,now()+interval '8 hours')", (digest(token), person_id, local))
    response.set_cookie(COOKIE, token, max_age=28800, httponly=True, secure=os.getenv('COOKIE_SECURE', 'true') == 'true', samesite='strict', path='/')


class Credentials(BaseModel):
    login: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)
    local: bool = False


class Setup(Credentials):
    token: str = Field(min_length=32, max_length=256)


@router.get('/api/auth/status')
def status():
    with db() as c:
        configured = bool(c.execute('SELECT 1 FROM portal_local WHERE id=1').fetchone())
    return {'setup_required': not configured, 'ad_configured': bool(directory.settings().get('host'))}


@router.post('/api/auth/setup')
def setup(data: Setup, response: Response):
    expected = os.environ.get('BOOTSTRAP_TOKEN', '')
    if len(expected) < 32 or not secrets.compare_digest(expected.encode(), data.token.encode()):
        raise HTTPException(403, 'Неверный ключ первоначальной настройки.')
    if len(data.password) < 12:
        raise HTTPException(422, 'Пароль должен содержать не менее 12 символов.')
    with db() as c:
        c.execute('SELECT pg_advisory_xact_lock(6071102)')
        if c.execute('SELECT 1 FROM portal_local WHERE id=1').fetchone():
            raise HTTPException(409, 'Первоначальная настройка уже завершена.')
        c.execute('INSERT INTO portal_local(id,login,password_hash) VALUES(1,%s,%s)', (data.login, password_hash(data.password)))
        audit(c, {'id': LOCAL_ID, 'name': data.login}, 'Создан локальный администратор')
        session(c, response, LOCAL_ID, True)
    return {'ok': True}


@router.post('/api/auth/login')
def login(data: Credentials, request: Request, response: Response):
    # Persistent per-client AND per-login throttles survive restarts and workers.
    keys = ['ip:' + (request.client.host if request.client else 'unknown'), 'user:' + data.login.casefold()]
    with db() as c:
        for key in sorted(keys):
            c.execute("INSERT INTO portal_attempts(key,attempts,started) VALUES(%s,1,now()) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN portal_attempts.started<now()-interval '15 minutes' THEN 1 ELSE portal_attempts.attempts+1 END, started=CASE WHEN portal_attempts.started<now()-interval '15 minutes' THEN now() ELSE portal_attempts.started END", (digest(key),))
        blocked = c.execute('SELECT 1 FROM portal_attempts WHERE key=ANY(%s) AND attempts>20', ([digest(k) for k in keys],)).fetchone()
    if blocked:
        raise HTTPException(429, 'Слишком много попыток входа. Повторите через 15 минут.')
    with db() as c:
        if data.local:
            row = c.execute('SELECT * FROM portal_local WHERE id=1').fetchone()
            valid = password_valid(data.password, row['password_hash'] if row else password_hash('unused-password'))
            if not row or not secrets.compare_digest(row['login'].encode(), data.login.encode()) or not valid:
                raise HTTPException(401, 'Неверный логин или пароль.')
            person_id = LOCAL_ID
        else:
            person_id = directory.authenticate(data.login, data.password)['id']
        session(c, response, person_id, data.local)
        audit(c, {'id': person_id, 'name': data.login}, 'Вход в портал')
    return {'ok': True}


@router.post('/api/auth/logout')
def logout(request: Request, response: Response):
    with db() as c:
        c.execute('DELETE FROM portal_sessions WHERE token_hash=%s', (digest(request.cookies.get(COOKIE, '')),))
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


@router.post('/api/admin/local-password')
def change_password(data: PasswordChange, user=Depends(administrator)):
    if not user['local']:
        raise HTTPException(403, 'Войдите под локальным администратором.')
    with db() as c:
        row = c.execute('SELECT password_hash FROM portal_local WHERE id=1 FOR UPDATE').fetchone()
        if not password_valid(data.current_password, row['password_hash']):
            raise HTTPException(401, 'Неверный текущий пароль.')
        c.execute('UPDATE portal_local SET password_hash=%s WHERE id=1', (password_hash(data.new_password),))
        c.execute('DELETE FROM portal_sessions WHERE local=true')
        audit(c, user, 'Изменён пароль локального администратора')
    return {'ok': True}


class ADSettings(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.-]*$')
    base_dn: str = Field(min_length=1, max_length=1000)
    bind_dn: str = Field(min_length=1, max_length=1000)
    password: str = Field(default='', max_length=256)
    realm: str = Field(min_length=1, max_length=253)


def candidate(data):
    result = data.model_dump()
    if not result['password']:
        result['password'] = directory.settings().get('password', '')
    if not result['password']:
        raise HTTPException(422, 'Введите пароль служебной учётной записи AD.')
    return result


@router.get('/api/admin/ad')
def ad_settings(user=Depends(administrator)):
    values = directory.settings()
    return {**{k: values.get(k, '') for k in ('host', 'base_dn', 'bind_dn', 'realm')}, 'password_set': bool(values.get('password'))}


@router.post('/api/admin/ad/test')
def test_ad(data: ADSettings, user=Depends(administrator)):
    directory.check(candidate(data))
    return {'ok': True}


@router.put('/api/admin/ad')
def save_ad(data: ADSettings, user=Depends(administrator)):
    values = candidate(data)
    directory.check(values)
    encrypted = cipher().encrypt(json.dumps(values).encode()).decode()
    with db() as c:
        c.execute('INSERT INTO portal_settings(id,payload) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (encrypted,))
        # Avoid keeping sessions authenticated against a previous directory.
        c.execute('DELETE FROM portal_sessions WHERE local=false')
        audit(c, user, 'Обновлены настройки AD', {'host': data.host})
    return {'ok': True}


@router.get('/api/admin/roles')
def roles(user=Depends(administrator)):
    with db() as c:
        return c.execute('SELECT person_id,person,admin,delegate FROM portal_roles ORDER BY person_id').fetchall()


class Role(BaseModel):
    admin: bool = False
    delegate: bool = False


@router.put('/api/admin/roles/{person_id}')
def assign_role(person_id: UUID, data: Role, user=Depends(administrator)):
    person = directory.person(str(person_id))
    with db() as c:
        c.execute('INSERT INTO portal_roles(person_id,person,admin,delegate) VALUES(%s,%s,%s,%s) ON CONFLICT(person_id) DO UPDATE SET person=excluded.person,admin=excluded.admin,delegate=excluded.delegate', (person['id'], Jsonb(person), data.admin, data.delegate))
        audit(c, user, 'Изменены права сотрудника: ' + person['name'], {'person': person, **data.model_dump()})
    return {'ok': True}
