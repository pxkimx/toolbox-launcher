# Toolbox Launcher

One page that opens, shows and switches off every offline tool.

Open **Toolbox Launcher** from the Toolbox. It opens in the browser at `http://localhost:8773`. Closing that tab
quits it, and switches off the tools that were open inside it.

## Using it

- **Control room**: one module per tool. Flip its switch (or press **Start**) and the tool starts and opens on its
  own key in the rail across the top, inside this same page. Click a key to move between tools; **Control room**
  brings the rack back. The tools keep running while you look at another one.
- **↗ Browser tab** opens a tool in a separate browser tab instead. For a tool that is already open inside the
  page, it moves it out to a browser tab.
- **×** on a key, or flipping the switch off, switches that tool off. If it is using a lot of processor, the
  launcher asks first, because an analysis may be running.
- **All off** switches every tool off (two clicks). **Power** does the same and quits the launcher.
- Each module shows a lamp and a word for its state, a small screen with the last two minutes of processor use,
  a needle for the current use, and its port, running time, memory and where its window is.
- **Background only** means the tool itself is closed but processes from it are still there (a worker left
  behind, a test server). **Stop these** removes them.
- **Logbook**: what the launcher has done since it was opened. A longer record is in
  `~/Library/Logs/toolbox-launcher.log`.

Finished analyses are on disk and are not affected by switching a tool off.

## How it works

- **Opening** runs the tool's own app from the Toolbox with `open`, the same as a double-click, so the tool keeps
  its Dock icon, its log and its own quitting rules. When the page is to be shown inside the launcher, the tool's
  launcher is given a stand-in `open` first on its `PATH` (kept in
  `~/Library/Application Support/Toolbox Launcher/bin`) that ignores web addresses on this Mac, and `BROWSER` is
  set for tools that open a tab from Python. That is the only difference: no browser tab of its own.
- **Showing** is an ordinary frame pointing at the tool's address. A tool sees it as a window, so its own
  "quit when the window closes" rule keeps working.
- **Finding** what is running: a process belongs to a tool when it listens on the tool's port; or is a Python
  process, or the tool's launcher, whose working folder or command line is inside the tool's folder or its app in
  the Toolbox; or was started by such a process; or was left behind there with no parent. A Terminal or an editor
  that merely sits in a tool's folder is not matched, and the launcher never lists or stops itself.
- **Switching off** sends the tool's server and workers the same signal the Dock sends, waits four seconds, stops
  whatever is still left, and lets the tool's launcher end by itself.
- **Closing the launcher's window** switches off the tools shown inside it, then quits. Tools open in their own
  browser tabs are left alone.

## For a tool to open inside the page

Its page must not refuse to be framed (`X-Frame-Options`, `frame-ancestors`), and it must treat a frame owned by
another address as a real window (Paper Reader 1.5.2 does this with `PR_PREVIEW`). A tool that cannot do this
gets `"embed": false` in `tools.json` and opens in a browser tab.

## Adding a tool

Add an entry to `tools.json` (name, port, folder under `Offline Tools`, app name in the Toolbox), then run
`./scripts/build_app.sh`. The port table on `TEAM BOARD.md` is the reference.

## Files

- `launcher.py`: the whole server: opening, reading the process table, matching, stopping, and the app's own
  quitting. Standard library only; any Python 3.9 or newer.
- `static/`: the page. `tools.json`: the list of tools and the two folders.
- `scripts/build_app.sh`: builds `Toolbox Launcher.app` and copies it to `~/Desktop/Toolbox`.
  `scripts/app_launcher.sh` is the app's own launcher. `scripts/make_icon.py` draws the icon (needs Pillow).
- `tests/test_launcher.py`: `python3 -m unittest discover tests` (about 35 s). The matching rules are tested on
  made-up process tables; opening, stopping and the app's own quitting are tested for real on throw-away tools.

## Running it from the folder

    python3 launcher.py --list                     # print what is running and exit
    python3 launcher.py --port 8774 --keep-alive   # serve without quitting when no window is open

Port 8773 is the app's; 8774 is for test and preview runs.

## Licence

MIT (see `LICENSE`). Source: https://github.com/pxkimx/toolbox-launcher
