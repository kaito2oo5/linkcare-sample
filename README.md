# linkcare-sample

位置情報・端末情報の共有アプリ「LinkCare」（個人開発中）の簡易サンプルです。

実物のソースコードは開発中のため、実績紹介用に、基本設計の核となる「アカウント連携 → 共有許可 → 位置情報の共有」の流れだけを小さく再現したAPIサンプルを公開しています。

## 実物（LinkCare）について

- 位置情報・移動履歴、バッテリーや端末状態、セキュリティ情報を、アカウント同士で共有できるアプリ（iPhone / Android）
- 構成：FastAPI + PostgreSQL / PostGIS + Redis（バックエンド）、Flutter（アプリ）、React（管理画面）
- 共有は項目ごとに細かく許可でき、連携を解除すると即座に共有が止まる設計
- カメラ共有は端末間 WebRTC の直接通信（サーバーはシグナリングのみ）

## このサンプルの内容

FastAPI + SQLite で、以下の一連の流れを実装しています。

| 操作 | エンドポイント |
|---|---|
| ヘルスチェック | `GET /api/v1/health` |
| ユーザー登録（トークン発行） | `POST /api/v1/users` |
| 端末登録 | `POST /api/v1/devices` |
| 位置情報の投稿 | `POST /api/v1/locations` |
| 連携リクエスト | `POST /api/v1/links` |
| 連携の承認 | `POST /api/v1/links/{id}/approve` |
| 共有許可の設定 | `PUT /api/v1/permissions` |
| 共有された最新位置の取得 | `GET /api/v1/locations/shared` |

認証は簡易化のため `X-Token` ヘッダー方式にしています（実物では JWT + リフレッシュトークン）。共有許可がオフの相手の位置は取得できないことも確認できます。

## 実行方法

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

起動後、http://127.0.0.1:8000/docs でAPIドキュメント（Swagger UI）が確認できます。

## 備考

- サンプル内のデータはすべて架空のものです。
