#!/usr/bin/env python3
"""
server.py — 供应商发票工作台（防重复付款）
================================================================
独立的供应商发票登记 / 查重 / 录入模块。

特性：
  - 角色权限：admin（全量读写）、staff（可录入发票、不可删除/改付款状态）、其他（只读）
  - 硬阻断去重（G7 原则）：相同 (发票号, 供应商) 不允许重复录入，不接受强制覆盖
  - 关联 BPM 付款申请单号（字段 bpm_no）
  - 多员工并发：SQLite WAL + busy_timeout

部署：
  python server.py [port]            # 默认 8137
  # 首次启动用环境变量设置 admin 密码（不写死、不被 git 跟踪）：
  FIN_ADMIN_USER=admin FIN_ADMIN_PASS='你的密码' python server.py
"""
import http.server
import socketserver
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# pythonw 下无 stdout/stderr，print 会崩；重定向到 /dev/null
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "workbench.db"
SESSION_TTL = 7 * 24 * 3600  # 7 天

# 角色：admin 全量读写；staff 仅可录入发票；其他角色只读
ROLE_WRITE = {"admin"}
ROLE_VENDOR_WRITE = {"admin", "staff"}

# 首次启动自动创建 admin 账号。密码从环境变量读，未设置则随机生成并打印到日志。
# 公开仓库不写死任何密码；本地部署用不被 git 跟踪的 .env / 环境变量设置 FIN_ADMIN_PASS。
ADMIN_INITIAL_USER = os.environ.get("FIN_ADMIN_USER", "admin")
ADMIN_INITIAL_PASSWORD = os.environ.get("FIN_ADMIN_PASS", "")

# 内存 session：token -> {"username": str, "expires": float}
SESSIONS = {}


# ---------- SQLite ----------
def _conn():
    # WAL 模式 + busy_timeout：多员工同时录入时避免 "database is locked"
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'assistant',
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS vendor_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT NOT NULL DEFAULT '',
            vendor TEXT NOT NULL DEFAULT '',
            vendor_full TEXT NOT NULL DEFAULT '',
            invoice_date TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            site TEXT NOT NULL DEFAULT '',
            service TEXT NOT NULL DEFAULT '',
            ex_tax REAL NOT NULL DEFAULT 0,
            gst REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'INR',
            src TEXT NOT NULL DEFAULT '',
            bpm_no TEXT NOT NULL DEFAULT '',
            pay_status TEXT NOT NULL DEFAULT '未付',
            pay_no TEXT NOT NULL DEFAULT '',
            pay_date TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # 首次启动自动建 admin
    row = c.execute("SELECT username FROM users WHERE username=?", (ADMIN_INITIAL_USER,)).fetchone()
    if not row:
        pw = ADMIN_INITIAL_PASSWORD or secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        ph = hash_password(pw, salt)
        c.execute(
            "INSERT INTO users(username, password_hash, salt, role, display_name) VALUES(?,?,?,?,?)",
            (ADMIN_INITIAL_USER, ph, salt, "admin", "Admin"),
        )
        c.commit()
        if ADMIN_INITIAL_PASSWORD:
            print(f"[init] 已创建 admin 账号 {ADMIN_INITIAL_USER}（请用环境变量 FIN_ADMIN_PASS 设置的密码登录）")
        else:
            print(f"[init] 未设置环境变量 FIN_ADMIN_PASS，已生成随机 admin 密码：{pw}（请尽快修改）")
    c.close()


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200000).hex()


def verify_password(username, password):
    c = _conn()
    row = c.execute("SELECT password_hash, salt FROM users WHERE username=?", (username,)).fetchone()
    c.close()
    if not row:
        return False
    return hmac.compare_digest(hash_password(password, row["salt"]), row["password_hash"])


def get_user(username):
    c = _conn()
    row = c.execute(
        "SELECT username, role, display_name FROM users WHERE username=?", (username,)
    ).fetchone()
    c.close()
    return dict(row) if row else None


def role_of(username):
    """返回用户的角色名；用户不存在返回 None。角色校验必须用它，而非拿用户名和角色名比较。"""
    gu = get_user(username)
    return gu["role"] if gu else None


# ---------- session ----------
def issue_session(username):
    token = secrets.token_hex(32)
    SESSIONS[token] = {"username": username, "expires": time.time() + SESSION_TTL}
    return token


def resolve_session(cookie_header):
    if not cookie_header:
        return None
    try:
        ck = SimpleCookie(cookie_header)
        token = ck["wb_session"].value if "wb_session" in ck else None
    except Exception:
        return None
    if not token:
        return None
    sess = SESSIONS.get(token)
    if not sess:
        return None
    if time.time() > sess["expires"]:
        SESSIONS.pop(token, None)
        return None
    return sess["username"]


# ---------- 供应商发票台账（防重复付款） ----------
# 重复判定依据 = 发票号(invoice_no) + 供应商(vendor)。
# DB 层不强制 UNIQUE（允许跨年 legit 重号），由 API 做"查重→拦截"。
VENDOR_PAY_STATUSES = {"未付", "已付", "部分支付"}


def vendor_row_dict(r):
    return {
        "id": r["id"],
        "invoice_no": r["invoice_no"],
        "vendor": r["vendor"],
        "vendor_full": r["vendor_full"],
        "invoice_date": r["invoice_date"],
        "period": r["period"],
        "site": r["site"],
        "service": r["service"],
        "ex_tax": float(r["ex_tax"] or 0),
        "gst": float(r["gst"] or 0),
        "amount": float(r["amount"] or 0),
        "currency": (r["currency"] or "INR").strip().upper() or "INR",
        "src": r["src"],
        "bpm_no": r["bpm_no"],
        "pay_status": r["pay_status"] or "未付",
        "pay_no": r["pay_no"],
        "pay_date": r["pay_date"],
        "note": r["note"],
        "created_at": r["created_at"],
        "created_by": r["created_by"],
        "updated_at": r["updated_at"],
        "updated_by": r["updated_by"],
    }


def vendor_dedup(invoice_no, vendor, exclude_id=None):
    """返回与 (发票号, 供应商) 相同的已登记发票（排除自身）。
    供应商匹配规则：大小写不敏感地匹配 vendor（短名）OR vendor_full（全名）任一即视为重复。
    """
    c = _conn()
    v = (vendor or "").strip().lower()
    if not v:
        return []
    if exclude_id:
        rows = c.execute(
            "SELECT * FROM vendor_invoices WHERE invoice_no=? AND (LOWER(vendor)=? OR LOWER(vendor_full)=?) AND id<>?",
            (invoice_no, v, v, exclude_id),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM vendor_invoices WHERE invoice_no=? AND (LOWER(vendor)=? OR LOWER(vendor_full)=?)",
            (invoice_no, v, v),
        ).fetchall()
    c.close()
    return [vendor_row_dict(r) for r in rows]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send_json(self, code, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session_cookie(self, token):
        return f"wb_session={token}; HttpOnly; Path=/; SameSite=Lax"

    def _current_user(self):
        return resolve_session(self.headers.get("Cookie"))

    def _require_login(self):
        """返回当前 username，未登录则发送 401 并返回 None。"""
        u = self._current_user()
        if not u:
            self._send_json(401, {"ok": False, "msg": "未登录"})
            return None
        return u

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        KNOWN_PREFIXES = ("/api/", "/assets/", "/static/")
        KNOWN_PAGES = ("/", "/index.html", "/login.html", "/login", "/api/me", "/favicon.ico")
        if path not in KNOWN_PAGES and not any(path.startswith(p) for p in KNOWN_PREFIXES):
            return self._redirect("/")

        # 需要登录的页面
        protected_pages = ("/", "/index.html")
        if path in protected_pages:
            if not self._current_user():
                return self._redirect("/login.html")

        if path in ("/", ""):
            self.path = "/index.html"
        elif path == "/api/me":
            u = self._current_user()
            if not u:
                return self._send_json(401, {"ok": False, "msg": "未登录"})
            return self._send_json(200, {"ok": True, "user": get_user(u)})
        elif path == "/api/vendor-invoices":
            if not self._require_login():
                return
            return self._handle_vendor_list(parsed)
        elif path == "/api/vendor-invoice-check":
            if not self._require_login():
                return
            return self._handle_vendor_check(parsed)
        elif path == "/api/health":
            return self._send_json(200, {"ok": True})
        return super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()

        u = self._require_login()
        if not u:
            return

        if path == "/api/vendor-invoice":
            return self._handle_vendor_upsert(u)
        if path == "/api/vendor-invoice-delete":
            return self._handle_vendor_delete(u)
        if path == "/api/vendor-invoice-pay":
            return self._handle_vendor_pay(u)
        if path == "/api/vendor-invoice-import":
            return self._handle_vendor_import(u)
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------- 认证 ----------
    def _handle_login(self):
        data = self._read_json_body() or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return self._send_json(400, {"ok": False, "msg": "用户名和密码必填"})
        if not verify_password(username, password):
            return self._send_json(401, {"ok": False, "msg": "用户名或密码错误"})
        token = issue_session(username)
        self._send_json(200, {"ok": True, "user": get_user(username)},
                        headers={"Set-Cookie": self._session_cookie(token)})

    def _handle_logout(self):
        token = None
        try:
            ck = SimpleCookie(self.headers.get("Cookie") or "")
            token = ck["wb_session"].value if "wb_session" in ck else None
        except Exception:
            pass
        if token:
            SESSIONS.pop(token, None)
        self._send_json(200, {"ok": True},
                        headers={"Set-Cookie": "wb_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"})

    # ---------- 供应商发票台账 ----------
    def _handle_vendor_list(self, parsed):
        qs = parse_qs(parsed.query)
        vendor = (qs.get("vendor", [""])[0] or "").strip()
        period = (qs.get("period", [""])[0] or "").strip()
        pay_status = (qs.get("pay_status", [""])[0] or "").strip()
        q = (qs.get("q", [""])[0] or "").strip()
        sql = "SELECT * FROM vendor_invoices WHERE 1=1"
        args = []
        if vendor:
            sql += " AND vendor=?"
            args.append(vendor)
        if period:
            sql += " AND period=?"
            args.append(period)
        if pay_status:
            sql += " AND pay_status=?"
            args.append(pay_status)
        if q:
            sql += " AND (invoice_no LIKE ? OR service LIKE ? OR note LIKE ? OR vendor LIKE ? OR vendor_full LIKE ?)"
            like = "%" + q + "%"
            args += [like, like, like, like, like]
        sql += " ORDER BY invoice_date DESC, id DESC"
        c = _conn()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return self._send_json(200, {"ok": True, "rows": [vendor_row_dict(r) for r in rows]})

    def _handle_vendor_check(self, parsed):
        qs = parse_qs(parsed.query)
        invoice_no = (qs.get("invoice_no", [""])[0] or "").strip()
        vendor = (qs.get("vendor", [""])[0] or "").strip()
        if not invoice_no or not vendor:
            return self._send_json(400, {"ok": False, "msg": "发票号与供应商必填"})
        existing = vendor_dedup(invoice_no, vendor)
        return self._send_json(200, {"ok": True, "exists": len(existing) > 0, "existing": existing})

    def _handle_vendor_upsert(self, username):
        if role_of(username) not in ROLE_VENDOR_WRITE:
            return self._send_json(403, {"ok": False, "msg": "无写入权限（仅管理员或录入员）"})
        data = self._read_json_body() or {}
        inv = data.get("invoice")
        if not isinstance(inv, dict):
            return self._send_json(400, {"ok": False, "msg": "invoice 必填"})
        invoice_no = (inv.get("invoice_no") or "").strip()
        vendor = (inv.get("vendor") or "").strip()
        if not invoice_no or not vendor:
            return self._send_json(400, {"ok": False, "msg": "发票号与供应商必填"})
        rid = inv.get("id")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        vendor_full = (inv.get("vendor_full") or "").strip()
        invoice_date = (inv.get("invoice_date") or "").strip()
        period = (inv.get("period") or "").strip()
        site = (inv.get("site") or "").strip()
        service = (inv.get("service") or "").strip()
        try:
            ex_tax = round(float(inv.get("ex_tax") or 0), 2)
        except (TypeError, ValueError):
            ex_tax = 0.0
        try:
            gst = round(float(inv.get("gst") or 0), 2)
        except (TypeError, ValueError):
            gst = 0.0
        try:
            amount = round(float(inv.get("amount") or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        currency = (inv.get("currency") or "INR").strip().upper() or "INR"
        src = (inv.get("src") or "").strip()
        bpm_no = (inv.get("bpm_no") or "").strip()
        note = (inv.get("note") or "").strip()
        c = _conn()
        if rid:
            c.execute(
                """UPDATE vendor_invoices SET invoice_no=?,vendor=?,vendor_full=?,invoice_date=?,period=?,site=?,service=?,ex_tax=?,gst=?,amount=?,currency=?,src=?,bpm_no=?,note=?,updated_at=?,updated_by=? WHERE id=?""",
                (invoice_no, vendor, vendor_full, invoice_date, period, site, service, ex_tax, gst, amount, currency, src, bpm_no, note, now, username, rid),
            )
        else:
            # 重复硬阻断：新增时若 (发票号, 供应商) 已存在则直接拒绝，不接受强制写入
            existing = vendor_dedup(invoice_no, vendor)
            if existing:
                c.close()
                return self._send_json(200, {"ok": False, "duplicate": True, "msg": "该发票已登记，重复发票无法录入", "existing": existing})
            cur = c.execute(
                """INSERT INTO vendor_invoices(invoice_no,vendor,vendor_full,invoice_date,period,site,service,ex_tax,gst,amount,currency,src,bpm_no,note,created_at,created_by,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invoice_no, vendor, vendor_full, invoice_date, period, site, service, ex_tax, gst, amount, currency, src, bpm_no, note, now, username, now, username),
            )
            rid = cur.lastrowid
        c.commit()
        c.close()
        return self._send_json(200, {"ok": True, "id": rid})

    def _handle_vendor_delete(self, username):
        if role_of(username) not in ROLE_WRITE:
            return self._send_json(403, {"ok": False, "msg": "无权限（仅管理员）"})
        data = self._read_json_body() or {}
        rid = data.get("id")
        if not rid:
            return self._send_json(400, {"ok": False, "msg": "id 必填"})
        c = _conn()
        c.execute("DELETE FROM vendor_invoices WHERE id=?", (rid,))
        c.commit()
        c.close()
        return self._send_json(200, {"ok": True})

    def _handle_vendor_pay(self, username):
        if role_of(username) not in ROLE_WRITE:
            return self._send_json(403, {"ok": False, "msg": "无权限（仅管理员）"})
        data = self._read_json_body() or {}
        rid = data.get("id")
        if not rid:
            return self._send_json(400, {"ok": False, "msg": "id 必填"})
        pay_no = (data.get("pay_no") or "").strip()
        pay_date = (data.get("pay_date") or "").strip()
        pay_status = (data.get("pay_status") or "已付").strip()
        if pay_status not in VENDOR_PAY_STATUSES:
            pay_status = "已付"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        c = _conn()
        c.execute(
            "UPDATE vendor_invoices SET pay_no=?, pay_date=?, pay_status=?, updated_at=?, updated_by=? WHERE id=?",
            (pay_no, pay_date, pay_status, now, username, rid),
        )
        c.commit()
        c.close()
        return self._send_json(200, {"ok": True})

    def _handle_vendor_import(self, username):
        if role_of(username) not in ROLE_VENDOR_WRITE:
            return self._send_json(403, {"ok": False, "msg": "无权限（仅管理员或录入员）"})
        data = self._read_json_body() or {}
        rows = data.get("rows")
        if not isinstance(rows, list):
            return self._send_json(400, {"ok": False, "msg": "rows 必须是数组"})
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        inserted, skipped, duplicates = 0, 0, []
        c = _conn()
        for inv in rows:
            if not isinstance(inv, dict):
                skipped += 1
                continue
            invoice_no = (inv.get("invoice_no") or "").strip()
            vendor = (inv.get("vendor") or "").strip()
            if not invoice_no or not vendor:
                skipped += 1
                continue
            # 导入同样遵守硬阻断：重复发票跳过并报告
            existing = vendor_dedup(invoice_no, vendor)
            if existing:
                duplicates.append({"invoice_no": invoice_no, "vendor": vendor, "existing": existing})
                continue
            vendor_full = (inv.get("vendor_full") or "").strip()
            invoice_date = (inv.get("invoice_date") or "").strip()
            period = (inv.get("period") or "").strip()
            site = (inv.get("site") or "").strip()
            service = (inv.get("service") or "").strip()
            try:
                ex_tax = round(float(inv.get("ex_tax") or 0), 2)
            except (TypeError, ValueError):
                ex_tax = 0.0
            try:
                gst = round(float(inv.get("gst") or 0), 2)
            except (TypeError, ValueError):
                gst = 0.0
            try:
                amount = round(float(inv.get("amount") or 0), 2)
            except (TypeError, ValueError):
                amount = 0.0
            currency = (inv.get("currency") or "INR").strip().upper() or "INR"
            src = (inv.get("src") or "").strip()
            bpm_no = (inv.get("bpm_no") or "").strip()
            note = (inv.get("note") or "").strip()
            c.execute(
                """INSERT INTO vendor_invoices(invoice_no,vendor,vendor_full,invoice_date,period,site,service,ex_tax,gst,amount,currency,src,bpm_no,note,created_at,created_by,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invoice_no, vendor, vendor_full, invoice_date, period, site, service, ex_tax, gst, amount, currency, src, bpm_no, note, now, username, now, username),
            )
            inserted += 1
        c.commit()
        c.close()
        return self._send_json(200, {"ok": True, "inserted": inserted, "skipped": skipped, "duplicates": duplicates, "duplicate_count": len(duplicates)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8137
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    init_db()
    # 用 ThreadingHTTPServer，单线程版本会被一个卡住的连接占住全部请求
    socketserver.ThreadingMixIn.daemon_threads = True
    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"供应商发票工作台：http://127.0.0.1:{port}/")
    print(f"按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
