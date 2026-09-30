#!/usr/bin/env python
"""AutoLearn 靶场服务。

一条命令起两个端口：

    python scripts/serve_mock.py
    →  题目靶场   http://127.0.0.1:8899/quiz.html
       网课靶场   http://127.0.0.1:8899/course.html
       跨域 frame http://127.0.0.1:8900/frame.html

**为什么要两个端口**：iframe 题必须真跨域才有效。``srcdoc`` 与同源 iframe
都会被同源策略放行，测不出跨域这一条路径。端口不同即不同源，这是最小代价的
真跨域方案。

媒体不落仓库：``/media/*.wav`` 按 ``?d=<秒>`` 现场合成 8kHz/16bit/单声道 WAV，
并完整支持 HTTP Range —— seek 与断点续跑都依赖它。

为何用 WAV 而不是真视频：本机没有可用的编码器，而 M5 的全部媒体断言只依赖
``paused`` / ``ended`` / ``currentTime`` / ``duration``，这些 WAV 全部满足；
画面变化由页面的 canvas 时间码叠加提供（截图差分够用）。好处是仓库零二进制、
服务零外部依赖。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import wave
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = ROOT / "mock_site"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
DEFAULT_FRAME_PORT = 8900
FRAME_PLACEHOLDER = "__FRAME_ORIGIN__"

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16bit
TONE_HZ = 440
TONE_AMPLITUDE = 1200

MIN_MEDIA_SECONDS = 1.0
MAX_MEDIA_SECONDS = 120.0

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".wav": "audio/wav",
}

QUESTION_API = re.compile(r"^/mock-api/question/(\d+)$")
MEDIA_PATH = re.compile(r"^/media/([A-Za-z0-9_.-]+)$")


# --------------------------------------------------------------------------- #
# 媒体合成
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=32)
def build_wav(seconds: float) -> bytes:
    """合成一段低幅 440Hz 正弦的 WAV。

    刻意不用纯数字静音：某些解码路径对全零 16bit 帧有奇怪优化，
    给一点幅度最省事。
    """
    import math
    from array import array

    frames = int(SAMPLE_RATE * seconds)
    pcm = array(
        "h",
        (
            int(TONE_AMPLITUDE * math.sin(2.0 * math.pi * TONE_HZ * i / SAMPLE_RATE))
            for i in range(frames)
        ),
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def parse_duration(query: dict[str, list[str]]) -> float:
    raw = (query.get("d") or [""])[0]
    try:
        value = float(raw)
    except ValueError:
        value = 24.0
    return max(MIN_MEDIA_SECONDS, min(MAX_MEDIA_SECONDS, value))


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """解析单区间 ``bytes=start-end``。不支持多区间，返回 ``None`` 表示不支持。"""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].strip()
    if "," in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # bytes=-N 表示最后 N 字节
            length = int(end_s)
            if length <= 0:
                return None
            return max(0, total - length), total - 1
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    if start >= total or start > end:
        return None
    return start, min(end, total - 1)


# --------------------------------------------------------------------------- #
# 请求处理
# --------------------------------------------------------------------------- #
class MockRequestHandler(BaseHTTPRequestHandler):
    """靶场 HTTP 处理。``root`` / ``role`` / ``frame_origin`` 由 server 注入。"""

    server_version = "AutoLearnMock/1.0"
    protocol_version = "HTTP/1.1"

    root: Path = SITE_ROOT
    role: str = "main"
    frame_origin: str = ""

    # -- 基础设施 ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write(f"[mock] {self.address_string()} {fmt % args}\n")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
        *,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Mock-Role", self.role)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            MIME[".json"],
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
            head_only=self.command == "HEAD",
        )

    def _send_error_text(self, status: int, message: str) -> None:
        self._send(
            status,
            (message + "\n").encode("utf-8"),
            "text/plain; charset=utf-8",
            head_only=self.command == "HEAD",
        )

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._send(
            HTTPStatus.NO_CONTENT,
            b"",
            "text/plain",
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path in ("/", ""):
            target = "/frame.html" if self.role == "frame" else "/quiz.html"
            self._send(
                HTTPStatus.FOUND,
                b"",
                "text/plain",
                {"Location": target},
                head_only=True,
            )
            return

        question_match = QUESTION_API.match(path)
        if question_match:
            self._serve_question_api(int(question_match.group(1)))
            return

        if MEDIA_PATH.match(path):
            self._serve_media(path, query)
            return

        self._serve_static(path)

    # -- /mock-api/question/<n> -------------------------------------------- #
    def _serve_question_api(self, index: int) -> None:
        payload = self._load_questions()
        if payload is None:
            self._send_json({"error": "questions.json 读取失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        question = next(
            (q for q in payload.get("questions", []) if q.get("index") == index),
            None,
        )
        if question is None:
            self._send_json({"error": f"题库序号 {index} 不存在"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(question)

    def _load_questions(self) -> dict | None:
        # 用 self.root 而非模块级 SITE_ROOT：打包成 exe 后 __file__ 失效，
        # 只有注入的 root 是可靠的（P9 打包策略）。
        target = self.root / "static" / "questions.json"
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except OSError:
            return None

    # -- /media/<name>.wav?d=<秒> ------------------------------------------ #
    def _serve_media(self, path: str, query: dict[str, list[str]]) -> None:
        seconds = parse_duration(query)
        body = build_wav(seconds)
        total = len(body)
        rng = parse_range(self.headers.get("Range"), total)

        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Mock-Media-Seconds": str(seconds),
        }

        if rng is None:
            self._send(
                HTTPStatus.OK, body, MIME[".wav"], headers, head_only=self.command == "HEAD"
            )
            return

        start, end = rng
        chunk = body[start : end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        self._send(
            HTTPStatus.PARTIAL_CONTENT,
            chunk,
            MIME[".wav"],
            headers,
            head_only=self.command == "HEAD",
        )

    # -- 静态文件 ---------------------------------------------------------- #
    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/")

        # frame.html 只在 frame 端口提供 —— 在主端口给出明确报错，
        # 免得有人误用同源 iframe 而以为跨域测得通。
        if self.role != "frame" and relative == "frame.html":
            self._send_error_text(
                HTTPStatus.NOT_FOUND,
                "frame.html 只在 frame 端口提供（避免同源 iframe）。"
                f" 请用 {self.frame_origin}/frame.html",
            )
            return

        target = (self.root / relative).resolve()
        if not str(target).startswith(str(self.root.resolve())) or not target.is_file():
            self._send_error_text(HTTPStatus.NOT_FOUND, f"未找到 {relative}")
            return

        raw = target.read_bytes()
        suffix = target.suffix.lower()
        content_type = MIME.get(suffix, "application/octet-stream")

        if suffix == ".html":
            # 让页面知道 frame 的真实源（端口可能被 --frame-port 改过）
            raw = raw.replace(FRAME_PLACEHOLDER.encode(), self.frame_origin.encode())
            content_type = MIME[".html"]

        self._send(
            HTTPStatus.OK,
            raw,
            content_type,
            {"Cache-Control": "no-store"},
            head_only=self.command == "HEAD",
        )


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def build_server(
    root: Path,
    host: str,
    port: int,
    role: str,
    frame_origin: str,
    quiet: bool = False,
) -> MockServer:
    handler = type(
        f"MockHandler_{role}",
        (MockRequestHandler,),
        {"root": root, "role": role, "frame_origin": frame_origin},
    )
    server = MockServer((host, port), handler)
    server.quiet = quiet  # type: ignore[attr-defined]
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serve_mock",
        description="启动 AutoLearn 双靶场（题目 / 网课 + 跨域 frame）",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AUTOLEARN_MOCK_PORT", DEFAULT_PORT)),
        help=f"主端口（默认 {DEFAULT_PORT}，可用 AUTOLEARN_MOCK_PORT 覆盖）",
    )
    parser.add_argument(
        "--frame-port",
        type=int,
        default=int(os.environ.get("AUTOLEARN_MOCK_FRAME_PORT", DEFAULT_FRAME_PORT)),
        help=f"跨域 frame 端口（默认 {DEFAULT_FRAME_PORT}，必须与主端口不同源）",
    )
    parser.add_argument("--root", default=str(SITE_ROOT), help="靶场根目录")
    parser.add_argument("--quiet", action="store_true", help="不打印每条请求")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()

    if not (root / "quiz.html").is_file():
        print(f"[mock] 找不到 {root / 'quiz.html'}，请检查 --root", file=sys.stderr)
        return 2

    if args.frame_port == args.port:
        print("[mock] frame 端口不能与主端口相同 —— 那样就不是跨域了", file=sys.stderr)
        return 2

    frame_origin = f"http://{args.host}:{args.frame_port}"

    main_server = build_server(root, args.host, args.port, "main", frame_origin, args.quiet)
    frame_server = build_server(root, args.host, args.frame_port, "frame", frame_origin, args.quiet)

    frame_thread = threading.Thread(
        target=frame_server.serve_forever, name="mock-frame", daemon=True
    )
    frame_thread.start()

    base = f"http://{args.host}:{args.port}"
    print("AutoLearn 靶场已启动")
    print(f"  题目靶场    {base}/quiz.html")
    print(f"  网课靶场    {base}/course.html")
    print(f"  跨域 frame  {frame_origin}/frame.html")
    print(f"  题目接口    {base}/mock-api/question/21")
    print(f"  媒体合成    {base}/media/ep01.wav?d=36")
    print("  按 Ctrl+C 停止")

    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] 停止中…")
    finally:
        main_server.shutdown()
        frame_server.shutdown()
        main_server.server_close()
        frame_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
