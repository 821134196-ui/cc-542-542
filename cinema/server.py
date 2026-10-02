"""HTTP 服务：stdlib http.server 实现 JSON API + 静态页面 + 本地亮度照片。"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import services as svc
from .adapter import MockProjectorAdapter
from .db import connect, init_db, is_empty, rows
from .seed import DEFECTIVE_SERIALS, seed
from .services import ServiceError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

_STATUS = {"not_found": 404, "permission": 403, "mutex": 409, "spare_mismatch": 409,
           "lamp_not_spare": 409, "step_order": 409, "state": 409}


class App:
    """共享状态：一条 SQLite 连接 + 一个模拟适配器（锁保护）。"""

    def __init__(self, db_path: str, photos_dir: str):
        self.db_path = db_path
        self.photos_dir = photos_dir
        self.conn = connect(db_path)
        self.adapter = MockProjectorAdapter()
        self.lock = threading.RLock()
        init_db(self.conn)
        if is_empty(self.conn):
            seed(self.conn, self.adapter)
        else:  # 已有库：按台账重建适配器读数
            for p in rows(self.conn, "SELECT * FROM projectors"):
                lamp = svc.current_lamp(self.conn, p["id"])
                self.adapter.attach(p["id"], lamp["cumulative_hours"] if lamp else 0)
            self.adapter.defective_serials.update(DEFECTIVE_SERIALS)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CinemaLamp/1.0"

        # ---------- 工具 ----------
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path, mime):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def log_message(self, fmt, *args):  # 静默
            pass

        # ---------- 路由 ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            q = self._query()
            with app.lock:
                try:
                    if path == "/" or path == "/index.html":
                        return self._file(os.path.join(STATIC_DIR, "index.html"),
                                          "text/html; charset=utf-8")
                    if path.startswith("/static/"):
                        return self._static(path)
                    if path.startswith("/photos/"):
                        name = os.path.basename(path)
                        return self._file(os.path.join(app.photos_dir, name), "image/svg+xml")
                    if path == "/api/overview":
                        return self._json(svc.hall_overview(app.conn))
                    if path == "/api/ledger":
                        return self._json(svc.equipment_ledger(app.conn))
                    if path == "/api/work_orders":
                        if q.get("id"):
                            return self._json(svc.get_work_order(app.conn, int(q["id"])))
                        return self._json(rows(app.conn,
                            "SELECT * FROM work_orders ORDER BY id DESC"))
                    if path == "/api/screenings":
                        return self._json(rows(app.conn,
                            "SELECT * FROM screenings ORDER BY id DESC"))
                    if path == "/api/incidents":
                        return self._json(rows(app.conn,
                            "SELECT * FROM incidents ORDER BY id DESC"))
                    if path == "/api/impacts":
                        return self._json(rows(app.conn, """
                            SELECT i.*, s.title, s.start_time, h.name AS hall_name
                            FROM show_impacts i
                            JOIN shows s ON s.id = i.show_id
                            JOIN halls h ON h.id = s.hall_id
                            ORDER BY i.id DESC"""))
                    if path == "/api/approvals":
                        return self._json(rows(app.conn, """
                            SELECT a.*, u.name AS approver_name FROM approvals a
                            JOIN users u ON u.id = a.approver_id ORDER BY a.id DESC"""))
                    if path == "/api/shows":
                        return self._json(rows(app.conn, """
                            SELECT s.*, h.name AS hall_name FROM shows s
                            JOIN halls h ON h.id = s.hall_id ORDER BY s.start_time"""))
                    if path == "/api/users":
                        return self._json(rows(app.conn, "SELECT * FROM users ORDER BY id"))
                    if path == "/api/spares":
                        pid = int(q.get("projector_id", 0))
                        if pid:
                            proj = svc._must("projectors", app.conn, pid)
                            return self._json(rows(app.conn,
                                "SELECT * FROM lamps WHERE status = 'spare' AND model = ?"
                                " ORDER BY serial", (proj["lamp_model"],)))
                        return self._json(rows(app.conn,
                            "SELECT * FROM lamps WHERE status = 'spare'"
                            " ORDER BY model, serial"))
                    return self._json({"error": "not found"}, 404)
                except ServiceError as e:
                    return self._json({"error": e.message, "code": e.code},
                                      _STATUS.get(e.code, 400))

        def do_POST(self):
            path = self.path.split("?")[0]
            b = self._body()
            with app.lock:
                try:
                    if path == "/api/work_orders":
                        return self._json(svc.create_work_order(
                            app.conn, app.adapter, int(b["projector_id"]),
                            int(b["technician_id"]), b.get("reason", "")), 201)
                    m = re.fullmatch(r"/api/work_orders/(\d+)/steps/(\d)", path)
                    if m:
                        return self._json(svc.complete_step(
                            app.conn, app.adapter, int(m.group(1)), int(m.group(2)),
                            note=b.get("note", ""),
                            new_lamp_id=b.get("new_lamp_id"),
                            photos_dir=app.photos_dir))
                    m = re.fullmatch(r"/api/work_orders/(\d+)/cancel", path)
                    if m:
                        return self._json(svc.cancel_work_order(
                            app.conn, int(m.group(1)), b.get("reason", "")))
                    m = re.fullmatch(r"/api/work_orders/(\d+)/rescreen", path)
                    if m:
                        return self._json(svc.rescreen(
                            app.conn, app.adapter, int(m.group(1)),
                            photos_dir=app.photos_dir))
                    m = re.fullmatch(r"/api/work_orders/(\d+)/release", path)
                    if m:
                        return self._json(svc.approve_release(
                            app.conn, int(m.group(1)), int(b["approver_id"]),
                            b.get("decision", "approved"), b.get("note", "")))
                    m = re.fullmatch(r"/api/incidents/(\d+)/resolve", path)
                    if m:
                        return self._json(svc.resolve_incident(
                            app.conn, int(m.group(1)), b.get("resolution", "")))
                    m = re.fullmatch(r"/api/incidents/(\d+)/retry_reset", path)
                    if m:
                        return self._json(svc.retry_timer_reset(
                            app.conn, app.adapter, int(m.group(1))))
                    if path == "/api/lamp_failure":
                        return self._json(svc.report_lamp_failure(
                            app.conn, app.adapter, int(b["projector_id"]),
                            b.get("detail", "")), 201)
                    m = re.fullmatch(r"/api/shows/(\d+)/delay", path)
                    if m:
                        return self._json(svc.delay_show(
                            app.conn, int(m.group(1)), int(b["minutes"]),
                            b.get("reason", "")))
                    m = re.fullmatch(r"/api/shows/(\d+)/complete", path)
                    if m:
                        return self._json(svc.complete_show(
                            app.conn, app.adapter, int(m.group(1))))
                    if path == "/api/overrides":
                        return self._json(svc.grant_override(
                            app.conn, int(b["projector_id"]), int(b["supervisor_id"]),
                            b.get("reason", "")), 201)
                    return self._json({"error": "not found"}, 404)
                except ServiceError as e:
                    return self._json({"error": e.message, "code": e.code},
                                      _STATUS.get(e.code, 400))
                except (KeyError, ValueError, TypeError) as e:
                    return self._json({"error": f"参数错误: {e}"}, 400)

        def _static(self, path):
            name = os.path.basename(path)
            mime = {".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8"}.get(
                os.path.splitext(name)[1], "application/octet-stream")
            return self._file(os.path.join(STATIC_DIR, name), mime)

        def _query(self) -> dict:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            return {k: v[0] for k, v in qs.items()}

    return Handler


def create_server(db_path: str, photos_dir: str, port: int) -> ThreadingHTTPServer:
    app = App(db_path, photos_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
