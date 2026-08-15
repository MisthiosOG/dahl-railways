#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dahl account manager - register, status, import to 9Router.

Stdlib-only. Data files live in project root (parent of this folder).
Supports Webshare proxy format (host:port:user:pass) and parallel registration.
"""
from __future__ import annotations

import argparse
import datetime
import http.cookiejar
import json
import os
import random
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ACCOUNTS_FILE = Path(os.environ.get("DAHL_ACCOUNTS_FILE") or ROOT / "dahl_accounts.jsonl")
KEYS_FILE = ROOT / "dahl_keys.txt"
PROXIES_FILE = ROOT / "proxies.txt"

BASE_URL = "https://inference.dahl.global"
SIGNUP_URL = f"{BASE_URL}/v1/auth/signup"
SIGNIN_URL = f"{BASE_URL}/v1/auth/signin"
ALLOCATE_URL = f"{BASE_URL}/v1/account/allocate"
ACCOUNT_URL = f"{BASE_URL}/account"
KEYS_API_URL = f"{BASE_URL}/v1/account/keys"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}

# 9Router provider name used for Dahl imports.
ROUTER_PROVIDER = os.environ.get("NINE_ROUTER_PROVIDER_NAME", "POWFUROUTER")
ROUTER_PROVIDER_ID = os.environ.get(
    "NINE_ROUTER_PROVIDER_ID",
    "openai-compatible-chat-faea8d42-b146-4797-bd6a-46ac9ebb7546",
)
# 9Router remote HTTP endpoint (Railway deployment). Import posts each account
# over HTTP instead of writing to a local SQLite file, so it works in an
# ephemeral Railway container.
ROUTER_URL = (os.environ.get("NINE_ROUTER_URL") or "").rstrip("/")
ROUTER_API_KEY = os.environ.get("NINE_ROUTER_API_KEY") or ""
# Session cookie from the 9Router dashboard (auth_token=...). Used for admin API
# calls (POST /api/providers). If unset, falls back to NINE_ROUTER_API_KEY as Bearer.
ROUTER_COOKIE = os.environ.get("NINE_ROUTER_COOKIE") or ""
# 9Router admin API paths (discovered from dashboard JS source).
IMPORT_PATH = os.environ.get("NINE_ROUTER_IMPORT_PATH", "/api/providers")
NODES_PATH = os.environ.get("NINE_ROUTER_NODES_PATH", "/api/provider-nodes")
# Provider node ID to attach connections to. If empty, the import will auto-find
# or auto-create a node named by PROVIDER_NODE_NAME.
PROVIDER_NODE_ID = os.environ.get("NINE_ROUTER_PROVIDER_NODE_ID", "")
PROVIDER_NODE_NAME = os.environ.get("NINE_ROUTER_PROVIDER_NODE_NAME", "dahlz")
ROUTER_DEFAULT_MODEL = "moonshotai/Kimi-K2.6"
ROUTER_PREFIX = "GOD"

# Lock for file writes during parallel create.
_FILE_LOCK = threading.Lock()

# Ensure Unicode renders on Windows (cp1252 can't encode box-drawing / bar chars).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def mask_key(k: str | None) -> str:
    if not k:
        return "None"
    if len(k) <= 12:
        return "****"
    return f"{k[:8]}...{k[-4:]}"


def _parse_proxy_line(line: str) -> str | None:
    """Parse one proxy line. Supports: full URL, host:port, host:port:user:pass (Webshare)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Already a URL (http://, https://, socks5://).
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 2:  # host:port
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:  # host:port:user:pass (Webshare)
        host, port, user, pw = parts
        return f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(pw)}@{host}:{port}"
    return None


def _load_proxies_from_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            p = _parse_proxy_line(line)
            if p:
                out.append(p)
    return out


def get_random_proxy(proxy_file: str | Path | None = None) -> str | None:
    """Random proxy: local pool (port 5010) first, then proxy file.

    Proxy file order: explicit arg > env DAHL_PROXY_FILE > ROOT/proxies.txt.
    """
    # 1. Local proxy pool (jhao104 on port 5010) if running.
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:5010/get/",
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            proxy = json.loads(resp.read().decode("utf-8")).get("proxy")
            if proxy:
                return f"http://{proxy}"
    except Exception:
        pass

    # 2. Static proxy file.
    pfile = Path(proxy_file) if proxy_file else None
    if pfile is None:
        env_file = os.environ.get("DAHL_PROXY_FILE")
        pfile = Path(env_file) if env_file else PROXIES_FILE
    proxies = _load_proxies_from_file(pfile)
    return random.choice(proxies) if proxies else None


def _router_db_path() -> Path:
    return Path(
        os.environ.get("NINE_ROUTER_DB")
        or (Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
            / "9router/db/data.sqlite")
    ).resolve()


def _read_accounts(path: Path = ACCOUNTS_FILE) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #
def create_account(max_attempts: int = 5, proxy_file: str | Path | None = None) -> dict[str, Any]:
    """Register one Dahl account. Rotates proxy per attempt, handles 429 backoff.

    New flow: signup -> sign-in (fingerprint) for an authenticated session ->
    allocate the free-token pool (100M) onto the key so it is usable.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        proxy = get_random_proxy(proxy_file)
        cj = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(cj)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)

        username = f"syn_{secrets.token_hex(6)}"  # 16 chars, max is 20
        try:
            req = urllib.request.Request(
                SIGNUP_URL,
                data=json.dumps({"username": username}).encode("utf-8"),
                headers=HEADERS,
                method="POST",
            )
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            cookie_str = "; ".join(f"{c.name}={c.value}" for c in cj)
            if not cookie_str:
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie:
                    cookie_str = set_cookie.split(";")[0]

            api_key = (
                data.get("api_key", {}).get("token")
                or data.get("api_key")
                or data.get("apiKey")
            )
            if not api_key:
                raise ValueError("API key not returned in signup response")

            key_id = data.get("api_key", {}).get("id")
            fingerprint = data.get("fingerprint")
            if not key_id or not fingerprint:
                raise ValueError("key_id/fingerprint not returned in signup response")

            # Signup now returns a session that is NOT authenticated. Sign in with
            # the fingerprint to get a valid session, then allocate the free-token
            # pool onto the key so it is usable.
            valid_session = cookie_str
            try:
                signin_req = urllib.request.Request(
                    SIGNIN_URL,
                    data=json.dumps({"fingerprint": fingerprint}).encode("utf-8"),
                    headers=HEADERS,
                    method="POST",
                )
                with opener.open(signin_req, timeout=15) as resp:
                    resp.read()  # 200; sets the authenticated session cookie on the jar
                valid_session = "; ".join(f"{c.name}={c.value}" for c in cj)

                # Allocate the free-token pool (100M) onto this key.
                pool_req = urllib.request.Request(
                    ALLOCATE_URL,
                    data=json.dumps({"amount": 100_000_000, "key_id": key_id}).encode("utf-8"),
                    headers=HEADERS,
                    method="POST",
                )
                with opener.open(pool_req, timeout=15) as resp:
                    alloc = json.loads(resp.read().decode("utf-8"))
                available_tokens = int(
                    alloc.get("key", {}).get("available_tokens") or 100_000_000
                )
            except urllib.error.HTTPError as e:
                raise ValueError(f"signin/allocate failed: HTTP {e.code}") from e
            except Exception as e:
                raise ValueError(f"signin/allocate failed: {e}") from e

            return {
                "username": username,
                "api_key": api_key,
                "key_id": key_id,
                "fingerprint": fingerprint,
                "dahl_session": valid_session,
                "available_tokens": available_tokens,
                "created_at": _now_iso(),
            }
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else float(2 ** attempt)
                except ValueError:
                    wait = 10.0
                time.sleep(1.0 if proxy else wait)  # proxy active -> just rotate
                continue
            if e.code in (403, 502, 503, 504):
                time.sleep(1)
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1)
            continue

    if last_err:
        raise last_err
    raise RuntimeError("Failed after max signup attempts")


def cmd_create(
    count: int,
    delay: float = 0.0,
    threads: int = 1,
    proxy_file: str | Path | None = None,
) -> dict[str, Any]:
    """Register N accounts. Parallel when threads>1; JSON progress lines."""
    ok = fail = done = 0
    accounts: list[dict[str, Any]] = []
    counter_lock = threading.Lock()

    def _one(idx: int) -> bool:
        nonlocal ok, fail, done
        try:
            acc = create_account(proxy_file=proxy_file)
            with counter_lock:
                accounts.append(acc)
            success = True
            detail = f"{acc['username']}  key={mask_key(acc['api_key'])}"
        except Exception as e:
            detail = f"#{idx}  {e}"
            success = False
        with counter_lock:
            if success:
                ok += 1
            else:
                fail += 1
            done += 1
            print(json.dumps({
                "type": "progress",
                "done": done, "total": count, "ok": ok, "fail": fail,
                "detail": f"OK {detail}" if success else detail,
            }, ensure_ascii=True), flush=True)
        return success

    if threads <= 1:
        for i in range(count):
            if i > 0 and delay > 0:
                time.sleep(delay)
            _one(i + 1)
    else:
        workers = max(1, threads)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_one, i + 1) for i in range(count)]
            for _ in futs:
                _.result()

    return {"ok": True, "created": ok, "failed": fail, "accounts": accounts}


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def check_session_status(account: dict[str, Any]) -> dict[str, Any]:
    """Probe one account's keys endpoint, return token totals + active flag."""
    cookie = account.get("dahl_session")
    if not cookie:
        return {"ok": False, "error": "No session cookie"}

    req = urllib.request.Request(
        KEYS_API_URL,
        headers={**HEADERS, "Cookie": cookie},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if isinstance(data, list):
        keys_list = data
    elif isinstance(data, dict):
        keys_list = data.get("keys") or data.get("data") or ([data] if data else [])
    else:
        keys_list = []

    total = avail = 0
    has_active = has_exhausted = False
    for kobj in keys_list:
        if not isinstance(kobj, dict):
            continue
        tot = kobj.get("total_tokens") or kobj.get("totalTokens") or kobj.get("limit") or kobj.get("quota") or 0
        av = (kobj.get("available_tokens") or kobj.get("availableTokens")
              or kobj.get("remaining") or kobj.get("remaining_tokens") or 0)
        status = kobj.get("status") or kobj.get("testStatus") or "active"
        total += int(tot)
        avail += int(av)
        if int(av) > 0 and status != "exhausted":
            has_active = True
        else:
            has_exhausted = True

    return {
        "ok": True,
        "total_tokens": total,
        "available_tokens": avail,
        "has_active": has_active,
        "has_exhausted": has_exhausted,
    }


def _router_stats() -> dict[str, int]:
    """Read live usage stats for the Dahl provider from 9Router sqlite."""
    out = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "used_tokens": 0, "errors": 0, "active_connections": 0}
    db = _router_db_path()
    try:
        con = sqlite3.connect(str(db))
        cur = con.cursor()
        node = cur.execute(
            "SELECT id FROM providerNodes WHERE name=?", (ROUTER_PROVIDER,)
        ).fetchone()
        if node:
            pid = node[0]
            out["active_connections"] = int(cur.execute(
                "SELECT COUNT(*) FROM providerConnections WHERE provider=? AND isActive=1",
                (pid,),
            ).fetchone()[0] or 0)
            row = cur.execute(
                """SELECT COUNT(*),
                          COALESCE(SUM(promptTokens), 0),
                          COALESCE(SUM(completionTokens), 0),
                          COALESCE(SUM(CASE WHEN status IS NOT NULL AND status != 'ok' THEN 1 ELSE 0 END), 0)
                   FROM usageHistory
                   WHERE connectionId IN (SELECT id FROM providerConnections WHERE provider=?)""",
                (pid,),
            ).fetchone()
            out["requests"] = int(row[0] or 0)
            out["prompt_tokens"] = int(row[1] or 0)
            out["completion_tokens"] = int(row[2] or 0)
            out["used_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
            out["errors"] = int(row[3] or 0)
        con.close()
    except Exception:
        pass
    return out


def cmd_status(workers: int = 5) -> dict[str, Any]:
    accounts = _read_accounts()
    total = avail_tokens = active = exhausted = errors = 0

    if accounts:
        workers = max(1, min(20, workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for r in pool.map(check_session_status, accounts):
                if not r["ok"]:
                    errors += 1
                else:
                    total += r["total_tokens"]
                    avail_tokens += r["available_tokens"]
                    if r["has_active"]:
                        active += 1
                    else:
                        exhausted += 1

    return {
        "ok": True,
        "total_tokens": total,
        "available_tokens": avail_tokens,
        "consumed_tokens": max(0, total - avail_tokens),
        "active_count": active,
        "exhausted_count": exhausted,
        "error_count": errors,
        "accounts_count": len(accounts),
        "nine_router": _router_stats(),
    }


# --------------------------------------------------------------------------- #
# Allocate pool -> key (auto top-up for accounts with idle pool tokens)
# --------------------------------------------------------------------------- #
def _account_pool(account: dict[str, Any]) -> int:
    """Return tokens sitting idle in the pool (not yet allocated to a key)."""
    cookie = account.get("dahl_session")
    if not cookie:
        return 0
    req = urllib.request.Request(
        f"{BASE_URL}/v1/account",
        headers={**HEADERS, "Cookie": cookie},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return int(data.get("token_balance") or 0)
    except Exception:
        return -1  # error (distinct from 0 = none idle)


def _first_key_id(account: dict[str, Any]) -> int | None:
    """Return the first (active) key id for the account, else None."""
    cookie = account.get("dahl_session")
    if not cookie:
        return None
    req = urllib.request.Request(
        KEYS_API_URL,
        headers={**HEADERS, "Cookie": cookie},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    keys = data.get("keys") if isinstance(data, dict) else data
    if isinstance(keys, list):
        for k in keys:
            if isinstance(k, dict) and k.get("id"):
                return int(k["id"])
    return None


def cmd_allocate_all(workers: int = 1, delay: float = 0.15) -> dict[str, Any]:
    """Allocate each account's idle pool tokens onto its first key.

    Skips accounts without a session or without any key. Idempotent — calling
    again when the pool is already empty allocates nothing.

    Defaults to sequential (workers=1) with a small delay: hitting many accounts
    from one IP in parallel trips Dahl's rate limiter (429/errors). Raise
    workers only when accounts are spread across proxies.
    """
    accounts = _read_accounts()
    allocated = skipped = empty = error = 0

    def _one(acc: dict[str, Any]) -> None:
        nonlocal allocated, skipped, empty, error
        try:
            pool = _account_pool(acc)
            if pool < 0:
                error += 1
                return
            if pool == 0:
                empty += 1
                return
            key_id = _first_key_id(acc)
            if key_id is None:
                skipped += 1
                return
            req = urllib.request.Request(
                ALLOCATE_URL,
                data=json.dumps({"amount": pool, "key_id": key_id}).encode("utf-8"),
                headers={**HEADERS, "Cookie": acc["dahl_session"]},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                pass
            allocated += 1
        except Exception:
            error += 1

    if not accounts:
        return {"ok": True, "allocated": 0, "empty": 0, "skipped": 0,
                "error": 0, "accounts": 0}

    if workers <= 1:
        for acc in accounts:
            _one(acc)
            time.sleep(delay)
    else:
        workers = max(1, min(20, workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, accounts))

    return {
        "ok": True,
        "allocated": allocated,
        "empty": empty,
        "skipped": skipped,
        "error": error,
        "accounts": len(accounts),
    }


# --------------------------------------------------------------------------- #
# Import to 9Router
# --------------------------------------------------------------------------- #
def _ensure_provider_node() -> str:
    """Find existing or create a new provider node. Returns node ID."""
    if PROVIDER_NODE_ID:
        return PROVIDER_NODE_ID

    headers = {"Content-Type": "application/json"}
    if ROUTER_COOKIE:
        headers["Cookie"] = ROUTER_COOKIE
    else:
        headers["Authorization"] = f"Bearer {ROUTER_API_KEY}"
        headers["X-API-Key"] = ROUTER_API_KEY

    # Try to find existing node by name.
    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}{NODES_PATH}", headers=headers, method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for node in data.get("nodes") or []:
            if node.get("name") == PROVIDER_NODE_NAME:
                return node["id"]
    except Exception:
        pass

    # Create a new node.
    payload = {
        "name": PROVIDER_NODE_NAME,
        "prefix": ROUTER_PREFIX,
        "apiType": "chat",
        "baseUrl": f"{BASE_URL}/v1",
        "type": "openai-compatible",
    }
    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}{NODES_PATH}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["node"]["id"]
    except Exception as e:
        raise RuntimeError(f"Failed to create provider node: {e}")


def cmd_import(accounts: list[dict[str, Any]] | None = None,
               max_workers: int = 4) -> dict[str, Any]:
    """Import Dahl accounts into a remote 9Router over HTTP (base URL + cookie).

    Uses POST /api/providers (discovered from the 9Router dashboard JS source).
    Automatically finds or creates a provider node.
    Auth: NINE_ROUTER_COOKIE (dashboard session cookie), or fallback to Bearer.
    """
    if accounts is None:
        accounts = _read_accounts()
    if not accounts:
        return {"ok": True, "added": 0, "updated": 0, "skipped": 0, "active": 0}
    if not ROUTER_URL:
        raise RuntimeError("NINE_ROUTER_URL not set - cannot import")

    # Resolve provider node.
    node_id = _ensure_provider_node()

    # Auth headers for admin API.
    auth_headers: dict[str, str] = {"Content-Type": "application/json"}
    if ROUTER_COOKIE:
        auth_headers["Cookie"] = ROUTER_COOKIE
    else:
        auth_headers["Authorization"] = f"Bearer {ROUTER_API_KEY}"
        auth_headers["X-API-Key"] = ROUTER_API_KEY

    added = updated = skipped = error = 0
    lock = threading.Lock()

    def _one(acc: dict[str, Any]) -> None:
        nonlocal added, updated, skipped, error
        api_key = acc.get("api_key")
        username = acc.get("username")
        if not api_key or not username:
            with lock:
                skipped += 1
            return
        payload = {
            "name": username,
            "apiKey": api_key,
            "provider": node_id,
            "authType": "apikey",
        }
        req = urllib.request.Request(
            f"{ROUTER_URL}{IMPORT_PATH}",
            data=json.dumps(payload).encode("utf-8"),
            headers=auth_headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            code = "added"
        except urllib.error.HTTPError as e:
            code = "updated" if e.code in (200, 201, 409) else "error"
            if e.code not in (200, 201, 409):
                print(json.dumps({"type": "import_error", "username": username,
                                  "http": e.code, "body": e.read().decode("utf-8", "replace")[:300]}),
                      flush=True)
        except Exception as e:
            code = "error"
            print(json.dumps({"type": "import_error", "username": username,
                              "error": str(e)}), flush=True)
        with lock:
            if code == "added":
                added += 1
            elif code == "updated":
                updated += 1
            elif code == "error":
                error += 1

    workers = max(1, min(max_workers, len(accounts)))
    if workers <= 1:
        for acc in accounts:
            _one(acc)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, accounts))

    return {"ok": True, "added": added, "updated": updated,
            "skipped": skipped, "error": error}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(prog="dahl.manager", description="Dahl account manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="register N Dahl accounts (parallel)")
    c.add_argument("--count", "-n", type=int, required=True)
    c.add_argument("--delay", "-d", type=float, default=0.0, help="delay between accounts (sequential mode)")
    c.add_argument("--threads", "-t", type=int, default=0,
                   help="parallel workers (0=auto: min(count, num_proxies))")
    c.add_argument("--proxy-file", "-p", default=None,
                   help="proxy list file (Webshare host:port:user:pass or URLs). Default: ROOT/proxies.txt or $DAHL_PROXY_FILE")

    st = sub.add_parser("status", help="pool status + 9Router usage")
    st.add_argument("--workers", "-w", type=int, default=8)
    st.add_argument("--raw", action="store_true", help="JSON output (for lineagent)")

    al = sub.add_parser("allocate-all", help="auto-allocate idle pool tokens to keys for all accounts")
    al.add_argument("--workers", "-w", type=int, default=1,
                    help="parallel workers (default 1 = sequential, avoids rate-limit)")
    al.add_argument("--delay", type=float, default=0.15, help="delay seconds between accounts (sequential mode)")

    sub.add_parser("import", help="import accounts into 9Router DB")

    args = p.parse_args()

    if args.cmd == "create":
        proxy_file = args.proxy_file or os.environ.get("DAHL_PROXY_FILE")
        # auto: threads = min(count, num_proxies)
        threads = args.threads
        if threads <= 0:
            num_proxies = len(_load_proxies_from_file(Path(proxy_file) if proxy_file else PROXIES_FILE))
            threads = min(args.count, num_proxies) if num_proxies else 1
        res = cmd_create(args.count, args.delay, threads, proxy_file)
        print(json.dumps({"type": "result", **res}))
    elif args.cmd == "status":
        res = cmd_status(getattr(args, "workers", 8))
        print(json.dumps({"type": "result", **res}))
    elif args.cmd == "allocate-all":
        res = cmd_allocate_all(getattr(args, "workers", 1), getattr(args, "delay", 0.15))
        print(json.dumps({"type": "result", **res}))
    elif args.cmd == "import":
        res = cmd_import()
        print(json.dumps({"type": "result", **res}))


if __name__ == "__main__":
    main()
