#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP server for Dahl Railways (keeps the Railway container alive).

Stdlib-only. Routes:
  GET  /health             -> {"ok": true}
  GET  /                   -> tiny status page
  GET  /accounts           -> last farmed accounts as JSONL
  GET  /jobs/<id>          -> job status / progress
  POST /farm {count}       -> start async job: create + import to 9Router
  POST /create {count}     -> start async job: create only (returns accounts on completion)
  POST /import {accounts}  -> import given accounts (synchronous, fast)

Long-running operations (/farm, /create) run in a background thread and return
a job_id immediately, so the HTTP client never waits (Railway-safe).

Security: optional ADMIN_TOKEN env var must match `x-admin-token` header on
POST routes. If unset, POSTs are allowed (ponytail: single-user tool; add
auth when exposed publicly).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import core
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

PORT = int(os.environ.get("PORT", "8080"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not ADMIN_TOKEN:
        return True
    return handler.headers.get("x-admin-token") == ADMIN_TOKEN


# In-memory job store: {job_id: {status, created, done, ok, fail, result, error}}
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _new_job(kind: str, count: int) -> tuple[str, dict[str, Any]]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "kind": kind, "count": count, "status": "started",
        "created": 0, "done": 0, "ok": 0, "fail": 0,
        "result": None, "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job_id, job


def _run_farm(job: dict[str, Any], count: int, threads: int) -> None:
    try:
        created = core.cmd_create(count, threads=threads)
        job["created"] = created.get("created", 0)
        imported = core.cmd_import(accounts=created.get("accounts", []))
        job["result"] = {"create": created, "import": imported}
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def _run_create(job: dict[str, Any], count: int, threads: int) -> None:
    try:
        res = core.cmd_create(count, threads=threads)
        # Persist for /accounts download.
        try:
            with open(core.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                for acc in res.get("accounts", []):
                    f.write(json.dumps(acc) + "\n")
        except Exception:
            pass
        job["created"] = res.get("created", 0)
        job["result"] = res
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def _start(kind: str, count: int, threads: int) -> dict[str, Any]:
    job_id, job = _new_job(kind, count)
    if kind == "farm":
        t = threading.Thread(target=_run_farm, args=(job, count, threads), daemon=True)
    else:
        t = threading.Thread(target=_run_create, args=(job, count, threads), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # quieter logs
        pass

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path in ("/health", "/health"):
            self._send(200, {"ok": True, "service": "dahl-railways"})
            return
        if path in ("", "/"):
            html_file = ROOT / "admin.html"
            if html_file.is_file():
                body = html_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(200, {
                    "service": "dahl-railways",
                    "endpoints": ["/health", "/farm", "/create", "/import", "/accounts", "/jobs/<id>"],
                    "note": "POST /farm {count:N} -> async create + import. Upload admin.html for web UI.",
                })
            return
        if path == "/accounts":
            f = core.ACCOUNTS_FILE
            if f.is_file():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-jsonlines")
                self.send_header("Content-Disposition", "attachment; filename=dahl_accounts.jsonl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(200, {"accounts": []})
            return
        if path.startswith("/jobs/"):
            job_id = path.split("/")[-1]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                self._send(404, {"error": "job not found"})
            else:
                self._send(200, {k: job[k] for k in
                                 ("id", "kind", "count", "status", "created", "ok", "fail", "error")})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not _authorized(self):
            self._send(401, {"error": "unauthorized"})
            return
        data = self._read_json()
        path = self.path.rstrip("/")

        if path == "/create":
            count = max(1, min(int(data.get("count") or 1), 100))
            threads = max(1, int(data.get("threads") or 1))
            self._send(202, _start("create", count, threads))
            return

        if path == "/import":
            accounts = data.get("accounts") or []
            if not isinstance(accounts, list):
                self._send(400, {"error": "accounts must be a list"})
                return
            res = core.cmd_import(accounts=accounts)
            self._send(200, {"type": "result", **res})
            return

        if path == "/farm":
            count = max(1, min(int(data.get("count") or 1), 100))
            threads = max(1, int(data.get("threads") or 1))
            self._send(202, _start("farm", count, threads))
            return

        self._send(404, {"error": "not found"})


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dahl-railways listening on :{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
