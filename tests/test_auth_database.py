"""Full auth lifecycle on a disposable PostgreSQL database; no real AD needed."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from server import auth, directory
from server.app import app, ORIGIN
from server.storage import db

PERSON = {'id': str(uuid4()), 'name': 'Test Employee', 'email': 'test@example.invalid', 'department': 'Test'}


@pytest.fixture
def client(monkeypatch):
    if not os.getenv('TEST_DATABASE_URL'):
        pytest.skip('Requires disposable PostgreSQL TEST_DATABASE_URL')
    monkeypatch.setenv('DATABASE_URL', os.environ['TEST_DATABASE_URL'])
    monkeypatch.setenv('SETTINGS_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('BOOTSTRAP_TOKEN', 'bootstrap-token-' * 4)
    with db() as c:
        c.execute(Path('server/schema.sql').read_text(encoding='utf-8'))
        c.execute('TRUNCATE portal_local,portal_sessions,portal_settings,portal_roles,portal_attempts,audit,portal_mail_settings,portal_mail_outbox')
    monkeypatch.setattr(directory, 'person', lambda _: PERSON.copy())
    monkeypatch.setattr(directory, 'authenticate', lambda *_: PERSON.copy())
    monkeypatch.setattr(directory, 'check', lambda _: None)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def send(client, path, data, method='POST'):
    return client.request(method, path, json=data, headers={'Origin': ORIGIN})


def setup(client):
    response = send(client, '/api/auth/setup', {'login':'admin', 'password':'long-local-password', 'token':'bootstrap-token-' * 4})
    assert response.status_code == 200, response.text


def test_bootstrap_login_logout_and_password_change(client):
    assert client.get('/api/auth/status').json()['setup_required']
    setup(client)
    assert not client.get('/api/auth/status').json()['setup_required']
    assert client.get('/api/me').json()['local']
    assert send(client, '/api/auth/setup', {'login':'other', 'password':'long-local-password', 'token':'bootstrap-token-' * 4}).status_code == 409
    assert send(client, '/api/admin/local-password', {'current_password':'wrong','new_password':'another-long-password'}).status_code == 401
    assert send(client, '/api/admin/local-password', {'current_password':'long-local-password','new_password':'another-long-password'}).status_code == 200
    assert client.get('/api/me').status_code == 401
    assert send(client, '/api/auth/login', {'login':'admin','password':'long-local-password','local':True}).status_code == 401
    assert send(client, '/api/auth/login', {'login':'admin','password':'another-long-password','local':True}).status_code == 200
    assert send(client, '/api/auth/logout', {}).status_code == 200
    assert client.get('/api/me').status_code == 401


def test_roles_rechecked_for_existing_ad_session(client):
    setup(client)
    assert send(client, '/api/admin/roles/'+PERSON['id'], {'admin':True, 'delegate':False}, 'PUT').status_code == 200
    # Preserve local cookie to revoke privileges while AD browser is signed in.
    local_cookie = client.cookies.get(auth.COOKIE)
    assert send(client, '/api/auth/login', {'login':'employee','password':'ad-password'}).status_code == 200
    ad_cookie = client.cookies.get(auth.COOKIE)
    assert client.get('/api/me').json()['admin']
    assert client.get('/api/admin/roles').status_code == 200
    client.cookies.set(auth.COOKIE, local_cookie, domain=client.base_url.host, path='/')
    assert send(client, '/api/admin/roles/'+PERSON['id'], {'admin':False, 'delegate':True}, 'PUT').status_code == 200
    client.cookies.set(auth.COOKIE, ad_cookie, domain=client.base_url.host, path='/')
    me = client.get('/api/me').json()
    assert not me['admin'] and me['delegate']
    assert client.get('/api/admin/roles').status_code == 403


def test_ad_settings_are_encrypted_and_never_return_password(client):
    setup(client)
    values = {'host':'dc.example.test','base_dn':'DC=example,DC=test','bind_dn':'reader@example.test','realm':'EXAMPLE.TEST','password':'private-service-password'}
    assert send(client, '/api/admin/ad', values, 'PUT').status_code == 200
    assert 'private-service-password' not in client.get('/api/admin/ad').text
    with db() as c:
        raw = c.execute('SELECT payload FROM portal_settings WHERE id=1').fetchone()['payload']
        assert 'private-service-password' not in raw
    assert directory.settings()['password'] == 'private-service-password'
    values['password'] = ''
    assert send(client, '/api/admin/ad', values, 'PUT').status_code == 200
    assert directory.settings()['password'] == 'private-service-password'


def test_login_throttle_survives_requests(client):
    setup(client)
    for _ in range(20):
        assert send(client, '/api/auth/login', {'login':'admin','password':'wrong','local':True}).status_code == 401
    assert send(client, '/api/auth/login', {'login':'admin','password':'long-local-password','local':True}).status_code == 429


def test_disabled_ad_account_cannot_reuse_session(client, monkeypatch):
    from fastapi import HTTPException
    setup(client)
    assert send(client, '/api/auth/login', {'login':'employee','password':'ad-password'}).status_code == 200
    def disabled(_):
        raise HTTPException(422, 'Disabled')
    monkeypatch.setattr(directory, 'person', disabled)
    assert client.get('/api/me').status_code == 401


def test_local_login_accepts_unicode(client):
    response = send(client, '/api/auth/setup', {'login':'администратор','password':'long-local-password','token':'bootstrap-token-' * 4})
    assert response.status_code == 200
    send(client, '/api/auth/logout', {})
    assert send(client, '/api/auth/login', {'login':'администратор','password':'long-local-password','local':True}).status_code == 200
