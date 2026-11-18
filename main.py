#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LinkCare 簡易サンプル（位置情報共有API）

開発中のアプリ「LinkCare」の基本設計をもとに、
アカウント連携と位置情報共有の流れを小さく再現したサンプル。

流れ:
  1. ユーザー登録（POST /api/v1/users）→ トークン発行
  2. 端末登録（POST /api/v1/devices）
  3. 位置情報の投稿（POST /api/v1/locations）
  4. 連携リクエスト → 承認（POST /api/v1/links, /approve）
  5. 共有許可の設定（PUT /api/v1/permissions）
  6. 共有された最新位置の取得（GET /api/v1/locations/shared）
  7. 連携の解除（DELETE /api/v1/links/{id}）→ 即座に共有停止

認証は簡易化のため X-Token ヘッダー方式（実物では JWT + リフレッシュトークン）。
トークンはハッシュ化して保存し、有効期限を設けている。
データ保存は SQLite（標準ライブラリ）。
"""

import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

DB_PATH = Path(os.environ.get("LINKCARE_DB", Path(__file__).parent / "sample.db"))
TOKEN_TTL_DAYS = 30


# ===== DB =====

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id),
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY,
                requester_id TEXT NOT NULL REFERENCES users(id),
                approver_id TEXT NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS permissions (
                user_id TEXT NOT NULL REFERENCES users(id),
                linked_user_id TEXT NOT NULL REFERENCES users(id),
                category TEXT NOT NULL,
                allowed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, linked_user_id, category)
            );
            """
        )
        conn.commit()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="LinkCare Sample API", version="0.2.0", lifespan=lifespan)


# ===== 認証 =====

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_user(x_token: str = Header(...)):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT id, name, token_expires_at FROM users WHERE token_hash = ?",
            (hash_token(x_token),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if datetime.fromisoformat(row["token_expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="token expired")
    return {"id": row["id"], "name": row["name"]}


def find_approved_link(conn, user_a, user_b):
    return conn.execute(
        """
        SELECT id FROM links
        WHERE status = 'approved'
          AND ((requester_id = ? AND approver_id = ?) OR (requester_id = ? AND approver_id = ?))
        """,
        (user_a, user_b, user_b, user_a),
    ).fetchone()


# ===== スキーマ =====

class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class LocationIn(BaseModel):
    device_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LinkIn(BaseModel):
    user_id: str


class PermissionIn(BaseModel):
    linked_user_id: str
    category: Literal["location"] = "location"  # 実物は項目ごと（位置・バッテリー等）。サンプルは位置のみ
    allowed: bool


# ===== エンドポイント =====

@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
def create_user(body: UserIn):
    user_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO users (id, name, token_hash, token_expires_at) VALUES (?, ?, ?, ?)",
            (user_id, body.name, hash_token(token), expires_at.isoformat()),
        )
        conn.commit()
    # トークンを平文で返すのは発行時の1回だけ（DBにはハッシュのみ保存）
    return {"id": user_id, "name": body.name, "token": token, "token_expires_at": expires_at.isoformat()}


@app.post("/api/v1/devices", status_code=status.HTTP_201_CREATED)
def create_device(body: DeviceIn, user=Depends(current_user)):
    device_id = str(uuid.uuid4())
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name) VALUES (?, ?, ?)",
            (device_id, user["id"], body.name),
        )
        conn.commit()
    return {"id": device_id, "name": body.name}


@app.post("/api/v1/locations", status_code=status.HTTP_201_CREATED)
def post_location(body: LocationIn, user=Depends(current_user)):
    with closing(get_db()) as conn:
        device = conn.execute(
            "SELECT id FROM devices WHERE id = ? AND user_id = ?",
            (body.device_id, user["id"]),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        recorded_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO locations (device_id, latitude, longitude, recorded_at) VALUES (?, ?, ?, ?)",
            (body.device_id, body.latitude, body.longitude, recorded_at),
        )
        conn.commit()
    return {"status": "stored", "recorded_at": recorded_at}


@app.get("/api/v1/links")
def list_links(user=Depends(current_user)):
    # 自分が関係する連携（送ったリクエスト・届いたリクエスト・承認済み）の一覧
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT id, requester_id, approver_id, status FROM links
            WHERE requester_id = ? OR approver_id = ?
            """,
            (user["id"], user["id"]),
        ).fetchall()
    return {
        "links": [
            {
                "id": r["id"],
                "partner_id": r["approver_id"] if r["requester_id"] == user["id"] else r["requester_id"],
                "direction": "outgoing" if r["requester_id"] == user["id"] else "incoming",
                "status": r["status"],
            }
            for r in rows
        ]
    }


@app.post("/api/v1/links", status_code=status.HTTP_201_CREATED)
def request_link(body: LinkIn, user=Depends(current_user)):
    if body.user_id == user["id"]:
        raise HTTPException(status_code=400, detail="cannot link to yourself")
    with closing(get_db()) as conn:
        target = conn.execute("SELECT id FROM users WHERE id = ?", (body.user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        dup = conn.execute(
            """
            SELECT id FROM links
            WHERE status IN ('pending', 'approved')
              AND ((requester_id = ? AND approver_id = ?) OR (requester_id = ? AND approver_id = ?))
            """,
            (user["id"], body.user_id, body.user_id, user["id"]),
        ).fetchone()
        if dup is not None:
            raise HTTPException(status_code=409, detail="link already exists")
        link_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO links (id, requester_id, approver_id) VALUES (?, ?, ?)",
            (link_id, user["id"], body.user_id),
        )
        conn.commit()
    return {"id": link_id, "status": "pending"}


@app.post("/api/v1/links/{link_id}/approve")
def approve_link(link_id: str, user=Depends(current_user)):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT status FROM links WHERE id = ? AND approver_id = ?",
            (link_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="link request not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="link is not pending")
        conn.execute("UPDATE links SET status = 'approved' WHERE id = ?", (link_id,))
        conn.commit()
    return {"id": link_id, "status": "approved"}


@app.delete("/api/v1/links/{link_id}")
def delete_link(link_id: str, user=Depends(current_user)):
    # 連携の解除（承認済み）・リクエストの取り消し／拒否（承認待ち）。どちらの当事者からでも可能
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT requester_id, approver_id FROM links WHERE id = ? AND (requester_id = ? OR approver_id = ?)",
            (link_id, user["id"], user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="link not found")
        a, b = row["requester_id"], row["approver_id"]
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        # 共有許可も双方向で削除する（再連携しても勝手に共有が復活しないように）
        conn.execute(
            "DELETE FROM permissions WHERE (user_id = ? AND linked_user_id = ?) OR (user_id = ? AND linked_user_id = ?)",
            (a, b, b, a),
        )
        conn.commit()
    return {"id": link_id, "status": "deleted"}


@app.put("/api/v1/permissions")
def set_permission(body: PermissionIn, user=Depends(current_user)):
    with closing(get_db()) as conn:
        if find_approved_link(conn, user["id"], body.linked_user_id) is None:
            raise HTTPException(status_code=404, detail="approved link not found")
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
    return {"status": "updated", "category": body.category, "allowed": body.allowed}


@app.get("/api/v1/locations/shared")
def shared_locations(user=Depends(current_user)):
    result = []
    with closing(get_db()) as conn:
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
        for partner_row in partners:
            partner_id = partner_row["partner"]
            perm = conn.execute(
                "SELECT allowed FROM permissions WHERE user_id = ? AND linked_user_id = ? AND category = 'location'",
                (partner_id, user["id"]),
            ).fetchone()
            if perm is None or perm["allowed"] != 1:
                continue
            devices = conn.execute(
                "SELECT id, name FROM devices WHERE user_id = ?", (partner_id,)
            ).fetchall()
            for device in devices:
                loc = conn.execute(
                    "SELECT latitude, longitude, recorded_at FROM locations WHERE device_id = ? ORDER BY id DESC LIMIT 1",
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
    return {"shared_locations": result}
