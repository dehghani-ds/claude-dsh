# Claude Workbench

A local web dashboard for [Claude Code](https://claude.com/claude-code): browse your past sessions by
project, chat with Claude, run `!` shell commands, manage git worktrees, and organise folders and chats
with groups and tags. It's two files (`server.py` + `index.html`), Python standard library only.

## Requirements

- Linux (tested on Ubuntu) with **Python 3.8+**
- **Claude Code** installed and logged in — the `claude` command must work in your terminal
- **git** (for the worktree features)

## Run it

```bash
python3 server.py            # then open http://127.0.0.1:8765/
python3 server.py --port 9000
```

## Start it automatically at login (Ubuntu / systemd)

```bash
./install-service.sh                  # install and start on port 8765
./install-service.sh --port 9000      # another port
./install-service.sh --dry-run        # just show the service file it would install
./install-service.sh --uninstall      # stop and remove it
```

The installer creates a **user** service (no `sudo` needed) at `~/.config/systemd/user/workbench.service`
for the folder you run it from, so put this folder where you want it to live first. If you move the folder
later, run the installer again. It also copies your current `PATH` into the service so it can find
`claude` and `git`: if you install Claude Code somewhere new, run it again too.

Useful commands:

```bash
systemctl --user status workbench       # is it running?
systemctl --user restart workbench      # restart (or use the ⏻ button in the page)
journalctl --user -u workbench -f       # logs
```

## Good to know

- **Local only.** The server listens on `127.0.0.1` and every request needs a random key that is put into the
  page on each start, so other sites and other machines can't use it. Anyone logged in as *you* on this
  computer can, just like your terminal.
- **Your data.** Sessions are read from Claude Code's own files in `~/.claude/projects/` (the dashboard only
  ever adds to them, e.g. when you rename a chat). Groups, tags and the updates inbox are stored in
  `workbench.db` next to `server.py`.
- **Which Claude account.** Chats run `claude -p`, which uses `ANTHROPIC_API_KEY` if that is set in the
  server's environment, otherwise your normal `claude` login.
- **Permissions.** In the page Claude can't stop to ask you, so anything the chosen *Mode* doesn't allow is
  denied. `bypassPermissions` lets Claude run anything — use it with care.
- **After updating the files**, click **⏻** (it shows an orange dot when `server.py` changed) so the server
  loads the new version.
