from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, sqlite3, time, io
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import qrcode
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BORROWHOOD_DB", str(ROOT.parent / "borrowhood.db")))
SECRET = os.getenv("BORROWHOOD_SECRET", "dev-only-change-this-secret-before-deployment").encode()
WEBHOOK_SECRET = os.getenv("BORROWHOOD_WEBHOOK_SECRET", "dev-webhook-secret-change-before-deployment").encode()
app = FastAPI(title="BorrowHood", version="0.1.0", description="Neighborhood tool library MVP")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS communities(id INTEGER PRIMARY KEY, name TEXT NOT NULL, join_code TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS memberships(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'member', karma INTEGER NOT NULL DEFAULT 0, joined_at INTEGER NOT NULL, PRIMARY KEY(user_id,community_id));
        CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id), owner_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Other', condition TEXT NOT NULL DEFAULT 'Good', daily_price_cents INTEGER NOT NULL DEFAULT 0, deposit_cents INTEGER NOT NULL DEFAULT 0, image_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id), listing_id INTEGER NOT NULL REFERENCES listings(id), borrower_id INTEGER NOT NULL REFERENCES users(id), owner_id INTEGER NOT NULL REFERENCES users(id), start_date TEXT NOT NULL, end_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'requested', payment_status TEXT NOT NULL DEFAULT 'not_required', handover_token_hash TEXT, return_token_hash TEXT, return_disputed INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE, sender_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id), reviewer_id INTEGER NOT NULL REFERENCES users(id), reviewee_id INTEGER NOT NULL REFERENCES users(id), rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), comment TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_events(id INTEGER PRIMARY KEY, provider_event_id TEXT UNIQUE NOT NULL, booking_id INTEGER NOT NULL REFERENCES bookings(id), event_type TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        ''')

@app.on_event("startup")
def startup(): init_db()

class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    community_name: str = Field(min_length=2, max_length=100)
class LoginIn(BaseModel): email: str; password: str
class CommunityIn(BaseModel): name: str = Field(min_length=2, max_length=100)
class JoinIn(BaseModel): join_code: str = Field(min_length=4, max_length=32)
class ListingIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="Other", max_length=50)
    condition: str = Field(default="Good", max_length=40)
    daily_price_cents: int = Field(default=0, ge=0, le=10000000)
    deposit_cents: int = Field(default=0, ge=0, le=100000000)
    image_url: str = Field(default="", max_length=500)
class BookingIn(BaseModel): start_date: str; end_date: str; note: str = Field(default="", max_length=1000)
class MessageIn(BaseModel): body: str = Field(min_length=1, max_length=2000)
class ReviewIn(BaseModel): rating: int = Field(ge=1, le=5); comment: str = Field(default="", max_length=1000)
class DisputeIn(BaseModel): reason: str = Field(min_length=5, max_length=1000)
class QRScanIn(BaseModel): booking_id: int; purpose: str; token: str = Field(min_length=12, max_length=200)
class DemoPaymentEvent(BaseModel): event_id: str = Field(min_length=4, max_length=200); booking_id: int; event_type: str; verified: bool = True

# Signed, expiring cookie token; production deployments should set BORROWHOOD_SECRET.
def make_session(user_id: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "exp": int(time.time()) + 60*60*24*7}, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig

def read_session(token: str) -> Optional[int]:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] < int(time.time()): return None
        return int(data["uid"])
    except Exception: return None

def current_user(request: Request) -> dict:
    uid = read_session(request.cookies.get("bh_session", ""))
    if not uid: raise HTTPException(401, "Please sign in to continue")
    with db() as c:
        row = c.execute("SELECT id,name,email FROM users WHERE id=?", (uid,)).fetchone()
        if not row: raise HTTPException(401, "Session is no longer valid")
        return dict(row)

def active_community(c, uid: int) -> dict:
    row = c.execute("SELECT c.id,c.name,c.join_code,m.karma,m.role FROM memberships m JOIN communities c ON c.id=m.community_id WHERE m.user_id=? ORDER BY m.rowid DESC LIMIT 1", (uid,)).fetchone()
    if not row: raise HTTPException(403, "Join a community before using BorrowHood")
    return dict(row)

def require_member(c, uid: int, community_id: int):
    if not c.execute("SELECT 1 FROM memberships WHERE user_id=? AND community_id=?", (uid, community_id)).fetchone():
        raise HTTPException(404, "Record not found in your community")

def booking_row(c, booking_id: int, uid: int):
    b = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not b: raise HTTPException(404, "Booking not found")
    require_member(c, uid, b["community_id"])
    if uid not in (b["borrower_id"], b["owner_id"]): raise HTTPException(403, "Only booking participants can access this booking")
    return b

def safe_listing(row):
    d = dict(row); d["daily_price"] = d.pop("daily_price_cents") / 100; d["deposit"] = d.pop("deposit_cents") / 100
    return d

def qr_data(payload: dict) -> str:
    img = qrcode.make(json.dumps(payload, separators=(",", ":")))
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def safe_booking(row):
    d = dict(row); d.pop("handover_token_hash", None); d.pop("return_token_hash", None); d["return_disputed"] = bool(d["return_disputed"]); return d

@app.get("/", response_class=HTMLResponse)
def home(): return (ROOT / "static" / "index.html").read_text(encoding="utf-8")
@app.get("/api/health")
def health(): return {"ok": True, "app": "BorrowHood"}

@app.post("/api/register")
def register(data: RegisterIn, response: Response):
    email = data.email.strip().lower()
    if "@" not in email: raise HTTPException(422, "Enter a valid email address")
    salt = secrets.token_bytes(16); ph = salt.hex() + ":" + hashlib.scrypt(data.password.encode(), salt=salt, n=2**14, r=8, p=1).hex()
    now = int(time.time())
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): raise HTTPException(409, "An account with this email already exists")
        code = secrets.token_urlsafe(7).replace("-", "").replace("_", "")[:8].upper()
        cur = c.execute("INSERT INTO communities(name,join_code,created_at) VALUES(?,?,?)", (data.community_name.strip(), code, now)); cid = cur.lastrowid
        cur = c.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)", (data.name.strip(), email, ph, now)); uid = cur.lastrowid
        c.execute("INSERT INTO memberships(user_id,community_id,role,joined_at) VALUES(?,?,?,?)", (uid,cid,"admin",now))
    response.set_cookie("bh_session", make_session(uid), httponly=True, samesite="lax", secure=os.getenv("BORROWHOOD_SECURE_COOKIE", "0") == "1", max_age=60*60*24*7)
    return {"user":{"id":uid,"name":data.name.strip(),"email":email},"community":{"id":cid,"name":data.community_name.strip(),"join_code":code}}

@app.post("/api/login")
def login(data: LoginIn, response: Response):
    with db() as c: row = c.execute("SELECT * FROM users WHERE email=?", (data.email.strip().lower(),)).fetchone()
    if not row: raise HTTPException(401, "Email or password is incorrect")
    try:
        salt_hex, digest = row["password_hash"].split(":",1)
        check = hashlib.scrypt(data.password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1).hex()
        if not hmac.compare_digest(check, digest): raise ValueError()
    except Exception: raise HTTPException(401, "Email or password is incorrect")
    response.set_cookie("bh_session", make_session(row["id"]), httponly=True, samesite="lax", secure=os.getenv("BORROWHOOD_SECURE_COOKIE", "0") == "1", max_age=60*60*24*7)
    return {"user":{"id":row["id"],"name":row["name"],"email":row["email"]}}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("bh_session", httponly=True, samesite="lax"); return {"ok":True}

@app.get("/api/me")
def me(user=Depends(current_user)):
    with db() as c:
        comm = active_community(c,user["id"])
        communities = [dict(r) for r in c.execute("SELECT c.id,c.name,c.join_code,m.role,m.karma FROM memberships m JOIN communities c ON c.id=m.community_id WHERE m.user_id=?",(user["id"],)).fetchall()]
    return {"user":user,"community":comm,"communities":communities}

@app.post("/api/communities")
def create_community(data: CommunityIn, user=Depends(current_user)):
    now=int(time.time()); code=secrets.token_urlsafe(7).replace("-", "").replace("_", "")[:8].upper()
    with db() as c:
        cur=c.execute("INSERT INTO communities(name,join_code,created_at) VALUES(?,?,?)",(data.name.strip(),code,now)); cid=cur.lastrowid
        c.execute("INSERT INTO memberships(user_id,community_id,role,joined_at) VALUES(?,?,?,?)",(user["id"],cid,"admin",now))
    return {"id":cid,"name":data.name.strip(),"join_code":code}

@app.post("/api/communities/join")
def join_community(data: JoinIn, user=Depends(current_user)):
    with db() as c:
        comm=c.execute("SELECT * FROM communities WHERE join_code=?",(data.join_code.strip().upper(),)).fetchone()
        if not comm: raise HTTPException(404,"That community code was not found")
        c.execute("INSERT OR IGNORE INTO memberships(user_id,community_id,role,joined_at) VALUES(?,?,?,?)",(user["id"],comm["id"],"member",int(time.time())))
    return {"id":comm["id"],"name":comm["name"],"join_code":comm["join_code"]}

@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    with db() as c:
        comm=active_community(c,user["id"]); cid=comm["id"]
        counts={"listings":c.execute("SELECT COUNT(*) FROM listings WHERE community_id=? AND status='active'",(cid,)).fetchone()[0],"my_listings":c.execute("SELECT COUNT(*) FROM listings WHERE community_id=? AND owner_id=?",(cid,user["id"])).fetchone()[0],"my_bookings":c.execute("SELECT COUNT(*) FROM bookings WHERE community_id=? AND (borrower_id=? OR owner_id=?)",(cid,user["id"],user["id"])).fetchone()[0],"pending":c.execute("SELECT COUNT(*) FROM bookings WHERE community_id=? AND owner_id=? AND status='requested'",(cid,user["id"])).fetchone()[0]}
        recent=[safe_listing(r) for r in c.execute("SELECT l.*,u.name owner_name FROM listings l JOIN users u ON u.id=l.owner_id WHERE l.community_id=? AND l.status='active' ORDER BY l.created_at DESC LIMIT 6",(cid,)).fetchall()]
    return {"community":comm,"counts":counts,"recent":recent}

@app.get("/api/listings")
def list_listings(q: str="", category: str="", user=Depends(current_user)):
    with db() as c:
        comm=active_community(c,user["id"]); sql="SELECT l.*,u.name owner_name FROM listings l JOIN users u ON u.id=l.owner_id WHERE l.community_id=? AND l.status='active'"; args=[comm["id"]]
        if q.strip(): sql+=" AND (l.title LIKE ? OR l.description LIKE ?)"; args += [f"%{q.strip()}%",f"%{q.strip()}%"]
        if category and category!="All": sql+=" AND l.category=?"; args.append(category)
        sql+=" ORDER BY l.created_at DESC"
        rows=c.execute(sql,args).fetchall()
    return [safe_listing(r) for r in rows]

@app.post("/api/listings")
def create_listing(data: ListingIn, user=Depends(current_user)):
    now=int(time.time())
    with db() as c:
        comm=active_community(c,user["id"])
        cur=c.execute("INSERT INTO listings(community_id,owner_id,title,description,category,condition,daily_price_cents,deposit_cents,image_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(comm["id"],user["id"],data.title.strip(),data.description.strip(),data.category,data.condition,data.daily_price_cents,data.deposit_cents,data.image_url.strip(),now,now))
        row=c.execute("SELECT l.*,u.name owner_name FROM listings l JOIN users u ON u.id=l.owner_id WHERE l.id=?",(cur.lastrowid,)).fetchone()
    return safe_listing(row)

@app.patch("/api/listings/{listing_id}")
def update_listing(listing_id:int, data:ListingIn, user=Depends(current_user)):
    now=int(time.time())
    with db() as c:
        comm=active_community(c,user["id"]); row=c.execute("SELECT * FROM listings WHERE id=? AND community_id=?",(listing_id,comm["id"])).fetchone()
        if not row: raise HTTPException(404,"Listing not found")
        if row["owner_id"]!=user["id"]: raise HTTPException(403,"You can only edit your own listing")
        c.execute("UPDATE listings SET title=?,description=?,category=?,condition=?,daily_price_cents=?,deposit_cents=?,image_url=?,updated_at=? WHERE id=?",(data.title.strip(),data.description.strip(),data.category,data.condition,data.daily_price_cents,data.deposit_cents,data.image_url.strip(),now,listing_id))
        return safe_listing(c.execute("SELECT l.*,u.name owner_name FROM listings l JOIN users u ON u.id=l.owner_id WHERE l.id=?",(listing_id,)).fetchone())

@app.get("/api/my/listings")
def my_listings(user=Depends(current_user)):
    with db() as c:
        comm = active_community(c, user["id"])
        rows = c.execute("SELECT l.*,u.name owner_name FROM listings l JOIN users u ON u.id=l.owner_id WHERE l.community_id=? AND l.owner_id=? ORDER BY l.created_at DESC", (comm["id"], user["id"])).fetchall()
        return [safe_listing(r) for r in rows]

@app.post("/api/listings/{listing_id}/hide")
def hide_listing(listing_id:int,user=Depends(current_user)):
    with db() as c:
        comm=active_community(c,user["id"]); r=c.execute("SELECT * FROM listings WHERE id=? AND community_id=?",(listing_id,comm["id"])).fetchone()
        if not r: raise HTTPException(404,"Listing not found")
        if r["owner_id"]!=user["id"]: raise HTTPException(403,"You can only hide your own listing")
        c.execute("UPDATE listings SET status='hidden',updated_at=? WHERE id=?",(int(time.time()),listing_id))
    return {"ok":True}
@app.post("/api/listings/{listing_id}/restore")
def restore_listing(listing_id:int,user=Depends(current_user)):
    with db() as c:
        comm=active_community(c,user["id"]); r=c.execute("SELECT * FROM listings WHERE id=? AND community_id=?",(listing_id,comm["id"])).fetchone()
        if not r: raise HTTPException(404,"Listing not found")
        if r["owner_id"]!=user["id"]: raise HTTPException(403,"You can only restore your own listing")
        c.execute("UPDATE listings SET status='active',updated_at=? WHERE id=?",(int(time.time()),listing_id))
    return {"ok":True}

@app.post("/api/listings/{listing_id}/bookings")
def request_booking(listing_id:int,data:BookingIn,user=Depends(current_user)):
    if data.end_date < data.start_date: raise HTTPException(422,"End date must be on or after start date")
    now=int(time.time())
    with db() as c:
        comm=active_community(c,user["id"]); l=c.execute("SELECT * FROM listings WHERE id=? AND community_id=? AND status='active'",(listing_id,comm["id"])).fetchone()
        if not l: raise HTTPException(404,"Listing not found")
        if l["owner_id"]==user["id"]: raise HTTPException(400,"You cannot book your own listing")
        overlap=c.execute("SELECT 1 FROM bookings WHERE listing_id=? AND status IN ('approved','handover','borrowed','return_review') AND NOT(end_date < ? OR start_date > ?)",(listing_id,data.start_date,data.end_date)).fetchone()
        if overlap: raise HTTPException(409,"Those dates overlap a confirmed booking")
        cur=c.execute("INSERT INTO bookings(community_id,listing_id,borrower_id,owner_id,start_date,end_date,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(comm["id"],listing_id,user["id"],l["owner_id"],data.start_date,data.end_date,data.note.strip(),now,now))
        return safe_booking(c.execute("SELECT * FROM bookings WHERE id=?",(cur.lastrowid,)).fetchone())

@app.get("/api/bookings")
def my_bookings(user=Depends(current_user)):
    with db() as c:
        comm=active_community(c,user["id"])
        rows=c.execute("SELECT b.*,l.title listing_title,bu.name borrower_name,ow.name owner_name FROM bookings b JOIN listings l ON l.id=b.listing_id JOIN users bu ON bu.id=b.borrower_id JOIN users ow ON ow.id=b.owner_id WHERE b.community_id=? AND (b.borrower_id=? OR b.owner_id=?) ORDER BY b.created_at DESC",(comm["id"],user["id"],user["id"])).fetchall()
        return [safe_booking(r) for r in rows]

@app.post("/api/bookings/{booking_id}/approve")
def approve_booking(booking_id:int,user=Depends(current_user)):
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["owner_id"]!=user["id"]: raise HTTPException(403,"Only the item owner can approve this request")
        if b["status"]!="requested": raise HTTPException(409,"This request is no longer pending")
        overlap=c.execute("SELECT 1 FROM bookings WHERE listing_id=? AND id<>? AND status IN ('approved','handover','borrowed','return_review') AND NOT(end_date < ? OR start_date > ?)",(b["listing_id"],booking_id,b["start_date"],b["end_date"])).fetchone()
        if overlap: raise HTTPException(409,"Another confirmed booking overlaps these dates")
        token=secrets.token_urlsafe(24)
        c.execute("UPDATE bookings SET status='approved',handover_token_hash=?,updated_at=? WHERE id=?",(hashlib.sha256(token.encode()).hexdigest(),int(time.time()),booking_id))
    return {"ok":True,"status":"approved","handover_code":token,"qr_data":qr_data({"booking_id":booking_id,"purpose":"handover","token":token}),"note":"One-time demo QR. Share only with the booking participant."}

@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id:int,user=Depends(current_user)):
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["status"] in ("completed","cancelled"): raise HTTPException(409,"This booking can no longer be cancelled")
        # Simple published MVP rule: cancel only before handover; paid refunds require provider-backed flow.
        if b["status"] in ("handover","borrowed","return_review"): raise HTTPException(409,"Contact the community admin to resolve a cancellation after handover")
        c.execute("UPDATE bookings SET status='cancelled',payment_status=CASE WHEN payment_status='held_demo' THEN 'refund_review' ELSE payment_status END,updated_at=? WHERE id=?",(int(time.time()),booking_id))
    return {"ok":True,"status":"cancelled"}

@app.post("/api/bookings/{booking_id}/handover")
def confirm_handover(booking_id:int, data:MessageIn, user=Depends(current_user)):
    # Client submits the one-time code as body: "CODE:<token>". Never accept arbitrary QR payloads.
    token=data.body.removeprefix("CODE:").strip()
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if user["id"] not in (b["owner_id"],b["borrower_id"]): raise HTTPException(403,"Not authorized")
        if b["status"]!="approved" or not b["handover_token_hash"]: raise HTTPException(409,"No valid handover code is available")
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),b["handover_token_hash"]): raise HTTPException(400,"Invalid or revoked handover code")
        c.execute("UPDATE bookings SET status='borrowed',handover_token_hash=NULL,updated_at=? WHERE id=?",(int(time.time()),booking_id))
    return {"ok":True,"status":"borrowed"}

@app.post("/api/bookings/{booking_id}/return-code")
def make_return_code(booking_id:int,user=Depends(current_user)):
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["status"]!="borrowed": raise HTTPException(409,"Item must be checked out before starting return")
        if user["id"]!=b["owner_id"]: raise HTTPException(403,"Only the item owner can initiate check-in")
        token=secrets.token_urlsafe(24)
        c.execute("UPDATE bookings SET return_token_hash=?,status='return_review',updated_at=? WHERE id=?",(hashlib.sha256(token.encode()).hexdigest(),int(time.time()),booking_id))
    return {"ok":True,"return_code":token,"qr_data":qr_data({"booking_id":booking_id,"purpose":"return","token":token}),"note":"One-time demo return QR. Give it only to the borrower."}

@app.post("/api/bookings/{booking_id}/return")
def confirm_return(booking_id:int,data:MessageIn,user=Depends(current_user)):
    token=data.body.removeprefix("CODE:").strip()
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["status"]!="return_review" or not b["return_token_hash"]: raise HTTPException(409,"No return check-in is pending")
        if user["id"]!=b["borrower_id"]: raise HTTPException(403,"Only the borrower can confirm the return")
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),b["return_token_hash"]): raise HTTPException(400,"Invalid or revoked return code")
        c.execute("UPDATE bookings SET status='completed',return_token_hash=NULL,updated_at=? WHERE id=?",(int(time.time()),booking_id))
        c.execute("UPDATE memberships SET karma=karma+2 WHERE user_id=? AND community_id=?",(b["borrower_id"],b["community_id"]))
        c.execute("UPDATE memberships SET karma=karma+1 WHERE user_id=? AND community_id=?",(b["owner_id"],b["community_id"]))
    return {"ok":True,"status":"completed","karma_awarded":{"borrower":2,"owner":1}}

@app.post("/api/bookings/{booking_id}/dispute")
def dispute_return(booking_id:int,data:DisputeIn,user=Depends(current_user)):
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["status"] not in ("borrowed","return_review"): raise HTTPException(409,"Only an active return can be disputed")
        c.execute("UPDATE bookings SET return_disputed=1,status='return_review',updated_at=? WHERE id=?",(int(time.time()),booking_id))
        c.execute("INSERT INTO messages(booking_id,sender_id,body,created_at) VALUES(?,?,?,?)",(booking_id,user["id"],"RETURN DISPUTE: "+data.reason.strip(),int(time.time())))
    return {"ok":True,"status":"return_review","message":"Dispute recorded; it remains unresolved until an authorized review is completed."}

@app.post("/api/qr/scan")
def scan_qr(data: QRScanIn, user=Depends(current_user)):
    """Validate a parsed BorrowHood QR payload and atomically consume its one-time token."""
    if data.purpose not in ("handover", "return"):
        raise HTTPException(400, "Unknown QR purpose")
    with db() as c:
        b = booking_row(c, data.booking_id, user["id"])
        if data.purpose == "handover":
            if b["status"] != "approved" or not b["handover_token_hash"]:
                raise HTTPException(409, "This handover QR is expired, revoked, or already used")
            if not hmac.compare_digest(hashlib.sha256(data.token.encode()).hexdigest(), b["handover_token_hash"]):
                raise HTTPException(400, "Invalid handover QR")
            c.execute("UPDATE bookings SET status='borrowed',handover_token_hash=NULL,updated_at=? WHERE id=?", (int(time.time()), data.booking_id))
            return {"ok": True, "status": "borrowed", "message": "Handover confirmed"}
        if b["status"] != "return_review" or not b["return_token_hash"]:
            raise HTTPException(409, "This return QR is expired, revoked, or already used")
        if user["id"] != b["borrower_id"]:
            raise HTTPException(403, "Only the borrower can confirm the return")
        if not hmac.compare_digest(hashlib.sha256(data.token.encode()).hexdigest(), b["return_token_hash"]):
            raise HTTPException(400, "Invalid return QR")
        c.execute("UPDATE bookings SET status='completed',return_token_hash=NULL,updated_at=? WHERE id=?", (int(time.time()), data.booking_id))
        c.execute("UPDATE memberships SET karma=karma+2 WHERE user_id=? AND community_id=?", (b["borrower_id"], b["community_id"]))
        c.execute("UPDATE memberships SET karma=karma+1 WHERE user_id=? AND community_id=?", (b["owner_id"], b["community_id"]))
        return {"ok": True, "status": "completed", "message": "Return confirmed", "karma_awarded": {"borrower": 2, "owner": 1}}

@app.get("/api/bookings/{booking_id}/messages")
def get_messages(booking_id:int,user=Depends(current_user)):
    with db() as c:
        booking_row(c,booking_id,user["id"])
        rows=c.execute("SELECT m.id,m.sender_id,m.body,m.created_at,u.name sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.booking_id=? ORDER BY m.created_at",(booking_id,)).fetchall()
        return [dict(r) for r in rows]
@app.post("/api/bookings/{booking_id}/messages")
def send_message(booking_id:int,data:MessageIn,user=Depends(current_user)):
    with db() as c:
        booking_row(c,booking_id,user["id"])
        cur=c.execute("INSERT INTO messages(booking_id,sender_id,body,created_at) VALUES(?,?,?,?)",(booking_id,user["id"],data.body.strip(),int(time.time())))
        return {"id":cur.lastrowid,"ok":True}

@app.post("/api/bookings/{booking_id}/reviews")
def review(booking_id:int,data:ReviewIn,user=Depends(current_user)):
    with db() as c:
        b=booking_row(c,booking_id,user["id"])
        if b["status"]!="completed": raise HTTPException(409,"Reviews are available after a completed return")
        reviewee=b["owner_id"] if user["id"]==b["borrower_id"] else b["borrower_id"]
        if user["id"] not in (b["borrower_id"],b["owner_id"]): raise HTTPException(403,"Not a participant")
        try: c.execute("INSERT INTO reviews(booking_id,reviewer_id,reviewee_id,rating,comment,created_at) VALUES(?,?,?,?,?,?)",(booking_id,user["id"],reviewee,data.rating,data.comment.strip(),int(time.time())))
        except sqlite3.IntegrityError: raise HTTPException(409,"A review has already been submitted for this booking")
    return {"ok":True}

@app.post("/api/webhooks/payment-demo")
async def payment_webhook_demo(request: Request, x_borrowhood_signature: str = Header(default="")):
    """Example provider webhook: accepts only server-signed payloads and idempotent event IDs. Not a real Stripe integration."""
    raw = await request.body()
    expected = hmac.new(WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest()
    if not x_borrowhood_signature or not hmac.compare_digest(expected, x_borrowhood_signature):
        raise HTTPException(401, "Invalid payment webhook signature")
    try: event = DemoPaymentEvent.model_validate_json(raw)
    except Exception: raise HTTPException(422, "Invalid webhook event payload")
    if event.event_type not in ("payment_intent.succeeded", "payment_intent.refunded"):
        raise HTTPException(400, "Unsupported payment event type")
    with db() as c:
        if not c.execute("SELECT 1 FROM bookings WHERE id=?", (event.booking_id,)).fetchone():
            raise HTTPException(404, "Booking not found")
        try:
            c.execute("INSERT INTO payment_events(provider_event_id,booking_id,event_type,verified,created_at) VALUES(?,?,?,?,?)", (event.event_id,event.booking_id,event.event_type,1,int(time.time())))
        except sqlite3.IntegrityError:
            return {"ok": True, "duplicate": True, "message": "Event already processed"}
        if event.event_type == "payment_intent.succeeded":
            c.execute("UPDATE bookings SET payment_status='paid_demo',updated_at=? WHERE id=?", (int(time.time()),event.booking_id))
        else:
            c.execute("UPDATE bookings SET payment_status='refunded_demo',updated_at=? WHERE id=?", (int(time.time()),event.booking_id))
    return {"ok": True, "duplicate": False, "message": "Signed demo event processed"}

# Demo-only payment state. This endpoint intentionally cannot be called by the browser to mark a booking paid.
@app.post("/api/bookings/{booking_id}/demo-payment")
def demo_payment(booking_id:int,user=Depends(current_user)):
    raise HTTPException(501,"Real payment collection is not enabled. Configure a payment provider and verify signed webhooks server-side; never trust client-supplied payment success.")
