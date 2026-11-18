#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LinkCare 簡易サンプル（位置情報共有API）

開発中のアプリ「LinkCare」の基本設計をもとに、
アカウント連携と位置情報共有の流れを小さく再現したサンプル。

流れ:
  1. ユーザー登録（POST /api/v1/users）→ トークン発行
  2. 端末登録（POST /api/v1/devices）
  3. 位置情報の投稿（POST /api/v1/locations）
  4. アカウント連携リクエストと承認（POST /api/v1/links, /approve）
  5. 共有許可の設定（PUT /api/v1/permissions）
  6. 共有された最新位置の取得（GET /api/v1/locations/shared）

認証は簡易化のため X-Token ヘッダー方式。実物では JWT を使用する。
データ保存は SQLite（標準ライブラリ）。
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "sample.db"

app = FastAPI(title="LinkCare Sample API", version="0.1.0")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS links (
            id TEXT PRIMARY KEY,
            requester_id TEXT NOT NULL,
            approver_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS permissions (
            user_id TEXT NOT NULL,
            linked_user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            allowed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, linked_user_id, category)
        );
        """
    )
    conn.commit()
    conn.close()


init_db()


def current_user(x_token: str = Header(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (x_token,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return dict(row)


class UserIn(BaseModel):
    name: str


class DeviceIn(BaseModel):
    name: str


class LocationIn(BaseModel):
    device_id: str
    latitude: float
    longitude: float


class LinkIn(BaseModel):
    user_id: str


class PermissionIn(BaseModel):
    linked_user_id: str
    category: str = "location"
    allowed: bool


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/users")
def create_user(body: UserIn):
    user_id = str(uuid.uuid4())
    token = uuid.uuid4().hex
    conn = get_db()
    conn.execute(
        "INSERT INTO users (id, name, token) VALUES (?, ?, ?)",
        (user_id, body.name, token),
    )
    conn.commit()
    conn.close()
    return {"id": user_id, "name": body.name, "token": token}


@app.post("/api/v1/devices")
def create_device(body: DeviceIn, user=Depends(current_user)):
    device_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO devices (id, user_id, name) VALUES (?, ?, ?)",
        (device_id, user["id"], body.name),
    )
    conn.commit()
    conn.close()
    return {"id": device_id, "name": body.name}


@app.post("/api/v1/locations")
def post_location(body: LocationIn, user=Depends(current_user)):
    conn = get_db()
    device = conn.execute(
        "SELECT * FROM devices WHERE id = ? AND user_id = ?",
        (body.device_id, user["id"]),
    ).fetchone()
    if device is None:
        conn.close()
        raise HTTPException(status_code=404, detail="device not found")
    recorded_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO locations (device_id, latitude, longitude, recorded_at) VALUES (?, ?, ?, ?)",
        (body.device_id, body.latitude, body.longitude, recorded_at),
    )
    conn.commit()
    conn.close()
    return {"status": "stored", "recorded_at": recorded_at}


@app.post("/api/v1/links")
def request_link(body: LinkIn, user=Depends(current_user)):
    link_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO links (id, requester_id, approver_id) VALUES (?, ?, ?)",
        (link_id, user["id"], body.user_id),
    )
    conn.commit()
    conn.close()
    return {"id": link_id, "status": "pending"}


@app.post("/api/v1/links/{link_id}/approve")
def approve_link(link_id: str, user=Depends(current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM links WHERE id = ? AND approver_id = ?",
        (link_id, user["id"]),
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="link request not found")
    conn.execute("UPDATE links SET status = 'approved' WHERE id = ?", (link_id,))
    conn.commit()
    conn.close()
    return {"id": link_id, "status": "approved"}


@app.put("/api/v1/permissions")
def set_permission(body: PermissionIn, user=Depends(current_user)):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO permissions (user_id, linked_user_id, category, allowed)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, linked_user_id, category)
        DO UPDATE SET allowed = excluded.allowed
        """,
        (user["id"], body.linked_user_id, body.category, 1 if body.allowed else 0),
    )
    conn.commit()
    conn.close()
    return {"status": "updated", "category": body.category, "allowed": body.allowed}


@app.get("/api/v1/locations/shared")
def shared_locations(user=Depends(current_user)):
    conn = get_db()
    partners = conn.execute(
        """
        SELECT requester_id AS partner FROM links
        WHERE approver_id = ? AND status = 'approved'
        UNION
        SELECT approver_id AS partner FROM links
        WHERE requester_id = ? AND status = 'approved'
        """,
        (user["id"], user["id"]),
    ).fetchall()
    result = []
    for partner_row in partners:
        partner_id = partner_row["partner"]
        perm = conn.execute(
            "SELECT allowed FROM permissions WHERE user_id = ? AND linked_user_id = ? AND category = 'location'",
            (partner_id, user["id"]),
        ).fetchone()
        if perm is None or perm["allowed"] != 1:
            continue
        devices = conn.execute(
            "SELECT * FROM devices WHERE user_id = ?", (partner_id,)
        ).fetchall()
        for device in devices:
            loc = conn.execute(
                "SELECT * FROM locations WHERE device_id = ? ORDER BY id DESC LIMIT 1",
                (device["id"],),
            ).fetchone()
            if loc is not None:
                result.append(
                    {
                        "user_id": partner_id,
                        "device_id": device["id"],
                        "device_name": device["name"],
                        "latitude": loc["latitude"],
                        "longitude": loc["longitude"],
                        "recorded_at": loc["recorded_at"],
                    }
                )
    conn.close()
    return {"shared_locations": result}
