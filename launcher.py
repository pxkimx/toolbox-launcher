#!/usr/bin/env python3
"""Toolbox Launcher: one page that opens, shows and quits every offline tool.

Every offline tool is a small local server whose window is a browser page. This app starts a tool's own app
from the Toolbox and shows its page on a tab inside this one page (or in a browser tab of its own), looks at
the process table and the listening ports to work out which processes belong to which tool, and can stop them.

Opening a tool (see `Hub.launch`): the tool's app is started with `open`, exactly as a double-click would, so
it keeps its own Dock icon, log and quit rules. When its page is to be shown inside this app, the tool's
launcher is told not to open a browser tab of its own (`PATH` gets a stand-in `open` that ignores web
addresses on this Mac, and `BROWSER` is set for the tools that open one from Python).

How a process is matched to a tool (see `attribute`):
  * it listens on the tool's port, or
  * it is a Python process, or the tool's launcher, whose working folder or command line is inside the
    tool's folder or its app in the Toolbox, or
  * it was started by such a process (workers, helper programs), or
  * it was left behind: its parent is gone and its working folder is inside the tool's folder.
Shells and editors that merely sit in a tool's folder are not matched.

Stopping a tool (see `stop_pids`): its servers and workers are asked to quit (SIGTERM), anything still alive
after a few seconds is killed, and the launcher is left to end by itself, which takes the icon out of the Dock.

The app itself follows the rule for every offline tool: it quits completely when its window is closed.
Standard library only, Python 3.9 or newer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").exists() else "dev"
HOST = "127.0.0.1"
PORT = 8773
SCAN_S = float(os.environ.get("TOOLBOX_LAUNCHER_SCAN_S", "2.5"))     # seconds between looks at the process table
FULL_EVERY = 4                # every n-th scan also reads every process's working folder (finds leftovers)
HISTORY = 48                  # CPU samples kept per tool (about two minutes)
BUSY_CPU = 25.0               # percent of one core above which a tool counts as busy
LAUNCHER_ENV = "TOOLBOX_LAUNCHER_APP_PID"
LOG = Path.home() / "Library" / "Logs" / "toolbox-launcher.log"
START_S = 240                 # a tool that has not come up this long after being opened has failed to start
SUPPORT = Path(os.environ.get("TOOLBOX_LAUNCHER_SUPPORT") or Path.home() / "Library" / "Application Support" / "Toolbox Launcher")
OTHER = "other"               # processes inside the tools folder that belong to no listed tool
SHELLS = {"bash", "sh", "zsh", "fish", "tcsh", "login"}


# ---------------------------------------------------------------- configuration
def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("TOOLBOX_LAUNCHER_CONFIG") or str(HERE / "tools.json")
    cfg = json.loads(Path(path).read_text())
    cfg["offline_root"] = os.path.expanduser(cfg["offline_root"]).rstrip("/")
    cfg["toolbox_root"] = os.path.expanduser(cfg["toolbox_root"]).rstrip("/")
    return cfg


def prefixes(cfg: dict) -> list:
    """(path prefix, tool key) pairs, longest first; this app's own folders map to None."""
    out = []
    me = cfg.get("self") or {}
    for root, name in ((cfg["offline_root"], me.get("folder")), (cfg["toolbox_root"], me.get("app"))):
        if name:
            out.append((f"{root}/{name}", None))
    for t in cfg["tools"]:
        out.append((f"{cfg['offline_root']}/{t['folder']}", t["key"]))
        out.append((f"{cfg['toolbox_root']}/{t['app']}", t["key"]))
    out.append((cfg["offline_root"], OTHER))
    return sorted(out, key=lambda x: -len(x[0]))


# ---------------------------------------------------------------- reading the process table
def run(cmd: list, timeout: float = 6.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def etime_seconds(text: str) -> int:
    """ps elapsed time: MM:SS, HH:MM:SS or D-HH:MM:SS."""
    days, _, rest = text.rpartition("-")
    parts = [int(p) for p in rest.split(":") if p.isdigit()]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(days) if days.isdigit() else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_ps(text: str) -> dict:
    """Output of `ps -axo pid=,ppid=,etime=,pcpu=,rss=,command=` -> {pid: process}."""
    procs = {}
    for line in text.splitlines():
        f = line.split(None, 5)
        if len(f) < 6 or not f[0].isdigit():
            continue
        try:
            procs[int(f[0])] = {"pid": int(f[0]), "ppid": int(f[1]), "etime": etime_seconds(f[2]),
                                "cpu": float(f[3].replace(",", ".")), "rss": int(f[4]), "cmd": f[5]}
        except ValueError:
            continue
    return procs


def parse_lsof_cwd(text: str) -> dict:
    """Output of `lsof -a -d cwd -Fpn` -> {pid: working folder}."""
    out, pid = {}, None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def _port(name: str) -> int | None:
    local = name.split("->")[0]
    tail = local.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


def parse_lsof_net(text: str) -> dict:
    """Output of `lsof -nP -iTCP -sTCP:<state> -Fpn` -> {pid: [local ports, one per socket]}."""
    out, pid = {}, None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            port = _port(line[1:])
            if port is not None:
                out.setdefault(pid, []).append(port)
    return out


def snapshot(full: bool) -> dict:
    """One look at the machine. `full` also reads the working folder of every process (slower)."""
    me = str(os.getuid())
    cwd_cmd = ["lsof", "-a", "-d", "cwd", "-Fpn"] + (["-u", me] if full else ["-c", "Python", "-c", "python", "-c", "bash"])
    return {
        "procs": parse_ps(run(["ps", "-axo", "pid=,ppid=,etime=,pcpu=,rss=,command="])),
        "cwd": parse_lsof_cwd(run(cwd_cmd)),
        "listen": parse_lsof_net(run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"])),
        "open": parse_lsof_net(run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpn"])),
    }


# ---------------------------------------------------------------- which process belongs to which tool
def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _mentions(cmd: str, prefix: str) -> bool:
    """The command line names this folder or something inside it (and not a longer name that starts the same way)."""
    i = cmd.find(prefix)
    while i != -1:
        after = cmd[i + len(prefix): i + len(prefix) + 1]
        if after in ("", "/", "'", '"', " "):
            return True
        i = cmd.find(prefix, i + 1)
    return False


def _program(cmd: str) -> str:
    """Name of the program a command line runs, without its folder: 'python3.12', 'bash', 'Python'."""
    m = re.match(r"(?:\S*/)?([^/\s]+)", cmd.strip())
    return m.group(1) if m else ""


def _is_python(cmd: str) -> bool:
    """A Python interpreter anywhere in the command line: its path may contain spaces, so do not rely on the first word."""
    return bool(re.search(r"(?:^|/)[Pp]ython[\d.]*(?:\s|$)", cmd))


def _is_launcher(cmd: str) -> bool:
    return _program(cmd) in SHELLS and (".app/Contents/" in cmd or cmd.rstrip().endswith("launcher.sh"))


def attribute(snap: dict, cfg: dict, ignore: set | None = None) -> dict:
    """{pid: tool key} for every process that belongs to a tool. `ignore` holds this app's own pids."""
    ignore = ignore or set()
    procs, cwds, listen = snap["procs"], snap["cwd"], snap["listen"]
    pre = prefixes(cfg)
    ports = {t["port"]: t["key"] for t in cfg["tools"]}

    def by_path(pid: int, p: dict):
        """(matched?, key). A match with key None is this app itself."""
        cwd = cwds.get(pid, "")
        for prefix, key in pre:
            if (cwd and _under(cwd, prefix)) or _mentions(p["cmd"], prefix):
                return True, key
        return False, None

    owner, mine = {}, set(ignore)
    for pid, p in procs.items():
        if pid in ignore:
            continue
        python, launcher = _is_python(p["cmd"]), _is_launcher(p["cmd"])
        orphan = p["ppid"] == 1 and _program(p["cmd"]) not in SHELLS
        if not (python or launcher or orphan):
            continue
        matched, key = by_path(pid, p)
        if matched and key is None:
            mine.add(pid)                 # this app's own launcher or server
        elif matched:
            owner[pid] = key
    for pid, plist in listen.items():     # a server on a tool's port belongs to that tool wherever it started
        if pid in procs and pid not in owner and pid not in mine:
            for port in plist:
                if port in ports:
                    owner[pid] = ports[port]
                    break
    # anything a tool's process started belongs to the tool too (workers, helper programs)
    children = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)
    queue = list(owner)
    while queue:
        pid = queue.pop()
        for kid in children.get(pid, []):
            if kid not in owner and kid not in mine:
                owner[kid] = owner[pid]
                queue.append(kid)
    return owner


def short(cmd: str, limit: int = 150) -> str:
    cmd = cmd.replace(str(Path.home()), "~")
    # the interpreter's own path says nothing useful and may contain spaces: keep just its name
    cmd = re.sub(r"^\S.*?/Python\.app/Contents/MacOS/Python(?=\s|$)", "python", cmd, count=1)
    cmd = re.sub(r"^\S.*?/bin/(python[\d.]*)(?=\s|$)", r"\1", cmd, count=1)
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"


def describe(snap: dict, cfg: dict, owner: dict) -> list:
    """The state of every tool, in the order of the configuration, then the 'other' group if it has anything."""
    procs, listen, opened, cwds = snap["procs"], snap["listen"], snap["open"], snap["cwd"]
    groups = {}
    for pid, key in owner.items():
        groups.setdefault(key, []).append(pid)
    tools = list(cfg["tools"])
    if OTHER in groups:
        tools.append({"key": OTHER, "name": "Other processes in the tools folder", "what": "", "port": None, "url": None})
    out = []
    for t in tools:
        pids = sorted(groups.get(t["key"], []))
        main = next((pid for pid in pids if t["port"] in listen.get(pid, [])), None) if t.get("port") else None
        rows = []
        for pid in pids:
            p = procs[pid]
            lports = sorted(set(listen.get(pid, [])))
            if pid == main:
                role = "server"
            elif _is_launcher(p["cmd"]):
                role = "launcher"
            elif lports:
                role = "test or dev server"
            elif p["ppid"] == 1:
                role = "left behind"
            elif p["ppid"] in owner:
                role = "worker"
            else:
                role = "process"
            cwd = cwds.get(pid, "")
            rows.append({"pid": pid, "ppid": p["ppid"], "role": role, "cpu": p["cpu"], "rss": p["rss"], "uptime": p["etime"],
                         "ports": lports, "cmd": short(p["cmd"]),
                         "from": "Toolbox app" if _under(cwd, cfg["toolbox_root"]) else "tool folder" if cwd else ""})
        state = "running" if main else "leftover" if pids else "stopped"
        cpu = round(sum(r["cpu"] for r in rows), 1)
        out.append({
            "key": t["key"], "name": t["name"], "what": t.get("what", ""), "port": t.get("port"), "url": t.get("url"),
            "state": state, "pid": main, "uptime": procs[main]["etime"] if main else None,
            "cpu": cpu, "rss": sum(r["rss"] for r in rows), "busy": cpu >= BUSY_CPU,
            "windows": sum(1 for port in opened.get(main, []) if port == t["port"]) if main else 0,
            "leftovers": sum(1 for r in rows if r["role"] == "left behind"),
            "procs": rows,
        })
    return out


# ---------------------------------------------------------------- stopping
def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def still_running(pids: list) -> list:
    """Those of `pids` that are really running. A process that has ended but has not been collected by its
    parent yet (a zombie) still answers a signal, so ask ps for its state as well."""
    pids = [p for p in pids if alive(p)]
    if not pids:
        return []
    out = run(["ps", "-o", "pid=,stat=", "-p", ",".join(str(p) for p in pids)])
    states = {int(f[0]): f[1] for f in (line.split() for line in out.splitlines()) if len(f) >= 2 and f[0].isdigit()}
    return [p for p in pids if p in states and not states[p].startswith("Z")]


def _wait_gone(pids: list, seconds: float) -> list:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pids = still_running(pids)
        if not pids:
            return []
        time.sleep(0.15)
    return still_running(pids)


def stop_pids(rows: list, grace: float = 4.0) -> dict:
    """Stop the processes of one tool. `rows` are process rows from `describe`.

    Servers and workers get SIGTERM first, so the tool can save what it is doing and stop its own helpers;
    what is still alive after `grace` seconds is killed. The launcher ends by itself once its server is gone
    and is only signalled if it does not."""
    protected = {0, 1, os.getpid(), os.getppid()}
    launchers = [r["pid"] for r in rows if r["role"] == "launcher" and r["pid"] not in protected]
    others = [r["pid"] for r in rows if r["role"] != "launcher" and r["pid"] not in protected]
    for pid in others:
        _signal(pid, signal.SIGTERM)
    left = _wait_gone(others, grace)
    for pid in left:
        _signal(pid, signal.SIGKILL)
    forced = len(left)
    left = _wait_gone(left, 2.0)
    stuck_launchers = _wait_gone(launchers, 2.5)
    for pid in stuck_launchers:
        _signal(pid, signal.SIGTERM)
    stuck_launchers = _wait_gone(stuck_launchers, 1.5)
    for pid in stuck_launchers:
        _signal(pid, signal.SIGKILL)
    left += _wait_gone(stuck_launchers, 1.0)
    return {"stopped": len(others) + len(launchers) - len(left), "forced": forced, "left": left}


# ---------------------------------------------------------------- opening a tool
STAND_IN_OPEN = """#!/bin/sh
# Stand-in for /usr/bin/open, used only by tools started from Toolbox Launcher: the tool's page is shown inside
# the launcher, so the browser tab its own launcher would open is skipped. Everything else goes to the real open.
for a in "$@"; do
  case "$a" in http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*) exit 0 ;; esac
done
exec /usr/bin/open "$@"
"""


def stand_in_dir() -> Path:
    """Folder holding the stand-in `open`; written on first use and whenever it changes."""
    d = SUPPORT / "bin"
    f = d / "open"
    if not f.exists() or f.read_text() != STAND_IN_OPEN:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(STAND_IN_OPEN)
        f.chmod(0o755)
    return d


def clean_env() -> dict:
    """The environment a tool gets when it is opened from here: what a double-click would give it. `open` hands
    its own environment on to the app, and a stray variable (PORT, PYTHONPATH, a virtual environment) from
    wherever this app was started would otherwise change how the tool starts."""
    keep = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SHELL", "__CF_USER_TEXT_ENCODING")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def open_command(app: str, inside: bool) -> list:
    """The command that starts a tool's app. `inside`: its page will be shown in this app, not in a browser tab."""
    if not inside:
        return ["/usr/bin/open", app]
    return ["/usr/bin/open", "-g", "--env", f"PATH={stand_in_dir()}:/usr/bin:/bin:/usr/sbin:/sbin",
            "--env", "BROWSER=/usr/bin/true", app]


# ---------------------------------------------------------------- the app
class Hub:
    def __init__(self, cfg: dict, port: int):
        self.cfg, self.port = cfg, port
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.state = {"tools": [], "seq": 0}
        self.history = {}            # tool key -> recent CPU samples
        self.versions = {}           # (tool key, server pid) -> version text
        self.activity = []           # newest first
        self.stopping = set()        # tool keys being stopped right now
        self.launching = {}          # tool key -> when it was opened from here (until its page answers)
        self.ready = set()           # (tool key, server pid) whose page has answered
        self.inside = set()          # tool keys whose page is shown inside this app's window (the page reports them)
        self.wanted = set()          # tool keys the window has been asked to bring up on a key (until it reports them)
        self.streams = 0             # open windows of this app
        self.life = {"seen": False, "closing": False}
        self.scans = 0
        self.cwds = {}               # working folders from the last full scan
        self.wake = threading.Event()

    # ---- scanning
    def own_pids(self) -> set:
        return {os.getpid(), os.getppid(), int(os.environ.get(LAUNCHER_ENV, "0") or 0)} - {0}

    def look(self, full: bool = False) -> list:
        snap = snapshot(full)
        if full:
            self.cwds = dict(snap["cwd"])
        else:       # a quick scan only reads Python's and bash's folders: keep what the last full one found
            snap["cwd"] = {**{pid: c for pid, c in self.cwds.items() if pid in snap["procs"]}, **snap["cwd"]}
        return describe(snap, self.cfg, attribute(snap, self.cfg, self.own_pids()))

    def version_of(self, tool: dict) -> str | None:
        spec = next((t.get("version") for t in self.cfg["tools"] if t["key"] == tool["key"]), None)
        if not spec or not tool["pid"]:
            return None
        key = (tool["key"], tool["pid"])
        if key not in self.versions:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{tool['port']}{spec['path']}", timeout=0.7) as r:
                    self.versions[key] = str(json.loads(r.read().decode()).get(spec["field"], "")) or None
            except Exception:  # noqa: BLE001 - the tool may still be starting; try again on a later scan
                return None
        return self.versions[key]

    def answers(self, tool: dict) -> bool:
        """The tool's page can be loaded. A server listens a moment before it can serve, so ask once per server."""
        if not tool["pid"] or not tool.get("url"):
            return False
        key = (tool["key"], tool["pid"])
        if key not in self.ready:
            try:
                with urllib.request.urlopen(tool["url"], timeout=0.8) as r:
                    if r.status < 500:
                        self.ready.add(key)
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    self.ready.add(key)
            except Exception:  # noqa: BLE001 - not up yet
                pass
        return key in self.ready

    def settle(self, t: dict) -> None:
        """Turn 'a server is listening' into what the user sees: starting until the page answers, and a note
        in the logbook when a tool opened from here comes up or fails to."""
        key, since = t["key"], self.launching.get(t["key"])
        up = t["state"] == "running" and self.answers(t)
        if t["state"] == "running" and not up:
            t["state"] = "starting"
        if since is None:
            return
        waited = time.time() - since
        if up:
            del self.launching[key]
            self.note(t["name"], f"opened in {waited:.0f} s", publish=False)
        elif waited > START_S or (not t["procs"] and waited > 25):
            del self.launching[key]
            self.note(t["name"], "did not start; its own log says why", ok=False, publish=False)
        else:
            t["state"] = "starting"

    def scan(self, full: bool | None = None) -> None:
        self.scans += 1
        tools = self.look(self.scans % FULL_EVERY == 1 if full is None else full)
        specs = {x["key"]: x for x in self.cfg["tools"]}
        for t in tools:
            self.settle(t)
            t["embed"] = bool(specs.get(t["key"], {}).get("embed", True))
            t["version"] = self.version_of(t)
            h = self.history.setdefault(t["key"], [])
            h.append(t["cpu"] if t["state"] != "stopped" else None)
            del h[:-HISTORY]
            t["history"] = list(h)
            t["stopping"] = t["key"] in self.stopping
            app = next((x.get("app") for x in self.cfg["tools"] if x["key"] == t["key"]), None)
            t["installed"] = bool(app) and os.path.isdir(f"{self.cfg['toolbox_root']}/{app}")
        with self.changed:
            self.state = {"tools": tools, "seq": self.state["seq"] + 1, "time": time.time(), "version": VERSION,
                          "interval": SCAN_S, "activity": self.activity[:40], "wanted": sorted(self.wanted)}
            self.changed.notify_all()

    def loop(self) -> None:
        while True:
            try:
                self.scan()
            except Exception as e:  # noqa: BLE001 - one bad scan must not end the app
                log(f"scan failed: {type(e).__name__}: {e}")
            self.wake.wait(SCAN_S)
            self.wake.clear()

    # ---- the logbook
    def note(self, tool: str, text: str, ok: bool = True, publish: bool = True) -> None:
        self.activity.insert(0, {"time": time.time(), "tool": tool, "text": text, "ok": ok})
        del self.activity[200:]
        log(f"{tool}: {text}")
        if not publish:              # called from inside a scan, which publishes the state itself
            return
        with self.changed:           # show it at once, without waiting for the next scan
            self.state = {**self.state, "activity": self.activity[:40], "seq": self.state["seq"] + 1}
            self.changed.notify_all()

    # ---- opening
    def launch(self, key: str, inside: bool, show: bool = False) -> dict:
        """Start a tool's app from the Toolbox. Returns at once; the scans report when its page answers.
        `show`: also ask the open window to bring the tool up on its key (for a request that does not come
        from the window itself, such as another program asking for a tool to be opened here)."""
        spec = next((x for x in self.cfg["tools"] if x["key"] == key), None)
        if spec is None:
            return {"ok": False, "text": "unknown tool"}
        if show and inside and spec.get("embed", True):
            self.wanted.add(key)
            self.wake.set()
        app = f"{self.cfg['toolbox_root']}/{spec['app']}"
        if not os.path.isdir(app):
            self.note(spec["name"], "is not in the Toolbox", ok=False)
            return {"ok": False, "text": f"{spec['app']} is not in the Toolbox"}
        t = next((x for x in self.look() if x["key"] == key), None)
        if t and t["state"] == "running":
            if not inside:          # already running: opening its app again shows its page in the browser
                subprocess.run(open_command(app, False), capture_output=True, timeout=20, env=clean_env())
            return {"ok": True, "text": "already running", "running": True}
        r, err = None, ""
        try:
            r = subprocess.run(open_command(app, inside and spec.get("embed", True)), capture_output=True, text=True, timeout=20, env=clean_env())
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
        if r is None or r.returncode != 0:
            text = "could not be opened: " + (err if r is None else (r.stderr or "").strip()[:160])
            self.note(spec["name"], text, ok=False)
            return {"ok": False, "text": text}
        self.launching[key] = time.time()
        self.wake.set()
        return {"ok": True, "text": "starting", "running": False}

    # ---- stopping
    def stop(self, key: str) -> dict:
        """Stop one tool (or the 'other' group). Looks again first, so only what is there now is touched."""
        self.launching.pop(key, None)
        tools = self.look(full=True)
        t = next((x for x in tools if x["key"] == key), None)
        if t is None or not t["procs"]:
            return {"ok": True, "text": "was not running"}
        self.stopping.add(key)
        self.wake.set()
        try:
            r = stop_pids(t["procs"])
            still = next((x for x in self.look(full=True) if x["key"] == key), None)
            remaining = len(still["procs"]) if still else 0
            if remaining:
                r2 = stop_pids(still["procs"], grace=1.0)        # helpers that appeared while it was stopping
                r["stopped"] += r2["stopped"]
                r["forced"] += r2["forced"]
                again = next((x for x in self.look(full=True) if x["key"] == key), None)
                remaining = len(again["procs"]) if again else 0
        finally:
            self.stopping.discard(key)
        n = r["stopped"]
        text = f"quit — {n} process{'es' if n != 1 else ''} stopped" + (f", {r['forced']} had to be forced" if r["forced"] else "")
        if remaining:
            text = f"could not stop {remaining} process{'es' if remaining != 1 else ''}"
        self.note(t["name"], text, ok=not remaining)
        self.wake.set()
        return {"ok": not remaining, "text": text}

    def stop_inside(self) -> None:
        """This app's window is gone, and with it the page of every tool that was shown inside it. Each of
        those tools would notice by itself, some only after minutes; switch them off now instead."""
        keys = sorted(self.inside)
        self.inside = set()
        if not keys:
            return
        running = {t["key"] for t in self.look(full=True) if t["procs"]}
        threads = [threading.Thread(target=self.stop, args=(k,), daemon=True) for k in keys if k in running]
        for th in threads:
            th.start()
        for th in threads:
            th.join(20)

    def stop_all(self) -> dict:
        keys = [t["key"] for t in self.look(full=True) if t["procs"]]
        results, threads = {}, []
        for key in keys:
            th = threading.Thread(target=lambda k=key: results.__setitem__(k, self.stop(k)), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        return {"ok": all(r["ok"] for r in results.values()), "text": f"{len(keys)} stopped", "results": results}

    # ---- this app's own life: quit completely when its window is closed
    def window_opened(self) -> None:
        with self.lock:
            self.streams += 1
            self.life["seen"], self.life["closing"] = True, False

    def window_closed(self) -> None:
        with self.lock:
            self.streams = max(0, self.streams - 1)
            self.life["closing"] = True

    def watch(self, keep_alive: bool = False) -> None:
        """Quit when the window is closed, when nobody ever opens one, or when the app's launcher is gone.
        `keep_alive` (development) never quits, but still switches off the tools shown inside a closed window."""
        launcher = int(os.environ.get(LAUNCHER_ENV, "0") or 0)
        empty, check = 0, 2.0
        after_close = int(os.environ.get("TOOLBOX_LAUNCHER_AFTER_CLOSE", "3"))     # checks, about six seconds
        after_silence = int(os.environ.get("TOOLBOX_LAUNCHER_AFTER_SILENCE", "90"))  # about three minutes
        while True:
            time.sleep(check)
            if launcher and not alive(launcher):
                self.stop_inside()
                quit_now("the launcher was closed")
            with self.lock:
                n, seen, closing = self.streams, self.life["seen"], self.life["closing"]
            if n > 0:
                empty = 0
                continue
            empty += 1
            if seen and closing and empty >= after_close:
                self.stop_inside()
                if keep_alive:
                    with self.lock:
                        self.life["closing"] = False
                    continue
                quit_now("its window was closed")
            if empty >= after_silence and not keep_alive:
                quit_now("no window has been open for about three minutes")


def log(text: str) -> None:
    line = f"[{time.strftime('%F %T')}] {text}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def quit_now(reason: str) -> None:
    log(f"quitting: {reason}")
    os._exit(0)


# ---------------------------------------------------------------- icons
def icon_png(cfg: dict, key: str) -> bytes | None:
    """The tool's own app icon as a small PNG, converted once with sips and kept in the temp folder."""
    t = next((x for x in cfg["tools"] if x["key"] == key), None)
    if not t:
        return None
    res = Path(cfg["toolbox_root"]) / t["app"] / "Contents" / "Resources"
    icns = sorted(res.glob("*.icns")) if res.is_dir() else []
    if not icns:
        return None
    cache = Path(tempfile.gettempdir()) / "toolbox-launcher-icons"
    cache.mkdir(exist_ok=True)
    out = cache / f"{key}-{int(icns[0].stat().st_mtime)}.png"
    if not out.exists():
        subprocess.run(["sips", "-s", "format", "png", "-Z", "128", str(icns[0]), "--out", str(out)],
                       capture_output=True, timeout=10)
    return out.read_bytes() if out.exists() else None


# ---------------------------------------------------------------- HTTP
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


def make_handler(mon: Hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # keep the log for what the app does, not every request
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _local(self) -> bool:
            """Only this Mac, and only through this app's own address (not another site's name for it)."""
            return self.headers.get("Host", "") in (f"localhost:{mon.port}", f"127.0.0.1:{mon.port}")

        def _authorised(self) -> bool:
            return self._local() and secrets.compare_digest(self.headers.get("X-Toolbox-Token", ""), mon.token)

        def do_GET(self):  # noqa: N802
            if not self._local():
                return self._json(403, {"error": "not allowed"})
            path, _, query = self.path.partition("?")
            if path == "/":
                html = (STATIC / "index.html").read_text().replace("__TOKEN__", mon.token).replace("__VERSION__", VERSION)
                return self._send(200, html.encode(), TYPES[".html"])
            if path == "/api/ping":
                return self._json(200, {"app": "toolbox-launcher", "version": VERSION})
            if path == "/api/state":
                with mon.lock:
                    return self._json(200, mon.state)
            if path == "/api/stream":
                if query != f"token={mon.token}":
                    return self._json(403, {"error": "not allowed"})
                return self._stream()
            if path.startswith("/icon/"):
                png = icon_png(mon.cfg, path[6:].split(".")[0])
                return self._send(200, png, "image/png", cache=True) if png else self._json(404, {"error": "no icon"})
            if path.startswith("/static/"):
                f = (STATIC / path[8:]).resolve()
                if STATIC in f.parents and f.is_file():
                    return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
            return self._json(404, {"error": "not found"})

        def _stream(self) -> None:
            """Server-sent events: the state after every scan. Holding it open is also how this app knows its
            window is there; when the window goes, the next write fails and the app quits a few seconds later."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            mon.window_opened()
            mon.wake.set()
            seq = -1
            try:
                self.wfile.write(b"retry: 1000\n\n")
                while True:
                    with mon.changed:
                        mon.changed.wait_for(lambda: mon.state["seq"] != seq, timeout=2.0)
                        state, new = mon.state, mon.state["seq"] != seq
                    seq = state["seq"]
                    self.wfile.write(f"data: {json.dumps(state)}\n\n".encode() if new else b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                mon.window_closed()

        def do_POST(self):  # noqa: N802
            if not self._authorised():
                return self._json(403, {"error": "not allowed"})
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "bad request"})
            if self.path == "/api/tabs":         # which tools this window is showing inside itself
                known = {t["key"] for t in mon.cfg["tools"]}
                mon.inside = {str(k) for k in body.get("keys", []) if str(k) in known}
                mon.wanted -= mon.inside
                return self._json(200, {"ok": True})
            if self.path == "/api/launch":
                r = mon.launch(str(body.get("key", "")), bool(body.get("inside", True)), bool(body.get("show", False)))
                return self._json(200 if r["ok"] or r["text"] != "unknown tool" else 400, r)
            if self.path == "/api/stop":
                key = str(body.get("key", ""))
                known = {t["key"] for t in mon.cfg["tools"]} | {OTHER}
                if key not in known:
                    return self._json(400, {"error": "unknown tool"})
                return self._json(200, mon.stop(key))
            if self.path == "/api/stop-all":
                return self._json(200, mon.stop_all())
            if self.path == "/api/quit":
                threading.Timer(0.3, lambda: (mon.stop_inside(), quit_now("Quit was pressed"))).start()
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})

    return Handler


def main(argv: list | None = None) -> None:
    ap = argparse.ArgumentParser(description="Toolbox Launcher: open, watch and quit the offline tools from one page.")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--config", help="tools.json to use instead of the one next to this file")
    ap.add_argument("--list", action="store_true", help="print what is running and exit")
    ap.add_argument("--keep-alive", action="store_true", help="do not quit when no window is open (development)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    mon = Hub(cfg, a.port)
    if a.list:
        for t in mon.look(full=True):
            if t["procs"]:
                print(f"{t['name']}: {t['state']}, {len(t['procs'])} process(es), CPU {t['cpu']}%, pids {[p['pid'] for p in t['procs']]}")
        return
    server = ThreadingHTTPServer((HOST, a.port), make_handler(mon))
    server.daemon_threads = True
    mon.scan(full=True)          # the first page already shows what is running
    threading.Thread(target=mon.loop, daemon=True, name="scan").start()
    threading.Thread(target=mon.watch, args=(a.keep_alive,), daemon=True, name="life").start()

    def on_signal(signum, frame):        # Quit from the Dock: take the tools shown in this window along
        threading.Thread(target=lambda: (mon.stop_inside(), quit_now("stopped by a signal")), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    log(f"Toolbox Launcher {VERSION} at http://localhost:{a.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
