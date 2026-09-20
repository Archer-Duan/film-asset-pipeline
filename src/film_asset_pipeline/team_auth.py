"""LAN member sessions. Administrator operations remain loopback-only."""

from contextlib import contextmanager
import ipaddress
import json
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256, repr=False)


class TeamAuth:
    def __init__(self, root):
        self.path = Path(root) / "data/local-assets/team.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.failures = {}
        config = Path(root) / "comfy.local.json"
        options = json.loads(config.read_text("utf-8")) if config.exists() else {}
        self.subnet = (
            ipaddress.ip_network(options["lan_subnet"])
            if options.get("lan_subnet")
            else None
        )
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS members(name TEXT PRIMARY KEY,salt TEXT,hash TEXT);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,name TEXT,expires REAL);""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_user(self, name, password):
        if (
            not name.strip()
            or len(name) > 64
            or len(password) < 10
            or len(password) > 256
            or name == "local"
        ):
            raise ValueError("账号不能为空或使用 local；密码须为 10–256 个字符")
        salt = secrets.token_hex(16)
        digest = hashlib.scrypt(
            password.encode(), salt=salt.encode(), n=16384, r=8, p=1
        ).hex()
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO members VALUES(?,?,?)", (name, salt, digest)
            )
            db.execute("DELETE FROM sessions WHERE name=?", (name,))

    def login(self, name, password):
        with self.connect() as db:
            row = db.execute(
                "SELECT salt,hash FROM members WHERE name=?", (name,)
            ).fetchone()
            salt, expected = row if row else ("dummy", "0" * 128)
            actual = hashlib.scrypt(
                password.encode(), salt=salt.encode(), n=16384, r=8, p=1
            ).hex()
            if not hmac.compare_digest(actual, expected):
                raise HTTPException(401, "账号或密码错误")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), name, time.time() + 43200),
            )
            return token

    def member(self, request):
        token = request.cookies.get("workbench_session", "")
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM sessions WHERE token=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            if row:
                return row[0]
        if request.client and request.client.host in ("127.0.0.1", "::1", "testclient"):
            return "local"
        return None

    def install(self, app):
        @app.middleware("http")
        async def authorize(request: Request, call_next):
            host = request.client.host if request.client else ""
            if self.subnet and host not in ("127.0.0.1", "::1", "testclient"):
                try:
                    allowed = ipaddress.ip_address(host) in self.subnet
                except ValueError:
                    allowed = False
                if not allowed:
                    return JSONResponse(
                        {"detail": "仅允许办公室局域网访问"}, status_code=403
                    )
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if (
                    origin and urlsplit(origin).netloc != request.headers.get("host")
                ) or request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
            member = self.member(request)
            request.state.member = member
            if (
                request.url.path.startswith("/api/")
                and request.url.path not in ("/api/health", "/api/team/login")
                and not member
            ):
                return JSONResponse({"detail": "请先登录工作台"}, status_code=401)
            return await call_next(request)

        @app.post("/api/team/login")
        def login(body: Login, request: Request):
            host = request.client.host if request.client else "unknown"
            attempts, until = self.failures.get(host, (0, 0))
            if until < time.time():
                attempts = 0
            if attempts >= 10:
                raise HTTPException(429, "登录尝试过多，请 5 分钟后重试")
            try:
                token = self.login(body.username, body.password)
            except HTTPException:
                self.failures[host] = (attempts + 1, time.time() + 300)
                raise
            self.failures.pop(host, None)
            response = JSONResponse({"username": body.username})
            response.set_cookie(
                "workbench_session",
                token,
                httponly=True,
                samesite="strict",
                max_age=43200,
                secure=request.url.scheme == "https",
            )
            return response

        @app.get("/api/team/me")
        def me(request: Request):
            return {
                "username": request.state.member,
                "admin": request.state.member == "local",
            }

        @app.post("/api/team/logout")
        def logout(request: Request):
            token = request.cookies.get("workbench_session", "")
            with self.connect() as db:
                db.execute(
                    "DELETE FROM sessions WHERE token=?",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )
            response = JSONResponse({"ok": True})
            response.delete_cookie("workbench_session")
            return response

        @app.post("/api/team/members")
        def add_member(body: Login, request: Request):
            if request.state.member != "local":
                raise HTTPException(403, "请在算力主机本机管理成员")
            try:
                self.add_user(body.username, body.password)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"username": body.username}
