#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path("/opt/openlist-worker")
CONFIG_FILE = Path(os.environ.get("OPENLIST_CONFIG_FILE", "/var/lib/openlist-worker/config.json"))
WORKER = os.environ.get("OPENLIST_WORKER", "/usr/local/bin/openlist-transcode-worker")
WEB_ROOT = ROOT / "web"
DEFAULT_CONFIG = {
    "worker": {
        "master": "http://openlist:5244",
        "secret": "",
        "name": "nvenc-worker",
        "capacity": 1,
        "hwaccel": "nvenc",
        "ffmpeg": "/usr/local/bin/ffmpeg-nvenc-wrapper",
        "workdir": "/var/lib/openlist-worker",
    },
    "nvenc": {
        "enabled": True,
        "preset": "p5",
        "tune": "hq",
        "rc": "vbr",
        "cq": 0,
        "multipass": "qres",
        "lookahead": 0,
        "bf": 0,
        "b_ref_mode": "disabled",
        "spatial_aq": True,
        "temporal_aq": True,
        "aq_strength": 8,
    },
}


def merge_defaults(value: dict) -> dict:
    result = json.loads(json.dumps(DEFAULT_CONFIG))
    for group in ("worker", "nvenc"):
        if isinstance(value.get(group), dict):
            result[group].update(value[group])
    return result


def load_config() -> dict:
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            return merge_defaults(json.load(f))
    except (OSError, ValueError):
        return merge_defaults({})


def save_config(value: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(merge_defaults(value), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temp, CONFIG_FILE)


def apply_cli_overrides(config: dict, args: list[str]) -> bool:
    worker = config["worker"]
    names = {
        "--master": "master",
        "--secret": "secret",
        "--name": "name",
        "--capacity": "capacity",
        "--hwaccel": "hwaccel",
        "--ffmpeg": "ffmpeg",
        "--workdir": "workdir",
    }
    changed = False
    index = 0
    while index < len(args):
        token = args[index]
        key = value = None
        if "=" in token:
            raw_key, value = token.split("=", 1)
            key = names.get(raw_key)
        elif token in names and index + 1 < len(args):
            key = names[token]
            value = args[index + 1]
            index += 1
        if key is not None and value is not None:
            worker[key] = int(value) if key == "capacity" else value
            changed = True
        index += 1
    return changed


def env_initial_config(args: list[str]) -> dict:
    config = load_config()
    worker = config["worker"]
    env_map = {
        "master": "OPENLIST_MASTER",
        "secret": "OPENLIST_SECRET",
        "name": "OPENLIST_WORKER_NAME",
        "capacity": "OPENLIST_CAPACITY",
        "hwaccel": "OPENLIST_HWACCEL",
        "workdir": "OPENLIST_WORKDIR",
    }
    changed = False
    for key, env_name in env_map.items():
        raw = os.environ.get(env_name)
        if raw:
            worker[key] = int(raw) if key == "capacity" else raw
            changed = True
    changed = apply_cli_overrides(config, args) or changed
    if changed or not CONFIG_FILE.exists():
        save_config(config)
    return config


def worker_args(config: dict) -> list[str]:
    worker = config["worker"]
    return [
        WORKER,
        "--master=" + str(worker["master"]),
        "--secret=" + str(worker["secret"]),
        "--name=" + str(worker["name"]),
        "--capacity=" + str(worker["capacity"]),
        "--hwaccel=" + str(worker["hwaccel"]),
        "--ffmpeg=" + str(worker["ffmpeg"]),
        "--workdir=" + str(worker["workdir"]),
    ]


class Runtime:
    def __init__(self, args: list[str]):
        self.lock = threading.RLock()
        self.config = env_initial_config(args)
        self.process: subprocess.Popen | None = None
        self.start_worker()

    def start_worker(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            config = self.config
            Path(config["worker"]["workdir"]).mkdir(parents=True, exist_ok=True)
            self.process = subprocess.Popen(worker_args(config), stdout=sys.stdout, stderr=sys.stderr)

    def restart_worker(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
            self.start_worker()

    def status(self) -> dict:
        with self.lock:
            process = self.process
            return {
                "running": bool(process and process.poll() is None),
                "pid": process.pid if process else None,
                "exit_code": process.poll() if process else None,
                "worker_args": [a if "--secret=" not in a else "--secret=********" for a in worker_args(self.config)],
            }

    def shutdown(self, *_args) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        raise SystemExit(0)


runtime = Runtime(sys.argv[1:])


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenListNVENCUI/1.0"

    def log_message(self, fmt, *args):
        print("[config-ui] " + fmt % args, flush=True)

    def send_json(self, value: dict, status: int = HTTPStatus.OK):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        password = os.environ.get("OPENLIST_UI_PASSWORD", "")
        if not password:
            return True
        return self.headers.get("X-UI-Password", "") == password

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            raw = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if not self.authorized():
            self.send_json({"error": "UI password required"}, HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path == "/api/config":
            value = json.loads(json.dumps(runtime.config))
            value["worker"]["secret"] = "" if not runtime.config["worker"].get("secret") else "********"
            self.send_json(value)
            return
        if parsed.path == "/api/status":
            self.send_json(runtime.status())
            return
        if parsed.path == "/api/capabilities":
            self.send_json({"ffmpeg": os.environ.get("OPENLIST_REAL_FFMPEG", "/usr/bin/ffmpeg"), "wrapper": runtime.config["worker"]["ffmpeg"]})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self.authorized():
            self.send_json({"error": "UI password required"}, HTTPStatus.UNAUTHORIZED)
            return
        if self.path not in ("/api/config", "/api/restart"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.path == "/api/restart":
            runtime.restart_worker()
            self.send_json(runtime.status())
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length))
            if not isinstance(incoming, dict):
                raise ValueError("config must be an object")
            value = merge_defaults(incoming)
            worker = value["worker"]
            if not worker["master"].startswith(("http://", "https://")):
                raise ValueError("master must use http:// or https://")
            if not 1 <= int(worker["capacity"]) <= 64:
                raise ValueError("capacity must be between 1 and 64")
            if worker["hwaccel"] not in {"none", "auto", "nvenc", "qsv", "vaapi", "amf", "videotoolbox"}:
                raise ValueError("unsupported hwaccel")
            # A blank secret from the masked UI means keep the current one.
            if incoming.get("worker", {}).get("secret", "") == "********":
                value["worker"]["secret"] = runtime.config["worker"]["secret"]
            save_config(value)
            runtime.config = value
            runtime.restart_worker()
            self.send_json({"ok": True, "status": runtime.status()})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    host, _, raw_port = os.environ.get("OPENLIST_UI_ADDR", "0.0.0.0:8080").rpartition(":")
    host = host or "0.0.0.0"
    server = ThreadingHTTPServer((host, int(raw_port or "8080")), Handler)
    signal.signal(signal.SIGTERM, runtime.shutdown)
    signal.signal(signal.SIGINT, runtime.shutdown)
    print(f"[config-ui] listening on {host}:{raw_port or '8080'}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.shutdown()


if __name__ == "__main__":
    main()
