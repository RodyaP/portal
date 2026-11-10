from fastapi.testclient import TestClient
from server.app import app
c=TestClient(app)
def test_no_corporate_identity():
 assert c.get('/api/me').status_code==401
 assert c.get('/api/bookings?start=2026-11-10&end=2026-11-11').status_code==401
 assert c.get('/api/people?q=Иван').status_code==401

def test_spoofed_header_no_secret():
 assert c.get('/api/me',headers={'X-Remote-User':'administrator@EKATCITY.EKAT-CITY.COM'}).status_code==401

def test_same_origin_required():
 assert c.post('/api/bookings',json={}).status_code==403
 assert c.post('/api/bookings',json={},headers={'Origin':'https://foreign.invalid'}).status_code==403

def test_production_not_demo():
 r=c.get('/config.js');assert 'demo:false' in r.text
 assert r.headers['cache-control']=='no-store'
 assert "frame-ancestors 'none'" in r.headers['content-security-policy']
