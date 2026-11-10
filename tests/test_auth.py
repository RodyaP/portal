"""Security checks that do not require a live domain or database."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from fastapi.testclient import TestClient
from ldap3.core.exceptions import LDAPInvalidCredentialsResult

from server import auth, directory
from server.app import app


def test_password_hashes_are_salted_and_verified():
    first = auth.password_hash('a sufficiently long password')
    second = auth.password_hash('a sufficiently long password')
    assert first != second
    assert auth.password_valid('a sufficiently long password', first)
    assert not auth.password_valid('incorrect', first)
    assert 'sufficiently' not in first


def test_settings_require_original_encryption_key(monkeypatch):
    monkeypatch.setenv('SETTINGS_KEY', Fernet.generate_key().decode())
    encrypted = auth.cipher().encrypt(b'service-account-secret')
    assert b'service-account-secret' not in encrypted
    monkeypatch.setenv('SETTINGS_KEY', Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        auth.cipher().decrypt(encrypted)


@pytest.fixture
def ldap(monkeypatch):
    config = dict(host='dc.example.test', base_dn='DC=example,DC=test', realm='EXAMPLE.TEST', bind_dn='reader', password='service-secret')
    monkeypatch.setattr(directory, 'settings', lambda: config)
    connection = MagicMock()
    connection.entries = [SimpleNamespace(entry_dn='CN=Employee,DC=example,DC=test')]
    monkeypatch.setattr(directory, 'connection', lambda *_: connection)
    monkeypatch.setattr(directory, 'encode', lambda _: {'id': 'employee', 'name': 'Employee'})
    factory = MagicMock()
    monkeypatch.setattr(directory, 'Connection', factory)
    return connection, factory


def test_domain_password_is_checked_by_bind_to_resolved_dn(ldap):
    service, factory = ldap
    assert directory.authenticate('employee', 'private-password')['id'] == 'employee'
    args = factory.call_args.kwargs
    assert args['user'] == 'CN=Employee,DC=example,DC=test'
    assert args['password'] == 'private-password'
    assert args['auto_referrals'] is False
    factory.return_value.unbind.assert_called_once()
    service.unbind.assert_called_once()


def test_ad_invalid_password_is_denied(ldap):
    _, factory = ldap
    factory.side_effect = LDAPInvalidCredentialsResult()
    with pytest.raises(HTTPException) as error:
        directory.authenticate('employee', 'wrong')
    assert error.value.status_code == 401


def test_ad_search_outage_has_service_error(ldap):
    service, _ = ldap
    service.search.side_effect = RuntimeError('private internal diagnostic')
    with pytest.raises(HTTPException) as error:
        directory.authenticate('employee', 'private-password')
    assert error.value.status_code == 503
    assert 'private' not in error.value.detail


def test_ldap_filter_is_escaped(ldap):
    service, _ = ldap
    directory.authenticate('*)(sAMAccountName=*)', 'password')
    assert r'\2a\29\28sAMAccountName=\2a\29' in service.search.call_args.args[1]


def test_empty_password_never_binds(ldap):
    _, factory = ldap
    with pytest.raises(HTTPException):
        directory.authenticate('employee', '')
    factory.assert_not_called()


def test_ldaps_requires_certificate_validation():
    import ssl
    server = directory.ldap_server({'host': 'dc.example.test'})
    assert server.ssl and server.port == 636
    assert server.tls.validate == ssl.CERT_REQUIRED


def test_non_admin_cannot_manage_roles():
    app.dependency_overrides[auth.current_user] = lambda: {'id': 'employee', 'admin': False}
    try:
        client = TestClient(app)
        assert client.get('/api/admin/roles').status_code == 403
        assert client.get('/api/admin/ad').status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_setup_requires_bootstrap_secret_before_database_access(monkeypatch):
    monkeypatch.setenv('BOOTSTRAP_TOKEN', 'x' * 48)
    client = TestClient(app)
    response = client.post('/api/auth/setup', headers={'Origin': 'https://peregovornii.ugmkstroy.ru'},
                           json={'login': 'admin', 'password': 'long-test-password', 'token': 'y' * 48})
    assert response.status_code == 403


def test_state_changes_require_origin_even_for_authentication():
    client = TestClient(app)
    assert client.post('/api/auth/login', json={'login':'admin','password':'password'}).status_code == 403
    assert client.post('/api/auth/logout', json={}).status_code == 403


def test_session_cookie_is_secure_and_token_is_not_stored(monkeypatch):
    from fastapi import Response
    monkeypatch.delenv('COOKIE_SECURE', raising=False)
    connection = MagicMock()
    response = Response()
    auth.session(connection, response, 'employee', False)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    token = cookie.split(';')[0].split('=', 1)[1]
    stored = connection.execute.call_args.args[1][0]
    assert stored == auth.digest(token) and token != stored
