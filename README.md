# Dahl Railways

Dahl account farming **dijalankan di Railway**, dengan import otomatis ke 9Router lewat HTTP API (base URL + session cookie) — bukan nulis SQLite lokal, jadi bisa jalan di container Railway yang ephemeral.

Beda sama tool `dahl` lokal: ini **server HTTP** yang nge-listen di `$PORT`, jadi Railway nge-jaga container tetap hidup.

## Endpoint

| Method | Path | Fungsi |
|--------|------|--------|
| `GET` | `/health` | Health check (dipake Railway healthcheck) |
| `GET` | `/` | Info endpoint |
| `GET` | `/accounts` | Donlot hasil farm terakhir (JSONL) |
| `GET` | `/jobs/<id>` | Polling status job |
| `POST` | `/farm` | `{"count":N}` → **async** create + import ke 9Router, return `job_id` |
| `POST` | `/create` | `{"count":N}` → async create aja, return `job_id` |
| `POST` | `/import` | `{"accounts":[...]}` → import akun tertentu (sync) |

`/farm` dan `/create` itu **async** — langsung balikin `job_id`, terus lo poll `/jobs/<id>` sampe `status: "done"`. Ini biar gak putus koneksi di Railway.

Contoh pakai `/farm`:
```bash
# 1. Mulai job
curl -X POST -H "Content-Type: application/json" \
  -d '{"count":5,"threads":3}' \
  https://YOUR-APP.up.railway.app/farm
# -> {"job_id":"abc123","status":"started"}

# 2. Poll
curl https://YOUR-APP.up.railway.app/jobs/abc123
# -> {"status":"done","created":5,...}
```

## Deploy ke Railway

1. Push folder ini ke GitHub.
2. Import repo ke Railway (bisa pake template / New Project → Deploy from GitHub).
3. `railway.json` udah nyetel `startCommand` & healthcheck — gak perlu ubah.
4. Tambah **Variables**:

| Variable | Wajib? | Contoh |
|----------|--------|--------|
| `NINE_ROUTER_URL` | ✅ | `https://9router-production-2465.up.railway.app` |
| `NINE_ROUTER_COOKIE` | ✅ | `auth_token=<JWT-dari-dashboard>` |
| `NINE_ROUTER_PROVIDER_NODE_NAME` | opsional | `dahlz` (node yang udah ada; auto-create kalo belum) |
| `NINE_ROUTER_PROVIDER_NODE_ID` | opsional | set ID node langsung biar skip lookup |
| `NINE_ROUTER_IMPORT_PATH` | opsional | `api/providers` (default) |
| `ADMIN_TOKEN` | opsional | kalo mau proteksi POST pake header `x-admin-token` |

> **Cara dapet cookie:** login dashboard 9Router di browser → F12 → tab Network/Application → copy value cookie `auth_token=...`.

## Cara kerja import

- Import pake `POST /api/providers` (ditemuin dari JS source dashboard 9Router).
- Setiap akun Dahl → 1 connection baru di node provider (`authType: "apikey"`).
- Otomatis cari node by name (`dahlz`), atau bikin baru kalo belum ada.
- Auth pake cookie `auth_token` (di-set via `NINE_ROUTER_COOKIE`).

## Struktur

```
dahl-railways/
├── app.py                 # Railway entrypoint
├── railway.json           # startCommand + healthcheck
├── requirements.txt       # stdlib-only (kosong)
├── dahl_railways/
│   ├── __init__.py
│   ├── core.py            # farming (create/allocate/status) + import HTTP
│   └── server.py          # HTTP server async
```
