#!/usr/bin/env python3
"""
LimitlyAI Sidecar Server for Antigravity 2.0
Serves the native auxiliary pane web UI and exposes /api/status for live AI quotas.
Zero external dependencies (uses standard Python library only).
"""

import os
import sys
import json
import ssl
import re
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Directory setup
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_DIR = Path.home() / ".gemini" / "config"
SETTINGS_FILE = CONFIG_DIR / "limitlyai_settings.json"

DEFAULT_SETTINGS = {
    "view_mode": "compact",
    "filter": "all",
    "auto_refresh": True,
    "refresh_interval": 30,
    "history": []
}

def load_settings():
    """Load persistent settings from disk."""
    if SETTINGS_FILE.is_file():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(data)
                return merged
        except Exception as e:
            print(f"[LimitlyAI] Error loading settings: {e}", file=sys.stderr)
    return dict(DEFAULT_SETTINGS)

def save_settings(new_settings):
    """Save persistent settings to disk atomically."""
    current = load_settings()
    for k, v in new_settings.items():
        if k in DEFAULT_SETTINGS or k == "history":
            current[k] = v
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = SETTINGS_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        temp_file.replace(SETTINGS_FILE)
    except Exception as e:
        print(f"[LimitlyAI] Error saving settings: {e}", file=sys.stderr)
    return current

def record_quota_history(ag_data):
    """Keep rolling 12h history samples in settings."""
    try:
        if not ag_data or not ag_data.get("online"):
            return
        groups = ag_data.get("groups", [])
        if not groups:
            return
        
        used = 0.0
        g = groups[0]
        buckets = g.get("buckets", [])
        for b in buckets:
            if b.get("window") == "5h":
                used = b.get("usedPercent", 0.0)
                break
        else:
            if buckets:
                used = buckets[0].get("usedPercent", 0.0)

        now = time.time()
        settings = load_settings()
        history = settings.get("history", [])
        # Record at most once every 60 seconds
        if not history or (now - (history[-1].get("t", 0) / 1000.0) >= 60):
            history.append({"t": int(now * 1000), "used": round(used, 1)})
            # Keep last 12 hours
            cutoff = int((now - 12 * 3600) * 1000)
            history = [h for h in history if h.get("t", 0) >= cutoff]
            settings["history"] = history
            save_settings({"history": history})
    except Exception:
        pass

# Cache for language server connection
_ls_cache = {
    "port": None,
    "csrf": None,
    "last_checked": 0
}

def discover_language_server(force=False):
    """Locate the Antigravity language server PID, listening port, and CSRF token."""
    global _ls_cache
    
    # Check cache first if tested within 15 seconds (unless force=True)
    if not force and _ls_cache["port"] and _ls_cache["csrf"] and (time.time() - _ls_cache["last_checked"] < 15):
        return _ls_cache["port"], _ls_cache["csrf"]

    try:
        # Get process CommandLine and PID without PowerShell table truncation
        ps_cmd = 'Get-CimInstance Win32_Process | Where-Object { $_.Name -match "language_server" } | ForEach-Object { "$($_.ProcessId) === $($_.CommandLine)" }'
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=5)
        
        csrf = None
        pids = []
        for line in out.splitlines():
            if "===" in line:
                pid_str, cmdline = line.split("===", 1)
                try:
                    pids.append(int(pid_str.strip()))
                except ValueError:
                    pass
                m_csrf = re.search(r'--csrf_token[=\s]+([a-zA-Z0-9\-]+)', cmdline)
                if m_csrf:
                    csrf = m_csrf.group(1)

        if not pids or not csrf:
            _ls_cache["port"] = None
            _ls_cache["csrf"] = None
            return None, None

        # Find listening TCP ports for these PIDs
        netstat = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, timeout=5)
        candidate_ports = []
        for line in netstat.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                try:
                    owner_pid = int(parts[4])
                    if owner_pid in pids:
                        port = int(parts[1].split(":")[-1])
                        candidate_ports.append(port)
                except (ValueError, IndexError):
                    continue

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Test candidate ports for HTTPS RetrieveUserQuotaSummary
        for port in sorted(set(candidate_ports)):
            url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            req = urllib.request.Request(
                url,
                data=b'{"metadata":{"ideName":"antigravity"}}',
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": csrf
                }
            )
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=1.2) as resp:
                    if resp.status == 200:
                        _ls_cache["port"] = port
                        _ls_cache["csrf"] = csrf
                        _ls_cache["last_checked"] = time.time()
                        return port, csrf
            except Exception:
                continue

    except Exception as e:
        print(f"[LimitlyAI] Error discovering language server: {e}", file=sys.stderr)

    _ls_cache["port"] = None
    _ls_cache["csrf"] = None
    return None, None

def fetch_antigravity_quota():
    """Fetch structured quota summary from the local Antigravity Language Server."""
    port, csrf = _ls_cache.get("port"), _ls_cache.get("csrf")
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Attempt cached port first
    if port and csrf:
        try:
            url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            req = urllib.request.Request(
                url,
                data=b'{"metadata":{"ideName":"antigravity"}}',
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": csrf
                }
            )
            with urllib.request.urlopen(req, context=ctx, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                _ls_cache["last_checked"] = time.time()
                return parse_antigravity_response(data)
        except Exception:
            # Stale cache: connection failed, invalidate immediately
            _ls_cache["port"] = None
            _ls_cache["csrf"] = None
            _ls_cache["last_checked"] = 0

    # Discover and retry with force=True
    port, csrf = discover_language_server(force=True)
    if not port or not csrf:
        return {"online": False, "error": "Antigravity language server is not running or unreachable"}

    try:
        url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
        req = urllib.request.Request(
            url,
            data=b'{"metadata":{"ideName":"antigravity"}}',
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf
            }
        )
        with urllib.request.urlopen(req, context=ctx, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _ls_cache["last_checked"] = time.time()
            return parse_antigravity_response(data)
    except Exception as e:
        _ls_cache["port"] = None
        _ls_cache["csrf"] = None
        return {"online": False, "error": str(e)}

def parse_antigravity_response(data):
    """Normalize Antigravity quota response into clean structured objects."""
    resp_obj = data.get("response", {})
    groups_raw = resp_obj.get("groups", [])
    
    clean_groups = []
    for g in groups_raw:
        display_name = g.get("displayName", "AI Models")
        description = g.get("description", "")
        buckets_raw = g.get("buckets", [])
        
        clean_buckets = []
        for b in buckets_raw:
            rem_frac = float(b.get("remainingFraction", 0.0))
            rem_pct = round(rem_frac * 100.0, 1)
            used_pct = round(max(0.0, min(100.0, 100.0 - rem_pct)), 1)
            
            clean_buckets.append({
                "bucketId": b.get("bucketId", ""),
                "displayName": b.get("displayName", ""),
                "description": b.get("description", ""),
                "window": b.get("window", ""),
                "remainingPercent": rem_pct,
                "usedPercent": used_pct,
                "remainingFraction": rem_frac,
                "resetTime": b.get("resetTime", "")
            })
            
        def window_rank(b):
            w = str(b.get("window", "")).lower()
            if "5h" in w:
                return 1
            if "day" in w or "24h" in w:
                return 2
            if "week" in w or "7d" in w:
                return 3
            return 4

        clean_buckets.sort(key=window_rank)

        clean_groups.append({
            "displayName": display_name,
            "description": description,
            "buckets": clean_buckets
        })
        
    return {
        "online": True,
        "plan": "Google AI (Antigravity)",
        "groups": clean_groups
    }

def get_claude_status():
    """Detect Claude Desktop status (installed / token present)."""
    appdata = os.environ.get("APPDATA", "")
    claude_dir = Path(appdata) / "Claude"
    if not claude_dir.exists():
        return {"online": False, "installed": False, "reason": "Claude Desktop не установлен"}
    return {"online": True, "installed": True, "plan": "Claude Pro / Free"}

def get_codex_status():
    """Detect Codex CLI / ChatGPT status."""
    home = Path(os.environ.get("USERPROFILE", ""))
    codex_auth = home / ".codex" / "auth.json"
    if not codex_auth.exists():
        return {"online": False, "installed": False, "reason": "Codex не авторизован"}
    return {"online": True, "installed": True, "plan": "Codex / ChatGPT Plus"}

class SidecarHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        if clean_path == "/api/settings":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                saved = save_settings(data)
                resp = json.dumps({"status": "ok", "settings": saved}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err_resp = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_resp)))
                self.end_headers()
                self.wfile.write(err_resp)
            return

        self.send_error(404, "Not Found")

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        
        # API Routes
        if clean_path == "/api/status":
            antigravity_data = fetch_antigravity_quota()
            claude_data = get_claude_status()
            codex_data = get_codex_status()
            record_quota_history(antigravity_data)
            settings = load_settings()
            
            payload = {
                "status": "online" if antigravity_data.get("online") else "offline",
                "antigravity": antigravity_data,
                "claude": claude_data,
                "codex": codex_data,
                "settings": settings,
                "server_time": time.time(),
                "server_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if clean_path == "/api/settings":
            settings = load_settings()
            body = json.dumps(settings, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if clean_path == "/api/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Static file routing
        if clean_path == "/" or clean_path == "/index.html":
            file_path = STATIC_DIR / "index.html"
            content_type = "text/html; charset=utf-8"
        elif clean_path == "/style.css":
            file_path = STATIC_DIR / "style.css"
            content_type = "text/css; charset=utf-8"
        elif clean_path == "/app.js":
            file_path = STATIC_DIR / "app.js"
            content_type = "application/javascript; charset=utf-8"
        elif clean_path == "/logo.svg":
            file_path = STATIC_DIR / "logo.svg"
            content_type = "image/svg+xml"
        else:
            rel = clean_path.lstrip("/")
            file_path = STATIC_DIR / rel
            ext = file_path.suffix.lower()
            mime_map = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".ico": "image/x-icon",
                ".json": "application/json"
            }
            content_type = mime_map.get(ext, "application/octet-stream")

        if file_path.is_file():
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, f"Error reading file: {e}")
        else:
            self.send_error(404, "File not found")

    def log_message(self, format, *args):
        # Quiet logger for smooth background execution
        pass

def start_parent_watcher():
    """Watch the parent process and exit when parent terminates to prevent orphaned processes."""
    try:
        import threading
        import ctypes

        target_pid = os.getppid()
        current = target_pid
        for _ in range(4):
            try:
                ps_cmd = f'$p = Get-CimInstance Win32_Process | Where-Object ProcessId -eq {current}; "$($p.Name)|$($p.ParentProcessId)"'
                out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=2).strip()
                if "|" in out:
                    name, next_ppid = out.split("|", 1)
                    if "language_server" in name.lower():
                        target_pid = current
                        break
                    if next_ppid.strip().isdigit():
                        current = int(next_ppid.strip())
                        if current <= 4:
                            break
                    else:
                        break
            except Exception:
                break

        if target_pid <= 4:
            return

        def _watch():
            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, target_pid)
            if handle:
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                kernel32.CloseHandle(handle)
                # Parent exited, cleanly exit
                os._exit(0)

        t = threading.Thread(target=_watch, daemon=True)
        t.start()
    except Exception:
        pass

def main():
    start_parent_watcher()
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", 8765))
    server = HTTPServer(("127.0.0.1", port), SidecarHandler)
    print(f"[LimitlyAI] Sidecar UI Server listening at http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
