# -*- coding: utf-8 -*-
"""LinkCare サンプルAPIのテスト

実行: pytest -q
（テストごとに一時DBを使うので、sample.db は作られません）
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    with TestClient(main.app) as c:
        yield c


# ===== ヘルパー =====

def make_user(client, name):
    r = client.post("/api/v1/users", json={"name": name})
    assert r.status_code == 201
    body = r.json()
    return body["id"], {"X-Token": body["token"]}


def make_device_with_location(client, headers, lat=35.6, lon=139.7):
    r = client.post("/api/v1/devices", json={"name": "端末"}, headers=headers)
    assert r.status_code == 201
    device_id = r.json()["id"]
    r = client.post(
        "/api/v1/locations",
        json={"device_id": device_id, "latitude": lat, "longitude": lon},
        headers=headers,
    )
    assert r.status_code == 201
    return device_id


def request_link(client, h_from, to_id):
    r = client.post("/api/v1/links", json={"user_id": to_id}, headers=h_from)
    assert r.status_code == 201
    return r.json()["id"]


def link(client, h_from, to_id, h_to):
    link_id = request_link(client, h_from, to_id)
    assert client.post(f"/api/v1/links/{link_id}/approve", headers=h_to).status_code == 200
    return link_id


def allow(client, headers, partner_id, allowed=True):
    return client.put(
        "/api/v1/permissions",
        json={"linked_user_id": partner_id, "allowed": allowed},
        headers=headers,
    )


def shared(client, headers):
    r = client.get("/api/v1/locations/shared", headers=headers)
    assert r.status_code == 200
    return r.json()["shared_locations"]


@pytest.fixture
def pair(client):
    """A と B が連携済みで、B は端末と位置を登録済み"""
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    device_id = make_device_with_location(client, hb)
    link_id = link(client, ha, b, hb)
    return {"a": a, "ha": ha, "b": b, "hb": hb, "device_id": device_id, "link_id": link_id}


# ===== 基本・認証 =====

def test_health(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_token_required(client):
    assert client.get("/api/v1/locations/shared").status_code == 422


def test_invalid_token(client):
    r = client.get("/api/v1/locations/shared", headers={"X-Token": "invalid"})
    assert r.status_code == 401


def test_token_is_stored_hashed(client):
    _, h = make_user(client, "A")
    with sqlite3.connect(main.DB_PATH) as conn:
        stored = conn.execute("SELECT token_hash FROM users").fetchone()[0]
    assert stored != h["X-Token"]
    assert stored == main.hash_token(h["X-Token"])


def test_expired_token_is_rejected(client):
    _, h = make_user(client, "A")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("UPDATE users SET token_expires_at = ?", (past,))
    r = client.get("/api/v1/locations/shared", headers=h)
    assert r.status_code == 401
    assert r.json()["detail"] == "token expired"


@pytest.mark.parametrize("name", ["", "あ" * 51])
def test_invalid_user_name_is_rejected(client, name):
    assert client.post("/api/v1/users", json={"name": name}).status_code == 422


# ===== 連携 → 承認 → 共有許可 =====

def test_full_flow(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    make_device_with_location(client, hb)

    link_id = request_link(client, ha, b)
    assert shared(client, ha) == []  # 承認前は見えない

    # 依頼した側は承認できない
    assert client.post(f"/api/v1/links/{link_id}/approve", headers=ha).status_code == 404
    assert client.post(f"/api/v1/links/{link_id}/approve", headers=hb).status_code == 200
    assert shared(client, ha) == []  # 承認だけでは見えない（許可が必要）

    assert allow(client, hb, a).status_code == 200
    s = shared(client, ha)
    assert len(s) == 1 and s[0]["latitude"] == 35.6

    allow(client, hb, a, allowed=False)
    assert shared(client, ha) == []  # 許可を切ると即見えなくなる


def test_sharing_is_one_directional(client, pair):
    # B が A に許可しても、B から A の位置は見えない
    make_device_with_location(client, pair["ha"])
    allow(client, pair["hb"], pair["a"])
    assert len(shared(client, pair["ha"])) == 1
    assert shared(client, pair["hb"]) == []


def test_only_latest_location_is_returned(client, pair):
    allow(client, pair["hb"], pair["a"])
    client.post(
        "/api/v1/locations",
        json={"device_id": pair["device_id"], "latitude": 1.0, "longitude": 2.0},
        headers=pair["hb"],
    )
    assert shared(client, pair["ha"])[0]["latitude"] == 1.0


def test_unrelated_user_sees_nothing(client, pair):
    allow(client, pair["hb"], pair["a"])
    _, hc = make_user(client, "C")
    assert shared(client, hc) == []


def test_cannot_post_to_others_device(client, pair):
    r = client.post(
        "/api/v1/locations",
        json={"device_id": pair["device_id"], "latitude": 0, "longitude": 0},
        headers=pair["ha"],
    )
    assert r.status_code == 404


def test_approve_twice_is_rejected(client, pair):
    r = client.post(f"/api/v1/links/{pair['link_id']}/approve", headers=pair["hb"])
    assert r.status_code == 409


# ===== 連携一覧 =====

def test_list_links_shows_both_sides(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    link_id = request_link(client, ha, b)

    out = client.get("/api/v1/links", headers=ha).json()["links"]
    assert out == [{"id": link_id, "partner_id": b, "direction": "outgoing", "status": "pending"}]

    inc = client.get("/api/v1/links", headers=hb).json()["links"]
    assert inc == [{"id": link_id, "partner_id": a, "direction": "incoming", "status": "pending"}]

    _, hc = make_user(client, "C")
    assert client.get("/api/v1/links", headers=hc).json()["links"] == []


# ===== 連携の解除 =====

@pytest.mark.parametrize("who", ["ha", "hb"])
def test_unlink_stops_sharing_immediately(client, pair, who):
    allow(client, pair["hb"], pair["a"])
    assert len(shared(client, pair["ha"])) == 1

    r = client.delete(f"/api/v1/links/{pair['link_id']}", headers=pair[who])
    assert r.status_code == 200
    assert shared(client, pair["ha"]) == []
    assert client.get("/api/v1/links", headers=pair["ha"]).json()["links"] == []


def test_relink_does_not_restore_old_permission(client, pair):
    allow(client, pair["hb"], pair["a"])
    client.delete(f"/api/v1/links/{pair['link_id']}", headers=pair["ha"])

    link(client, pair["ha"], pair["b"], pair["hb"])  # 再連携
    assert shared(client, pair["ha"]) == []  # 許可は改めて設定し直す必要がある


def test_pending_request_can_be_cancelled_or_declined(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    link_id = request_link(client, ha, b)
    assert client.delete(f"/api/v1/links/{link_id}", headers=ha).status_code == 200  # 取り消し

    link_id = request_link(client, ha, b)
    assert client.delete(f"/api/v1/links/{link_id}", headers=hb).status_code == 200  # 拒否

    request_link(client, ha, b)  # 再リクエストもできる


def test_third_party_cannot_unlink(client, pair):
    _, hc = make_user(client, "C")
    assert client.delete(f"/api/v1/links/{pair['link_id']}", headers=hc).status_code == 404


def test_unlink_unknown_link(client):
    _, ha = make_user(client, "A")
    assert client.delete("/api/v1/links/no-such-link", headers=ha).status_code == 404


# ===== 入力チェック =====

def test_link_to_self_is_rejected(client):
    a, ha = make_user(client, "A")
    assert client.post("/api/v1/links", json={"user_id": a}, headers=ha).status_code == 400


def test_link_to_unknown_user_is_rejected(client):
    _, ha = make_user(client, "A")
    r = client.post("/api/v1/links", json={"user_id": "no-such-user"}, headers=ha)
    assert r.status_code == 404


def test_duplicate_link_is_rejected(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    request_link(client, ha, b)
    assert client.post("/api/v1/links", json={"user_id": b}, headers=ha).status_code == 409
    # 逆方向からの重複も弾く
    assert client.post("/api/v1/links", json={"user_id": a}, headers=hb).status_code == 409


def test_permission_requires_approved_link(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    assert allow(client, hb, a).status_code == 404  # 未連携
    request_link(client, ha, b)
    assert allow(client, hb, a).status_code == 404  # 承認待ち


def test_unknown_permission_category_is_rejected(client, pair):
    r = client.put(
        "/api/v1/permissions",
        json={"linked_user_id": pair["a"], "category": "camera", "allowed": True},
        headers=pair["hb"],
    )
    assert r.status_code == 422


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_out_of_range_coordinates_are_rejected(client, pair, lat, lon):
    r = client.post(
        "/api/v1/locations",
        json={"device_id": pair["device_id"], "latitude": lat, "longitude": lon},
        headers=pair["hb"],
    )
    assert r.status_code == 422
