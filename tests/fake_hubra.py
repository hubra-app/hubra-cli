#!/usr/bin/env python3
"""Stand-in for the Hubra platform API (made-up data, real shapes).

    python tests/fake_hubra.py 8765 &
    HUBRA_API_URL=http://127.0.0.1:8765 HUBRA_TOKEN=hbr_plat_... python hubra_gh.py hubra health
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "hbr_plat_AAAAAAAAAAAA_" + "B" * 43
COMMIT = "0850a39c5eb9f90aef01b086262079499f79cbca"
OLD = "e1946be8df1437af384f71a5c157c5967ed508a6"

DATA = {
    "/api/v1/platform/api-tokens/self": {
        "kind": "platform", "name": "CLI test", "prefix": "hbr_plat_AAAAAAAAAAAA_…BBBB",
        "scopes": ["platform.diagnostics"], "expiresAt": "2026-12-04T23:37:51+00:00",
        "lastUsedAt": "2026-11-05T08:14:56+00:00", "useCount": 17, "club": None},
    "/api/v1/platform/health": {
        "status": "ok", "stale": False, "version": "0.2.0", "commit": COMMIT, "checked_at": 1793866491.7,
        "services": {"db": {"ok": True, "latency_ms": 115}, "redis": {"ok": True, "latency_ms": 18},
                     "smtp": {"ok": True, "latency_ms": 317},
                     "imap": {"ok": False, "latency_ms": 0, "error": "IMAP_HOST not configured"}},
        "processes": [
            {"component": "api", "host": "backend-1", "commit": COMMIT, "seen": 1793866488, "version": "0.2.0"},
            {"component": "worker", "host": "celery-worker-1", "commit": OLD, "seen": 1793866497, "version": "0.2.0"}],
        "last_upgrade": {"message": "Upgrade completed: 0.2.0 (e1946be) → 0.2.0 (0850a39)",
                         "time": "2026-11-04T23:31:22+00:00"}},
    "/api/v1/platform/alerts/overview": {
        "counts": {"relay": {"error": 1, "warning": 0}, "system": {"error": 2, "warning": 3}},
        "maintenance": {"active": False, "message": "", "until": None}, "problems": [],
        "recent": [
            {"level": "error", "message": "High API Error Rate", "time": "2026-11-04T20:23:05+00:00",
             "details": {"lines": ["5.4% of API requests in the last hour failed", "Requests: 295"]}},
            {"level": "info", "message": "Fallback relay is working again", "time": "2026-11-04T14:35:10+00:00",
             "details": {"lines": ["The check just logged in after 2 failures."]}}]},
    "/api/v1/platform/errors": {
        "counts": {"open": 1, "resolved": 0}, "current_build": "0.2.0 (0850a39)", "total": 1,
        "errors": [{"id": 5, "count": 7, "exc_type": "sqlalchemy.exc.ProgrammingError",
                    "route": "GET /api/v1/app/finance/invoices/mine",
                    "location": "hubra/api/finance/services.py in _paginate",
                    "message": "(psycopg2.errors.UndefinedColumn) column invoices.period_start does not exist\nLINE 2",
                    "first_seen": "2026-11-04T19:44:28+00:00", "last_seen": "2026-11-04T20:39:24+00:00",
                    "last_build": "0.2.0 (e1946be)", "new_in_current": False, "status": "open"}]},
    "/api/v1/platform/jobs": {
        "hours": 24, "queued": 0, "reserved": 0, "totals": {"done": 613, "failed": 1, "retried": 0},
        "running": [{"task": "hubra.core.tasks.upgrade_tasks.check_upgrades", "seconds": 7181}],
        "failed": [{"task": "hubra.core.tasks.mail_tasks.reconcile_mail", "time": "2026-11-05T06:00:00+00:00",
                    "error": "TimeoutError: relay"}],
        "tasks": [{"task": "hubra.core.tasks.health_tasks.run_health_checks", "done": 225, "failed": 0, "avg_ms": 405},
                  {"task": "hubra.core.tasks.mail_tasks.reconcile_mail", "done": 5, "failed": 1, "avg_ms": 350}]},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"code": "API_TOKEN_INVALID", "message": "Token is not valid"})
        if path not in DATA:
            return self.reply(404, {"message": "Not found"})
        self.reply(200, DATA[path])

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), Handler).serve_forever()
