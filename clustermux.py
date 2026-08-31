#!/usr/bin/env python3
"""A small, dependency-free TUI for tmux sessions spread across SSH hosts."""

import argparse
import base64
import concurrent.futures
import curses
import glob
import json
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


__version__ = "0.2.0"

DEFAULT_CONFIG = Path.home() / ".config" / "clustermux" / "hosts.json"
WORKSPACE_SESSION = "clustermux"
SCRIPT_PATH = Path(__file__).resolve()
# Some older tmux builds (e.g. 3.4) strip the C0 unit-separator byte that
# newer tmux builds preserve. Use a printable sentinel so one protocol works
# with every remote tmux version.
FIELD_SEPARATOR = "__CLUSTERMUX_FIELD__"
TMUX_FORMAT = FIELD_SEPARATOR.join(
    (
        "#{session_name}",
        "#{session_attached}",
        "#{session_windows}",
        "#{session_activity}",
        "#{window_index}",
        "#{window_name}",
        "#{window_active}",
        "#{pane_index}",
        "#{pane_active}",
        "#{pane_pid}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_dead}",
    )
)
REMOTE_LIST = "tmux list-panes -a -F " + shlex.quote(TMUX_FORMAT)


@dataclass(frozen=True)
class Host:
    group: str
    label: str
    target: str
    connect_timeout: Optional[int] = None

    @property
    def display_name(self) -> str:
        return f"{self.group} / {self.label}"


@dataclass
class Session:
    name: str
    attached: int = 0
    windows: int = 0
    activity: int = 0
    window_index: str = ""
    window_name: str = ""
    pane_pid: str = ""
    pane_index: str = ""
    command: str = ""
    path: str = ""
    pane_count: int = 0
    dead_panes: int = 0


@dataclass
class Snapshot:
    host: Host
    status: str = "loading"
    sessions: List[Session] = field(default_factory=list)
    latency: Optional[float] = None
    error: str = ""


def self_argv() -> List[str]:
    """Argv prefix used to re-invoke clustermux inside tmux panes and iTerm tabs.

    Works for both distribution forms: an executable single-file script on
    PATH, or a pip-installed module (whose .py in site-packages is not
    executable, so fall back to the console script or the interpreter).
    """
    if os.access(SCRIPT_PATH, os.X_OK):
        return [str(SCRIPT_PATH)]
    exe = shutil.which("clustermux")
    if exe:
        return [exe]
    return [sys.executable, str(SCRIPT_PATH)]


def ssh_argv(host: Host, timeout: int, tty: bool = False) -> List[str]:
    timeout = host.connect_timeout or timeout
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=ERROR",
    ]
    if tty:
        args.append("-t")
    args.append(host.target)
    return args


def shell_command(argv: Sequence[object]) -> str:
    return " ".join(shlex.quote(str(value)) for value in argv)


def applescript_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def open_iterm_tab(command: str, title: str) -> None:
    """Open a command in a sibling iTerm tab without disturbing this terminal."""
    script = f'''tell application "iTerm2"
  activate
  if (count of windows) is 0 then
    set w to (create window with default profile)
  else
    set w to current window
    tell w to create tab with default profile
  end if
  tell current session of w
    set name to "{applescript_escape(title)}"
    write text "{applescript_escape(command)}"
  end tell
end tell'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(compact_error(result.stderr))


def remote_attach_command(host: Host, session: Session, handoff_pane: Optional[str] = None) -> str:
    """Build the supervised command used by a standalone iTerm tab."""
    return shell_command(
        [
            *self_argv(),
            "--tab-attach",
            host.target,
            session.name,
            host.display_name,
            str(host.connect_timeout or 12),
            handoff_pane or "-",
        ]
    )


def open_session_tab(host: Host, session: Session, handoff_pane: Optional[str] = None) -> None:
    open_iterm_tab(
        remote_attach_command(host, session, handoff_pane),
        f"{host.group}:{host.label}:{session.name}",
    )


def connection_key(host: Host, session: Session) -> str:
    raw = f"{host.target}\0{session.name}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def compact_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "unknown SSH error"
    message = lines[-1]
    message = re.sub(r"^ssh: ", "", message)
    return message[:180]


def natural_key(value: str) -> Tuple[Tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
    )


def parse_sessions(output: str) -> List[Session]:
    sessions: Dict[str, Session] = {}
    active_seen: Dict[str, bool] = {}
    for raw_line in output.splitlines():
        fields = raw_line.split(FIELD_SEPARATOR)
        if len(fields) != 13:
            continue
        (
            name,
            attached,
            windows,
            activity,
            window_index,
            window_name,
            window_active,
            pane_index,
            pane_active,
            pane_pid,
            command,
            path,
            pane_dead,
        ) = fields
        session = sessions.get(name)
        if session is None:
            session = Session(
                name=name,
                attached=int(attached or 0),
                windows=int(windows or 0),
                activity=int(activity or 0),
            )
            sessions[name] = session
        session.pane_count += 1
        session.dead_panes += int(pane_dead or 0)
        is_active = window_active == "1" and pane_active == "1"
        if is_active or not active_seen.get(name):
            session.window_index = window_index
            session.window_name = window_name
            session.pane_index = pane_index
            session.pane_pid = pane_pid
            session.command = command
            session.path = path
            active_seen[name] = is_active
    return sorted(sessions.values(), key=lambda item: natural_key(item.name))


def query_host(host: Host, timeout: int) -> Snapshot:
    timeout = host.connect_timeout or timeout
    started = time.monotonic()
    try:
        result = subprocess.run(
            ssh_argv(host, timeout) + [REMOTE_LIST],
            capture_output=True,
            text=True,
            timeout=timeout + 8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Snapshot(host=host, status="offline", latency=time.monotonic() - started, error="connection timed out")
    except OSError as exc:
        return Snapshot(host=host, status="error", latency=time.monotonic() - started, error=str(exc))

    latency = time.monotonic() - started
    if result.returncode == 0:
        sessions = parse_sessions(result.stdout)
        return Snapshot(host=host, status="online" if sessions else "empty", sessions=sessions, latency=latency)

    stderr = result.stderr.strip()
    if result.returncode != 255 and (
        "no server running" in stderr.lower()
        or "error connecting to" in stderr.lower()
        or "no sessions" in stderr.lower()
    ):
        return Snapshot(host=host, status="empty", latency=latency)
    return Snapshot(host=host, status="offline" if result.returncode == 255 else "error", latency=latency, error=compact_error(stderr))


def query_all(hosts: Sequence[Host], timeout: int) -> List[Snapshot]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = [pool.submit(query_host, host, timeout) for host in hosts]
        return [future.result() for future in futures]


def encoded_bash_command(script: str) -> str:
    """Run a Bash script through SSH without relying on the login shell's quoting rules."""
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"printf %s {payload} | base64 -d | bash"


def run_remote_command(host: Host, timeout: int, command: str) -> subprocess.CompletedProcess:
    effective_timeout = host.connect_timeout or timeout
    return subprocess.run(
        ssh_argv(host, timeout) + [command],
        capture_output=True,
        text=True,
        timeout=effective_timeout + 8,
        check=False,
    )


def command_error(result: subprocess.CompletedProcess) -> str:
    message = compact_error(result.stderr or result.stdout)
    return message.removeprefix("ERROR|")


def capture_preview(host: Host, session: Session, timeout: int, lines: int) -> Tuple[Tuple[str, str], str, str]:
    timeout = host.connect_timeout or timeout
    key = (host.target, session.name)
    command = "tmux capture-pane -p -S " + str(-max(lines, 20)) + " -t " + shlex.quote(session.name)
    try:
        result = subprocess.run(
            ssh_argv(host, timeout) + [command],
            capture_output=True,
            text=True,
            timeout=timeout + 8,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return key, "", str(exc)
    if result.returncode != 0:
        return key, "", compact_error(result.stderr)
    clean = "\n".join(line.rstrip() for line in result.stdout.splitlines()).strip("\n")
    return key, clean, ""


def load_hosts(path: Path) -> List[Host]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(
            f"clustermux: config not found: {path}\n"
            "Run 'clustermux --init' to scan your SSH setup and create one interactively."
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"clustermux: cannot read {path}: {exc}")
    if not isinstance(data, list) or not data:
        raise SystemExit(f"clustermux: {path} must contain a non-empty JSON list")
    hosts = []
    for index, item in enumerate(data, 1):
        try:
            configured_timeout = item.get("connect_timeout")
            if configured_timeout is not None:
                configured_timeout = int(configured_timeout)
            host = Host(
                group=str(item["group"]),
                label=str(item["label"]),
                target=str(item["target"]),
                connect_timeout=configured_timeout,
            )
        except (KeyError, TypeError):
            raise SystemExit(f"clustermux: host #{index} needs group, label and target")
        if not host.target or host.target.startswith("-") or re.search(r"[^A-Za-z0-9_.@:%+-]", host.target):
            raise SystemExit(f"clustermux: unsafe SSH target for {host.display_name}: {host.target!r}")
        if host.connect_timeout is not None and not 1 <= host.connect_timeout <= 120:
            raise SystemExit(f"clustermux: invalid connect_timeout for {host.display_name}")
        hosts.append(host)
    return hosts


def shorten_path(path: str, width: int) -> str:
    if len(path) <= width:
        return path
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        tail = "/".join(parts[-2:])
        if len(tail) + 2 <= width:
            return "…/" + tail
    return "…" + path[-max(0, width - 1) :]


class Dashboard:
    def __init__(self, screen, hosts: Sequence[Host], timeout: int, refresh_seconds: int, previews: bool):
        self.screen = screen
        self.hosts = list(hosts)
        self.timeout = timeout
        self.refresh_seconds = refresh_seconds
        self.previews = previews
        self.snapshots = [Snapshot(host=host) for host in hosts]
        self.host_index = 0
        self.session_index = 0
        self.focus = "hosts"
        self.message = "Discovering remote tmux sessions…"
        self.last_refresh: Optional[float] = None
        self.last_refresh_wall = "never"
        self.preview_key: Optional[Tuple[str, str]] = None
        self.preview_text = ""
        self.preview_error = ""
        self.requested_preview: Optional[Tuple[str, str]] = None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.refresh_future = None
        self.preview_future = None
        self.refresh_started = 0.0
        self.spinner_index = 0

    def selected_snapshot(self) -> Snapshot:
        return self.snapshots[self.host_index]

    def selected_session(self) -> Optional[Session]:
        sessions = self.selected_snapshot().sessions
        if not sessions:
            return None
        self.session_index = max(0, min(self.session_index, len(sessions) - 1))
        return sessions[self.session_index]

    def start_refresh(self) -> None:
        if self.refresh_future is not None:
            self.message = "A refresh is already running"
            return
        self.refresh_started = time.monotonic()
        self.refresh_future = self.executor.submit(query_all, self.hosts, self.timeout)
        self.message = "Refreshing all nodes in parallel…"

    def request_preview(self) -> None:
        if not self.previews:
            return
        session = self.selected_session()
        if session is None:
            self.preview_key = None
            self.preview_text = ""
            self.preview_error = ""
            return
        key = (self.selected_snapshot().host.target, session.name)
        self.requested_preview = key
        if self.preview_key == key or self.preview_future is not None:
            return
        self.preview_error = ""
        self.preview_text = ""
        self.preview_future = self.executor.submit(
            capture_preview, self.selected_snapshot().host, session, self.timeout, 120
        )

    def poll(self) -> None:
        if self.refresh_future is not None and self.refresh_future.done():
            try:
                self.snapshots = self.refresh_future.result()
                online = sum(snapshot.status in ("online", "empty") for snapshot in self.snapshots)
                sessions = sum(len(snapshot.sessions) for snapshot in self.snapshots)
                self.message = f"Refresh complete · {online}/{len(self.snapshots)} nodes reachable · {sessions} sessions"
            except Exception as exc:  # keep the dashboard alive on unexpected local errors
                self.message = f"Refresh failed: {exc}"
            self.refresh_future = None
            self.last_refresh = time.monotonic()
            self.last_refresh_wall = datetime.now().strftime("%H:%M:%S")
            self.session_index = 0
            self.preview_key = None
            self.request_preview()

        if self.preview_future is not None and self.preview_future.done():
            try:
                key, output, error = self.preview_future.result()
                if key == self.requested_preview:
                    self.preview_key = key
                    self.preview_text = output
                    self.preview_error = error
            except Exception as exc:
                self.preview_error = str(exc)
            self.preview_future = None
            if self.preview_key != self.requested_preview:
                self.request_preview()

        if (
            self.refresh_future is None
            and self.last_refresh is not None
            and self.refresh_seconds > 0
            and time.monotonic() - self.last_refresh >= self.refresh_seconds
        ):
            self.start_refresh()

    def color(self, name: str) -> int:
        pairs = {
            "header": 1,
            "selected": 2,
            "online": 3,
            "offline": 4,
            "muted": 5,
            "accent": 6,
        }
        return curses.color_pair(pairs[name])

    def add(self, y: int, x: int, text: object, attr: int = 0, width: Optional[int] = None) -> None:
        height, screen_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= screen_width:
            return
        value = str(text).replace("\t", "    ")
        allowed = screen_width - x - 1
        if width is not None:
            allowed = min(allowed, max(0, width))
        if allowed <= 0:
            return
        try:
            self.screen.addnstr(y, x, value, allowed, attr)
        except curses.error:
            pass

    def horizontal(self, y: int, x: int, width: int, char: str = "─", attr: int = 0) -> None:
        self.add(y, x, char * max(0, width), attr, width)

    def draw_hosts(self, top: int, left: int, width: int, height: int) -> None:
        focus_attr = self.color("accent") | curses.A_BOLD if self.focus == "hosts" else curses.A_BOLD
        self.add(top, left + 1, "NODES", focus_attr, width - 2)
        self.horizontal(top + 1, left, width)
        row = top + 2
        markers = {"loading": "…", "online": "●", "empty": "○", "offline": "×", "error": "!"}
        for index, snapshot in enumerate(self.snapshots):
            if row >= top + height:
                break
            selected = index == self.host_index
            attr = self.color("selected") | curses.A_BOLD if selected else 0
            status_color = self.color("online") if snapshot.status in ("online", "empty") else self.color("offline")
            if snapshot.status == "loading":
                status_color = self.color("muted")
            line = f"  {snapshot.host.group:<5} {snapshot.host.label:<12}"
            count = str(len(snapshot.sessions)) if snapshot.status in ("online", "empty") else "–"
            line = line[: max(0, width - 4)].ljust(max(0, width - 3)) + count
            self.add(row, left, " " * width, attr, width)
            self.add(row, left + 1, markers.get(snapshot.status, "?"), attr | status_color, 1)
            self.add(row, left + 2, line[2:], attr, width - 2)
            row += 1

    def draw_sessions(self, top: int, left: int, width: int, height: int) -> None:
        snapshot = self.selected_snapshot()
        focus_attr = self.color("accent") | curses.A_BOLD if self.focus == "sessions" else curses.A_BOLD
        title = f"SESSIONS · {snapshot.host.display_name}"
        if snapshot.latency is not None:
            title += f" · {snapshot.latency:.1f}s"
        self.add(top, left + 1, title, focus_attr, width - 2)
        self.horizontal(top + 1, left, width)
        content_top = top + 2
        if snapshot.status == "loading":
            self.add(content_top, left + 2, "Waiting for SSH discovery…", self.color("muted"), width - 4)
            return
        if snapshot.status in ("offline", "error"):
            self.add(content_top, left + 2, snapshot.status.upper(), self.color("offline") | curses.A_BOLD, width - 4)
            for offset, line in enumerate(textwrap.wrap(snapshot.error, max(10, width - 4))):
                self.add(content_top + 2 + offset, left + 2, line, self.color("muted"), width - 4)
            return
        if not snapshot.sessions:
            self.add(content_top, left + 2, "Connected · no tmux sessions", self.color("muted"), width - 4)
            return

        for index, session in enumerate(snapshot.sessions[: max(0, height - 2)]):
            row = content_top + index
            selected = index == self.session_index
            attr = self.color("selected") | curses.A_BOLD if selected else 0
            attached = "◆" if session.attached else " "
            path_room = max(8, width - 31)
            path = shorten_path(session.path, path_room)
            line = f" {attached} {session.name:<8.8} {session.command:<10.10} {path}"
            self.add(row, left, " " * width, attr, width)
            self.add(row, left + 1, line, attr, width - 2)

    def draw_preview(self, top: int, left: int, width: int, height: int) -> None:
        snapshot = self.selected_snapshot()
        session = self.selected_session()
        self.horizontal(top, left, width)
        if session is None:
            self.add(top + 1, left + 1, "PREVIEW", curses.A_BOLD, width - 2)
            return
        source = f"{snapshot.host.display_name} · session {session.name}"
        self.add(top + 1, left + 1, f"REMOTE PREVIEW · {source} · {session.command}", self.color("accent") | curses.A_BOLD, width - 2)
        self.add(top + 2, left + 1, shorten_path(session.path, max(1, width - 2)), self.color("muted"), width - 2)
        body_top = top + 4
        body_height = max(0, height - 4)
        key = (snapshot.host.target, session.name)
        if not self.previews:
            self.add(body_top, left + 1, "Preview disabled", self.color("muted"), width - 2)
        elif self.preview_future is not None and self.preview_key != key:
            self.add(body_top, left + 1, "Loading recent pane output…", self.color("muted"), width - 2)
        elif self.preview_error:
            self.add(body_top, left + 1, self.preview_error, self.color("offline"), width - 2)
        elif self.preview_key == key:
            lines = self.preview_text.splitlines()
            visible = lines[-body_height:] if body_height else []
            for offset, line in enumerate(visible):
                self.add(body_top + offset, left + 1, line, 0, width - 2)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 14 or width < 72:
            self.add(0, 0, "clustermux needs a terminal of at least 72×14", curses.A_BOLD)
            self.add(2, 0, f"current size: {width}×{height}")
            self.screen.refresh()
            return

        groups = "+".join(dict.fromkeys(host.group for host in self.hosts))
        header = f" CLUSTERMUX  ·  {groups} " if groups else " CLUSTERMUX "
        self.add(0, 0, " " * width, self.color("header") | curses.A_BOLD, width)
        self.add(0, 0, header, self.color("header") | curses.A_BOLD, width)
        reachable = sum(snapshot.status in ("online", "empty") for snapshot in self.snapshots)
        sessions = sum(len(snapshot.sessions) for snapshot in self.snapshots)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self.spinner_index % 10] if self.refresh_future is not None else ""
        self.spinner_index += 1
        summary = f" {spinner} {reachable}/{len(self.snapshots)} reachable · {sessions} sessions · refreshed {self.last_refresh_wall} "
        self.add(1, 0, summary, self.color("muted"), width)

        footer_top = height - 2
        body_top = 3
        body_height = footer_top - body_top
        host_width = min(30, max(24, width // 4))
        right_left = host_width + 1
        right_width = width - right_left
        self.draw_hosts(body_top, 0, host_width, body_height)
        for y in range(body_top, footer_top):
            self.add(y, host_width, "│", self.color("muted"), 1)
        session_height = min(max(7, len(self.selected_snapshot().sessions) + 2), max(7, body_height // 2))
        self.draw_sessions(body_top, right_left, right_width, session_height)
        self.draw_preview(body_top + session_height, right_left, right_width, body_height - session_height)

        self.horizontal(footer_top, 0, width)
        keys = "↑↓ select   Enter attach here   t new tab   r refresh   p preview   q quit"
        self.add(height - 1, 0, " " * width, self.color("header"), width)
        self.add(height - 1, 1, keys, self.color("header"), width - 2)
        if self.message and width > len(keys) + 24:
            room = width - len(keys) - 4
            self.add(height - 1, len(keys) + 3, self.message, self.color("header"), room)
        self.screen.refresh()

    def move_host(self, delta: int) -> None:
        self.host_index = (self.host_index + delta) % len(self.snapshots)
        self.session_index = 0
        self.preview_key = None
        self.request_preview()

    def move_session(self, delta: int) -> None:
        sessions = self.selected_snapshot().sessions
        if sessions:
            self.session_index = (self.session_index + delta) % len(sessions)
            self.preview_key = None
            self.request_preview()

    def attach(self) -> None:
        session = self.selected_session()
        if session is None:
            self.message = "The selected node has no tmux session"
            return
        snapshot = self.selected_snapshot()
        remote_command = "tmux attach-session -t " + shlex.quote(session.name)
        curses.def_prog_mode()
        curses.endwin()
        try:
            result = subprocess.run(ssh_argv(snapshot.host, self.timeout, tty=True) + [remote_command], check=False)
            self.message = f"Detached from {snapshot.host.display_name}:{session.name} · ssh exit {result.returncode}"
        except OSError as exc:
            self.message = f"Could not attach: {exc}"
        finally:
            curses.reset_prog_mode()
            curses.curs_set(0)
            self.screen.keypad(True)
            self.screen.refresh()
        self.preview_key = None
        self.start_refresh()

    def attach_in_new_tab(self) -> None:
        session = self.selected_session()
        if session is None:
            self.message = "The selected node has no tmux session"
            return
        snapshot = self.selected_snapshot()
        try:
            open_session_tab(snapshot.host, session)
            self.message = f"Opened {snapshot.host.display_name}:{session.name} in a new iTerm tab"
        except (OSError, RuntimeError) as exc:
            self.message = f"Could not open iTerm tab: {exc}"

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_CYAN, -1)
            curses.init_pair(6, curses.COLOR_YELLOW, -1)
        self.start_refresh()
        try:
            while True:
                self.poll()
                self.draw()
                key = self.screen.getch()
                if key in (ord("q"), ord("Q")):
                    break
                if key in (curses.KEY_LEFT, ord("h")):
                    self.focus = "hosts"
                elif key in (curses.KEY_RIGHT, ord("l")):
                    self.focus = "sessions"
                    self.request_preview()
                elif key == ord("\t"):
                    self.focus = "sessions" if self.focus == "hosts" else "hosts"
                    self.request_preview()
                elif key in (curses.KEY_UP, ord("k")):
                    self.move_host(-1) if self.focus == "hosts" else self.move_session(-1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    self.move_host(1) if self.focus == "hosts" else self.move_session(1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    if self.focus == "hosts" and self.selected_snapshot().sessions:
                        self.focus = "sessions"
                        self.request_preview()
                    else:
                        self.attach()
                elif key in (ord("r"), ord("R")):
                    self.start_refresh()
                elif key in (ord("p"), ord("P")):
                    self.preview_key = None
                    self.request_preview()
                elif key in (ord("t"), ord("T")):
                    self.attach_in_new_tab()
        finally:
            self.executor.shutdown(wait=False)


class Navigator:
    """Compact left pane used by the split tmux workspace."""

    def __init__(self, screen, hosts: Sequence[Host], target_pane: str, timeout: int, refresh_seconds: int):
        self.screen = screen
        self.hosts = list(hosts)
        self.target_pane = target_pane
        self.timeout = timeout
        self.refresh_seconds = refresh_seconds
        self.snapshots = [Snapshot(host=host) for host in hosts]
        self.cursor = 0
        self.scroll = 0
        self.message = "Discovering…"
        self.last_refresh: Optional[float] = None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.spinner_index = 0
        self.last_layout_check = 0.0

    def rows(self) -> List[Tuple[str, int, Optional[int]]]:
        rows: List[Tuple[str, int, Optional[int]]] = []
        for host_index, snapshot in enumerate(self.snapshots):
            rows.append(("host", host_index, None))
            for session_index, _session in enumerate(snapshot.sessions):
                rows.append(("session", host_index, session_index))
        return rows

    def selected(self) -> Optional[Tuple[Snapshot, Session]]:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        kind, host_index, session_index = rows[self.cursor]
        snapshot = self.snapshots[host_index]
        if kind == "host":
            if not snapshot.sessions:
                return None
            return snapshot, snapshot.sessions[0]
        assert session_index is not None
        return snapshot, snapshot.sessions[session_index]

    def selection_key(self) -> Optional[Tuple[str, str]]:
        selected = self.selected()
        if selected is None:
            return None
        return selected[0].host.target, selected[1].name

    def restore_selection(self, key: Optional[Tuple[str, str]]) -> None:
        if key is None:
            self.cursor = min(self.cursor, max(0, len(self.rows()) - 1))
            return
        for index, (kind, host_index, session_index) in enumerate(self.rows()):
            if kind != "session" or session_index is None:
                continue
            snapshot = self.snapshots[host_index]
            session = snapshot.sessions[session_index]
            if (snapshot.host.target, session.name) == key:
                self.cursor = index
                return
        self.cursor = min(self.cursor, max(0, len(self.rows()) - 1))

    def start_refresh(self) -> None:
        if self.future is None:
            self.future = self.executor.submit(query_all, self.hosts, self.timeout)
            self.message = "Refreshing session list…"

    def poll(self) -> None:
        if time.monotonic() - self.last_layout_check >= 1.0:
            self.normalize_layout()
            self.last_layout_check = time.monotonic()
        if self.future is not None and self.future.done():
            key = self.selection_key()
            try:
                self.snapshots = self.future.result()
                reachable = sum(item.status in ("online", "empty") for item in self.snapshots)
                sessions = sum(len(item.sessions) for item in self.snapshots)
                self.message = f"{reachable}/{len(self.snapshots)} up · {sessions} sessions"
            except Exception as exc:
                self.message = f"Refresh failed: {exc}"
            self.future = None
            self.last_refresh = time.monotonic()
            self.restore_selection(key)
        if (
            self.future is None
            and self.last_refresh is not None
            and self.refresh_seconds > 0
            and time.monotonic() - self.last_refresh >= self.refresh_seconds
        ):
            self.start_refresh()

    def normalize_layout(self) -> None:
        """Keep the navigator readable after an attached client resizes tmux."""
        own_pane = os.environ.get("TMUX_PANE")
        if not own_pane:
            return
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", own_pane, "#{window_width}"],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value.isdigit():
            return
        window_width = int(value)
        desired = min(44, max(30, int(window_width * 0.28)))
        subprocess.run(
            ["tmux", "resize-pane", "-t", own_pane, "-x", str(desired)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def add(self, y: int, x: int, value: object, attr: int = 0, width: Optional[int] = None) -> None:
        height, screen_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= screen_width:
            return
        allowed = screen_width - x - 1
        if width is not None:
            allowed = min(allowed, max(0, width))
        if allowed <= 0:
            return
        try:
            self.screen.addnstr(y, x, str(value).replace("\t", "  "), allowed, attr)
        except curses.error:
            pass

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        header_attr = curses.color_pair(1) | curses.A_BOLD
        selected_attr = curses.color_pair(2) | curses.A_BOLD
        muted_attr = curses.color_pair(5)
        self.add(0, 0, " " * width, header_attr, width)
        self.add(0, 1, "CLUSTERMUX · NAVIGATOR", header_attr, width - 2)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self.spinner_index % 10] if self.future is not None else ""
        self.spinner_index += 1
        self.add(1, 1, f"{spinner} {self.message}", muted_attr, width - 2)
        try:
            self.screen.hline(2, 0, curses.ACS_HLINE, max(0, width - 1))
        except curses.error:
            pass

        rows = self.rows()
        body_height = max(1, height - 7)
        self.cursor = max(0, min(self.cursor, max(0, len(rows) - 1)))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + body_height:
            self.scroll = self.cursor - body_height + 1
        visible = rows[self.scroll : self.scroll + body_height]
        markers = {"loading": "…", "online": "●", "empty": "○", "offline": "×", "error": "!"}
        for offset, (kind, host_index, session_index) in enumerate(visible):
            row_index = self.scroll + offset
            y = 3 + offset
            snapshot = self.snapshots[host_index]
            attr = selected_attr if row_index == self.cursor else 0
            self.add(y, 0, " " * width, attr, width)
            if kind == "host":
                marker = markers.get(snapshot.status, "?")
                count = len(snapshot.sessions) if snapshot.status in ("online", "empty") else "–"
                label = f"{marker} {snapshot.host.group}/{snapshot.host.label}"
                line = label[: max(1, width - 4)].ljust(max(1, width - 3)) + str(count)
                self.add(y, 1, line, attr | curses.A_BOLD, width - 2)
            else:
                assert session_index is not None
                session = snapshot.sessions[session_index]
                attached = "◆" if session.attached else " "
                line = f"  {attached} {session.name:<8.8} {session.command:<10.10}"
                self.add(y, 1, line, attr, width - 2)

        footer_y = max(3, height - 4)
        try:
            self.screen.hline(footer_y, 0, curses.ACS_HLINE, max(0, width - 1))
        except curses.error:
            pass
        selected_kind = rows[self.cursor][0] if rows else "host"
        if selected_kind == "host":
            self.add(footer_y + 1, 1, "Cluster: b Bash · t new tmux", curses.A_BOLD, width - 2)
            self.add(footer_y + 2, 1, "↑↓ select · r refresh · q close", muted_attr, width - 2)
            self.add(footer_y + 3, 1, "Shift+← sidebar", muted_attr, width - 2)
        else:
            self.add(footer_y + 1, 1, "Session: Enter open · f fork · x kill", curses.A_BOLD, width - 2)
            self.add(footer_y + 2, 1, "e rename · o handoff tab · r refresh", muted_attr, width - 2)
            self.add(footer_y + 3, 1, "Shift+← sidebar · q close", muted_attr, width - 2)
        self.screen.refresh()

    def move(self, delta: int) -> None:
        rows = self.rows()
        if rows:
            self.cursor = (self.cursor + delta) % len(rows)

    def connection_pane(self, key: str) -> Optional[str]:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-t",
                f"={WORKSPACE_SESSION}",
                "-F",
                "#{pane_id}" + FIELD_SEPARATOR + "#{@clustermux_key}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            pane_id, separator, pane_key = line.partition(FIELD_SEPARATOR)
            if separator and pane_key == key:
                return pane_id
        return None

    def create_persistent_pane(self, key: str, command: str, title: str) -> str:
        result = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                f"={WORKSPACE_SESSION}",
                "-n",
                "connection",
                command,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(compact_error(result.stderr))
        pane_id = result.stdout.strip()
        if not re.fullmatch(r"%\d+", pane_id):
            raise RuntimeError(f"tmux returned an invalid pane id: {pane_id!r}")
        subprocess.run(
            ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_key", key],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "select-pane",
                "-t",
                pane_id,
                "-T",
                title,
            ],
            check=False,
        )
        return pane_id

    def create_connection_pane(self, snapshot: Snapshot, session: Session, key: str) -> str:
        return self.create_persistent_pane(
            key,
            self.connection_command(snapshot, session),
            f"{snapshot.host.display_name}:{session.name}",
        )

    def connection_command(self, snapshot: Snapshot, session: Session) -> str:
        return shell_command(
            [*self_argv(), "--attach-only", snapshot.host.target, session.name, snapshot.host.display_name]
        )

    def shell_connection_command(self, snapshot: Snapshot) -> str:
        return shell_command(
            [*self_argv(), "--shell-only", snapshot.host.target, snapshot.host.display_name]
        )

    def pane_state(self, pane_id: str) -> str:
        return subprocess.run(
            ["tmux", "show-option", "-pqv", "-t", pane_id, "@clustermux_state"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

    def reconnect_pane(self, pane_id: str, snapshot: Snapshot, session: Session, key: str) -> None:
        result = subprocess.run(
            ["tmux", "respawn-pane", "-k", "-t", pane_id, self.connection_command(snapshot, session)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(compact_error(result.stderr))
        subprocess.run(
            ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_key", key],
            check=False,
        )

    def handoff_pane(self, pane_id: str, snapshot: Snapshot, session: Session) -> None:
        """Stop only the embedded display client and leave a handoff marker."""
        command = shell_command(
            [*self_argv(), "--pane-handoff", snapshot.host.display_name, session.name]
        )
        result = subprocess.run(
            ["tmux", "respawn-pane", "-k", "-t", pane_id, command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(compact_error(result.stderr))
        subprocess.run(
            ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_state", "handed-off"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", f"{snapshot.host.display_name}:{session.name} (tab)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def load_right_pane(self) -> None:
        selected = self.selected()
        if selected is None:
            self.message = "Selected node has no session"
            return
        snapshot, session = selected
        key = connection_key(snapshot.host, session)
        pane_id = self.connection_pane(key)
        reused = pane_id is not None
        reconnected = False
        try:
            if pane_id is None:
                pane_id = self.create_connection_pane(snapshot, session, key)
            elif self.pane_state(pane_id) in ("", "disconnected"):
                self.reconnect_pane(pane_id, snapshot, session, key)
                reconnected = True
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.message = f"Could not create persistent connection: {exc}"
            return

        current = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.target_pane, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if current != pane_id:
            result = subprocess.run(
                ["tmux", "swap-pane", "-s", pane_id, "-t", self.target_pane],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self.message = f"Could not show persistent connection: {compact_error(result.stderr)}"
                return
        subprocess.run(
            ["tmux", "select-pane", "-t", self.target_pane],
            check=False,
        )
        action = "Reconnected" if reconnected else ("Reused" if reused else "Opened")
        self.message = f"{action} persistent link · {snapshot.host.display_name}:{session.name}"

    def open_cluster_bash(self, snapshot: Snapshot, session: Optional[Session]) -> None:
        if session is not None:
            self.message = "b applies to a cluster row, not a session"
            return
        if snapshot.status not in ("online", "empty"):
            self.message = f"Node unavailable: {snapshot.host.display_name}"
            return
        shell_session = Session(name="__clustermux_bash__")
        key = connection_key(snapshot.host, shell_session)
        command = self.shell_connection_command(snapshot)
        pane_id = self.connection_pane(key)
        reused = pane_id is not None
        reconnected = False
        try:
            if pane_id is None:
                pane_id = self.create_persistent_pane(
                    key,
                    command,
                    f"{snapshot.host.display_name}:bash",
                )
            elif self.pane_state(pane_id) in ("", "disconnected"):
                result = subprocess.run(
                    ["tmux", "respawn-pane", "-k", "-t", pane_id, command],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(compact_error(result.stderr))
                subprocess.run(
                    ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_key", key],
                    check=False,
                )
                reconnected = True
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.message = f"Could not open cluster Bash: {exc}"
            return

        current = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.target_pane, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if current != pane_id:
            result = subprocess.run(
                ["tmux", "swap-pane", "-s", pane_id, "-t", self.target_pane],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self.message = f"Could not show cluster Bash: {compact_error(result.stderr)}"
                return
        subprocess.run(["tmux", "select-pane", "-t", self.target_pane], check=False)
        action = "Reconnected" if reconnected else ("Reused" if reused else "Opened")
        self.message = f"{action} persistent Bash · {snapshot.host.display_name}"

    def open_new_tab(self) -> None:
        selected = self.selected_row()
        if selected is None:
            self.message = "Selected node has no session"
            return
        snapshot, session = selected
        if session is None:
            self.message = "Select a session row for a new tab"
            return
        key = connection_key(snapshot.host, session)
        pane_id = self.connection_pane(key)
        if pane_id is not None:
            if self.pane_state(pane_id) == "handed-off":
                self.message = f"Already active in a tab · {snapshot.host.display_name}:{session.name}"
                return
            try:
                self.handoff_pane(pane_id, snapshot, session)
            except (OSError, RuntimeError) as exc:
                self.message = f"Handoff failed; tab not opened: {exc}"
                return
        try:
            open_session_tab(snapshot.host, session, pane_id)
            self.message = f"Handed off to tab · {snapshot.host.display_name}:{session.name}"
        except (OSError, RuntimeError) as exc:
            if pane_id is not None:
                try:
                    self.reconnect_pane(pane_id, snapshot, session, key)
                except RuntimeError:
                    pass
            self.message = f"New tab failed: {exc}"

    def prompt_text(self, message: str, prompt: str, initial: str = "") -> Optional[str]:
        value: List[str] = list(initial)
        self.message = message
        try:
            self.screen.timeout(-1)
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            while True:
                self.draw()
                height, width = self.screen.getmaxyx()
                y = max(0, height - 1)
                label = " " + prompt.strip() + " "
                available = max(1, width - len(label) - 1)
                visible = "".join(value)[-available:]
                self.screen.move(y, 0)
                self.screen.clrtoeol()
                self.add(y, 0, label + visible, curses.A_BOLD, width - 1)
                self.screen.move(y, min(width - 1, len(label) + len(visible)))
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\n", "\r"):
                    return "".join(value).strip()
                if key == "\x1b":
                    return None
                if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
                    if value:
                        value.pop()
                elif key == "\x15":
                    value.clear()
                elif isinstance(key, str) and key.isprintable() and len(value) < 128:
                    value.append(key)
        except (curses.error, KeyboardInterrupt):
            return None
        finally:
            self.screen.timeout(100)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def prompt_session_name(self, old_name: str) -> Optional[str]:
        return self.prompt_text(
            f"Rename {old_name} · Enter save · Esc cancel",
            "New name:",
        )

    def prompt_confirm(self, message: str) -> bool:
        self.message = message
        try:
            self.screen.timeout(-1)
            while True:
                self.draw()
                height, width = self.screen.getmaxyx()
                y = max(0, height - 1)
                self.screen.move(y, 0)
                self.screen.clrtoeol()
                self.add(y, 0, " " + message + "  [y/N] ", curses.A_BOLD, width - 1)
                self.screen.refresh()
                key = self.screen.getch()
                if key in (ord("y"), ord("Y")):
                    return True
                if key in (ord("n"), ord("N"), 27):
                    return False
        except (curses.error, KeyboardInterrupt):
            return False
        finally:
            self.screen.timeout(100)

    def rename_selected(self) -> None:
        rows = self.rows()
        if not rows:
            self.message = "No tmux session selected"
            return
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        kind, host_index, session_index = rows[self.cursor]
        if kind != "session" or session_index is None:
            self.message = "Select a session row before renaming"
            return

        snapshot = self.snapshots[host_index]
        session = snapshot.sessions[session_index]
        old_name = session.name
        new_name = self.prompt_session_name(old_name)
        if not new_name or new_name == old_name:
            self.message = "Rename cancelled"
            return

        self.message = f"Renaming {old_name} -> {new_name}…"
        self.draw()
        timeout = snapshot.host.connect_timeout or self.timeout
        remote_command = shell_command(
            ["tmux", "rename-session", "-t", "=" + old_name, new_name]
        )
        try:
            result = subprocess.run(
                ssh_argv(snapshot.host, self.timeout) + [remote_command],
                capture_output=True,
                text=True,
                timeout=timeout + 8,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.message = f"Rename timed out on {snapshot.host.display_name}"
            return
        except OSError as exc:
            self.message = f"Rename failed: {exc}"
            return
        if result.returncode != 0:
            self.message = f"Rename failed: {compact_error(result.stderr)}"
            return

        old_key = connection_key(snapshot.host, session)
        pane_id = self.connection_pane(old_key)
        session.name = new_name
        new_key = connection_key(snapshot.host, session)
        if pane_id is not None:
            subprocess.run(
                ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_key", new_key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["tmux", "select-pane", "-t", pane_id, "-T", f"{snapshot.host.display_name}:{new_name}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        snapshot.sessions.sort(key=lambda item: natural_key(item.name))
        self.restore_selection((snapshot.host.target, new_name))
        # Queue a fresh query even if an older automatic refresh is still in
        # flight, otherwise that older result could briefly restore the old
        # session name in the navigator.
        self.future = self.executor.submit(query_all, self.hosts, self.timeout)
        self.message = f"Renamed {old_name} -> {new_name}"

    def discard_connection_pane(self, snapshot: Snapshot, session: Session) -> None:
        """Tear down the local persistent pane tied to a killed session."""
        pane_id = self.connection_pane(connection_key(snapshot, session))
        if pane_id is None:
            return
        window = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{window_name}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if window == "hub":
            # The pane is on display in the right slot; reset it to idle so
            # the two-pane workspace layout stays intact.
            idle_command = shell_command([*self_argv(), "--pane-idle"])
            subprocess.run(
                ["tmux", "respawn-pane", "-k", "-t", pane_id, idle_command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["tmux", "set-option", "-p", "-u", "-t", pane_id, "@clustermux_key"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["tmux", "select-pane", "-t", pane_id, "-T", "Remote tmux"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def kill_selected(self) -> None:
        rows = self.rows()
        if not rows:
            self.message = "No tmux session selected"
            return
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        kind, host_index, session_index = rows[self.cursor]
        if kind != "session" or session_index is None:
            self.message = "Select a session row before killing"
            return
        snapshot = self.snapshots[host_index]
        if snapshot.status not in ("online", "empty"):
            self.message = f"Node unavailable: {snapshot.host.display_name}"
            return
        session = snapshot.sessions[session_index]
        name = session.name
        if not self.prompt_confirm(f"Kill {snapshot.host.label}:{name}?"):
            self.message = "Kill cancelled"
            return

        self.message = f"Killing {snapshot.host.display_name}:{name}…"
        self.draw()
        timeout = snapshot.host.connect_timeout or self.timeout
        remote_command = shell_command(["tmux", "kill-session", "-t", "=" + name])
        try:
            result = subprocess.run(
                ssh_argv(snapshot.host, self.timeout) + [remote_command],
                capture_output=True,
                text=True,
                timeout=timeout + 8,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.message = f"Kill timed out on {snapshot.host.display_name}"
            return
        except OSError as exc:
            self.message = f"Kill failed: {exc}"
            return
        if result.returncode != 0:
            self.message = f"Kill failed: {compact_error(result.stderr)}"
            return

        self.discard_connection_pane(snapshot, session)
        snapshot.sessions = [item for item in snapshot.sessions if item.name != name]
        self.cursor = max(0, min(self.cursor, len(self.rows()) - 1))
        self.queue_fresh_snapshot()
        self.message = f"Killed {snapshot.host.display_name}:{name}"

    def selected_row(self) -> Optional[Tuple[Snapshot, Optional[Session]]]:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        kind, host_index, session_index = rows[self.cursor]
        snapshot = self.snapshots[host_index]
        if kind == "session" and session_index is not None:
            return snapshot, snapshot.sessions[session_index]
        return snapshot, None

    def queue_fresh_snapshot(self) -> None:
        # A new query is deliberately queued even when an automatic refresh is
        # already running, so stale results cannot undo an immediately visible
        # create/rename result.
        self.future = self.executor.submit(query_all, self.hosts, self.timeout)

    def remember_new_session(
        self,
        snapshot: Snapshot,
        name: str,
        command: str,
        path: str,
    ) -> None:
        if not any(item.name == name for item in snapshot.sessions):
            snapshot.sessions.append(
                Session(name=name, windows=1, command=command, path=path)
            )
        snapshot.status = "online"
        snapshot.sessions.sort(key=lambda item: natural_key(item.name))
        self.restore_selection((snapshot.host.target, name))
        self.queue_fresh_snapshot()

    def create_empty_tmux(self, snapshot: Snapshot, session: Optional[Session]) -> None:
        if session is not None:
            self.message = "t applies to a cluster row, not a session"
            return
        if snapshot.status not in ("online", "empty"):
            self.message = f"Node unavailable: {snapshot.host.display_name}"
            return
        default_name = "shell-" + datetime.now().strftime("%m%d-%H%M")
        name = self.prompt_text(
            f"New empty tmux on {snapshot.host.display_name} · Ctrl-u clears",
            "Session name:",
            default_name,
        )
        if not name:
            self.message = "Create tmux cancelled"
            return
        start_path = ""
        args: List[object] = ["tmux", "new-session", "-d", "-s", name]
        if start_path:
            args.extend(["-c", start_path])
        args.append("bash")
        self.message = f"Creating tmux {name}…"
        self.draw()
        try:
            result = run_remote_command(snapshot.host, self.timeout, shell_command(args))
        except subprocess.TimeoutExpired:
            self.message = f"tmux creation timed out on {snapshot.host.display_name}"
            return
        except OSError as exc:
            self.message = f"tmux creation failed: {exc}"
            return
        if result.returncode != 0:
            self.message = f"tmux creation failed: {command_error(result)}"
            return
        self.remember_new_session(snapshot, name, "bash", start_path)
        self.message = f"Created empty tmux · {snapshot.host.display_name}:{name}"

    def fork_codex_tmux(self, snapshot: Snapshot, session: Optional[Session]) -> None:
        if session is None:
            self.message = "Select a Codex session row before forking"
            return
        if session.command != "codex":
            self.message = f"Selected pane is {session.command or 'not Codex'} · cannot fork"
            return
        if not session.pane_pid.isdigit():
            self.message = "Codex pane PID unavailable · refresh and try again"
            return
        default_name = f"{session.name}-fork-{datetime.now().strftime('%H%M')}"
        name = self.prompt_text(
            f"Fork Codex from {session.name} · Ctrl-u clears",
            "New tmux:",
            default_name,
        )
        if not name:
            self.message = "Fork cancelled"
            return

        preamble = (
            "root_pid=" + shlex.quote(session.pane_pid) + "\n"
            "new_name=" + shlex.quote(name) + "\n"
            "start_dir=" + shlex.quote(session.path) + "\n"
        )
        script = preamble + r'''
if tmux has-session -t "=$new_name" 2>/dev/null; then
  printf 'ERROR|tmux session already exists: %s\n' "$new_name"
  exit 2
fi

latest_mtime=-1
latest_file=
latest_codex_pid=
latest_tied=0
queue="$root_pid"
while [ -n "$queue" ]; do
  set -- $queue
  proc_pid="$1"
  shift
  queue="$*"
  comm=$(cat "/proc/$proc_pid/comm" 2>/dev/null || true)
  case "$comm" in
    codex|codex-*)
      for fd in /proc/$proc_pid/fd/*; do
        file=$(readlink "$fd" 2>/dev/null || true)
        case "$file" in
          *rollout-*.jsonl)
            if head -n 1 "$file" 2>/dev/null | grep -Eq '"thread_source"[[:space:]]*:[[:space:]]*"user"' &&
               { [ -z "$start_dir" ] || head -n 1 "$file" 2>/dev/null | grep -Fq "\"cwd\":\"$start_dir\""; }; then
              mtime=$(stat -c '%Y' "$file" 2>/dev/null || printf '0')
              if [ "$mtime" -gt "$latest_mtime" ]; then
                latest_mtime="$mtime"
                latest_file="$file"
                latest_codex_pid="$proc_pid"
                latest_tied=0
              elif [ "$mtime" -eq "$latest_mtime" ] && [ "$file" != "$latest_file" ]; then
                latest_tied=1
              fi
            fi
            ;;
        esac
      done
      ;;
  esac
  children=$(pgrep -P "$proc_pid" 2>/dev/null || true)
  queue="$queue $children"
done

if [ "$latest_tied" -eq 1 ]; then
  printf 'ERROR|multiple active Codex contexts are ambiguous\n'
  exit 3
fi
thread_id=$(basename "$latest_file" 2>/dev/null | grep -Eo '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | tail -n 1)
if ! [[ "$thread_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  printf 'ERROR|could not identify the selected Codex context\n'
  exit 3
fi

codex_bin=$(bash -ic 'type -P codex' 2>/dev/null | tail -n 1)
if [ -z "$codex_bin" ] && [ -n "$latest_codex_pid" ]; then
  codex_bin=$(readlink "/proc/$latest_codex_pid/exe" 2>/dev/null || true)
fi
if [ ! -x "$codex_bin" ] || ! "$codex_bin" fork --help >/dev/null 2>&1; then
  printf 'ERROR|this node does not provide codex fork\n'
  exit 4
fi

printf -v inner 'exec %q fork %q' "$codex_bin" "$thread_id"
printf -v launch 'bash -ic %q' "$inner"
if [ -n "$start_dir" ] && [ -d "$start_dir" ]; then
  tmux new-session -d -s "$new_name" -c "$start_dir" "$launch"
else
  tmux new-session -d -s "$new_name" "$launch"
fi
printf 'OK|%s\n' "$thread_id"
'''
        self.message = f"Forking Codex {session.name} -> {name}…"
        self.draw()
        try:
            result = run_remote_command(
                snapshot.host,
                self.timeout,
                encoded_bash_command(script),
            )
        except subprocess.TimeoutExpired:
            self.message = f"Codex fork timed out on {snapshot.host.display_name}"
            return
        except OSError as exc:
            self.message = f"Codex fork failed: {exc}"
            return
        if result.returncode != 0:
            self.message = f"Codex fork failed: {command_error(result)}"
            return
        thread_lines = [line for line in result.stdout.splitlines() if line.startswith("OK|")]
        if not thread_lines:
            self.message = "Codex fork failed: missing confirmation"
            return
        self.remember_new_session(snapshot, name, "codex", session.path)
        self.message = f"Forked Codex · {session.name} -> {name}"

    def close_workspace(self) -> None:
        # Killing the local workspace only disconnects its SSH client.  The
        # remote tmux server and every command inside it continue running.
        subprocess.Popen(
            ["tmux", "kill-session", "-t", f"={WORKSPACE_SESSION}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(5, curses.COLOR_CYAN, -1)
        self.start_refresh()
        try:
            while True:
                self.poll()
                self.draw()
                key = self.screen.getch()
                if key in (ord("q"), ord("Q")):
                    self.close_workspace()
                    break
                if key in (curses.KEY_UP, ord("k")):
                    self.move(-1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    self.move(1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    self.load_right_pane()
                elif key in (ord("b"), ord("B")):
                    selected = self.selected_row()
                    if selected is not None:
                        self.open_cluster_bash(*selected)
                elif key in (ord("t"), ord("T")):
                    selected = self.selected_row()
                    if selected is not None:
                        self.create_empty_tmux(*selected)
                elif key in (ord("f"), ord("F")):
                    selected = self.selected_row()
                    if selected is not None:
                        self.fork_codex_tmux(*selected)
                elif key in (ord("x"), ord("X")):
                    self.kill_selected()
                elif key in (ord("o"), ord("O")):
                    self.open_new_tab()
                elif key in (ord("e"), ord("E")):
                    self.rename_selected()
                elif key in (ord("r"), ord("R")):
                    self.start_refresh()
                elif key == curses.KEY_RESIZE:
                    self.normalize_layout()
        finally:
            self.executor.shutdown(wait=False)


def set_own_pane_state(state: str) -> None:
    own_pane = os.environ.get("TMUX_PANE")
    if own_pane:
        subprocess.run(
            ["tmux", "set-option", "-p", "-t", own_pane, "@clustermux_state", state],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def static_pane(title: str, lines: Sequence[str]) -> int:
    def draw_static(screen) -> None:
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(500)
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            block = [title, "", *lines]
            start_y = max(1, (height - len(block)) // 3)
            for offset, line in enumerate(block):
                x = max(1, (width - len(line)) // 2)
                attr = curses.A_BOLD if offset == 0 else 0
                try:
                    screen.addnstr(start_y + offset, x, line, max(1, width - x - 1), attr)
                except curses.error:
                    pass
            screen.refresh()
            screen.getch()

    try:
        curses.wrapper(draw_static)
    except KeyboardInterrupt:
        pass
    return 0


def run_attached_pane(target: str, session_name: str, display_name: str) -> int:
    host = Host(group="REMOTE", label=display_name, target=target)
    session = Session(name=session_name)
    set_own_pane_state("connected")
    print(f"Connecting to {display_name} · tmux {session_name}…", flush=True)
    result = subprocess.run(
        ssh_argv(host, 12, tty=True) + ["tmux attach-session -t " + shlex.quote(session.name)],
        check=False,
    )
    set_own_pane_state("disconnected")
    return static_pane(
        "REMOTE TMUX DISCONNECTED",
        (
            f"{display_name} · session {session_name} · ssh exit {result.returncode}",
            "Return to the navigator with Shift+Left.",
            "Press Enter on this session to reconnect.",
        ),
    )


def update_handoff_state(pane_id: str, state: str) -> None:
    """Update the manager placeholder if the original local pane still exists."""
    if not re.fullmatch(r"%\d+", pane_id):
        return
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, "@clustermux_state", state],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run_tab_attach(
    target: str,
    session_name: str,
    display_name: str,
    timeout_value: str,
    handoff_pane: str,
) -> int:
    """Supervise a full-tab remote tmux client and survive transient SSH loss."""
    try:
        timeout = max(1, int(timeout_value))
    except ValueError:
        timeout = 12
    pane_id = handoff_pane if re.fullmatch(r"%\d+", handoff_pane) else ""
    host = Host(group="REMOTE", label=display_name, target=target, connect_timeout=timeout)
    remote = "tmux attach-session -t " + shlex.quote(session_name)

    def stop_on_signal(signum, _frame) -> None:
        raise SystemExit(128 + signum)

    for signame in ("SIGHUP", "SIGTERM"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            signal.signal(signum, stop_on_signal)

    if pane_id:
        update_handoff_state(pane_id, "handed-off")
    try:
        while True:
            print(f"Connecting to {display_name} · tmux {session_name}…", flush=True)
            try:
                result = subprocess.run(
                    ssh_argv(host, timeout, tty=True) + [remote],
                    check=False,
                )
            except KeyboardInterrupt:
                print("\nTab connection stopped.", flush=True)
                return 130

            if result.returncode == 255:
                print(
                    "\nSSH connection was interrupted; reconnecting in 3 seconds. "
                    "Press Ctrl-C to stop.",
                    flush=True,
                )
                try:
                    time.sleep(3)
                except KeyboardInterrupt:
                    print("\nAutomatic reconnect stopped.", flush=True)
                    return 130
                continue

            reason = "detached" if result.returncode == 0 else f"ended with ssh exit {result.returncode}"
            print(f"\nRemote tmux {reason}.", flush=True)
            try:
                answer = input("Press Enter to reconnect, or type q then Enter to leave this tab: ")
            except (EOFError, KeyboardInterrupt):
                print()
                return result.returncode
            if answer.strip().lower() == "q":
                return result.returncode
    finally:
        if pane_id:
            update_handoff_state(pane_id, "disconnected")


def run_handoff_pane(display_name: str, session_name: str) -> int:
    set_own_pane_state("handed-off")

    def draw_handoff(screen) -> None:
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(500)
        while True:
            own_pane = os.environ.get("TMUX_PANE", "")
            state = ""
            if own_pane:
                state = subprocess.run(
                    ["tmux", "show-option", "-pqv", "-t", own_pane, "@clustermux_state"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
            active = state == "handed-off"
            title = "REMOTE TMUX HANDED OFF" if active else "REMOTE TAB CLOSED"
            lines = (
                f"{display_name} · session {session_name}",
                "This session is active in its standalone iTerm tab."
                if active
                else "The standalone tab is no longer connected.",
                "Use that tab for input and scrollback."
                if active
                else "Return left with Shift+Left, then press Enter to reconnect here.",
            )
            screen.erase()
            height, width = screen.getmaxyx()
            block = [title, "", *lines]
            start_y = max(1, (height - len(block)) // 3)
            for offset, line in enumerate(block):
                x = max(1, (width - len(line)) // 2)
                attr = curses.A_BOLD if offset == 0 else 0
                try:
                    screen.addnstr(start_y + offset, x, line, max(1, width - x - 1), attr)
                except curses.error:
                    pass
            screen.refresh()
            screen.getch()

    try:
        curses.wrapper(draw_handoff)
    except KeyboardInterrupt:
        pass
    return 0


def run_shell_pane(target: str, display_name: str) -> int:
    host = Host(group="REMOTE", label=display_name, target=target)
    set_own_pane_state("connected")
    print(f"Connecting to {display_name} · Bash…", flush=True)
    result = subprocess.run(
        ssh_argv(host, 12, tty=True) + ["exec bash -il"],
        check=False,
    )
    set_own_pane_state("disconnected")
    return static_pane(
        "REMOTE BASH DISCONNECTED",
        (
            f"{display_name} · ssh exit {result.returncode}",
            "Return to the navigator with Shift+Left.",
            "Select the cluster row and press b to reconnect.",
        ),
    )


def run_idle_pane() -> int:
    set_own_pane_state("idle")
    return static_pane(
        "CLUSTERMUX · REMOTE TERMINAL",
        (
            "Choose a cluster or tmux session on the left.",
            "Cluster: b Bash / t tmux    Session: Enter / f fork / x kill",
            "Shift+Left: return to sidebar    remote tmux: Ctrl-b",
        ),
    )


def tmux_checked(args: Sequence[str]) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(compact_error(result.stderr))
    return result.stdout.strip()


def workspace_layout_healthy() -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={WORKSPACE_SESSION}:hub", "-F", "#{pane_index}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and set(result.stdout.split()) >= {"0", "1"}


def setup_workspace(config: Path, timeout: int, refresh_seconds: int) -> int:
    workspace_exists = subprocess.run(
        ["tmux", "has-session", "-t", f"={WORKSPACE_SESSION}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if workspace_exists:
        version = subprocess.run(
            ["tmux", "show-option", "-qv", "-t", WORKSPACE_SESSION, "@clustermux_version"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if version != "4" or not workspace_layout_healthy():
            # This only closes the obsolete local manager/SSH clients. Remote
            # tmux sessions and the commands inside them remain alive.
            subprocess.run(["tmux", "kill-session", "-t", f"={WORKSPACE_SESSION}"], check=False)
            workspace_exists = False
    if not workspace_exists:
        idle_command = shell_command([*self_argv(), "--pane-idle"])
        left_pane = tmux_checked(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-x",
                "160",
                "-y",
                "42",
                "-s",
                WORKSPACE_SESSION,
                "-n",
                "hub",
                idle_command,
            ]
        )
        right_pane = tmux_checked(
            ["split-window", "-h", "-p", "72", "-P", "-F", "#{pane_id}", "-t", left_pane, idle_command]
        )
        option_commands = [
            ["set-option", "-t", WORKSPACE_SESSION, "prefix", "C-a"],
            ["set-option", "-t", WORKSPACE_SESSION, "status-style", "bg=colour24,fg=white"],
            ["set-option", "-t", WORKSPACE_SESSION, "status-left", " CLUSTERMUX "],
            ["set-option", "-t", WORKSPACE_SESSION, "status-right", " Shift+Left: sidebar · remote tmux: C-b "],
            ["set-option", "-t", WORKSPACE_SESSION, "pane-border-status", "top"],
            ["set-option", "-t", WORKSPACE_SESSION, "pane-border-format", " #{pane_title} "],
            ["set-option", "-p", "-t", left_pane, "remain-on-exit", "on"],
            ["select-pane", "-t", left_pane, "-T", "Navigator"],
            ["select-pane", "-t", right_pane, "-T", "Remote tmux"],
            ["select-pane", "-t", left_pane],
        ]
        for command in option_commands:
            tmux_checked(command)
        right_slot = f"={WORKSPACE_SESSION}:hub.1"
        navigator_command = shell_command(
            [
                *self_argv(),
                "--navigator",
                right_slot,
                "--config",
                config,
                "--timeout",
                timeout,
                "--refresh",
                refresh_seconds,
            ]
        )
        tmux_checked(["respawn-pane", "-k", "-t", left_pane, navigator_command])
        tmux_checked(["set-option", "-t", WORKSPACE_SESSION, "@clustermux_version", "4"])
    # tmux key tables are server-wide, so keep Shift+Left transparent outside
    # clustermux. There is deliberately no matching Shift+Right binding.
    subprocess.run(["tmux", "unbind-key", "-n", "F1"], check=False)
    subprocess.run(["tmux", "unbind-key", "-n", "C-Left"], check=False)
    tmux_checked(
        [
            "bind-key",
            "-n",
            "S-Left",
            "if-shell",
            "-F",
            f"#{{==:#{{session_name}},{WORKSPACE_SESSION}}}",
            f"select-pane -t ={WORKSPACE_SESSION}:hub.0",
            "send-keys S-Left",
        ]
    )
    tmux_checked(
        ["set-option", "-t", WORKSPACE_SESSION, "status-right", " Shift+Left: sidebar · remote tmux: C-b "]
    )
    # A persistent workspace remembers whichever pane was active when its last
    # client detached. Always reopen on the navigator so arrow keys work
    # immediately, even if the previous interaction ended in the remote pane.
    tmux_checked(["select-pane", "-t", f"={WORKSPACE_SESSION}:hub.0"])
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={WORKSPACE_SESSION}"])
    return 0


def open_workspace_tab(config: Path, timeout: int, refresh_seconds: int) -> None:
    command = shell_command(
        [*self_argv(), "--workspace", "--config", config, "--timeout", timeout, "--refresh", refresh_seconds]
    )
    open_iterm_tab(command, "CLUSTERMUX")


def print_list(hosts: Sequence[Host], timeout: int) -> int:
    snapshots = query_all(hosts, timeout)
    print(f"{'NODE':<24} {'STATUS':<9} {'SESSION':<12} {'COMMAND':<12} PATH")
    print("-" * 100)
    for snapshot in snapshots:
        if not snapshot.sessions:
            detail = snapshot.error if snapshot.error else "no tmux sessions"
            print(f"{snapshot.host.display_name:<24} {snapshot.status:<9} {'-':<12} {'-':<12} {detail}")
            continue
        for index, session in enumerate(snapshot.sessions):
            node = snapshot.host.display_name if index == 0 else ""
            print(f"{node:<24} {snapshot.status:<9} {session.name:<12.12} {session.command:<12.12} {session.path}")
    return 0


def ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return default if not answer else answer in ("y", "yes")


def ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return answer or default


def find_public_key() -> Optional[Path]:
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub", "id_dsa.pub"):
        candidate = Path.home() / ".ssh" / name
        if candidate.is_file():
            return candidate
    return None


def generate_ssh_key() -> Optional[Path]:
    key_path = Path.home() / ".ssh" / "id_ed25519"
    print("Running ssh-keygen — choose a passphrase, or press Enter twice for none.")
    result = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(key_path)], check=False)
    return find_public_key() if result.returncode == 0 else None


def install_key_on_host(target: str, pubkey: Path) -> bool:
    if shutil.which("ssh-copy-id"):
        return subprocess.run(["ssh-copy-id", "-i", str(pubkey), target], check=False).returncode == 0
    remote = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    try:
        key_text = pubkey.read_text().strip() + "\n"
    except OSError:
        return False
    return subprocess.run(["ssh", target, remote], input=key_text, text=True, check=False).returncode == 0


def ssh_config_files() -> List[Path]:
    """The user ssh_config plus any files pulled in via Include directives."""
    found: List[Path] = []

    def visit(path: Path, depth: int) -> None:
        if depth > 4 or path in found:
            return
        found.append(path)
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("include "):
                for pattern in stripped.split()[1:]:
                    if pattern.startswith("~"):
                        pattern = str(Path.home()) + pattern[1:]
                    candidate = Path(pattern)
                    if not candidate.is_absolute():
                        candidate = path.parent / candidate
                    for match in sorted(glob.glob(str(candidate))):
                        visit(Path(match), depth + 1)

    visit(Path.home() / ".ssh" / "config", 0)
    return found


def discover_ssh_aliases() -> List[str]:
    """Concrete Host aliases from ssh config; wildcard and negated patterns are skipped."""
    aliases: List[str] = []
    for path in ssh_config_files():
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0].lower() != "host":
                continue
            for pattern in parts[1:]:
                if any(mark in pattern for mark in "*?!"):
                    continue
                if pattern not in aliases:
                    aliases.append(pattern)
    return aliases


def discover_known_hosts() -> List[str]:
    """Plain-text hostnames from known_hosts; hashed entries cannot be recovered."""
    hosts: List[str] = []
    try:
        lines = (Path.home() / ".ssh" / "known_hosts").read_text().splitlines()
    except OSError:
        return hosts
    for line in lines:
        if not line or line[0] in "#@|":
            continue
        for token in line.split(" ", 1)[0].split(","):
            token = token.strip()
            if not token:
                continue
            bracketed = re.fullmatch(r"\[([^\]]+)\]:\d+", token)
            name = bracketed.group(1) if bracketed else token
            if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in hosts:
                hosts.append(name)
    return hosts


def resolve_alias(alias: str) -> str:
    """Resolve an ssh config alias to user@hostname for display and dedup."""
    try:
        result = subprocess.run(
            ["ssh", "-G", alias], capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    user = hostname = ""
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "user":
            user = value.strip()
        elif key == "hostname":
            hostname = value.strip()
    if not hostname:
        return ""
    return f"{user}@{hostname}" if user else hostname


def probe_ssh_target(target: str, timeout: int = 6) -> str:
    """Classify BatchMode SSH reachability: ok / auth (key rejected) / unreachable."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "LogLevel=ERROR",
                target,
                "true",
            ],
            capture_output=True,
            text=True,
            # ConnectTimeout only bounds TCP setup; slow links can need much
            # longer for the handshake and auth negotiation to finish.
            timeout=timeout + 20,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unreachable"
    if result.returncode == 0:
        return "ok"
    return "auth" if "permission denied" in result.stderr.lower() else "unreachable"


def default_group_label(target: str) -> Tuple[str, str]:
    alias = target.split("@", 1)[-1]
    head, separator, tail = alias.partition("-")
    if separator and head and tail:
        return head.split(".")[0].upper()[:16], tail.split(".")[0][:24]
    return alias.split(".")[0].upper()[:16], "login"


def cmd_init(config_path: Path, timeout: int) -> int:
    print("clustermux init — scan your SSH setup and build a hosts file\n")

    print("[1/5] SSH key")
    pubkey = find_public_key()
    if pubkey:
        print(f"  Using existing key: {pubkey}")
    else:
        print("  No SSH key found in ~/.ssh (id_ed25519.pub, id_ecdsa.pub, id_rsa.pub).")
        if ask_yes_no("  Generate a new ed25519 key now?"):
            pubkey = generate_ssh_key()
        if pubkey is None:
            print("  WARNING: without a key, hosts that need one will not connect.")

    print("\n[2/5] Discover hosts")
    existing: List[dict] = []
    existing_targets = set()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("target"):
                        existing.append(item)
                        existing_targets.add(str(item["target"]))
            print(f"  Existing config: {len(existing)} host(s) — new discoveries will be merged in.")
        except (OSError, json.JSONDecodeError):
            print(f"  WARNING: existing {config_path} is not valid JSON; it will be backed up.")

    discovered: List[Tuple[str, str]] = []
    for alias in discover_ssh_aliases():
        discovered.append((alias, "ssh config"))
    resolved_hostnames = set()
    for alias, _source in discovered:
        resolved = resolve_alias(alias)
        if resolved:
            resolved_hostnames.add(resolved.split("@", 1)[-1])
    alias_targets = {target for target, _ in discovered}
    for name in discover_known_hosts():
        if name not in resolved_hostnames and name not in alias_targets:
            discovered.append((name, "known_hosts"))
    new_discoveries = [item for item in discovered if item[0] not in existing_targets]
    if not discovered:
        print("  No Host entries in ssh config and no usable known_hosts entries.")
    else:
        print(f"  Found {len(discovered)} candidate(s), {len(new_discoveries)} not yet in the config.")

    print("\n[3/5] Probe connectivity (BatchMode, in parallel)")
    statuses: List[str] = []
    if new_discoveries:
        workers = min(16, len(new_discoveries))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(lambda item: probe_ssh_target(item[0], timeout), new_discoveries))
        marks = {"ok": "ok  ", "auth": "auth", "unreachable": "down"}
        for (target, source), status in zip(new_discoveries, statuses):
            note = " — from known_hosts, verify the username" if source == "known_hosts" else ""
            print(f"  [{marks[status]}] {target}{note}")
    else:
        print("  Nothing new to probe.")

    print("\n[4/5] Key installation")
    auth_hosts = [target for (target, _), status in zip(new_discoveries, statuses) if status == "auth"]
    if not auth_hosts:
        print("  Every reachable host already accepts your key.")
    elif pubkey is None:
        print(f"  {len(auth_hosts)} host(s) reject key auth, but you have no key to install.")
    else:
        for index, (target, _source) in enumerate(new_discoveries):
            if statuses[index] != "auth":
                continue
            if ask_yes_no(f"  {target} rejects your key. Install it (you will be asked for the password)?"):
                if install_key_on_host(target, pubkey):
                    statuses[index] = probe_ssh_target(target, timeout)
                    print(f"  Key installed on {target}." if statuses[index] == "ok" else f"  Installed, but {target} still does not connect.")
                else:
                    print(f"  Key installation failed on {target}.")

    print(f"\n[5/5] Write {config_path}")
    entries = list(existing)
    for (target, _source), status in zip(new_discoveries, statuses):
        if status == "auth":
            include = ask_yes_no(f"  {target} still rejects your key; include it anyway?", default=False)
        elif status != "ok":
            include = ask_yes_no(f"  {target} is not reachable now; include it anyway?", default=False)
        else:
            include = True
        if not include:
            continue
        group, label = default_group_label(target)
        entries.append({"group": group, "label": label, "target": target})
        print(f"  + {group} / {label}  ({target})")
    while ask_yes_no("  Add another host manually?", default=False):
        target = ask_text("  SSH target (alias or user@host)")
        if not target or target in {entry["target"] for entry in entries}:
            continue
        group, label = default_group_label(target)
        entries.append({
            "group": ask_text("  Group", group),
            "label": ask_text("  Label", label),
            "target": target,
        })
    if not entries:
        print("Nothing to write — no hosts selected.")
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        backup = config_path.with_suffix(".json.bak")
        backup.write_text(config_path.read_text())
        print(f"  Previous config backed up to {backup}")
    config_path.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"  Wrote {len(entries)} host(s) to {config_path}")

    editor = os.environ.get("EDITOR")
    if editor and ask_yes_no(f"  Review it in {editor} now?", default=False):
        subprocess.run([editor, str(config_path)], check=False)
    try:
        load_hosts(config_path)
    except SystemExit as exc:
        print(f"  WARNING: {exc}")
        return 1
    print("\nDone. Next: clustermux --check  ·  clustermux")
    return 0


def cmd_check(config_path: Path, timeout: int) -> int:
    failures = 0

    def report(good: bool, label: str, hint: str = "") -> None:
        nonlocal failures
        print(f"  [{'ok' if good else 'FAIL'}] {label}" + (f" — {hint}" if hint and not good else ""))
        if not good:
            failures += 1

    print("Local environment")
    py = sys.version_info
    report(py >= (3, 9), f"Python {py.major}.{py.minor}.{py.micro}", "clustermux needs Python >= 3.9")
    tmux = shutil.which("tmux")
    if tmux:
        version = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False).stdout.strip()
        report(True, f"tmux ({version or 'found'})")
    else:
        report(False, "tmux not found", "brew install tmux / apt install tmux")
    report(shutil.which("ssh") is not None, "ssh client")
    if sys.platform == "darwin":
        iterm = Path("/Applications/iTerm.app").is_dir() or (Path.home() / "Applications" / "iTerm.app").is_dir()
        report(iterm, "iTerm2 (needed for tab features)", "install from https://iterm2.com, or use --workspace/--here")
    report(find_public_key() is not None, "SSH key present", "run clustermux --init to create one")

    print("\nConfiguration")
    hosts: List[Host] = []
    if not config_path.exists():
        report(False, f"{config_path} missing", "run clustermux --init")
    else:
        try:
            hosts = load_hosts(config_path)
            report(True, f"{config_path} — {len(hosts)} host(s)")
        except SystemExit as exc:
            report(False, str(exc))

    if hosts:
        print("\nRemote hosts")
        for snapshot in query_all(hosts, timeout):
            if snapshot.status == "online":
                latency = f", {snapshot.latency:.1f}s" if snapshot.latency is not None else ""
                print(f"  [ok  ] {snapshot.host.display_name} — {len(snapshot.sessions)} session(s){latency}")
            elif snapshot.status == "empty":
                print(f"  [ok  ] {snapshot.host.display_name} — reachable, no tmux sessions")
            else:
                print(f"  [warn] {snapshot.host.display_name} — {snapshot.status}: {snapshot.error}")
        print("\nOffline hosts do not fail the check; they may simply be down.")

    if failures:
        print(f"\n{failures} problem(s) found.")
        return 1
    print("\nAll checks passed.")
    return 0


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description="Manage tmux sessions across SSH hosts without replacing the current terminal tab."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="host JSON file")
    parser.add_argument(
        "--init",
        action="store_true",
        help="first-time setup: scan ssh config/known_hosts, install your key, write the hosts file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify local requirements and probe every configured host, then exit",
    )
    parser.add_argument("--list", action="store_true", help="print one snapshot without starting the TUI")
    parser.add_argument("--here", action="store_true", help="run the full-screen preview in this terminal")
    parser.add_argument("--workspace", action="store_true", help="run the split navigator/terminal workspace here")
    parser.add_argument("--timeout", type=int, default=8, help="SSH connection timeout in seconds")
    parser.add_argument("--refresh", type=int, default=30, help="automatic refresh interval; 0 disables")
    parser.add_argument("--no-preview", action="store_true", help="do not capture recent tmux pane output")
    parser.add_argument("--navigator", metavar="PANE", help=argparse.SUPPRESS)
    parser.add_argument("--attach-only", nargs=3, metavar=("HOST", "SESSION", "LABEL"), help=argparse.SUPPRESS)
    parser.add_argument(
        "--tab-attach",
        nargs=5,
        metavar=("HOST", "SESSION", "LABEL", "TIMEOUT", "PANE"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--pane-handoff", nargs=2, metavar=("LABEL", "SESSION"), help=argparse.SUPPRESS)
    parser.add_argument("--shell-only", nargs=2, metavar=("HOST", "LABEL"), help=argparse.SUPPRESS)
    parser.add_argument("--pane-idle", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.refresh < 0:
        parser.error("--timeout must be positive and --refresh cannot be negative")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args(argv)
    if args.pane_idle:
        return run_idle_pane()
    if args.attach_only:
        return run_attached_pane(*args.attach_only)
    if args.tab_attach:
        return run_tab_attach(*args.tab_attach)
    if args.pane_handoff:
        return run_handoff_pane(*args.pane_handoff)
    if args.shell_only:
        return run_shell_pane(*args.shell_only)
    if args.init:
        return cmd_init(args.config, args.timeout)
    if args.check:
        return cmd_check(args.config, args.timeout)
    hosts = load_hosts(args.config)
    if args.list:
        return print_list(hosts, args.timeout)
    if args.navigator:
        if not (
            re.fullmatch(r"%\d+", args.navigator)
            or args.navigator == f"={WORKSPACE_SESSION}:hub.1"
        ):
            raise SystemExit(f"clustermux: invalid tmux target pane: {args.navigator!r}")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("clustermux: navigator needs an interactive terminal")

        def run_navigator(screen) -> None:
            Navigator(screen, hosts, args.navigator, args.timeout, args.refresh).run()

        curses.wrapper(run_navigator)
        return 0
    if args.workspace:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("clustermux: workspace needs an interactive terminal")
        return setup_workspace(args.config, args.timeout, args.refresh)
    if not args.here:
        try:
            open_workspace_tab(args.config, args.timeout, args.refresh)
        except (OSError, RuntimeError) as exc:
            raise SystemExit(f"clustermux: could not open a new iTerm tab: {exc}\nUse 'clustermux --workspace' to run here.")
        print("clustermux: opened the manager in a new iTerm tab")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("clustermux: the TUI needs an interactive terminal (use --list for plain output)")

    def run_dashboard(screen) -> None:
        Dashboard(screen, hosts, args.timeout, args.refresh, not args.no_preview).run()

    curses.wrapper(run_dashboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
