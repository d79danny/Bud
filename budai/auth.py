import base64
import hashlib
import hmac
import io
import secrets
import time
from urllib.parse import urlsplit

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from qrcode.image.pure import PyPNGImage


hasher = PasswordHasher()
SESSION_SECONDS = 8 * 3600
IDLE_SECONDS = 30 * 60
COOKIE = "bud_session"


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Login(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    otp: str = Field(default="", max_length=6)
    device: str = Field(default="Browser", max_length=80)


class Pair(BaseModel):
    token: str = Field(min_length=30, max_length=100)
    label: str = Field(default="Mobile", min_length=1, max_length=80)


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=12, max_length=256)
    otp: str = Field(default="", max_length=6)


class SecondFactor(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=6)


class Auth:
    def __init__(self, store, config, stop):
        self.store = store
        self.config = config
        self.stop = stop
        self.pending_totp = {}
        self.dummy_hash = hasher.hash(secrets.token_hex(32))

    def remote(self, request):
        return self.config.origin and request.headers.get("host") == urlsplit(self.config.origin).netloc

    def check_rate(self, request, action):
        source = f"{action}:{request.client.host if request.client else 'unknown'}"
        now = int(time.time())
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE created<=?", (now - 60,))
            count = db.execute("SELECT count(*) FROM login_attempts WHERE source=?", (source,)).fetchone()[0]
            if count >= 5:
                raise HTTPException(429, "Too many attempts. Try again in a minute.")
            db.execute("INSERT INTO login_attempts VALUES (?,?)", (source, now))
        return source

    def session(self, request: Request):
        token = request.cookies.get(COOKIE)
        row = None
        if token and len(token) <= 100:
            hashed = digest(token)
            now = int(time.time())
            row = self.store.one(
                "SELECT s.user_id,s.device,s.seen,u.name,u.totp IS NOT NULL AS second_factor FROM sessions s "
                "JOIN users u ON u.id=s.user_id LEFT JOIN devices d ON d.id=s.device "
                "WHERE s.token=? AND s.expires>? AND (s.seen=0 OR s.seen>?) AND d.revoked IS NULL",
                (hashed, now, now - IDLE_SECONDS),
            )
            if row and row["seen"] < now - 60:
                with self.store.connect() as db:
                    db.execute("UPDATE sessions SET seen=? WHERE token=?", (now, hashed))
                    db.execute("UPDATE devices SET seen=CURRENT_TIMESTAMP WHERE id=?", (row["device"],))
        if not row:
            raise HTTPException(401, "Sign in to BudAI")
        return row

    def account(self, request: Request):
        identifier = self.session(request)["user_id"]
        request.state.account = identifier
        return identifier

    def session_cookie(self, account, label, request, response):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR (seen>0 AND seen<=?)", (now, now - IDLE_SECONDS))
            device = db.execute(
                "INSERT INTO devices(user_id,label,seen) VALUES (?,?,CURRENT_TIMESTAMP)",
                (account, label.strip() or "Browser"),
            ).lastrowid
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                       (digest(token), account, now + SESSION_SECONDS, device, now))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                            secure=bool(self.remote(request)), max_age=SESSION_SECONDS)
        return {"authenticated": True, "device": device}

    @staticmethod
    def password_matches(password, hashed):
        try:
            return hasher.verify(hashed, password)
        except VerificationError:
            return False

    def accept_code(self, account, secret, code):
        if not secret:
            return True
        engine = pyotp.TOTP(secret)
        current = int(time.time() / engine.interval)
        for offset in (0, -1, 1):
            step = current + offset
            if hmac.compare_digest(engine.generate_otp(step), code):
                with self.store.connect() as db:
                    return db.execute(
                        "UPDATE users SET totp_used=? WHERE id=? AND totp_used<?",
                        (step, account, step),
                    ).rowcount == 1
        return False

    def routes(self):
        router = APIRouter(prefix="/api")

        @router.get("/session")
        def session(row=Depends(self.session)):
            return row

        @router.post("/login")
        def login(body: Login, request: Request, response: Response):
            source = self.check_rate(request, "login")
            row = self.store.one("SELECT * FROM users WHERE name=?", (body.name,))
            valid = self.password_matches(body.password, row["password"] if row else self.dummy_hash)
            if not row or not valid or not self.accept_code(row["id"], row["totp"], body.otp):
                self.store.audit(row["id"] if row else None, "login_failed")
                raise HTTPException(401, "Invalid credentials or verification code")
            self.store.run("DELETE FROM login_attempts WHERE source=?", (source,))
            self.store.audit(row["id"], "login")
            if hasher.check_needs_rehash(row["password"]):
                self.store.run("UPDATE users SET password=? WHERE id=? AND password=?",
                               (hasher.hash(body.password), row["id"], row["password"]))
            return self.session_cookie(row["id"], body.device, request, response)

        @router.post("/logout", status_code=204)
        async def logout(request: Request, response: Response, account=Depends(self.account)):
            self.stop(account)
            self.store.run("DELETE FROM sessions WHERE token=?", (digest(request.cookies[COOKIE]),))
            response.delete_cookie(COOKIE, secure=bool(self.remote(request)), httponly=True, samesite="strict")
            self.store.audit(account, "logout")

        @router.get("/devices")
        def devices(row=Depends(self.session)):
            return {"current": row["device"], "devices": self.store.all(
                "SELECT id,label,created,seen,revoked FROM devices WHERE user_id=? ORDER BY id DESC",
                (row["user_id"],)
            )}

        @router.delete("/devices/{device}", status_code=204)
        async def revoke(device: int, account=Depends(self.account)):
            with self.store.connect() as db:
                if not db.execute("UPDATE devices SET revoked=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                                  (device, account)).rowcount:
                    raise HTTPException(404, "Device not found")
                db.execute("DELETE FROM sessions WHERE device=? AND user_id=?", (device, account))
            self.stop(account)
            self.store.audit(account, "device_revoked", str(device))

        @router.post("/pairings")
        def pairing(account=Depends(self.account)):
            if not self.config.origin:
                raise HTTPException(409, "Configure the HTTPS BudAI origin to pair another device")
            token = secrets.token_urlsafe(32)
            with self.store.connect() as db:
                db.execute("DELETE FROM pairings WHERE user_id=? OR expires<=?", (account, int(time.time())))
                db.execute("INSERT INTO pairings VALUES (?,?,?)", (digest(token), account, int(time.time()) + 120))
            url = self.config.origin + "/#pair=" + token
            qr = qrcode.make(url, image_factory=PyPNGImage)
            image = io.BytesIO()
            qr.save(image)
            self.store.audit(account, "pairing_created")
            return {"url": url, "png": base64.b64encode(image.getvalue()).decode(), "expires_in": 120}

        @router.post("/pair")
        def pair(body: Pair, request: Request, response: Response):
            if not self.remote(request):
                raise HTTPException(403, "Device pairing requires the configured HTTPS origin")
            self.check_rate(request, "pair")
            with self.store.connect() as db:
                row = db.execute("DELETE FROM pairings WHERE token=? AND expires>? RETURNING user_id",
                                 (digest(body.token), int(time.time()))).fetchone()
            if not row:
                raise HTTPException(401, "Pairing link expired or was already used")
            account = row["user_id"]
            self.store.audit(account, "paired")
            return self.session_cookie(account, body.label, request, response)

        @router.post("/password", status_code=204)
        async def change_password(body: PasswordChange, request: Request, account=Depends(self.account)):
            source = self.check_rate(request, "password")
            row = self.store.one("SELECT password,totp FROM users WHERE id=?", (account,))
            if not self.password_matches(body.current, row["password"]) or not self.accept_code(
                account, row["totp"], body.otp
            ):
                raise HTTPException(401, "Invalid credentials or verification code")
            with self.store.connect() as db:
                db.execute("UPDATE users SET password=? WHERE id=?", (hasher.hash(body.new), account))
                db.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                           (account, digest(request.cookies[COOKIE])))
                db.execute("DELETE FROM pairings WHERE user_id=?", (account,))
                db.execute("DELETE FROM login_attempts WHERE source=?", (source,))
            self.stop(account)
            self.store.audit(account, "password_changed")

        @router.post("/second-factor/setup")
        def setup_factor(body: SecondFactor, request: Request, account=Depends(self.account)):
            source = self.check_rate(request, "second-factor")
            row = self.store.one("SELECT name,password,totp FROM users WHERE id=?", (account,))
            if row["totp"]:
                raise HTTPException(409, "Disable the current second factor before enrolling again")
            if not self.password_matches(body.password, row["password"]):
                raise HTTPException(401, "Invalid password")
            secret = pyotp.random_base32()
            self.pending_totp[account] = (secret, time.monotonic() + 300, digest(request.cookies[COOKIE]))
            self.store.run("DELETE FROM login_attempts WHERE source=?", (source,))
            return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(row["name"], issuer_name="BudAI")}

        @router.post("/second-factor/verify", status_code=204)
        def verify_factor(body: SecondFactor, request: Request, account=Depends(self.account)):
            self.check_rate(request, "second-factor")
            pending = self.pending_totp.get(account)
            row = self.store.one("SELECT password FROM users WHERE id=?", (account,))
            if not pending or pending[1] < time.monotonic() or pending[2] != digest(request.cookies[COOKIE]):
                raise HTTPException(409, "Second-factor enrollment expired")
            if not self.password_matches(body.password, row["password"]) or not self.accept_code(
                account, pending[0], body.code
            ):
                raise HTTPException(401, "Invalid password or verification code")
            with self.store.connect() as db:
                db.execute("UPDATE users SET totp=? WHERE id=?", (pending[0], account))
                db.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                           (account, digest(request.cookies[COOKIE])))
                db.execute("DELETE FROM pairings WHERE user_id=?", (account,))
            del self.pending_totp[account]
            self.store.audit(account, "second_factor_enabled")

        @router.delete("/second-factor", status_code=204)
        def disable_factor(body: SecondFactor, request: Request, account=Depends(self.account)):
            self.check_rate(request, "second-factor")
            row = self.store.one("SELECT password,totp FROM users WHERE id=?", (account,))
            if not self.password_matches(body.password, row["password"]) or not self.accept_code(
                account, row["totp"], body.code
            ):
                raise HTTPException(401, "Invalid password or verification code")
            self.store.run("UPDATE users SET totp=NULL,totp_used=0 WHERE id=?", (account,))
            self.store.audit(account, "second_factor_disabled")

        @router.get("/audit")
        def audit(account=Depends(self.account)):
            return self.store.all("SELECT created,action,detail FROM audit WHERE user_id=? "
                                  "ORDER BY id DESC LIMIT 250", (account,))

        return router
