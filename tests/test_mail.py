"""Mail lifecycle on real PostgreSQL and a loopback-only SMTP capture server."""
from contextlib import contextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import smtplib
import socketserver
import ssl
import threading
from unittest.mock import MagicMock
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from server import auth, directory, mail
from server import app as m
from server.domain import now
from server.mail_admin import MailSettings
from server.storage import db

USERS=[dict(id=str(uuid4()),name=name,email=f'person{i}@example.invalid',department='Тест',admin=i==0,delegate=False)
       for i,name in enumerate(['Организатор','Первый участник','Второй участник'])]
BASE=(now()+timedelta(days=2)).date().isoformat()


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 local test SMTP\r\n')
        recipients=[]
        while line:=self.rfile.readline():
            command=line.decode('ascii',errors='replace').strip()
            verb=command.split(' ',1)[0].upper()
            if verb in ('EHLO','HELO'):
                if self.server.tls_context:
                    self.wfile.write(b'250-localhost\r\n250 STARTTLS\r\n')
                else:
                    self.wfile.write(b'250 localhost\r\n')
            elif verb=='STARTTLS':
                self.wfile.write(b'220 start TLS\r\n');self.wfile.flush()
                self.connection=self.server.tls_context.wrap_socket(self.connection,server_side=True)
                self.rfile=self.connection.makefile('rb');self.wfile=self.connection.makefile('wb',buffering=0)
            elif verb=='MAIL':
                recipients=[];self.wfile.write(b'250 OK\r\n')
            elif verb=='RCPT':
                recipients.append(command.split(':',1)[1].strip().strip('<>'));self.wfile.write(b'250 OK\r\n')
            elif verb=='DATA':
                self.wfile.write(b'354 data\r\n')
                body=[]
                while (part:=self.rfile.readline())!=b'.\r\n':
                    if not part:return
                    body.append(part[1:] if part.startswith(b'..') else part)
                self.server.messages.append((recipients[:],b''.join(body)))
                self.wfile.write(b'250 accepted\r\n')
            elif verb=='QUIT':
                self.wfile.write(b'221 bye\r\n');return
            else:self.wfile.write(b'250 OK\r\n')


@contextmanager
def capture_smtp(context=None):
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),SMTPHandler)
    server.daemon_threads=True;server.messages=[];server.tls_context=context
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join()


@pytest.fixture
def portal(monkeypatch):
    if not os.getenv('TEST_DATABASE_URL'):
        pytest.skip('Requires disposable PostgreSQL TEST_DATABASE_URL')
    monkeypatch.setenv('DATABASE_URL',os.environ['TEST_DATABASE_URL'])
    monkeypatch.setenv('SETTINGS_KEY',Fernet.generate_key().decode())
    with db() as c:
        c.execute(Path('server/schema.sql').read_text(encoding='utf-8'))
        c.execute('TRUNCATE bookings,audit,requests,portal_mail_settings,portal_mail_outbox RESTART IDENTITY')
    monkeypatch.setattr(directory,'person',lambda identifier:next(dict(p) for p in USERS if p['id']==identifier))
    m.app.dependency_overrides[auth.current_user]=lambda:dict(USERS[0])
    client=TestClient(m.app)
    with capture_smtp() as server:
        config=dict(enabled=True,host='127.0.0.1',port=server.server_address[1],security='none',sender='portal@example.invalid',username='',password='')
        assert request(client,'/api/admin/mail',config,'PUT').status_code==200
        yield client,server,config
    m.app.dependency_overrides.clear()
    with db() as c:c.execute('TRUNCATE portal_mail_settings,portal_mail_outbox')


def request(client,path,data,method='POST'):
    return client.request(method,path,json=data,headers={'Origin':m.ORIGIN})


def booking(client,**changes):
    payload=dict(request_id=str(uuid4()),room=1,date=BASE,start='10:00',end='11:00',title='Совещание',purpose='Повестка',participants=[USERS[1]['id']])
    payload.update(changes)
    response=request(client,'/api/bookings',payload)
    assert response.status_code==200,response.text
    return response.json()['ids'],payload


def rows():
    with db() as c:return c.execute('SELECT * FROM portal_mail_outbox ORDER BY id').fetchall()


def drain():
    while mail.deliver_one():pass


def test_creation_is_deduplicated_and_delivered_individually(portal):
    client,server,_=portal
    ids,payload=booking(client,participants=[USERS[1]['id'],USERS[1]['id'],USERS[0]['id']])
    assert request(client,'/api/bookings',payload).status_code==200
    assert len(rows())==2
    drain()
    assert len(server.messages)==2
    for recipients,raw in server.messages:
        message=BytesParser(policy=policy.default).parsebytes(raw)
        assert len(recipients)==1 and str(message['To'])==recipients[0]
        assert 'Совещание' in str(message['Subject'])
        text=message.get_body(preferencelist=('plain',)).get_content()
        assert '10:00–11:00' in text and 'Екатеринбург' in text
        assert str(ids[0]) in text
        attachment=next(message.iter_attachments())
        assert b'BEGIN:VCALENDAR' in attachment.get_payload(decode=True)
    assert all(row['state']=='sent' for row in rows())


def test_edit_notifies_removed_person_without_new_private_details(portal):
    client,server,_=portal
    ids,payload=booking(client)
    payload.update(request_id=str(uuid4()),version=1,title='Новая закрытая тема',purpose='Новая повестка',participants=[USERS[2]['id']])
    assert request(client,'/api/bookings/'+ids[0],payload,'PUT').status_code==200
    records=rows()
    assert all(row['state']=='skipped' for row in records if row['version']==1)
    removed=next(row for row in records if row['event']=='removed')
    assert removed['recipient']==USERS[1]['email']
    assert 'Новая закрытая тема' not in json.dumps(removed['payload'],ensure_ascii=False)
    assert 'Новая повестка' not in removed['payload']['text']
    drain()
    assert len(server.messages)==3


def test_cancel_and_series_enqueue_each_instance(portal):
    client,_,_=portal
    until=(now()+timedelta(days=3)).date().isoformat()
    ids,payload=booking(client,repeat='daily',until=until)
    assert len(ids)==2 and len(rows())==4
    versions=client.get('/api/bookings/'+ids[0]+'/series').json()
    response=request(client,'/api/bookings/'+ids[0]+'/cancel',dict(version=1,scope='future',versions=versions))
    assert response.status_code==200,response.text
    active=[row for row in rows() if row['state']=='pending']
    assert len(active)==4 and all(row['event']=='cancelled' for row in active)
    assert all('STATUS:CANCELLED' in row['payload']['ics'] for row in active)


def test_failed_booking_and_rolled_back_transaction_never_queue(portal):
    client,_,_=portal
    booking(client)
    count=len(rows())
    _,payload=booking(client,start='12:00',end='13:00')
    payload.update(request_id=str(uuid4()),start='10:00',end='11:00')
    assert request(client,'/api/bookings',payload).status_code==409
    assert len(rows())==count+2
    with pytest.raises(RuntimeError):
        with db() as c:
            source=m.get_booking(c,rows()[0]['booking_id'])
            source=dict(source,id=uuid4())
            mail.enqueue(c,source,'created')
            raise RuntimeError('rollback')
    assert len(rows())==count+2


def test_smtp_failure_retries_without_affecting_booking(portal,monkeypatch):
    client,_,_=portal
    ids,_=booking(client)
    monkeypatch.setattr(mail,'send',MagicMock(side_effect=smtplib.SMTPDataError(451,b'sensitive diagnostic')))
    assert mail.deliver_one()
    first=rows()[0]
    assert first['state']=='pending' and first['attempts']==1
    assert first['last_error']=='SMTPDataError:451'
    assert client.get('/api/bookings/'+ids[0]).status_code==200
    with db() as c:c.execute("UPDATE portal_mail_outbox SET attempts=9,next_attempt=now() WHERE id=%s",(first['id'],))
    mail.deliver_one()
    assert rows()[0]['state']=='failed'
    assert request(client,f"/api/admin/mail/queue/{first['id']}/retry",{}).status_code==200
    assert rows()[0]['state']=='pending' and rows()[0]['attempts']==0


def test_missing_ad_mail_is_visible_and_smtp_disable_stops_new_jobs(portal,monkeypatch):
    client,_,config=portal
    monkeypatch.setattr(directory,'person',lambda _:dict(USERS[1],email=''))
    booking(client)
    assert any(row['state']=='skipped' and row['last_error']=='missing_or_invalid_ad_mail' for row in rows())
    config['enabled']=False
    assert request(client,'/api/admin/mail',config,'PUT').status_code==200
    count=len(rows());booking(client,start='12:00',end='13:00')
    assert len(rows())==count and not mail.deliver_one()


def test_settings_secret_is_encrypted_and_access_is_admin_only(portal):
    client,_,config=portal
    values=dict(config,security='starttls',username='smtp-user',password='smtp-private-secret')
    assert request(client,'/api/admin/mail',values,'PUT').status_code==200
    assert 'smtp-private-secret' not in client.get('/api/admin/mail').text
    with db() as c:
        assert 'smtp-private-secret' not in c.execute('SELECT payload FROM portal_mail_settings').fetchone()['payload']
    values['password']=''
    assert request(client,'/api/admin/mail',values,'PUT').status_code==200
    with db() as c:assert mail.settings(c)['password']=='smtp-private-secret'
    m.app.dependency_overrides[auth.current_user]=lambda:dict(USERS[1])
    assert client.get('/api/admin/mail').status_code==403
    assert client.get('/api/admin/mail/queue').status_code==403
    assert request(client,'/api/admin/mail/test',dict(config,recipient='test@example.invalid')).status_code==403


def test_test_button_sends_to_explicit_recipient(portal):
    client,server,config=portal
    response=request(client,'/api/admin/mail/test',dict(config,recipient='test@example.invalid'))
    assert response.status_code==200,response.text
    assert server.messages[0][0]==['test@example.invalid']


def test_disabled_mail_still_invalidates_messages_for_removed_participants(portal):
    client,server,config=portal
    ids,payload=booking(client)
    config['enabled']=False
    assert request(client,'/api/admin/mail',config,'PUT').status_code==200
    payload.update(version=1,request_id=str(uuid4()),participants=[])
    assert request(client,'/api/bookings/'+ids[0],payload,'PUT').status_code==200
    config['enabled']=True
    assert request(client,'/api/admin/mail',config,'PUT').status_code==200
    drain()
    assert not server.messages
    assert all(row['state']=='skipped' for row in rows())


def test_extend_and_early_finish_have_distinct_notifications(portal,monkeypatch):
    from datetime import datetime
    client,_,_=portal
    ids,_=booking(client)
    monkeypatch.setattr(m,'now',lambda:datetime.fromisoformat(BASE+'T10:30:00+05:00'))
    assert request(client,'/api/bookings/'+ids[0]+'/extend',{'version':1,'end':BASE+'T11:15:00+05:00'}).status_code==200
    assert all(row['event']=='extended' for row in rows() if row['state']=='pending')
    assert request(client,'/api/bookings/'+ids[0]+'/finish',{'version':2}).status_code==200
    pending=[row for row in rows() if row['state']=='pending']
    assert len(pending)==2 and all(row['event']=='finished' for row in pending)


def test_concurrent_workers_do_not_send_same_job_twice(portal):
    from concurrent.futures import ThreadPoolExecutor
    client,server,_=portal
    booking(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:mail.deliver_one(),range(2)))
    drain()
    assert len(server.messages)==2
    identifiers=[BytesParser(policy=policy.default).parsebytes(raw)['Message-ID'] for _,raw in server.messages]
    assert len(set(identifiers))==2


def test_relay_rejects_plaintext_password():
    with pytest.raises(ValidationError):
        MailSettings(enabled=True,host='localhost',sender='test@example.invalid',security='none',username='user',password='secret')


@pytest.mark.parametrize('value',['','a@example.invalid\r\nBcc: other@example.invalid','a@example.invalid,b@example.invalid','Name <a@example.invalid>'])
def test_recipient_lists_and_header_injection_are_rejected(value):
    assert not mail.mailbox(value)


def test_starttls_and_password_use_verified_context(monkeypatch):
    client=MagicMock();factory=MagicMock(return_value=client)
    monkeypatch.setattr(mail.smtplib,'SMTP',factory)
    with mail.smtp(dict(host='smtp.example.invalid',port=587,security='starttls',username='user',password='password')):
        pass
    context=client.starttls.call_args.kwargs['context']
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    client.login.assert_called_once_with('user','password')
    client.close.assert_called_once()


def test_real_starttls_delivery_with_trusted_certificate(tmp_path,monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime,timezone
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    clock=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(clock-timedelta(days=1)).not_valid_after(clock+timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).sign(key,hashes.SHA256()))
    cert_path=tmp_path/'smtp.pem';key_path=tmp_path/'smtp.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert_path,key_path)
    monkeypatch.setenv('SMTP_CA_FILE',str(cert_path))
    with capture_smtp(context) as server:
        config=dict(host='localhost',port=server.server_address[1],security='starttls',sender='portal@example.invalid',username='',password='')
        mail.send(config,'test@example.invalid',{'subject':'Тест TLS','text':'Защищённая отправка'},'tls-test')
        assert len(server.messages)==1
