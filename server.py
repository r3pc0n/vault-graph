#!/usr/bin/env python3
"""Serves a live force-directed graph of an Obsidian vault: notes as nodes,
[[wikilinks]] as edges. Re-scans the vault only when a file's mtime changes,
so polling stays cheap even on a vault with hundreds of notes.
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "vault-graph.json")) as f:
    CONFIG = json.load(f)

VAULT_DIR = CONFIG["vault_dir"]
PORT = CONFIG["port"]
VAULT_DIR_NORM = os.path.normpath(VAULT_DIR)

# Optional: only needed for the "open in Obsidian" links, which use
# obsidian://open?vault=<name>&file=<path> - Obsidian identifies a vault by
# its registered NAME, not its filesystem path, and the two usually but not
# always match. Falls back to the folder's own basename, the common case.
OBSIDIAN_VAULT_NAME = CONFIG.get("obsidian_vault_name") or os.path.basename(VAULT_DIR_NORM)

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
SKIP_DIRS = {".obsidian", ".git"}

# ---- static assets (currently just the wind-chime sample) served from our
# own assets/ folder, kept separate from the vault-note serving above.
ASSETS_DIR = os.path.join(HERE, "assets")
ASSET_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}


def _resolve_asset_path(name):
    """Same shape of check as _resolve_vault_relpath: reject anything that
    isn't a plain filename normalizing to inside ASSETS_DIR with a known
    audio extension. Returns the absolute path, or None."""
    if not name or "\x00" in name or os.path.isabs(name):
        return None
    candidate = os.path.normpath(os.path.join(ASSETS_DIR, name))
    if candidate != ASSETS_DIR and not candidate.startswith(ASSETS_DIR + os.sep):
        return None
    if os.path.splitext(candidate)[1].lower() not in ASSET_CONTENT_TYPES:
        return None
    return candidate

_cache = {"sig": None, "data": None}

# ---- activity feed: your coding agent's hooks (e.g. a Claude Code
# PostToolUse hook, see integrations/claude-code/) report reads/writes here
# so the graph can pulse the matching note. In-memory only - a ring buffer, not a
# persistent log; losing it on restart is fine for a live "what's happening
# now" feed. Cursor-based (event id, not timestamp) so pollers can ask for
# "since id N" without missing or double-counting events.
ACTIVITY_MAX = 500
_activity_lock = threading.Lock()
_activity = {"events": [], "next_id": 1}


def _resolve_vault_relpath(raw_path):
    """Validate that raw_path is a .md file that normalizes to inside the
    vault, and return its path relative to VAULT_DIR. Returns None if raw_path
    is missing, absolute, contains a NUL byte, or - after normalization
    (which collapses any number of ".." segments, nested or not) - resolves
    outside the vault directory. Percent-encoded traversal sequences (e.g.
    "%2e%2e") are not decoded here and so are never treated as "..": they end
    up as literal characters in the path, which then simply fails to resolve
    to any real vault file.
    """
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None
    if os.path.isabs(raw_path):
        return None
    candidate = os.path.normpath(os.path.join(VAULT_DIR_NORM, raw_path))
    if candidate != VAULT_DIR_NORM and not candidate.startswith(VAULT_DIR_NORM + os.sep):
        return None
    if not candidate.endswith(".md"):
        return None
    return os.path.relpath(candidate, VAULT_DIR_NORM)


def _add_activity(action, relpath, timestamp):
    with _activity_lock:
        event = {
            "id": _activity["next_id"],
            "action": action,
            "path": relpath,
            "timestamp": timestamp,
        }
        _activity["next_id"] += 1
        _activity["events"].append(event)
        if len(_activity["events"]) > ACTIVITY_MAX:
            _activity["events"] = _activity["events"][-ACTIVITY_MAX:]
        return event


def _activity_since(since_id):
    with _activity_lock:
        return [e for e in _activity["events"] if e["id"] > since_id]


def _scan_files():
    """Yield (relpath, abspath, mtime) for every .md file in the vault."""
    for root, dirs, files in os.walk(VAULT_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".md"):
                abspath = os.path.join(root, name)
                relpath = os.path.relpath(abspath, VAULT_DIR)
                yield relpath, abspath, os.path.getmtime(abspath)


def _top_folder(relpath):
    parts = relpath.split(os.sep)
    return parts[0] if len(parts) > 1 else "(root)"


def _build_graph():
    files = list(_scan_files())

    # stem (filename without extension, lowercased) -> relpath, for resolving
    # [[Wikilinks]] the same way Obsidian does: by name, not by full path.
    stems = {}
    for relpath, _, _ in files:
        stem = os.path.splitext(os.path.basename(relpath))[0].lower()
        stems.setdefault(stem, relpath)

    # Categorical color slots are scarce (8, see the dataviz skill's palette) —
    # the first 8 top-level folders encountered get a real hue in folder order;
    # everything past that folds into the shared "Other" bucket rather than
    # inventing a 9th hue.
    folder_order = []
    for relpath, _, _ in sorted(files):
        folder = _top_folder(relpath)
        if folder not in folder_order:
            folder_order.append(folder)

    nodes = []
    degree = {}
    edges = []
    seen_edges = set()

    for relpath, abspath, mtime in files:
        with open(abspath, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        for match in WIKILINK_RE.findall(text):
            target_stem = match.strip().split("/")[-1].lower()
            target = stems.get(target_stem)
            if target and target != relpath:
                pair = tuple(sorted((relpath, target)))
                if pair not in seen_edges:
                    seen_edges.add(pair)
                    edges.append({"source": pair[0], "target": pair[1]})
                    degree[pair[0]] = degree.get(pair[0], 0) + 1
                    degree[pair[1]] = degree.get(pair[1], 0) + 1

    for relpath, _, mtime in files:
        folder = _top_folder(relpath)
        slot = folder_order.index(folder)
        nodes.append({
            "id": relpath,
            "label": os.path.splitext(os.path.basename(relpath))[0],
            "folder": folder,
            "slot": slot if slot < 8 else -1,  # -1 = folds into "Other"
            "mtime": int(mtime * 1000),
            "degree": degree.get(relpath, 0),
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "folders": [f for i, f in enumerate(folder_order) if i < 8],
        "vault_name": OBSIDIAN_VAULT_NAME,
        "generated_at": int(__import__("time").time() * 1000),
    }


def _graph_json():
    sig = tuple(sorted((r, m) for r, _, m in _scan_files()))
    if sig != _cache["sig"]:
        _cache["sig"] = sig
        _cache["data"] = _build_graph()
    return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; this runs unattended most of the time

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self._serve_file("index.html", "text/html")
        elif parsed.path == "/api/graph":
            self._send_json(200, _graph_json())
        elif parsed.path == "/api/activity":
            since_raw = parse_qs(parsed.query).get("since", ["0"])[0]
            try:
                since_id = int(since_raw)
            except ValueError:
                since_id = 0
            self._send_json(200, {"events": _activity_since(since_id)})
        elif parsed.path.startswith("/assets/"):
            self._serve_asset(parsed.path[len("/assets/"):])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/activity":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        action = payload.get("action") if isinstance(payload, dict) else None
        raw_path = payload.get("path") if isinstance(payload, dict) else None
        raw_timestamp = payload.get("timestamp") if isinstance(payload, dict) else None

        if action not in ("read", "write"):
            self._send_json(400, {"error": "action must be 'read' or 'write'"})
            return

        relpath = _resolve_vault_relpath(raw_path)
        if relpath is None:
            self._send_json(400, {"error": "path must be a .md file inside the vault"})
            return

        if isinstance(raw_timestamp, (int, float)):
            timestamp = int(raw_timestamp)
        else:
            timestamp = int(time.time() * 1000)

        event = _add_activity(action, relpath, timestamp)
        self._send_json(201, {"id": event["id"]})

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name, content_type):
        path = os.path.join(HERE, name)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, name):
        path = _resolve_asset_path(name)
        if path is None or not os.path.isfile(path):
            self.send_response(404)
            self.end_headers()
            return
        content_type = ASSET_CONTENT_TYPES[os.path.splitext(path)[1].lower()]
        self._serve_file(os.path.relpath(path, HERE), content_type)


def main():
    no_open = "--no-open" in sys.argv
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"vault-graph: serving {VAULT_DIR} at {url}")
    if not no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
