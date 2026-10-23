#!/usr/bin/env python3
"""Terminal client for vault-graph: the same force-directed graph as
index.html, flattened to 2D and drawn with Unicode Braille dots instead of a
canvas. Reads nothing from the vault itself - it polls the already-running
server.py over HTTP exactly like the browser page does, so all the vault
walking and [[wikilink]] parsing stays in one place.

Run server.py first, then this, in the same folder:
    python3 server.py --no-open &
    python3 tui.py
"""
import curses
import json
import locale
import math
import os
import random
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "vault-graph.json")) as f:
    CONFIG = json.load(f)

BASE_URL = f"http://127.0.0.1:{CONFIG['port']}"

GRAPH_POLL_SECS = 2.0
ACTIVITY_POLL_SECS = 1.0
RECENCY_GLOW_MS = 90_000
PULSE_DURATION_SECS = 3.0
INPUT_BUSY_SECS = 1.5  # stay at full rate this long after the last keypress

# Frame pacing, same idea as index.html's loadFrameSettings(): /api/settings
# gives max_fps/idle_fps from vault-graph.json, and with nothing actually
# moving (no pulse, no recent key) the loop backs off to idle_fps instead of
# ticking - and redrawing, and re-running physics - at full rate forever.
# These are the pre-/api/settings defaults (older server, or unreachable).
BUSY_INTERVAL_MS = 80
IDLE_INTERVAL_MS = 100

# Physics: a gravitational anchor (the highest-degree note) pinned at the
# world origin, everything else under inverse-square repulsion, Hookean
# springs along edges, a slow tangential swirl, and a weak pull back toward
# center so disconnected notes don't drift off forever - the same shape of
# simulation as index.html's 3D version, just flattened to 2D.
REPEL_K = 900.0
SPRING_K = 0.02
REST_LEN = 14.0
SWIRL_K = 0.0015
CENTER_GRAVITY = 0.0008
DAMPING = 0.80
MIN_DIST = 0.5

ZOOM_MIN, ZOOM_MAX, ZOOM_FACTOR = 0.5, 20.0, 1.15
PAN_STEP = 2.0
LEGEND_WIDTH = 26

# Default-theme folder palette, lifted straight from index.html's THEMES
# dict so the terminal and browser agree on which color means which folder.
FOLDER_HEX = {
    0: "#3987e5", 1: "#d95926", 2: "#199e70", 3: "#c98500",
    4: "#d55181", 5: "#008300", 6: "#9085e9", 7: "#e66767",
}
OTHER_HEX = "#6b6a65"
GLOW_HEX = "#ffffff"
PULSE_READ_HEX = "#2bbbad"
PULSE_WRITE_HEX = "#d4af37"
DIM_HEX = "#45443f"
EDGE_HEX = "#5a5952"


# ---------------------------------------------------------------- networking

def _get_json(path, timeout=1.5):
    with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -------------------------------------------------------------------- color

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_xterm256(r, g, b):
    def scale(c):
        return round(c / 255 * 5)
    return 16 + 36 * scale(r) + 6 * scale(g) + scale(b)


_BASIC_RGB = {
    curses.COLOR_BLACK: (0, 0, 0), curses.COLOR_RED: (205, 0, 0),
    curses.COLOR_GREEN: (0, 205, 0), curses.COLOR_YELLOW: (205, 205, 0),
    curses.COLOR_BLUE: (0, 0, 205), curses.COLOR_MAGENTA: (205, 0, 205),
    curses.COLOR_CYAN: (0, 205, 205), curses.COLOR_WHITE: (229, 229, 229),
}


def _closest_basic(r, g, b):
    return min(_BASIC_RGB, key=lambda c: sum(
        (a - b_) ** 2 for a, b_ in zip(_BASIC_RGB[c], (r, g, b))))


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    palette = {f"folder{slot}": hexval for slot, hexval in FOLDER_HEX.items()}
    palette.update(other=OTHER_HEX, glow=GLOW_HEX, pulse_read=PULSE_READ_HEX,
                   pulse_write=PULSE_WRITE_HEX, dim=DIM_HEX, edge=EDGE_HEX)
    pairs = {}
    wide = curses.COLORS >= 256
    for idx, (name, hexval) in enumerate(palette.items(), start=1):
        r, g, b = _hex_to_rgb(hexval)
        fg = _rgb_to_xterm256(r, g, b) if wide else _closest_basic(r, g, b)
        curses.init_pair(idx, fg, -1)
        pairs[name] = idx
    return pairs


def folder_color_name(slot):
    return f"folder{slot}" if slot is not None and 0 <= slot < 8 else "other"


# ------------------------------------------------------------------ braille

BRAILLE_BASE = 0x2800
_DOT_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
            (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}


class Canvas:
    """A virtual grid 2x4 "pixels" per terminal cell, addressed by pixel
    (x, y), rendered as Braille Unicode characters one cell at a time."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.w, self.h = cols * 2, rows * 4
        self.cells = {}  # (col, row) -> [bitmask, color_name, priority]

    def set(self, x, y, color_name, priority=0):
        x, y = int(round(x)), int(round(y))
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        col, row = x // 2, y // 4
        bit = _DOT_BIT[(x % 2, y % 4)]
        cell = self.cells.get((col, row))
        if cell is None:
            self.cells[(col, row)] = [bit, color_name, priority]
        else:
            cell[0] |= bit
            if priority >= cell[2]:
                cell[1], cell[2] = color_name, priority

    def line(self, x0, y0, x1, y1, color_name, priority=0):
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx - dy
        while True:
            self.set(x0, y0, color_name, priority)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def blit(self, win, color_pairs, top, left):
        for (col, row), (bits, color_name, _priority) in self.cells.items():
            if row >= self.rows or col >= self.cols:
                continue
            ch = chr(BRAILLE_BASE + bits)
            attr = curses.color_pair(color_pairs.get(color_name, 0))
            try:
                win.addstr(top + row, left + col, ch, attr)
            except curses.error:
                pass  # bottom-right screen corner refuses the last cell


# --------------------------------------------------------------------- state

class Node:
    __slots__ = ("id", "label", "folder", "slot", "mtime", "degree",
                 "x", "y", "vx", "vy")

    def __init__(self, data):
        self.id = data["id"]
        self.label = data["label"]
        self.folder = data["folder"]
        self.slot = data["slot"]
        self.mtime = data["mtime"]
        self.degree = data["degree"]
        self.x = self.y = self.vx = self.vy = 0.0


class AppState:
    def __init__(self):
        self.nodes = {}       # id -> Node
        self.edges = []       # [(source, target)]
        self.folders = []     # top-level folder names, index = slot
        self.vault_name = ""
        self.anchor_id = None
        self.pulses = {}      # id -> (color_name, expires_at)
        self.last_activity_id = 0
        self.connection_error = None
        self.selected_id = None
        self.folder_filter = None  # slot index, or None for "all"
        self.view_ox = self.view_oy = 0.0
        self.zoom = 3.0
        self.quit = False
        self.last_graph_fetch = 0.0
        self.preview_open = False
        self.preview_cache = {}   # note id -> raw text (or an error message)
        self.preview_scroll = 0

    # -- data ingestion --------------------------------------------------

    def apply_graph(self, data):
        incoming = {n["id"]: n for n in data["nodes"]}
        for node_id, node_data in incoming.items():
            existing = self.nodes.get(node_id)
            if existing is not None:
                existing.label = node_data["label"]
                existing.folder = node_data["folder"]
                existing.slot = node_data["slot"]
                existing.mtime = node_data["mtime"]
                existing.degree = node_data["degree"]
            else:
                node = Node(node_data)
                angle = random.uniform(0, 2 * math.pi)
                radius = random.uniform(4, 12)
                node.x = math.cos(angle) * radius
                node.y = math.sin(angle) * radius
                self.nodes[node_id] = node
        for stale_id in set(self.nodes) - set(incoming):
            del self.nodes[stale_id]
            self.pulses.pop(stale_id, None)
            if self.selected_id == stale_id:
                self.selected_id = None

        self.edges = [(e["source"], e["target"]) for e in data["edges"]
                      if e["source"] in self.nodes and e["target"] in self.nodes]
        self.folders = data["folders"]
        self.vault_name = data["vault_name"]
        self.anchor_id = max(self.nodes.values(), key=lambda n: n.degree).id \
            if self.nodes else None
        if self.selected_id is None and self.nodes:
            self.selected_id = self.anchor_id

    def apply_activity(self, events):
        now = time.time()
        for event in events:
            self.last_activity_id = max(self.last_activity_id, event["id"])
            color = "pulse_write" if event["action"] == "write" else "pulse_read"
            self.pulses[event["path"]] = (color, now + PULSE_DURATION_SECS)

    # -- physics -----------------------------------------------------------

    def physics_tick(self):
        nodes = self.nodes
        ids = list(nodes.keys())
        if not ids:
            return
        fx = {i: 0.0 for i in ids}
        fy = {i: 0.0 for i in ids}

        for a in range(len(ids)):
            ni = nodes[ids[a]]
            for b in range(a + 1, len(ids)):
                nj = nodes[ids[b]]
                dx, dy = ni.x - nj.x, ni.y - nj.y
                dist = max(math.hypot(dx, dy), MIN_DIST)
                force = REPEL_K / (dist * dist)
                ux, uy = dx / dist, dy / dist
                fx[ni.id] += ux * force
                fy[ni.id] += uy * force
                fx[nj.id] -= ux * force
                fy[nj.id] -= uy * force

        for source, target in self.edges:
            ns, nt = nodes[source], nodes[target]
            dx, dy = nt.x - ns.x, nt.y - ns.y
            dist = max(math.hypot(dx, dy), MIN_DIST)
            force = SPRING_K * (dist - REST_LEN)
            ux, uy = dx / dist, dy / dist
            fx[source] += ux * force
            fy[source] += uy * force
            fx[target] -= ux * force
            fy[target] -= uy * force

        for i in ids:
            n = nodes[i]
            dist = max(math.hypot(n.x, n.y), MIN_DIST)
            fx[i] += -n.x * CENTER_GRAVITY
            fy[i] += -n.y * CENTER_GRAVITY
            fx[i] += -n.y / dist * SWIRL_K * dist
            fy[i] += n.x / dist * SWIRL_K * dist

        for i in ids:
            if i == self.anchor_id:
                nodes[i].x = nodes[i].y = nodes[i].vx = nodes[i].vy = 0.0
                continue
            n = nodes[i]
            n.vx = (n.vx + fx[i]) * DAMPING
            n.vy = (n.vy + fy[i]) * DAMPING
            n.x += n.vx
            n.y += n.vy

    # -- view ---------------------------------------------------------------

    def world_to_px(self, node, canvas_w, canvas_h):
        px = (node.x - self.view_ox) * self.zoom + canvas_w / 2
        py = (node.y - self.view_oy) * self.zoom + canvas_h / 2
        return px, py

    def pan(self, dx, dy):
        self.view_ox += dx * PAN_STEP / self.zoom
        self.view_oy += dy * PAN_STEP / self.zoom

    def zoom_by(self, factor):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))

    def node_color(self, node, now):
        pulse = self.pulses.get(node.id)
        if pulse and pulse[1] > now:
            return pulse[0]
        if node.mtime and (now * 1000 - node.mtime) < RECENCY_GLOW_MS:
            return "glow"
        if self.folder_filter is not None and node.slot != self.folder_filter:
            return "dim"
        return folder_color_name(node.slot)

    def selected_node(self):
        return self.nodes.get(self.selected_id)

    def select_cycle(self, step):
        ordered = sorted(self.nodes.values(), key=lambda n: (-n.degree, n.label))
        if not ordered:
            return
        ids = [n.id for n in ordered]
        if self.selected_id not in ids:
            self.selected_id = ids[0]
            return
        idx = (ids.index(self.selected_id) + step) % len(ids)
        self.selected_id = ids[idx]


# --------------------------------------------------------------------- draw

def _age_str(mtime_ms):
    if not mtime_ms:
        return "unknown"
    secs = max(0, time.time() - mtime_ms / 1000)
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def draw(stdscr, state, color_pairs):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    show_legend = width > 60 and LEGEND_WIDTH + 20 < width
    graph_w = width - (LEGEND_WIDTH + 1 if show_legend else 0)
    graph_h = max(1, height - 2)

    if state.connection_error:
        msg = [f"Can't reach vault-graph server at {BASE_URL}",
               "Start it with: python3 server.py --no-open",
               f"({state.connection_error})",
               "Retrying... (q to quit)"]
        for row, line in enumerate(msg):
            stdscr.addstr(min(height - 2, 2 + row), max(0, (graph_w - len(line)) // 2), line)
    else:
        canvas = Canvas(graph_w, graph_h)
        now = time.time()
        for source, target in state.edges:
            ns, nt = state.nodes[source], state.nodes[target]
            x0, y0 = state.world_to_px(ns, canvas.w, canvas.h)
            x1, y1 = state.world_to_px(nt, canvas.w, canvas.h)
            canvas.line(x0, y0, x1, y1, "edge", priority=0)
        for node in state.nodes.values():
            x, y = state.world_to_px(node, canvas.w, canvas.h)
            color = state.node_color(node, now)
            canvas.set(x, y, color, priority=2)
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                             (1, 1), (1, -1), (-1, 1), (-1, -1)):
                canvas.set(x + ddx, y + ddy, color, priority=1)
            if node.degree >= 5:
                for ddx, ddy in ((2, 0), (-2, 0), (0, 2), (0, -2),
                                  (2, 2), (2, -2), (-2, 2), (-2, -2)):
                    canvas.set(x + ddx, y + ddy, color, priority=1)
            if node.id == state.selected_id:
                for ddx, ddy in ((2, 0), (-2, 0), (0, 2), (0, -2)):
                    canvas.set(x + ddx, y + ddy, "glow", priority=1)
        canvas.blit(stdscr, color_pairs, 0, 0)

    if show_legend:
        draw_legend(stdscr, state, color_pairs, graph_h, graph_w + 1, LEGEND_WIDTH)

    if state.preview_open:
        draw_preview(stdscr, state, height, width)

    draw_status(stdscr, state, height - 1, width)
    stdscr.refresh()


def draw_preview(stdscr, state, height, width):
    node = state.selected_node()
    if node is None:
        state.preview_open = False
        return
    top, left = 1, 2
    box_h = max(3, height - 3)
    box_w = max(20, width - 4)
    inner_w = box_w - 2

    text = state.preview_cache.get(node.id, "")
    lines = []
    for raw_line in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(raw_line, inner_w) or [""])

    max_scroll = max(0, len(lines) - (box_h - 2))
    state.preview_scroll = max(0, min(state.preview_scroll, max_scroll))

    try:
        stdscr.addstr(top, left, "+" + "-" * (box_w - 2) + "+")
        title = f" {node.label} "[:box_w - 2]
        stdscr.addstr(top, left + 2, title, curses.A_BOLD)
        for row in range(1, box_h - 1):
            stdscr.addstr(top + row, left, "|")
            stdscr.addstr(top + row, left + box_w - 1, "|")
        stdscr.addstr(top + box_h - 1, left, "+" + "-" * (box_w - 2) + "+")
        visible = lines[state.preview_scroll:state.preview_scroll + box_h - 2]
        for i, line in enumerate(visible):
            stdscr.addstr(top + 1 + i, left + 1, line.ljust(inner_w)[:inner_w])
    except curses.error:
        pass


def draw_legend(stdscr, state, color_pairs, height, left, width):
    row = 0
    title = state.vault_name or "vault"
    stdscr.addstr(row, left, title[:width], curses.A_BOLD)
    row += 1
    stdscr.addstr(row, left, f"{len(state.nodes)} notes, {len(state.edges)} links")
    row += 2
    for slot, folder in enumerate(state.folders):
        if row >= height:
            break
        marker = ">" if state.folder_filter == slot else " "
        attr = curses.color_pair(color_pairs[folder_color_name(slot)])
        label = f"{marker}{slot + 1} {folder}"[:width]
        stdscr.addstr(row, left, label, attr | (curses.A_BOLD if marker == ">" else 0))
        row += 1
    row += 1
    node = state.selected_node()
    if node and row < height:
        stdscr.addstr(row, left, "selected:", curses.A_BOLD)
        row += 1
        for line in (node.label, node.folder, f"{node.degree} links",
                     _age_str(node.mtime)):
            if row >= height:
                break
            stdscr.addstr(row, left, line[:width])
            row += 1


def draw_status(stdscr, state, row, width):
    if state.preview_open:
        hints = "j/k scroll  p/esc close  q quit"
    else:
        hints = ("arrows/hjkl pan  +/- zoom  tab select  1-8 isolate  0 all  "
                 "o open  p preview  r sync  q quit")
    if state.connection_error:
        text = "reconnecting... | " + hints
    else:
        text = hints
    try:
        stdscr.addstr(row, 0, text[:width], curses.A_DIM)
    except curses.error:
        pass


# --------------------------------------------------------------------- main

def handle_key(ch, state):
    if ch == ord("q"):
        state.quit = True
        return
    if state.preview_open:
        if ch in (ord("p"), 27):
            state.preview_open = False
        elif ch in (curses.KEY_UP, ord("k")):
            state.preview_scroll = max(0, state.preview_scroll - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            state.preview_scroll += 1
        return
    if ch == 27:
        state.quit = True
    elif ch in (curses.KEY_LEFT, ord("h")):
        state.pan(-1, 0)
    elif ch in (curses.KEY_RIGHT, ord("l")):
        state.pan(1, 0)
    elif ch in (curses.KEY_UP, ord("k")):
        state.pan(0, -1)
    elif ch in (curses.KEY_DOWN, ord("j")):
        state.pan(0, 1)
    elif ch in (ord("+"), ord("=")):
        state.zoom_by(ZOOM_FACTOR)
    elif ch in (ord("-"), ord("_")):
        state.zoom_by(1 / ZOOM_FACTOR)
    elif ch == ord("\t"):
        state.select_cycle(1)
    elif ch == curses.KEY_BTAB:
        state.select_cycle(-1)
    elif ord("1") <= ch <= ord("8"):
        slot = ch - ord("1")
        state.folder_filter = None if state.folder_filter == slot else slot
    elif ch == ord("0"):
        state.folder_filter = None
    elif ch == ord("o"):
        node = state.selected_node()
        if node:
            url = ("obsidian://open?vault=" + urllib.parse.quote(state.vault_name)
                   + "&file=" + urllib.parse.quote(node.id))
            webbrowser.open(url)
    elif ch == ord("r"):
        state.last_graph_fetch = 0.0
        state.preview_cache = {}
    elif ch == ord("p"):
        node = state.selected_node()
        if node:
            if node.id not in state.preview_cache:
                try:
                    text = _get_json("/api/note?path=" + urllib.parse.quote(node.id))["content"]
                except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as exc:
                    text = f"(couldn't load note: {exc})"
                state.preview_cache[node.id] = text
            state.preview_open = True
            state.preview_scroll = 0


def run(stdscr):
    global BUSY_INTERVAL_MS, IDLE_INTERVAL_MS
    curses.curs_set(0)
    color_pairs = init_colors()
    state = AppState()
    last_activity_poll = 0.0
    last_input_at = 0.0

    try:
        settings = _get_json("/api/settings")
        if settings.get("max_fps"):
            BUSY_INTERVAL_MS = 1000 / settings["max_fps"]
        if settings.get("idle_fps"):
            IDLE_INTERVAL_MS = max(1000 / settings["idle_fps"], BUSY_INTERVAL_MS)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass  # older server without /api/settings, etc - keep the defaults

    while not state.quit:
        now = time.time()
        busy = state.pulses or (now - last_input_at < INPUT_BUSY_SECS)
        stdscr.timeout(int(BUSY_INTERVAL_MS if busy else IDLE_INTERVAL_MS))

        if now - state.last_graph_fetch > GRAPH_POLL_SECS:
            try:
                state.apply_graph(_get_json("/api/graph"))
                state.connection_error = None
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                state.connection_error = str(exc)
            state.last_graph_fetch = now

        if not state.connection_error and now - last_activity_poll > ACTIVITY_POLL_SECS:
            try:
                events = _get_json(f"/api/activity?since={state.last_activity_id}")["events"]
                state.apply_activity(events)
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            last_activity_poll = now

        state.physics_tick()
        draw(stdscr, state, color_pairs)

        ch = stdscr.getch()
        if ch != -1:
            last_input_at = time.time()
            handle_key(ch, state)


def main():
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(run)


if __name__ == "__main__":
    main()
