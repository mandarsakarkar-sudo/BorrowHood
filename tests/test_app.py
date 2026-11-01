import hashlib, hmac, json
from fastapi.testclient import TestClient
from app import main


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'test.db')
    main.init_db()
    return TestClient(main.app)


def register(client, name, email, community):
    r = client.post('/api/register', json={'name':name,'email':email,'password':'StrongPass123!','community_name':community})
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_and_community_listing_isolation(tmp_path, monkeypatch):
    c1=setup_client(tmp_path,monkeypatch)
    a=register(c1,'Owner One','owner@example.com','Green Block')
    l=c1.post('/api/listings',json={'title':'Cordless drill','category':'Power tools','daily_price_cents':0}).json()
    code=a['community']['join_code']
    c2=TestClient(main.app)
    register(c2,'Neighbor Two','neighbor@example.com','Other Block')
    assert c2.post('/api/communities/join',json={'join_code':code}).status_code==200
    # A member sees only the currently joined community; the newest membership is Green Block.
    rows=c2.get('/api/listings').json()
    assert any(x['id']==l['id'] for x in rows)


def test_listing_owner_only_and_booking_double_book_guard(tmp_path, monkeypatch):
    c=setup_client(tmp_path,monkeypatch)
    owner=register(c,'Owner','owner2@example.com','Oak Street')
    listing=c.post('/api/listings',json={'title':'Carpet cleaner','category':'Cleaning','daily_price_cents':1000}).json()
    other=TestClient(main.app)
    register(other,'Borrower','borrower2@example.com','Other Place')
    other.post('/api/communities/join',json={'join_code':owner['community']['join_code']})
    edit=other.patch(f"/api/listings/{listing['id']}",json={'title':'Hijacked','category':'Other'})
    assert edit.status_code==403
    b1=other.post(f"/api/listings/{listing['id']}/bookings",json={'start_date':'2026-12-01','end_date':'2026-12-03'}).json()
    assert b1['status']=='requested'
    approved=c.post(f"/api/bookings/{b1['id']}/approve",json={})
    assert approved.status_code==200
    b2=other.post(f"/api/listings/{listing['id']}/bookings",json={'start_date':'2026-12-02','end_date':'2026-12-04'})
    assert b2.status_code==409


def test_handover_qr_one_time_and_dispute(tmp_path, monkeypatch):
    c=setup_client(tmp_path,monkeypatch)
    owner=register(c,'Owner','owner3@example.com','Cedar Lane')
    listing=c.post('/api/listings',json={'title':'Ladder','category':'Other'}).json()
    other=TestClient(main.app)
    register(other,'Borrower','borrower3@example.com','Another Lane')
    other.post('/api/communities/join',json={'join_code':owner['community']['join_code']})
    b=other.post(f"/api/listings/{listing['id']}/bookings",json={'start_date':'2026-12-10','end_date':'2026-12-10'}).json()
    approved=c.post(f"/api/bookings/{b['id']}/approve",json={}).json()
    assert approved['qr_data'].startswith('data:image/png;base64,')
    payload={'booking_id':b['id'],'purpose':'handover','token':approved['handover_code']}
    scan=other.post('/api/qr/scan',json=payload)
    assert scan.status_code==200 and scan.json()['status']=='borrowed'
    again=other.post('/api/qr/scan',json=payload)
    assert again.status_code==409
    dispute=other.post(f"/api/bookings/{b['id']}/dispute",json={'reason':'The item was damaged during use.'})
    assert dispute.status_code==200
    status=other.get('/api/bookings').json()[0]
    assert status['return_disputed'] is True and status['status']=='return_review'


def test_payment_webhook_requires_signature_and_is_idempotent(tmp_path, monkeypatch):
    c=setup_client(tmp_path,monkeypatch)
    u=register(c,'Owner','owner4@example.com','Pine Court')
    listing=c.post('/api/listings',json={'title':'Saw','category':'Power tools'}).json()
    other=TestClient(main.app); register(other,'Borrower','borrower4@example.com','Other Court'); other.post('/api/communities/join',json={'join_code':u['community']['join_code']})
    b=other.post(f"/api/listings/{listing['id']}/bookings",json={'start_date':'2026-12-12','end_date':'2026-12-12'}).json()
    payload=json.dumps({'event_id':'evt_demo_001','booking_id':b['id'],'event_type':'payment_intent.succeeded','verified':True},separators=(',',':')).encode()
    assert other.post('/api/webhooks/payment-demo',content=payload).status_code==401
    signature=hmac.new(main.WEBHOOK_SECRET,payload,hashlib.sha256).hexdigest()
    first=other.post('/api/webhooks/payment-demo',content=payload,headers={'Content-Type':'application/json','X-Borrowhood-Signature':signature})
    second=other.post('/api/webhooks/payment-demo',content=payload,headers={'Content-Type':'application/json','X-Borrowhood-Signature':signature})
    assert first.status_code==200 and first.json()['duplicate'] is False
    assert second.status_code==200 and second.json()['duplicate'] is True
