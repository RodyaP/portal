"""Read-only AD lookup. No demo fallback and no storage of user passwords."""
import os, ssl, uuid
from ldap3 import Server, Connection, Tls, SUBTREE
from ldap3.utils.conv import escape_filter_chars
from fastapi import HTTPException

ATTRS = ['objectGUID', 'displayName', 'mail', 'department', 'sAMAccountName', 'userPrincipalName']
BASE_FILTER = '(&(objectCategory=person)(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))'

def settings():
    from .storage import db
    from .auth import cipher
    import json
    with db() as c:
        row = c.execute('SELECT payload FROM portal_settings WHERE id=1').fetchone()
    if row:
        return json.loads(cipher().decrypt(row['payload'].encode()))
    return dict(host=os.getenv('AD_HOST',''), base_dn=os.getenv('AD_BASE_DN',''),
                bind_dn=os.getenv('AD_BIND_DN',''), password=os.getenv('AD_BIND_PASSWORD',''),
                realm=os.getenv('AD_REALM',''))

def ldap_server(config):
    if not config.get('host'):
        raise HTTPException(503, 'Подключение к AD ещё не настроено.')
    return Server(config['host'], port=636, use_ssl=True,
                  tls=Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=os.getenv('AD_CA_FILE') or None), connect_timeout=5)

def check(config):
    c = connection(config)
    try:
        if not c.search(config['base_dn'], '(objectClass=*)', search_scope='BASE', attributes=['distinguishedName']):
            raise HTTPException(422, 'Область поиска AD не найдена или недоступна.')
    finally:
        c.unbind()

def authenticate(login, password):
    config = settings()
    # Resolve the account using the read-only service identity, then bind as its
    # exact DN. Never attempt anonymous/empty-password binds or follow referrals.
    if not password:
        raise HTTPException(401, 'Неверный логин или пароль.')
    if '\\' in login:
        domain, login = login.split('\\', 1)
        if domain.casefold() != config.get('realm', '').split('.')[0].casefold():
            raise HTTPException(401, 'Неверный домен учётной записи.')
    if '@' in login:
        filt = f'(userPrincipalName={escape_filter_chars(login)})'
    else:
        filt = f'(sAMAccountName={escape_filter_chars(login)})'
    c = connection(config)
    try:
        c.search(config['base_dn'], f'(&{BASE_FILTER}{filt})', SUBTREE, attributes=ATTRS, size_limit=2)
        if len(c.entries) != 1:
            raise HTTPException(401, 'Неверный логин или пароль.')
        entry = c.entries[0]
        person = encode(entry)
        from ldap3.core.exceptions import LDAPInvalidCredentialsResult
        try:
            user_connection = Connection(ldap_server(config), user=entry.entry_dn, password=password,
                                         auto_bind=True, receive_timeout=8, raise_exceptions=True, auto_referrals=False)
            user_connection.unbind()
        except LDAPInvalidCredentialsResult:
            raise HTTPException(401, 'Неверный логин или пароль.')
        except Exception:
            raise HTTPException(503, 'Не удалось проверить учётную запись в AD.')
        return person
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Не удалось проверить учётную запись в AD.')
    finally:
        c.unbind()

def connection(config=None):
    config = config or settings()
    try:
        if not config.get('bind_dn') or not config.get('password'):
            raise HTTPException(503, 'Подключение к AD ещё не настроено.')
        return Connection(ldap_server(config), user=config['bind_dn'], password=config['password'],
                          auto_bind=True, receive_timeout=8, raise_exceptions=True, auto_referrals=False)
    except Exception:
        raise HTTPException(503, 'Корпоративный справочник недоступен. Обратитесь в ИТ-службу.')

def encode(entry):
    return {'id': str(uuid.UUID(bytes_le=bytes(entry.objectGUID.raw_values[0]))),
            'name': str(entry.displayName.value or ''), 'email': str(entry.mail.value or ''),
            'department': str(entry.department.value or '')}

def query(filt, limit=30):
    config = settings()
    c = connection(config)
    try:
        c.search(config['base_dn'], f'(&{BASE_FILTER}{filt})', SUBTREE,
                 attributes=ATTRS, size_limit=limit)
        return [encode(e) for e in c.entries if e.displayName.value]
    except Exception:
        raise HTTPException(503, 'Ошибка поиска в корпоративном справочнике.')
    finally:
        c.unbind()

def people(term):
    if len(term.strip()) < 2:
        return []
    s = escape_filter_chars(term.strip()[:100])
    return query(f'(|(displayName=*{s}*)(mail=*{s}*))')

def person(identifier):
    try:
        raw = uuid.UUID(identifier).bytes_le
    except ValueError:
        raise HTTPException(422, 'Некорректный идентификатор сотрудника.')
    v = ''.join('\\%02x' % b for b in raw)
    matches = query(f'(objectGUID={v})', 1)
    if not matches:
        raise HTTPException(422, 'Сотрудник не найден или отключён в AD.')
    return matches[0]

