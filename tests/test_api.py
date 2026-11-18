# -*- coding: utf-8 -*-
"""LinkCare サンプルAPIのテスト

実行: pytest -q
（テストごとに一時DBを使うので、sample.db は汚れません）
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.init_db()
    return TestClient(main.app)


def make_user(client, name):
    r = client.post("/api/v1/users", json={"name": name}).json()
    return r["id"], {"X-Token": r["token"]}


def make_device_with_location(client, headers, lat=35.6, lon=139.7):
    device_id = client.post("/api/v1/devices", json={"name": "端末"}, headers=headers).json()["id"]
    client.post(
        "/api/v1/locations",
        json={"device_id": device_id, "latitude": lat, "longitude": lon},
        headers=headers,
    )
    return device_id


def link(client, h_from, to_id, h_to):
    link_id = client.post("/api/v1/links", json={"user_id": to_id}, headers=h_from).json()["id"]
    client.post(f"/api/v1/links/{link_id}/approve", headers=h_to)
    return link_id


def shared(client, headers):
    return client.get("/api/v1/locations/shared", headers=headers).json()["shared_locations"]


# ===== 基本 =====

def test_health(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_token_required(client):
    assert client.get("/api/v1/locations/shared").status_code == 422


def test_invalid_token(client):
    r = client.get("/api/v1/locations/shared", headers={"X-Token": "invalid"})
    assert r.status_code == 401


# ===== 連携 → 承認 → 共有許可 =====

def test_full_flow(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    make_device_with_location(client, hb)

    link_id = client.post("/api/v1/links", json={"user_id": b}, headers=ha).json()["id"]
    assert shared(client, ha) == []  # 承認前は見えない

    # 依頼した側は承認できない
    assert client.post(f"/api/v1/links/{link_id}/approve", headers=ha).status_code == 404
    assert client.post(f"/api/v1/links/{link_id}/approve", headers=hb).status_code == 200
    assert shared(client, ha) == []  # 承認だけでは見えない（許可が必要）

    client.put("/api/v1/permissions", json={"linked_user_id": a, "allowed": True}, headers=hb)
    s = shared(client, ha)
    assert len(s) == 1 and s[0]["latitude"] == 35.6

    client.put("/api/v1/permissions", json={"linked_user_id": a, "allowed": False}, headers=hb)
    assert shared(client, ha) == []  # 許可を切ると即見えなくなる


def test_permission_without_link_is_not_shared(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    make_device_with_location(client, hb)
    client.put("/api/v1/permissions", json={"linked_user_id": a, "allowed": True}, headers=hb)
    assert shared(client, ha) == []


def test_only_latest_location_is_returned(client):
    a, ha = make_user(client, "A")
    b, hb = make_user(client, "B")
    device_id = make_device_with_location(client, hb)
    link(client, ha, b, hb)
    client.put("/api/v1/permissions", json={"linked_user_id": a, "allowed": True}, headers=hb)
    client.post(
        "/api/v1/locations",
        json={"device_id": device_id, "latitude": 1.0, "longitude": 2.0},
        headers=hb,
    )
    assert shared(client, ha)[0]["latitude"] == 1.0


def test_cannot_post_to_others_device(client):
    _, ha = make_user(client, "A")
    _, hb = make_user(client, "B")
    device_id = make_device_with_location(client, hb)
    r = client.post(
        "/api/v1/locations",
        json={"device_id": device_id, "latitude": 0, "longitude": 0},
        headers=ha,
    )
    assert r.status_code == 404


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
    assert client.post("/api/v1/links", json={"user_id": b}, headers=ha).status_code == 200
    assert client.post("/api/v1/links", json={"user_id": b}, headers=ha).status_code == 409
    # 逆方向からの重複も弾く
    assert client.post("/api/v1/links", json={"user_id": a}, headers=hb).status_code == 409


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_out_of_range_coordinates_are_rejected(client, lat, lon):
    _, hb = make_user(client, "B")
    device_id = client.post("/api/v1/devices", json={"name": "端末"}, headers=hb).json()["id"]
    r = client.post(
        "/api/v1/locations",
        json={"device_id": device_id, "latitude": lat, "longitude": lon},
        headers=hb,
    )
    assert r.status_code == 422
