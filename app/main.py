"""FastAPI app: REST endpoints, auth, and the WebSocket hub."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .exchange import Exchange, ExchangeError

STATIC = Path(__file__).resolve().parent.parent / "static"


class JoinIn(BaseModel):
    code: str
    name: str


class OrderIn(BaseModel):
    contract_id: int
    side: Literal["buy", "sell"]
    price: int
    qty: int
    tif: Literal["limit", "ioc"] = "limit"


class CancelAllIn(BaseModel):
    contract_id: int | None = None


class LoginIn(BaseModel):
    password: str


class SessionIn(BaseModel):
    code: str
    starting_cash: int = 10_000


class ContractIn(BaseModel):
    name: str
    description: str = ""
    kind: Literal["binary", "numeric"] = "binary"
    min: int | None = None
    max: int | None = None
    tick: int = 1


class ContractEdit(BaseModel):
    name: str | None = None
    description: str | None = None
    kind: Literal["binary", "numeric"] | None = None
    min: int | None = None
    max: int | None = None
    tick: int | None = None


class StateIn(BaseModel):
    state: Literal["open", "halted"]


class SettleIn(BaseModel):
    value: int


class AnnounceIn(BaseModel):
    text: str


class Hub:
    """Pushes full state snapshots to WebSocket clients, coalescing bursts of changes."""

    def __init__(self, ex: Exchange):
        self.ex = ex
        self.conns: dict[WebSocket, tuple[str, int, int | None]] = {}  # ws -> (kind, sid, uid)
        self.dirty: set[int] = set()
        self.event = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def mark(self, session_id: int) -> None:
        def _mark():
            self.dirty.add(session_id)
            self.event.set()
        self.loop.call_soon_threadsafe(_mark)

    def message(self, kind: str, sid: int, uid: int | None, cache: dict | None = None) -> dict:
        cache = {} if cache is None else cache
        if kind == "admin":
            if ("admin", sid) not in cache:
                cache[("admin", sid)] = self.ex.admin_state(sid)
            return {"type": "admin_state", "state": cache[("admin", sid)]}
        if ("public", sid) not in cache:
            cache[("public", sid)] = self.ex.public_state(sid)
        return {"type": "state", "public": cache[("public", sid)], "me": self.ex.private_state(uid)}

    async def run(self) -> None:
        while True:
            await self.event.wait()
            await asyncio.sleep(0.05)  # coalesce bursts
            self.event.clear()
            dirty, self.dirty = self.dirty, set()
            cache: dict = {}
            sends = [self._send(ws, self.message(kind, sid, uid, cache))
                     for ws, (kind, sid, uid) in list(self.conns.items()) if sid in dirty]
            await asyncio.gather(*sends)

    async def _send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await asyncio.wait_for(ws.send_json(msg), 5)
        except Exception:
            self.conns.pop(ws, None)

    async def serve(self, ws: WebSocket, kind: str, sid: int, uid: int | None) -> None:
        await ws.accept()
        self.conns[ws] = (kind, sid, uid)
        try:
            await ws.send_json(self.message(kind, sid, uid))
            while True:
                await ws.receive_text()  # client pings; content ignored
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.conns.pop(ws, None)


def create_app(db_path: str | None = None, admin_password: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "exchange.db")
    admin_password = admin_password or os.environ.get("ADMIN_PASSWORD")
    if not admin_password:
        raise RuntimeError("Set the ADMIN_PASSWORD environment variable")
    admin_token = hmac.new(admin_password.encode(), b"exchange-admin", hashlib.sha256).hexdigest()
    ex = Exchange(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = Hub(ex)
        app.state.hub = hub
        ex.listeners.append(hub.mark)
        task = asyncio.create_task(hub.run())
        yield
        task.cancel()
        ex.listeners.remove(hub.mark)

    app = FastAPI(lifespan=lifespan)
    app.state.ex = ex

    @app.exception_handler(ExchangeError)
    async def _exchange_error(request: Request, exc: ExchangeError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": "; ".join(msgs) or "Invalid request"}, status_code=422)

    def player(x_token: str | None = Header(None)):
        u = ex.user_by_token(x_token)
        if not u:
            raise ExchangeError("Not logged in", 401)
        return u

    def admin(x_admin_token: str | None = Header(None)):
        if not x_admin_token or not hmac.compare_digest(x_admin_token, admin_token):
            raise ExchangeError("Admin login required", 401)

    # ---- pages
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin")
    async def admin_page():
        return FileResponse(STATIC / "admin.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- player API
    @app.post("/api/join")
    async def join(body: JoinIn):
        u = ex.join(body.code, body.name)
        return {"token": u.token, "user_id": u.id, "session_id": u.session_id}

    @app.get("/api/state")
    async def state(u=Depends(player)):
        return {"public": ex.public_state(u.session_id), "me": ex.private_state(u.id)}

    @app.post("/api/orders")
    async def place(body: OrderIn, u=Depends(player)):
        return ex.place_order(u.id, body.contract_id, body.side, body.price, body.qty, body.tif)

    @app.delete("/api/orders/{order_id}")
    async def cancel(order_id: int, u=Depends(player)):
        ex.cancel_order(u.id, order_id)
        return {"ok": True}

    @app.post("/api/cancel_all")
    async def cancel_all(body: CancelAllIn, u=Depends(player)):
        return {"cancelled": ex.cancel_all(u.id, body.contract_id)}

    # ---- admin API
    @app.post("/api/admin/login")
    async def login(body: LoginIn):
        if not hmac.compare_digest(body.password.encode(), admin_password.encode()):
            raise ExchangeError("Wrong password", 401)
        return {"token": admin_token}

    @app.get("/api/admin/sessions", dependencies=[Depends(admin)])
    async def sessions():
        return ex.list_sessions()

    @app.post("/api/admin/sessions", dependencies=[Depends(admin)])
    async def create_session(body: SessionIn):
        s = ex.create_session(body.code, body.starting_cash)
        return {"id": s.id, "code": s.code}

    @app.post("/api/admin/sessions/{sid}/end", dependencies=[Depends(admin)])
    async def end_session(sid: int):
        ex.end_session(sid)
        return {"ok": True}

    @app.get("/api/admin/sessions/{sid}/state", dependencies=[Depends(admin)])
    async def admin_state(sid: int):
        return ex.admin_state(sid)

    @app.post("/api/admin/sessions/{sid}/contracts", dependencies=[Depends(admin)])
    async def create_contract(sid: int, body: ContractIn):
        ct = ex.create_contract(sid, body.name, body.description, body.kind,
                                body.min, body.max, body.tick)
        return {"id": ct.id}

    @app.patch("/api/admin/contracts/{cid}", dependencies=[Depends(admin)])
    async def edit_contract(cid: int, body: ContractEdit):
        fields = body.model_dump(exclude_none=True)
        if "min" in fields:
            fields["lo"] = fields.pop("min")
        if "max" in fields:
            fields["hi"] = fields.pop("max")
        ex.edit_contract(cid, **fields)
        return {"ok": True}

    @app.post("/api/admin/contracts/{cid}/state", dependencies=[Depends(admin)])
    async def contract_state(cid: int, body: StateIn):
        ex.set_contract_state(cid, body.state)
        return {"ok": True}

    @app.post("/api/admin/contracts/{cid}/settle", dependencies=[Depends(admin)])
    async def settle(cid: int, body: SettleIn):
        ex.settle(cid, body.value)
        return {"ok": True}

    @app.post("/api/admin/sessions/{sid}/announcements", dependencies=[Depends(admin)])
    async def announce(sid: int, body: AnnounceIn):
        return ex.announce(sid, body.text)

    @app.get("/api/admin/sessions/{sid}/export/{what}.csv", dependencies=[Depends(admin)])
    async def export(sid: int, what: str):
        data = ex.export_csv(sid, what)
        code = ex.sessions[sid].code
        return Response(data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{code}-{what}.csv"'})

    # ---- websockets
    @app.websocket("/ws")
    async def ws_player(ws: WebSocket, token: str = ""):
        u = ex.user_by_token(token)
        if not u:
            await ws.accept()
            await ws.close(code=4401)
            return
        await app.state.hub.serve(ws, "player", u.session_id, u.id)

    @app.websocket("/ws/admin")
    async def ws_admin(ws: WebSocket, token: str = "", session_id: int = 0):
        if not hmac.compare_digest(token, admin_token) or session_id not in ex.sessions:
            await ws.accept()
            await ws.close(code=4401)
            return
        await app.state.hub.serve(ws, "admin", session_id, None)

    return app


def app_from_env() -> FastAPI:
    return create_app()
