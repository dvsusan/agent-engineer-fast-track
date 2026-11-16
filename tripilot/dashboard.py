"""Serve the TripPilot V1 interactive dashboard and JSON API."""

import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .main import ProjectError, TripPilotService


# WEB_DIR 保存界面需要的 HTML、CSS 和 JavaScript 文件。
WEB_DIR = Path(__file__).parent / "web"
# MAX_REQUEST_BYTES 限制请求体大小，避免界面接口接收过大的输入。
MAX_REQUEST_BYTES = 64 * 1024
# STATIC_FILES 把允许访问的静态文件名映射到浏览器内容类型。
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


# DashboardAssetError 表示界面文件没有准备完整，服务器不应继续启动。
class DashboardAssetError(RuntimeError):
    """The dashboard cannot start without its static web assets."""


# 检查三个静态文件是否存在，并把缺失路径一次告诉学习者。
def validate_web_assets():
    # missing_files 记录当前缺失的静态资源。
    missing_files = [
        filename
        for filename in STATIC_FILES
        if not (WEB_DIR / filename).is_file()
    ]
    if missing_files:
        raise DashboardAssetError(
            f"缺少界面文件：{', '.join(missing_files)}；"
            f"请检查目录：{WEB_DIR}"
        )


# DashboardHandler 处理静态页面和四个 JSON API。
class DashboardHandler(BaseHTTPRequestHandler):
    # service 由创建服务器时注入，保存当前 TripPilot 会话。
    service = None

    # 返回 JSON 响应，并禁止浏览器缓存 API 状态。
    def send_json(self, status_code, payload):
        # body 是编码后的 JSON 字节。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # 返回允许访问的静态文件，防止通过路径穿越读取其他文件。
    def send_static(self, filename):
        if filename not in STATIC_FILES:
            self.send_error(404, "Not Found")
            return

        # file_path 是当前白名单静态文件的真实位置。
        file_path = WEB_DIR / filename
        if not file_path.is_file():
            self.send_error(
                404,
                "Missing dashboard asset",
                f"未找到 {filename}，请检查目录：{WEB_DIR}",
            )
            return

        try:
            # content 是准备返回给浏览器的文件字节。
            content = file_path.read_bytes()
        except OSError as error:
            self.send_error(
                500,
                "Dashboard asset read failed",
                f"{filename}：{error}",
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", STATIC_FILES[filename])
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    # 处理页面加载和当前运行状态查询。
    def do_GET(self):
        # path 是去掉查询字符串后的请求路径。
        path = urlparse(self.path).path
        if path == "/api/state":
            self.send_json(200, self.service.snapshot())
            return
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return

        # filename 把根路径转换为默认页面。
        filename = "index.html" if path == "/" else path.lstrip("/")
        self.send_static(filename)

    # 处理运行计划、发送、补偿和重置操作。
    def do_POST(self):
        # path 是当前 API 操作路径。
        path = urlparse(self.path).path
        # content_length 是浏览器声明的请求体长度。
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "请求体过大"})
            return

        try:
            # payload 是请求体解析出的 JSON 对象。
            payload = (
                json.loads(self.rfile.read(content_length))
                if content_length
                else {}
            )
            if not isinstance(payload, dict):
                raise ProjectError("请求体必须是 JSON 对象")

            if path == "/api/run":
                state = self.service.start(payload)
            elif path == "/api/send":
                state = self.service.send()
            elif path == "/api/compensate":
                state = self.service.compensate()
            elif path == "/api/reset":
                self.service.reset()
                state = self.service.snapshot()
            else:
                self.send_json(404, {"error": "接口不存在"})
                return

            self.send_json(200, state)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json(400, {"error": f"JSON 格式错误：{error}"})
        except ProjectError as error:
            self.send_json(
                422,
                {
                    "error": str(error),
                    "state": self.service.snapshot(),
                },
            )
        except Exception as error:
            traceback.print_exc()
            self.send_json(500, {"error": f"服务器内部错误：{error}"})

    # 使用简洁日志，避免每次页面请求打印完整请求头。
    def log_message(self, format_text, *args):
        print(f"[{self.address_string()}] {format_text % args}")


# 创建绑定到指定目录和端口的 TripPilot 网页服务器。
def create_server(output_dir, host="127.0.0.1", port=8000):
    validate_web_assets()
    # service 是这个服务器实例共享的 TripPilot 项目会话。
    service = TripPilotService(output_dir)

    # BoundHandler 把服务实例注入每个 HTTP 请求处理器。
    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.service = service
    # server 是负责接收本地浏览器请求的 HTTP 服务器。
    server = HTTPServer((host, port), BoundHandler)
    return server


# 启动网页界面，直到用户按 Ctrl+C 结束。
def run_dashboard(output_dir, host="127.0.0.1", port=8000):
    # server 是本次启动的 TripPilot 网页服务器。
    try:
        server = create_server(output_dir, host=host, port=port)
    except DashboardAssetError as error:
        print(f"TripPilot UI 启动失败：{error}")
        raise SystemExit(1) from error
    except OSError as error:
        print(f"TripPilot UI 启动失败：无法使用端口 {port}（{error}）")
        print("请关闭占用该端口的旧服务，或修改 tripilot_app.py 中的 DASHBOARD_PORT。")
        raise SystemExit(1) from error

    print(f"TripPilot UI 已启动：http://{host}:{port}")
    print("按 Ctrl+C 停止服务器。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTripPilot UI 已停止。")
    finally:
        server.server_close()
