"""Tests for Toolbox Launcher.

    python3 -m unittest discover tests        (standard library only; about 50 s)

Two kinds:
  * matching: made-up process tables go through the same functions the app uses, so every rule in
    `launcher.attribute` is pinned down without starting anything;
  * for real: a throw-away "tool" (a tiny server with a worker and a launcher, as an app in a temporary
    Toolbox) is opened, found and stopped through the app's own HTTP interface, and the app's own life is
    checked (quits when its window closes and takes the tools shown in that window along, survives a reload,
    quits when its launcher is gone).

The processes that must be gone afterwards are looked up with `ps` directly, never through the code being tested.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launcher as m  # noqa: E402

CFG = {
    "offline_root": "/T/Offline Tools", "toolbox_root": "/D/Toolbox",
    "self": {"folder": "Toolbox Launcher", "app": "Toolbox Launcher.app"},
    "tools": [
        {"key": "alpha", "name": "Alpha", "port": 9001, "folder": "Alpha", "app": "Alpha.app", "url": "http://localhost:9001"},
        {"key": "beta", "name": "Beta Bench", "port": 9002, "folder": "Beta Bench", "app": "Beta Bench.app", "url": "http://localhost:9002"},
    ],
}


def snap(rows, cwd=None, listen=None, opened=None):
    """rows: (pid, ppid, command) -> the same shape `launcher.snapshot` returns."""
    procs = {pid: {"pid": pid, "ppid": ppid, "etime": 60, "cpu": 1.0, "rss": 1000, "cmd": cmd} for pid, ppid, cmd in rows}
    return {"procs": procs, "cwd": cwd or {}, "listen": listen or {}, "open": opened or {}}


class Parsing(unittest.TestCase):
    def test_elapsed_time(self):
        self.assertEqual(m.etime_seconds("00:07"), 7)
        self.assertEqual(m.etime_seconds("12:34"), 754)
        self.assertEqual(m.etime_seconds("01:02:03"), 3723)
        self.assertEqual(m.etime_seconds("2-01:02:03"), 2 * 86400 + 3723)

    def test_ps(self):
        out = "  501     1 01:02:03   0.5  20480 /bin/bash /D/Toolbox/Alpha.app/Contents/MacOS/Alpha\n  77 501 00:09 12,5 99 python3 -m x --flag a b\ngarbage\n"
        p = m.parse_ps(out)
        self.assertEqual(set(p), {501, 77})
        self.assertEqual(p[501]["cmd"], "/bin/bash /D/Toolbox/Alpha.app/Contents/MacOS/Alpha")
        self.assertEqual((p[77]["ppid"], p[77]["etime"], p[77]["cpu"], p[77]["rss"]), (501, 9, 12.5, 99))
        self.assertEqual(p[77]["cmd"], "python3 -m x --flag a b")

    def test_lsof(self):
        self.assertEqual(m.parse_lsof_cwd("p12\nfcwd\nn/T/Offline Tools/Alpha\np13\nfcwd\nn/\n"), {12: "/T/Offline Tools/Alpha", 13: "/"})
        net = m.parse_lsof_net("p12\nf5\nn127.0.0.1:9001\nf6\nn[::1]:9001\np13\nf9\nn127.0.0.1:9001->127.0.0.1:50000\nf10\nn*:8599\n")
        self.assertEqual(net, {12: [9001, 9001], 13: [9001, 8599]})

    def test_short_command(self):
        self.assertEqual(m.short("/T/Offline Tools/Alpha/.venv/bin/python3 -m alpha.app"), "python3 -m alpha.app")
        self.assertEqual(m.short("/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python -m uvicorn x"), "python -m uvicorn x")
        self.assertTrue(m.short("x" * 400).endswith("…"))


class Matching(unittest.TestCase):
    def owners(self, s, ignore=None):
        return m.attribute(s, CFG, ignore or set())

    def test_server_by_port_even_from_elsewhere(self):
        s = snap([(10, 1, "/usr/bin/python3 -m http.server 9001")], cwd={10: "/somewhere/else"}, listen={10: [9001]})
        self.assertEqual(self.owners(s), {10: "alpha"})

    def test_python_by_folder_and_by_toolbox_app(self):
        s = snap([(10, 5, "python3 -m alpha.app"), (11, 5, "python -m uvicorn server.app:app --port 9002")],
                 cwd={10: "/T/Offline Tools/Alpha", 11: "/D/Toolbox/Beta Bench.app/Contents/Resources/app"})
        self.assertEqual(self.owners(s), {10: "alpha", 11: "beta"})

    def test_interpreter_path_with_spaces(self):
        s = snap([(10, 5, "/T/Offline Tools/Beta Bench/.venv/bin/python3 -m beta")])
        self.assertEqual(self.owners(s), {10: "beta"})

    def test_launcher_and_everything_it_started(self):
        s = snap([(20, 1, "/bin/bash /D/Toolbox/Alpha.app/Contents/MacOS/Alpha"), (21, 20, "python3 serve.py"),
                  (22, 21, "/opt/tools/sage search.json"), (23, 22, "sleep 5")], cwd={21: "/elsewhere"})
        self.assertEqual(self.owners(s), {20: "alpha", 21: "alpha", 22: "alpha", 23: "alpha"})

    def test_left_behind_worker_of_any_name(self):
        s = snap([(30, 1, "/opt/tools/sage search.json"), (31, 1, "/usr/sbin/cfprefsd agent")], cwd={30: "/T/Offline Tools/Beta Bench/source", 31: "/"})
        self.assertEqual(self.owners(s), {30: "beta"})

    def test_shells_and_editors_in_the_folder_are_not_tools(self):
        s = snap([(40, 39, "/bin/zsh -il"), (41, 40, "vim notes.md"), (42, 1, "/bin/zsh -c sleep 99"), (43, 40, "grep -r x .")],
                 cwd={40: "/T/Offline Tools/Alpha", 41: "/T/Offline Tools/Alpha", 42: "/T/Offline Tools/Alpha", 43: "/T/Offline Tools/Alpha"})
        self.assertEqual(self.owners(s), {})

    def test_similar_names_do_not_match(self):
        s = snap([(50, 5, "python3 x.py"), (51, 5, "python3 /T/Offline Tools/Alpha2/x.py")], cwd={50: "/T/Offline Tools/Alphabet"})
        self.assertEqual(self.owners(s), {50: "other", 51: "other"})

    def test_the_app_never_lists_itself(self):
        s = snap([(60, 1, "/bin/bash /D/Toolbox/Toolbox Launcher.app/Contents/MacOS/ToolboxLauncher"), (61, 60, "python3 launcher.py --port 8773"),
                  (62, 9, "/bin/sh -c cd '/T/Offline Tools/Toolbox Launcher' && exec python3 launcher.py"), (63, 62, "python3 launcher.py --port 8774"),
                  (64, 61, "ps -axo pid=")],
                 cwd={61: "/D/Toolbox/Toolbox Launcher.app/Contents/Resources/app", 63: "/T/Offline Tools/Toolbox Launcher"}, listen={61: [8773], 63: [8774]})
        self.assertEqual(self.owners(s), {})
        self.assertEqual(self.owners(s, ignore={61}), {})

    def test_other_group(self):
        s = snap([(70, 5, "python3 scratch.py")], cwd={70: "/T/Offline Tools"})
        self.assertEqual(self.owners(s), {70: "other"})


class Describing(unittest.TestCase):
    def test_states_roles_and_window(self):
        s = snap([(20, 1, "/bin/bash /D/Toolbox/Alpha.app/Contents/MacOS/Alpha"), (21, 20, "python3 serve.py"), (22, 21, "python3 -c worker"),
                  (30, 1, "python3 -c leftover")],
                 cwd={21: "/D/Toolbox/Alpha.app/Contents/Resources/app", 30: "/T/Offline Tools/Beta Bench"},
                 listen={21: [9001]}, opened={21: [9001, 9001, 51234], 99: [9001]})
        s["procs"][22]["cpu"] = 80.0
        tools = {t["key"]: t for t in m.describe(s, CFG, m.attribute(s, CFG))}
        a, b = tools["alpha"], tools["beta"]
        self.assertEqual((a["state"], a["pid"], a["windows"], a["busy"]), ("running", 21, 2, True))
        self.assertEqual({r["pid"]: r["role"] for r in a["procs"]}, {20: "launcher", 21: "server", 22: "worker"})
        self.assertEqual((b["state"], b["pid"], b["leftovers"], b["windows"]), ("leftover", None, 1, 0))
        self.assertEqual(b["procs"][0]["role"], "left behind")
        self.assertNotIn("other", tools)

    def test_nothing_running(self):
        tools = m.describe(snap([]), CFG, {})
        self.assertEqual([(t["key"], t["state"], t["procs"]) for t in tools], [("alpha", "stopped", []), ("beta", "stopped", [])])


# ---------------------------------------------------------------- for real
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pids_matching(text: str) -> list:
    """Process ids whose command line contains `text`, read from ps directly (not through the code being tested)."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [int(line.split(None, 1)[0]) for line in out.splitlines() if text in line and "ps -axo" not in line]


def wait_for(cond, seconds: float, step: float = 0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


FAKE_SERVER = textwrap.dedent('''
    """A stand-in for an offline tool: serves on a port, keeps a worker, and can be told to ignore SIGTERM."""
    import http.server, signal, subprocess, sys, time
    port, tag, stubborn = int(sys.argv[1]), sys.argv[2], sys.argv[3] == "stubborn"
    worker = subprocess.Popen([sys.executable, "-c", "import time, signal, sys\\n"
                               + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if stubborn else "")
                               + "time.sleep(600)  # " + tag + "-worker"])
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler).serve_forever()
''')


class ForReal(unittest.TestCase):
    """A temporary Toolbox with two fake tools, and a real Toolbox Launcher process looking at it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="toolbox-launcher-test-")).resolve()
        self.tag = self.tmp.name
        self.offline, self.toolbox = self.tmp / "Offline Tools", self.tmp / "Toolbox"
        self.ports = {"good": free_port(), "stubborn": free_port()}
        tools = []
        for key in self.ports:
            (self.offline / key).mkdir(parents=True)
            app = self.toolbox / f"{key}.app" / "Contents"
            (app / "MacOS").mkdir(parents=True)
            (app / "Resources" / "app").mkdir(parents=True)
            (app / "Resources" / "app" / "server.py").write_text(FAKE_SERVER)
            launcher = app / "MacOS" / "launcher.sh"
            launcher.write_text(f'#!/bin/bash\n# what this launcher was given, for the tests to read\n'
                                f'echo "$(command -v open)|${{BROWSER:-}}|${{PORT:-unset}}" > "{app}/env.txt"\n'
                                f'cd "{app}/Resources/app"\n"{sys.executable}" server.py {self.ports[key]} {self.tag} {key} &\nwait $!\n')
            launcher.chmod(0o755)
            (app / "Info.plist").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>CFBundleExecutable</key><string>launcher.sh</string>'
                f'<key>CFBundleIdentifier</key><string>test.toolbox-launcher.{self.tag}.{key}</string>'
                '<key>CFBundlePackageType</key><string>APPL</string></dict></plist>\n')
            tools.append({"key": key, "name": f"Fake {key}", "port": self.ports[key], "folder": key, "app": f"{key}.app",
                          "url": f"http://127.0.0.1:{self.ports[key]}"})
        self.cfg = self.tmp / "tools.json"
        self.cfg.write_text(json.dumps({"offline_root": str(self.offline), "toolbox_root": str(self.toolbox),
                                        "self": {"folder": "Toolbox Launcher", "app": "Toolbox Launcher.app"}, "tools": tools}))
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.started = []
        self.orphans = []
        self.app = None

    def tearDown(self):
        for p in self.started + ([self.app] if self.app else []):
            if p.poll() is None:
                p.kill()
            p.wait()
        for pid in pids_matching(self.tag) + self.orphans:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        subprocess.run(["rm", "-rf", str(self.tmp)])

    # ---- helpers
    def start_app(self, *args, env=None):
        e = dict(os.environ, TOOLBOX_LAUNCHER_SCAN_S="0.5", TOOLBOX_LAUNCHER_SUPPORT=str(self.tmp / "support"), **(env or {}))
        self.app = subprocess.Popen([sys.executable, str(ROOT / "launcher.py"), "--port", str(self.port), "--config", str(self.cfg), *args],
                                        env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_for(lambda: self.get("/api/ping", fail_ok=True), 10), "the app did not start")
        html = urllib.request.urlopen(self.base + "/").read().decode()
        self.token = html.split('name="toolbox-token" content="')[1].split('"')[0]

    def get(self, path, fail_ok=False):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return json.loads(r.read().decode())
        except Exception:
            if fail_ok:
                return None
            raise

    def post(self, path, body=None, token=None, host=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Toolbox-Token": self.token if token is None else token})
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def launch(self, key):
        p = subprocess.Popen(["/bin/bash", str(self.toolbox / f"{key}.app" / "Contents" / "MacOS" / "launcher.sh")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started.append(p)
        self.assertTrue(wait_for(lambda: len(pids_matching(self.tag + " " + key)) >= 1 and len(pids_matching(self.tag + "-worker")) >= 1, 10))
        return p

    def tool(self, key):
        return next(t for t in self.get("/api/state")["tools"] if t["key"] == key)

    # ---- tests
    def test_finds_and_stops_a_tool(self):
        self.start_app("--keep-alive")
        self.assertEqual(self.tool("good")["state"], "stopped")
        self.launch("good")
        t = wait_for(lambda: (lambda x: x if x["state"] == "running" else None)(self.tool("good")), 10)
        self.assertTrue(t, "the running tool was not found")
        self.assertEqual(sorted(r["role"] for r in t["procs"]), ["launcher", "server", "worker"])
        self.assertEqual(t["windows"], 0)
        mine = pids_matching(self.tag)
        self.assertGreaterEqual(len(mine), 3)

        r = self.post("/api/stop", {"key": "good"})
        self.assertTrue(r["ok"], r)
        self.assertIn("3 processes stopped", r["text"])
        self.assertEqual(wait_for(lambda: not pids_matching(self.tag + " good") and not pids_matching(self.tag + "-worker"), 5), True)
        self.assertEqual(pids_matching(str(self.toolbox / "good.app")), [], "the launcher is still there")
        self.assertEqual(wait_for(lambda: self.tool("good")["state"] == "stopped", 5), True)
        act = self.get("/api/state")["activity"]
        self.assertEqual((act[0]["tool"], act[0]["ok"]), ("Fake good", True))

    def test_a_tool_that_ignores_the_request_is_forced(self):
        self.start_app("--keep-alive")
        self.launch("stubborn")
        wait_for(lambda: self.tool("stubborn")["state"] == "running", 10)
        r = self.post("/api/stop", {"key": "stubborn"})
        self.assertTrue(r["ok"], r)
        self.assertIn("forced", r["text"])
        self.assertEqual(pids_matching(self.tag + " stubborn") + pids_matching(self.tag + "-worker"), [])

    def test_left_behind_worker_and_stop_all(self):
        self.start_app("--keep-alive")
        self.launch("good")
        # an orphan: started through a shell that exits at once, working folder inside the tool's folder
        subprocess.run(["/bin/sh", "-c", f'cd "{self.offline / "stubborn"}" && (sleep 600 >/dev/null 2>&1 & )  # {self.tag}'], check=True)
        orphan = wait_for(lambda: [p for p in pids_matching("sleep 600") if p in self._children_of_init()], 5)
        self.assertTrue(orphan, "could not make an orphan to test with")
        self.orphans = list(orphan)
        t = wait_for(lambda: (lambda x: x if x["state"] == "leftover" else None)(self.tool("stubborn")), 10)
        self.assertTrue(t, "the left-behind process was not found")
        self.assertEqual([r["role"] for r in t["procs"]], ["left behind"])
        r = self.post("/api/stop-all")
        self.assertTrue(r["ok"], r)
        self.assertEqual([p for p in orphan if m.alive(p)], [])
        self.assertEqual(pids_matching(self.tag + " good"), [])

    def _children_of_init(self):
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
        return {int(a) for a, b in (line.split() for line in out.splitlines()) if b == "1"}

    def given(self, key):
        """What the fake tool's launcher saw: (where `open` resolves, BROWSER, PORT)."""
        f = self.toolbox / f"{key}.app" / "Contents" / "env.txt"
        self.assertTrue(wait_for(f.exists, 15), "the tool's app was not started")
        return f.read_text().strip().split("|")

    def test_opens_a_tool_for_this_window_without_a_browser_tab(self):
        self.start_app("--keep-alive", env={"PORT": "9", "PYTHONPATH": "/nowhere"})      # strays that must not reach the tool
        r = self.post("/api/launch", {"key": "good", "inside": True})
        self.assertTrue(r["ok"], r)
        self.assertTrue(wait_for(lambda: self.tool("good")["state"] in ("starting", "running"), 5), "opening is not shown as starting")
        where, browser, port = self.given("good")
        self.assertEqual(where, str(self.tmp / "support" / "bin" / "open"), "the stand-in for open is not first on PATH")
        self.assertEqual((browser, port), ("/usr/bin/true", "unset"))
        t = wait_for(lambda: (lambda x: x if x["state"] == "running" else None)(self.tool("good")), 20)
        self.assertTrue(t, "the opened tool never reported running")
        self.assertTrue(t["embed"])
        act = self.get("/api/state")["activity"]
        self.assertTrue(act and act[0]["tool"] == "Fake good" and act[0]["text"].startswith("opened in"), act)
        self.assertEqual(self.post("/api/launch", {"key": "good", "inside": True})["text"], "already running")
        self.assertTrue(self.post("/api/stop", {"key": "good"})["ok"])

    def test_asking_the_window_to_bring_a_tool_up(self):
        self.start_app("--keep-alive")
        self.assertTrue(self.post("/api/launch", {"key": "good", "inside": True, "show": True})["ok"])
        self.assertEqual(wait_for(lambda: self.get("/api/state").get("wanted"), 5), ["good"])
        self.post("/api/tabs", {"keys": ["good"]})                      # the window reports the key is open
        self.assertEqual(wait_for(lambda: self.get("/api/state").get("wanted") == [], 5), True)
        wait_for(lambda: self.tool("good")["state"] == "running", 20)
        self.assertTrue(self.post("/api/stop", {"key": "good"})["ok"])

    def test_opens_a_tool_in_its_own_browser_tab_untouched(self):
        self.start_app("--keep-alive")
        self.assertTrue(self.post("/api/launch", {"key": "good", "inside": False})["ok"])
        where, browser, port = self.given("good")
        self.assertEqual((where, browser, port), ("/usr/bin/open", "", "unset"))
        wait_for(lambda: self.tool("good")["state"] == "running", 20)
        self.assertTrue(self.post("/api/stop", {"key": "good"})["ok"])

    def test_a_tool_that_is_not_in_the_toolbox(self):
        self.start_app("--keep-alive")
        subprocess.run(["rm", "-rf", str(self.toolbox / "good.app")])
        r = self.post("/api/launch", {"key": "good", "inside": True})
        self.assertFalse(r["ok"])
        self.assertIn("not in the Toolbox", r["text"])
        self.assertTrue(wait_for(lambda: not self.tool("good")["installed"], 5))

    def test_the_stand_in_for_open(self):
        d = Path(tempfile.mkdtemp(prefix="toolbox-launcher-standin-"))
        try:
            f = d / "open"
            f.write_text(m.STAND_IN_OPEN)
            f.chmod(0o755)
            for url in ("http://localhost:8765", "http://127.0.0.1:8770/", "http://localhost"):
                r = subprocess.run([str(f), url], capture_output=True, text=True, timeout=10)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), url)
            r = subprocess.run([str(f), "--no-such-option"], capture_output=True, text=True, timeout=10)     # handed to the real open
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("open", (r.stdout + r.stderr).lower())
        finally:
            subprocess.run(["rm", "-rf", str(d)])

    def test_closing_the_window_switches_off_the_tools_shown_in_it(self):
        self.start_app(env={"TOOLBOX_LAUNCHER_AFTER_CLOSE": "2"})
        self.launch("good")
        self.launch("stubborn")
        wait_for(lambda: self.tool("good")["state"] == "running" and self.tool("stubborn")["state"] == "running", 10)
        stream = urllib.request.urlopen(f"{self.base}/api/stream?token={self.token}", timeout=10)
        self.post("/api/tabs", {"keys": ["good", "no-such-tool"]})            # only "good" is shown inside the window
        stream.close()
        self.assertEqual(wait_for(lambda: self.app.poll() is not None, 25), True, "the app did not quit after its window closed")
        self.assertEqual(pids_matching(self.tag + " good"), [], "a tool shown in the closed window is still running")
        self.assertTrue(pids_matching(self.tag + " stubborn"), "a tool that was not in the window was stopped too")

    def test_requests_from_elsewhere_are_refused(self):
        self.start_app("--keep-alive")
        for kwargs in ({"token": "wrong"}, {"host": "evil.example:80"}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.post("/api/stop", {"key": "good"}, **kwargs)
            self.assertEqual(e.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/stop", {"key": "no-such-tool"})
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/api/stream?token=wrong", timeout=5)
        self.assertEqual(e.exception.code, 403)

    def test_quits_when_its_window_closes_and_survives_a_reload(self):
        self.start_app(env={"TOOLBOX_LAUNCHER_AFTER_CLOSE": "2"})
        stream = urllib.request.urlopen(f"{self.base}/api/stream?token={self.token}", timeout=10)
        self.assertTrue(stream.readline().startswith(b"retry"))
        stream.close()                                           # a reload: the window goes and comes straight back
        stream = urllib.request.urlopen(f"{self.base}/api/stream?token={self.token}", timeout=10)
        time.sleep(6)
        self.assertIsNone(self.app.poll(), "a reload must not quit the app")
        stream.close()                                           # the window is closed for good
        self.assertEqual(wait_for(lambda: self.app.poll() is not None, 12), True, "the app did not quit after its window closed")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()

    def test_quits_when_its_launcher_is_gone(self):
        launcher = subprocess.Popen(["sleep", "600"])
        self.started.append(launcher)
        self.start_app(env={m.LAUNCHER_ENV: str(launcher.pid)})
        launcher.kill()
        launcher.wait()
        self.assertEqual(wait_for(lambda: self.app.poll() is not None, 8), True, "the app outlived its launcher")


if __name__ == "__main__":
    unittest.main()
