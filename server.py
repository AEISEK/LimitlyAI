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
import glob
import sqlite3
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Directory setup
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_DIR = Path.home() / ".gemini" / "config"
SETTINGS_FILE = CONFIG_DIR / "limitlyai_settings.json"

# In-memory mapping of webview session tokens to conversation IDs
_SESSION_TOKENS = {}

# Performance caches
_SETTINGS_CACHE = {"data": None, "mtime": 0.0}
_WORKSPACE_CACHE = {} # conv_id -> workspace_path
_PROJECTS_CACHE = {"raw_list": [], "timestamp": 0.0}
_TRANSCRIPT_CACHE = {} # conv_id -> {"mtime": 0.0, "fsize": 0, "last_step": dict}
_PROJ_CONFIG_CACHE = {} # workspace_path -> (limit, source, mtime, check_time)

# Pre-initialized SSL Context (zero allocation per request)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_SETTINGS = {
    "view_mode": "compact",
    "filter": "all",
    "auto_refresh": True,
    "refresh_interval": 30,
    "default_memory_limit": 1048576,
    "project_limits": {},
    "history": []
}

def load_settings():
    """Load persistent settings from disk with mtime validation cache."""
    global _SETTINGS_CACHE
    try:
        if SETTINGS_FILE.is_file():
            cur_mtime = SETTINGS_FILE.stat().st_mtime
            if _SETTINGS_CACHE["data"] is not None and _SETTINGS_CACHE["mtime"] == cur_mtime:
                return dict(_SETTINGS_CACHE["data"])
            with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(data)
                _SETTINGS_CACHE = {"data": merged, "mtime": cur_mtime}
                return dict(merged)
    except Exception as e:
        print(f"[LimitlyAI] Error loading settings: {e}", file=sys.stderr)
    return dict(DEFAULT_SETTINGS)

def save_settings(new_settings):
    """Save persistent settings to disk atomically and update cache."""
    global _SETTINGS_CACHE
    current = load_settings()
    for k, v in new_settings.items():
        current[k] = v
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = SETTINGS_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        temp_file.replace(SETTINGS_FILE)
        try:
            cur_mtime = SETTINGS_FILE.stat().st_mtime
        except Exception:
            cur_mtime = time.time()
        _SETTINGS_CACHE = {"data": current, "mtime": cur_mtime}
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

def parse_token_limit(val, default=1048576):
    """Parse integer or shorthand string ('200k', '1M', 180000, 1048576) into integer tokens."""
    if not val:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    val_str = str(val).strip().lower()
    try:
        if val_str == "1m":
            return 1048576
        if val_str.endswith("m"):
            return int(float(val_str[:-1]) * 1048576)
        if val_str.endswith("k"):
            num = float(val_str[:-1])
            return 1048576 if num == 1024 else int(num * 1000)
        return int(float(val_str))
    except (ValueError, TypeError):
        return default

def get_workspace_for_conversation(conv_id):
    """Extract project root folder from Antigravity conversation db trajectory metadata (cached)."""
    if not conv_id:
        return None
    if conv_id in _WORKSPACE_CACHE:
        return _WORKSPACE_CACHE[conv_id]

    db_path = os.path.expanduser(f"~/.gemini/antigravity/conversations/{conv_id}.db")
    if not os.path.exists(db_path):
        _WORKSPACE_CACHE[conv_id] = None
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        cur = conn.cursor()
        cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            text = row[0].decode("utf-8", errors="ignore")
            matches = re.findall(r'file:///([a-zA-Z]:/[^\x00-\x1f\x7f"\'\s]+)', text)
            for m in matches:
                path = os.path.normpath(urllib.parse.unquote(m))
                if os.path.isdir(path):
                    _WORKSPACE_CACHE[conv_id] = path
                    return path
                # Also check under Documents/antigravity/<basename>
                bname = os.path.basename(path)
                alt = os.path.normpath(os.path.join(os.path.expanduser("~/Documents/antigravity"), bname))
                if os.path.isdir(alt):
                    _WORKSPACE_CACHE[conv_id] = alt
                    return alt
                # Still return the path so basename can be used
                _WORKSPACE_CACHE[conv_id] = path
                return path
    except Exception:
        pass
    _WORKSPACE_CACHE[conv_id] = None
    return None

def get_project_memory_config(workspace_path):
    """Read memory_limit from .limitlyai.json or limitlyai.json in the project root (cached)."""
    if not workspace_path or not os.path.isdir(workspace_path):
        return None, None
    now = time.time()
    cached = _PROJ_CONFIG_CACHE.get(workspace_path)
    if cached and (now - cached.get("check_time", 0) < 5.0):
        return cached["limit"], cached["source"]

    candidates = [
        os.path.join(workspace_path, ".limitlyai.json"),
        os.path.join(workspace_path, "limitlyai.json"),
        os.path.join(workspace_path, ".limitlyai")
    ]
    for c in candidates:
        if os.path.isfile(c):
            try:
                cmtime = os.path.getmtime(c)
                if cached and cached.get("mtime") == cmtime and cached.get("path") == c:
                    _PROJ_CONFIG_CACHE[workspace_path]["check_time"] = now
                    return cached["limit"], cached["source"]
                with open(c, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                    lim = data.get("memory_limit") or data.get("memory_limit_tokens") or data.get("limit")
                    if lim:
                        parsed = parse_token_limit(lim)
                        bname = os.path.basename(c)
                        _PROJ_CONFIG_CACHE[workspace_path] = {
                            "limit": parsed,
                            "source": bname,
                            "path": c,
                            "mtime": cmtime,
                            "check_time": now
                        }
                        return parsed, bname
            except Exception:
                pass
    _PROJ_CONFIG_CACHE[workspace_path] = {"limit": None, "source": None, "path": None, "mtime": 0, "check_time": now}
    return None, None

def resolve_conversation_id(token=None, requested_cid=None):
    """Determine the active conversation ID based on webview token, requested ID, or IDE focus."""
    # 1. Explicit conversation ID requested (from dropdown or client)
    if requested_cid:
        tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{requested_cid}/.system_generated/logs/transcript.jsonl")
        if os.path.exists(tpath):
            return requested_cid

    # 2. In-memory session tokens mapping (registered on GET /?token=...&conversationId=...)
    if token and token in _SESSION_TOKENS:
        cid = _SESSION_TOKENS[token]
        tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
        if os.path.exists(tpath):
            return cid

    # 3. Check aux-pane-v2-session in app_storage.json for token
    if token:
        try:
            path = os.path.expanduser(r"~\AppData\Roaming\Antigravity\app_storage.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sess = json.loads(data.get("aux-pane-v2-session", "{}"))
                for cid, pane in sess.get("conversationPanes", {}).items():
                    for t in pane.get("tabs", []):
                        url = t.get("content", {}).get("sidecarView", {}).get("url", "")
                        if token in url:
                            _SESSION_TOKENS[token] = cid
                            return cid
        except Exception:
            pass

    # 4. Check projectsConversationOrder from app_storage.json (focused conversation in IDE)
    try:
        path = os.path.expanduser(r"~\AppData\Roaming\Antigravity\app_storage.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            order = json.loads(data.get("projectsConversationOrder", "[]"))
            for cid in order:
                tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
                if os.path.exists(tpath):
                    return cid
    except Exception:
        pass

    # 5. Check the most recently modified conversation DB
    try:
        db_pattern = os.path.expanduser(r"~/.gemini/antigravity/conversations/*.db")
        db_files = glob.glob(db_pattern)
        if db_files:
            db_files.sort(key=os.path.getmtime, reverse=True)
            for db_f in db_files[:5]:
                cid = os.path.splitext(os.path.basename(db_f))[0]
                tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
                if os.path.exists(tpath):
                    return cid
    except Exception:
        pass

    # 6. Fallback: newest modified transcript
    pattern = os.path.expanduser(r"~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl")
    files = glob.glob(pattern)
    if files:
        files.sort(key=os.path.getmtime, reverse=True)
        return os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(files[0]))))

    return None

def get_recent_projects_list(current_cid=None):
    """List recent open projects with conversation IDs and names for dropdown selector (cached)."""
    global _PROJECTS_CACHE
    now = time.time()
    if (now - _PROJECTS_CACHE.get("timestamp", 0) < 5.0) and _PROJECTS_CACHE.get("raw_list"):
        return [
            {
                "conversation_id": item["conversation_id"],
                "project_name": item["project_name"],
                "workspace_path": item["workspace_path"],
                "is_active": (item["conversation_id"] == current_cid)
            }
            for item in _PROJECTS_CACHE["raw_list"]
        ]

    projects = []
    seen_convs = set()
    order = []
    try:
        path = os.path.expanduser(r"~\AppData\Roaming\Antigravity\app_storage.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            order = json.loads(data.get("projectsConversationOrder", "[]"))
    except Exception:
        pass

    try:
        db_pattern = os.path.expanduser(r"~/.gemini/antigravity/conversations/*.db")
        db_files = glob.glob(db_pattern)
        if db_files:
            db_files.sort(key=os.path.getmtime, reverse=True)
            for db_f in db_files[:10]:
                cid = os.path.splitext(os.path.basename(db_f))[0]
                if cid not in order:
                    order.append(cid)

        if current_cid and current_cid not in order:
            order.insert(0, current_cid)

        raw_list = []
        for cid in order[:15]:
            if cid in seen_convs:
                continue
            tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{cid}/.system_generated/logs/transcript.jsonl")
            if not os.path.exists(tpath):
                continue
            seen_convs.add(cid)
            ws = get_workspace_for_conversation(cid)
            pname = os.path.basename(ws) if ws else ""
            if not pname:
                pname = f"Диалог {cid[:6]}"
            mtime = os.path.getmtime(tpath)
            raw_list.append({
                "conversation_id": cid,
                "project_name": pname,
                "workspace_path": ws or "",
                "mtime": mtime
            })

        # Disambiguate if multiple conversations share the same project name
        name_counts = {}
        for item in raw_list:
            name_counts[item["project_name"]] = name_counts.get(item["project_name"], 0) + 1

        cached_templates = []
        for item in raw_list:
            display_name = item["project_name"]
            if name_counts.get(display_name, 0) > 1:
                t_str = time.strftime("%H:%M", time.localtime(item["mtime"]))
                display_name = f"{display_name} ({t_str})"
            cached_templates.append({
                "conversation_id": item["conversation_id"],
                "project_name": display_name,
                "workspace_path": item["workspace_path"]
            })
            projects.append({
                "conversation_id": item["conversation_id"],
                "project_name": display_name,
                "workspace_path": item["workspace_path"],
                "is_active": (item["conversation_id"] == current_cid)
            })
        _PROJECTS_CACHE["raw_list"] = cached_templates
        _PROJECTS_CACHE["timestamp"] = now
    except Exception as e:
        print(f"[LimitlyAI] Error listing projects: {e}", file=sys.stderr)
    return projects

def get_agent_memory_info(token=None, requested_cid=None):
    """Extract context token metrics from the active conversation transcript with project-level limit."""
    try:
        conv_id = resolve_conversation_id(token=token, requested_cid=requested_cid)
        if not conv_id:
            return {"available": False, "reason": "Активный диалог не найден"}

        tpath = os.path.expanduser(f"~/.gemini/antigravity/brain/{conv_id}/.system_generated/logs/transcript.jsonl")
        if not os.path.exists(tpath):
            return {"available": False, "reason": f"Транскрипт диалога {conv_id} не найден"}

        stat_info = os.stat(tpath)
        fsize = stat_info.st_size
        fmtime = stat_info.st_mtime

        cached_trans = _TRANSCRIPT_CACHE.get(conv_id)
        if cached_trans and cached_trans.get("fsize") == fsize and cached_trans.get("mtime") == fmtime:
            last_step = cached_trans["last_step"]
            compact_count = cached_trans.get("compact_count", 0)
            last_compact_step = cached_trans.get("last_compact_step", 0)
        else:
            compact_count = 0
            last_compact_step = 0
            read_bytes = min(fsize, 65536)
            with open(tpath, "rb") as f:
                if fsize > read_bytes:
                    f.seek(fsize - read_bytes)
                chunk = f.read().decode("utf-8", errors="ignore")

            lines = chunk.strip().split("\n")
            last_step = None
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "input_tokens" in data and "cache_read_tokens" in data:
                        last_step = data
                        break
                except Exception:
                    continue

            # Fast scan for automatic context compactions
            try:
                with open(tpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if 'CHECKPOINT' in line and 'compaction' in line:
                            compact_count += 1
                            try:
                                cdata = json.loads(line)
                                last_compact_step = cdata.get("step_index", 0)
                            except Exception:
                                pass
            except Exception:
                pass

            _TRANSCRIPT_CACHE[conv_id] = {
                "fsize": fsize,
                "mtime": fmtime,
                "last_step": last_step,
                "compact_count": compact_count,
                "last_compact_step": last_compact_step
            }

        if not last_step:
            return {"available": False, "reason": "Нет данных о токенах в активном диалоге"}

        cache_read = int(last_step.get("cache_read_tokens", 0))
        input_tok = int(last_step.get("input_tokens", 0))
        output_tok = int(last_step.get("output_tokens", 0))
        step_idx = int(last_step.get("step_index", 0))
        created_at = last_step.get("created_at", "")

        # Project detection & custom project limit
        workspace_path = get_workspace_for_conversation(conv_id)
        project_name = os.path.basename(workspace_path) if workspace_path else ""
        
        # 1. Project config file (.limitlyai.json)
        proj_limit, cfg_source = get_project_memory_config(workspace_path)
        
        # 2. Settings project_limits
        settings = load_settings()
        if not proj_limit and workspace_path:
            p_limits = settings.get("project_limits", {})
            stored = p_limits.get(workspace_path) or p_limits.get(project_name)
            if stored:
                proj_limit = parse_token_limit(stored)
                cfg_source = "настройки LimitlyAI"

        # 3. Default fallback
        if not proj_limit:
            proj_limit = parse_token_limit(settings.get("default_memory_limit", 1048576))
            cfg_source = "по умолчанию (1M)"

        context_limit = proj_limit
        total_context = cache_read + input_tok
        used_pct = round(min(100.0, (total_context / context_limit) * 100.0), 1)

        if used_pct >= 85.0:
            level = "critical"
            message = "Контекст критически заполнен! Рекомендуется вызвать /compact"
        elif used_pct >= 70.0:
            level = "warning"
            message = "Память диалога заполнена более чем на 70%. Рекомендуется /compact"
        else:
            level = "normal"
            message = "Контекст в норме"

        recent_projects = get_recent_projects_list(current_cid=conv_id)

        return {
            "available": True,
            "conversation_id": conv_id,
            "workspace_path": workspace_path or "",
            "project_name": project_name or "",
            "limit_source": cfg_source,
            "recent_projects": recent_projects,
            "step_index": step_idx,
            "cache_read_tokens": cache_read,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "total_context_tokens": total_context,
            "context_limit_tokens": context_limit,
            "used_percent": used_pct,
            "remaining_tokens": max(0, context_limit - total_context),
            "remaining_percent": round(max(0.0, 100.0 - used_pct), 1),
            "is_compacted": (compact_count > 0),
            "compact_count": compact_count,
            "last_compact_step": last_compact_step,
            "level": level,
            "message": message,
            "updated_at": created_at
        }
    except Exception as e:
        return {"available": False, "error": str(e)}

# Cache for language server connection
_ls_cache = {
    "port": None,
    "csrf": None,
    "last_checked": 0
}

def discover_language_server(force=False):
    """Locate the Antigravity language server PID, listening port, and CSRF token."""
    global _ls_cache
    
    # Check cache first (retained indefinitely until connection failure)
    if not force and _ls_cache["port"] and _ls_cache["csrf"]:
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
                with urllib.request.urlopen(req, context=_SSL_CTX, timeout=1.2) as resp:
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
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=2.5) as resp:
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
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=3.0) as resp:
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

        if clean_path == "/api/project_limit":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                ws_path = data.get("workspace_path")
                new_lim = parse_token_limit(data.get("limit", 1048576))
                save_file = data.get("save_to_file", True)
                conv_id = data.get("conversation_id")
                token = data.get("token")

                file_saved = False
                if ws_path and os.path.isdir(ws_path) and save_file:
                    target_cfg = os.path.join(ws_path, ".limitlyai.json")
                    try:
                        cfg_content = {}
                        if os.path.isfile(target_cfg):
                            with open(target_cfg, "r", encoding="utf-8") as f:
                                cfg_content = json.load(f)
                        cfg_content["memory_limit"] = new_lim
                        with open(target_cfg, "w", encoding="utf-8") as f:
                            json.dump(cfg_content, f, indent=2, ensure_ascii=False)
                        file_saved = True
                    except Exception as e:
                        print(f"[LimitlyAI] Error writing .limitlyai.json: {e}", file=sys.stderr)

                # Persist to settings
                settings = load_settings()
                p_limits = settings.get("project_limits", {})
                if ws_path:
                    p_limits[ws_path] = new_lim
                settings["project_limits"] = p_limits
                save_settings({"project_limits": p_limits})

                updated_mem = get_agent_memory_info(token=token, requested_cid=conv_id)
                resp = json.dumps({
                    "status": "ok",
                    "limit": new_lim,
                    "file_saved": file_saved,
                    "agent_memory": updated_mem
                }, ensure_ascii=False).encode("utf-8")
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
            parsed_url = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            token = query_params.get("token", [""])[0]
            requested_cid = query_params.get("conv_id", [""])[0] or query_params.get("conversationId", [""])[0]
            if token and requested_cid:
                _SESSION_TOKENS[token] = requested_cid

            antigravity_data = fetch_antigravity_quota()
            claude_data = get_claude_status()
            codex_data = get_codex_status()
            memory_data = get_agent_memory_info(token=token, requested_cid=requested_cid)
            if isinstance(antigravity_data, dict):
                antigravity_data["agent_memory"] = memory_data
            record_quota_history(antigravity_data)
            settings = load_settings()
            
            payload = {
                "status": "online" if antigravity_data.get("online") else "offline",
                "antigravity": antigravity_data,
                "agent_memory": memory_data,
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
            if file_path.is_file():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        html_text = f.read()
                    parsed = urllib.parse.urlparse(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    tok = params.get("token", [""])[0]
                    cid = params.get("conversationId", [""])[0] or params.get("conv_id", [""])[0]
                    if tok and cid:
                        _SESSION_TOKENS[tok] = cid
                    init_data = json.dumps({"token": tok, "conversationId": cid})
                    injection = f'<script>window.__LIMITLYAI_INIT__ = {init_data};</script></head>'
                    html_text = html_text.replace("</head>", injection)
                    content = html_text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception as e:
                    self.send_error(500, f"Error reading file: {e}")
                    return
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

def cleanup_orphaned_servers():
    """Kill any previous orphaned server.py processes on startup to prevent duplicates."""
    try:
        my_pid = os.getpid()
        my_ppid = os.getppid()
        ps_cmd = 'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "server.py" } | ForEach-Object { "$($_.ProcessId)|$($_.ParentProcessId)" }'
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=3)
        for line in out.splitlines():
            line = line.strip()
            if "|" in line:
                pid_str, ppid_str = line.split("|", 1)
                try:
                    p = int(pid_str.strip())
                    pp = int(ppid_str.strip())
                    if p != my_pid and p != my_ppid:
                        os.kill(p, 9)
                except Exception:
                    pass
    except Exception:
        pass

def main():
    cleanup_orphaned_servers()
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
