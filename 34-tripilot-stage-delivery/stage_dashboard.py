"""Serve the TripPilot V2 stage delivery dashboard."""

import argparse
import json
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from tripilot_stage import PROJECT_DIR, run_stage_demo
from travel_agent import PlanningInputError, TravelPlanningAgent


# WEB_DIR 保存第 34 课工作台需要的 HTML、CSS 和 JavaScript。
WEB_DIR = Path(__file__).resolve().parent / "web"
# REPORT_FILE 是 stage_dashboard.py 展示的结构化运行报告。
REPORT_FILE = PROJECT_DIR / "stage-report.json"
# STATIC_FILES 把允许访问的静态文件名映射到浏览器内容类型。
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


class DashboardAssetError(RuntimeError):
    """The dashboard cannot start without its static web assets."""


def validate_web_assets():
    """Check all page assets before opening the local port."""
    missing_files = [
        filename
        for filename in STATIC_FILES
        if not (WEB_DIR / filename).is_file()
    ]
    if missing_files:
        raise DashboardAssetError(
            f"缺少界面文件：{', '.join(missing_files)}；请检查目录：{WEB_DIR}"
        )


def load_state():
    """Load the latest report and add summary values used by the page header."""
    if not REPORT_FILE.is_file():
        run_stage_demo(PROJECT_DIR)

    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    scenarios = report["scenarios"]
    checks = report["checks"]
    cache = report["cache"]
    generated_at = datetime.fromtimestamp(REPORT_FILE.stat().st_mtime).astimezone()
    return {
        "checks": checks,
        "cache": cache,
        "preferences": report["preferences"],
        "scenarios": scenarios,
        "summary": {
            "scenario_count": len(scenarios),
            "checks_passed": sum(bool(value) for value in checks.values()),
            "checks_total": len(checks),
            "blocked_scenarios": sum(bool(item.get("denied")) for item in scenarios),
            "evidence_scenarios": sum(bool(item.get("citations")) for item in scenarios),
            "generated_at": generated_at.isoformat(timespec="seconds"),
        },
    }


class StageDashboardHandler(BaseHTTPRequestHandler):
    """Serve the interactive planning console and its local JSON API."""

    def send_json(self, status_code, payload):
        """Return JSON without allowing browsers to reuse stale state."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename):
        """Serve only the three whitelisted page assets."""
        if filename not in STATIC_FILES:
            self.send_error(404, "Not Found")
            return

        file_path = WEB_DIR / filename
        if not file_path.is_file():
            self.send_error(404, "Missing dashboard asset", f"未找到 {filename}")
            return

        try:
            content = file_path.read_bytes()
        except OSError as error:
            self.send_error(500, "Dashboard asset read failed", str(error))
            return

        self.send_response(200)
        self.send_header("Content-Type", STATIC_FILES[filename])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def read_json_body(self):
        """Read one bounded JSON object from the browser request."""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise PlanningInputError("请求必须使用 application/json")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise PlanningInputError("Content-Length 不合法") from error
        if content_length <= 0 or content_length > 65536:
            raise PlanningInputError("请求体为空或超过 64KB")
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PlanningInputError(f"JSON 请求体无法解析：{error}") from error

    def do_GET(self):
        """Serve the page, health check, or current stage state."""
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/state":
            try:
                self.send_json(200, load_state())
            except Exception as error:
                traceback.print_exc()
                self.send_json(500, {"error": f"读取阶段报告失败：{error}"})
            return
        if path == "/api/bootstrap":
            try:
                self.send_json(200, self.server.planning_agent.bootstrap())
            except Exception as error:
                traceback.print_exc()
                self.send_json(500, {"error": f"规划工作台初始化失败：{error}"})
            return

        filename = "index.html" if path == "/" else path.lstrip("/")
        self.send_static(filename)

    def do_POST(self):
        """Run a new trip plan or re-run the stage validation suite."""
        path = urlparse(self.path).path
        if path == "/api/plan":
            try:
                payload = self.read_json_body()
                result = self.server.planning_agent.plan(payload)
                self.send_json(200, result)
            except PlanningInputError as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                traceback.print_exc()
                self.send_json(500, {"error": f"行程规划失败：{error}"})
            return

        if path != "/api/run":
            self.send_json(404, {"error": "接口不存在"})
            return

        try:
            run_stage_demo(PROJECT_DIR)
            self.send_json(200, load_state())
        except Exception as error:
            traceback.print_exc()
            self.send_json(500, {"error": f"阶段校验失败：{error}"})

    def log_message(self, format_text, *args):
        """Keep server logs short and beginner-friendly."""
        print(f"[{self.address_string()}] {format_text % args}")


def create_server(host="127.0.0.1", port=8010):
    """Create the local server after validating all page assets."""
    validate_web_assets()
    load_state()
    server = HTTPServer((host, port), StageDashboardHandler)
    server.planning_agent = TravelPlanningAgent()
    return server


def main():
    """Parse the optional port and keep serving until Ctrl+C."""
    parser = argparse.ArgumentParser(description="启动 TripPilot V2 阶段工作台")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    try:
        server = create_server(port=args.port)
    except DashboardAssetError as error:
        print(f"TripPilot V2 工作台启动失败：{error}")
        raise SystemExit(1) from error
    except OSError as error:
        print(f"TripPilot V2 工作台启动失败：无法使用端口 {args.port}（{error}）")
        print("请关闭占用该端口的旧服务，或使用 --port 8011 启动。")
        raise SystemExit(1) from error

    print(f"TripPilot V2 工作台已启动：http://127.0.0.1:{args.port}")
    print("按 Ctrl+C 停止服务器。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTripPilot V2 工作台已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
