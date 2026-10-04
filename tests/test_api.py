import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"), "pw")
    with TestClient(app) as c:
        yield c


def admin_headers(client):
    r = client.post("/api/admin/login", json={"password": "pw"})
    assert r.status_code == 200
    return {"X-Admin-Token": r.json()["token"]}


def setup_game(client):
    h = admin_headers(client)
    sid = client.post("/api/admin/sessions", json={"code": "abc"}, headers=h).json()["id"]
    cid = client.post(f"/api/admin/sessions/{sid}/contracts",
                      json={"name": "Coin", "kind": "binary"}, headers=h).json()["id"]
    client.post(f"/api/admin/contracts/{cid}/state", json={"state": "open"}, headers=h)
    toks = [client.post("/api/join", json={"code": "ABC", "name": n}).json()["token"]
            for n in ("p1", "p2")]
    return h, sid, cid, toks


def test_admin_requires_password(client):
    assert client.post("/api/admin/login", json={"password": "nope"}).status_code == 401
    r = client.get("/api/admin/sessions", headers={"X-Admin-Token": "x"})
    assert r.status_code == 401 and r.json()["error"]


def test_trade_flow_and_errors(client):
    h, sid, cid, (t1, t2) = setup_game(client)
    r = client.post("/api/orders", json={"contract_id": cid, "side": "sell", "price": 60, "qty": 5},
                    headers={"X-Token": t1})
    assert r.json()["status"] == "open"
    r = client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": 100, "qty": 2,
                                         "tif": "ioc"}, headers={"X-Token": t2})
    assert r.json()["filled"] == 2
    st = client.get("/api/state", headers={"X-Token": t2}).json()
    assert st["me"]["user"]["cash"] == 10_000 - 120
    assert st["public"]["contracts"][0]["asks"] == [[60, 3]]
    r = client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": 50, "qty": 1000},
                    headers={"X-Token": t2})
    assert r.status_code == 400 and "not enough available cash" in r.json()["error"]
    r = client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": 5.5, "qty": 1},
                    headers={"X-Token": t2})
    assert r.status_code == 422 and "price" in r.json()["error"]
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/join", json={"code": "abc", "name": "P1"}).status_code == 400


def test_cancel_endpoints(client):
    h, sid, cid, (t1, t2) = setup_game(client)
    hd = {"X-Token": t1}
    ids = [client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": p, "qty": 1},
                       headers=hd).json()["order_id"] for p in (10, 11, 12)]
    assert client.delete(f"/api/orders/{ids[0]}", headers={"X-Token": t2}).status_code == 404
    assert client.delete(f"/api/orders/{ids[0]}", headers=hd).status_code == 200
    assert client.post("/api/cancel_all", json={"contract_id": cid}, headers=hd).json() == {"cancelled": 2}


def test_websocket_snapshot_and_updates(client):
    h, sid, cid, (t1, t2) = setup_game(client)
    with client.websocket_connect(f"/ws?token={t1}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["me"]["user"]["name"] == "p1"
        client.post(f"/api/admin/sessions/{sid}/announcements", json={"text": "Die 1: 4"}, headers=h)
        msg = ws.receive_json()
        assert msg["public"]["announcements"][0]["text"] == "Die 1: 4"
    with client.websocket_connect(f"/ws/admin?token={h['X-Admin-Token']}&session_id={sid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "admin_state" and len(msg["state"]["users"]) == 2


def test_settle_end_and_export(client):
    h, sid, cid, (t1, t2) = setup_game(client)
    client.post("/api/orders", json={"contract_id": cid, "side": "sell", "price": 30, "qty": 4},
                headers={"X-Token": t1})
    client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": 30, "qty": 4},
                headers={"X-Token": t2})
    assert client.post(f"/api/admin/sessions/{sid}/end", headers=h).status_code == 400
    client.post(f"/api/admin/contracts/{cid}/settle", json={"value": 100}, headers=h)
    assert client.post(f"/api/admin/sessions/{sid}/end", headers=h).status_code == 200
    r = client.get(f"/api/admin/sessions/{sid}/export/results.csv", headers=h)
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[1].startswith("1,p2,10000,10280,280")
    # ended session stays viewable read-only
    st = client.get("/api/state", headers={"X-Token": t1}).json()
    assert st["public"]["session"]["status"] == "ended"
    r = client.post("/api/orders", json={"contract_id": cid, "side": "buy", "price": 30, "qty": 1},
                    headers={"X-Token": t1})
    assert r.status_code == 400
