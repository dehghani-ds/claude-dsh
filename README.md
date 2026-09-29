# Claude Workbench

A local web dashboard for [Claude Code](https://claude.com/claude-code). Browse all your past sessions by
folder, chat with Claude (several chats at once), run `!` shell commands, manage git worktrees, keep
to-dos and notes per chat, organise folders and chats with groups and tags, and get told when Claude Code
changes. It's two files (`server.py` + `index.html`), Python standard library only.

- [Requirements](#requirements) · [Run it](#run-it) · [Start at login](#start-it-automatically-at-login-ubuntu--systemd)
- [Features](#features) · [Your data](#your-data) · [Good to know](#good-to-know) · [Troubleshooting](#troubleshooting) · [For developers](#for-developers)

## Requirements

- Linux (tested on Ubuntu) with **Python 3.8+**
- **Claude Code** installed and logged in — the `claude` command must work in your terminal
- **git** (for the worktree features)
- A modern browser (Chrome, Firefox, Edge)

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
./install-service.sh --uninstall      # stop and remove it (your data is kept)
```

The installer creates a **user** service (no `sudo` needed) at `~/.config/systemd/user/workbench.service`
for the folder you run it from, so put this folder where you want it to live first. If you move the folder
later, run the installer again. It also copies your current `PATH` into the service so it can find
`claude` and `git`: if you install Claude Code somewhere new, run it again too. It checks that the port is
free and that the page answers before it finishes.

```bash
systemctl --user status workbench       # is it running?
systemctl --user restart workbench      # restart (or use the ⏻ button in the page)
journalctl --user -u workbench -f       # logs
```

---

## Features

The page has three columns: **folders** (left), **chats and worktrees of the selected folder** (middle)
and **the open chat** (right). On a phone they become three tabs. Almost everything has a tooltip — hover
over it to see what it does.

### Top bar (left)

| Button | What it does |
|---|---|
| **⏻** | Restart the dashboard server (e.g. after updating `server.py`). An **orange dot** means `server.py` changed since the server started. Works both as the systemd service and when started by hand; the page reloads by itself. |
| **🔔** | Updates inbox — see [Claude Code updates](#claude-code-updates-). The number is how many are unread. |
| **⚙** | Manage groups and tags: rename, recolor, reorder, delete. |
| **◐** | Switch light / dark theme. |
| **↻** | Reload folders, chats, groups and tags from disk. |

### Folders and chats (left column)

- **Every folder you've used Claude Code in**, newest first, with its number of chats. Badges: **working**
  (a chat there is running), **worktree**, **missing** (the folder was deleted or moved — its chats can
  still be read).
- **A tree:** click a folder to select it and unfold its chats; click it again (or ▶) to fold. Click a chat
  to open it. Long folders show 8 chats and a "Show all" link.
- **Folders and chats look different:** folders are bold with a filled folder icon; chats are lighter,
  indented, with a chat icon.
- **Double-click a chat** to rename it (see [Renaming](#renaming)).
- **>\_** opens a terminal window in that folder. **🏷** sets the folder's group and tags.
- **＋ Chat in folder…** starts a new chat in any folder you pick. **＋ Group** creates a group.

**Groups and tags** — folders *and* chats can have one group and any number of tags (shared between them).

- Set them with **🏷** (on a folder, or on a chat in either list), or **drag** a folder or chat onto a group.
- A chat whose group differs from its folder's also appears in that group as a 💬 shortcut, so a group can
  collect single chats from anywhere.
- Click a group heading to fold it; **double-click it to rename** the group.

**Filters** (above the list):

- **Search** matches folder paths, chat titles and tag names; start with `#` to match tags only.
- **▣ Group** dropdown: one group, or *Ungrouped*.
- **🏷 Tags** dropdown with checkboxes and **Match all / any**.
- **▦ / ☰** switches between grouped view and one flat list (most recent first).
- A folder shows if it matches, or if some of its chats match (then only those chats are listed).
  Active filters appear as chips with ×, next to "N of M" and **Clear**.

### Chats and git worktrees (middle column)

- The selected folder's chats, newest first, with age, number of messages, git branch and cost.
  Badges: **working**, **● new** (finished while you were elsewhere), **📝3** (open to-dos).
- **New chat** starts a chat in this folder; **>\_** opens a terminal here; **🏷** edits its group and tags.

**Git worktrees** (for folders that are git repositories):

- **Chats are grouped by the worktree they ran in** — the main checkout, each worktree (named after its
  folder, with its branch), and removed worktrees (crossed out, folded) so old chats stay readable.
  Badges: **main**, **changes** (uncommitted changes), **here** (the folder you selected).
- **＋ New** creates a worktree in `.claude/worktrees/<name>`: pick a name, a new or existing branch and
  what to base it on, then review a summary (including the exact `git worktree add` command) and confirm.
  Optionally start a chat in it right away.
- **⤵ Apply** brings a worktree's changes into a branch (e.g. `main`). It first shows a **preview**: the
  commits and files, whether it applies cleanly (a dry-run merge that changes nothing), files that would
  **conflict** (then Apply is blocked), "nothing to apply" if it's already in, and uncommitted changes
  (optionally commit them first). Choose **Merge** (keep all commits) or **Squash** (one commit), a commit
  message, and optionally **remove the worktree afterwards** (its branch is always kept). If git still
  hits a problem, the merge is undone — nothing is left half-done.
- **✕** removes a worktree folder (you're warned about uncommitted changes; the branch is kept).
  **💬** starts a chat in it; **>\_** opens a terminal in it. **⇕** folds/unfolds all groups.

### The chat (right column)

- **Full transcripts:** Claude's answers (markdown), its thinking (folded), every tool call with its input
  and output (folded), your messages as bubbles, pasted text as a folded "📋 Pasted text" box, and your
  commands as terminal blocks with their output.
- **Streaming replies** with a live status ("Thinking…", "Running Bash…", "Writing…"). **■ Stop** stops it.
- **Several chats at once:** switch chats freely while others keep working in the background. The
  **⟳ N working** button in the header lists them — click one to jump to it. A toast and a **● new** badge
  tell you when a background chat finishes.
- **Mode** (permission mode), **Model** and **Effort** apply to the next message. Hover each option for
  what it means.
- Under each reply: time, cost and number of turns; a warning if tool calls were denied by the mode.
  A line at the start of each reply shows the Claude Code version, model, mode, MCP servers and plugins,
  with warnings for servers that failed to connect or plugins that failed to load.
- **⧉ Terminal cmd** copies `cd <folder> && claude --resume <id>` to continue the same chat in a terminal.
- **Slash commands:** `/usage`, `/cost` and `/context` work (answered locally, no usage). `/help`,
  `/status` and other interactive screens only exist in the terminal app — use **>\_** for those.

**`!` shell mode** — start a message with `!` (e.g. `!git status`) to run it in bash instead of sending it
to Claude, like in the terminal app:

- The box turns dark and **Send** becomes **Run**; output streams into a terminal block ending in
  ✓ exit 0 / ✗ exit N / ■ stopped. **■ Stop** kills the command and anything it started.
- It runs in the folder the session was last working in, and **`cd` carries over** to the next command.
  Each block shows the folder it ran in.
- Like the terminal app, the commands and their output are **sent to Claude with your next message**
  (a 📎 chip shows what will be sent; **✕ don't send** drops it).
- There is no terminal attached, so programs that wait for typing (a `sudo` password, `vim`, `less`) don't work.

### Notes & to-dos per chat (📝)

The **📝** button in the chat header opens a panel for that chat, saved in the database (still there after a
reload or reboot). Chats with open to-dos show a **📝N** badge in the lists, so you remember where you were.

- **☐ To-do** (with a checkbox; done items fold into *Done*) or **📝 Note**. Enter adds, Shift+Enter is a
  new line, double-click edits, 🗑 deletes.
- **▶ Run** sends the item to this chat as a prompt (or runs it as a shell command if it starts with `!`).
- **⏭ Queue** runs it after the current reply, one after another (right away if Claude is idle); **⏸**
  takes it out of the queue. Queues keep running when you switch to another chat.
- Each item shows its status: ⏳ queued · ⟳ running · ✓ applied · ✗ failed. **A failed item pauses the
  queue**; **▶ Run queue (N)** continues it. After a page reload, queued items wait for that button.

### Renaming

- **Chats:** double-click a chat's title in either list, or the title at the top of the open chat.
  Enter or clicking away saves, Esc cancels. It's the same as `/rename` in the terminal — `claude --resume`
  shows the new name too.
- **Groups:** double-click a group heading in the left column (or rename in **⚙**).

### Claude Code updates (🔔)

The server checks four things at start-up, every 6 hours, and when you click **↻ Check now**:

| Check | Source |
|---|---|
| New versions and features | Claude Code's public [changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) (or Claude Code's cached copy when offline) |
| Installed version changed | `claude --version` |
| CLI options added or removed | `claude --help` — with a ⚠ warning if an option this dashboard uses disappears |
| New session-file formats | record types in `~/.claude/projects/` this dashboard doesn't show yet |

- New items show as a number on 🔔, a dot on the browser tab icon, and a banner above the chat.
- Tabs **All / Unread / 🔧 Dashboard** (items that may need a change in this dashboard). Opening an item
  marks it read; you can mark read/unread, select several (shift-click for a range) for batch actions, and
  delete (with undo). Deleted items don't come back.
- **✳ Ask Claude about impact** asks Claude (Haiku, no tools, not saved as a chat) what the change means for
  this dashboard: what could break and what could be added.

---

## Your data

| What | Where |
|---|---|
| Your chats | Claude Code's own files in `~/.claude/projects/` — read directly. The dashboard only adds to them (a rename adds a title line) and never deletes anything. |
| Groups, tags, notes & to-dos, updates inbox | `workbench.db` (SQLite) next to `server.py`. Back it up by copying the file. |
| Small page settings (theme, filters, folded groups, open panels) | your browser's local storage |
| Worktrees | `<repo>/.claude/worktrees/<name>` — add `.claude/` to the repo's `.gitignore` |

## Good to know

- **Local only.** The server listens on `127.0.0.1` and every request needs a random key that is put into the
  page on each start, so other sites and other machines can't use it. Anyone logged in as *you* on this
  computer can, just like your terminal.
- **Which Claude account.** Chats run `claude -p`, which uses `ANTHROPIC_API_KEY` if it's set in the server's
  environment, otherwise your normal `claude` login (your subscription).
- **Permissions.** In the page Claude can't stop to ask you, so anything the chosen *Mode* doesn't allow is
  denied (the reply tells you). `bypassPermissions` lets Claude run anything — use it with care.
- **Restarting** the server stops chats and commands that are still running. The ⏻ confirmation lists them.

## Troubleshooting

| Problem | Fix |
|---|---|
| A button says **"not found"** | The running server is older than the page. Click **⏻** (or `systemctl --user restart workbench`). |
| **429 / rate limit of 0 tokens per minute** | `ANTHROPIC_API_KEY` points at a Console workspace with a 0 limit. Remove it from the server's environment (e.g. `~/.bashrc`) to use your subscription, or raise the limit in the Anthropic Console. |
| Every chat fails at once | You may have hit your usage limit — run `/usage` in a chat to see when it resets. |
| **>\_** doesn't open a terminal | It needs a desktop session. Set `WORKBENCH_TERMINAL` (e.g. `kitty`) in the service environment to choose the program. |
| `!` commands can't find a program | The service uses the `PATH` it was installed with — run `./install-service.sh` again from a terminal where the program works. |
| Worktree features missing | The folder isn't a git repository, or `git` isn't installed. |

## For developers

- **`server.py`** — the HTTP server (standard library `http.server`): reads sessions, runs
  `claude -p --output-format stream-json …` and streams it to the page, runs `!` commands, git worktree
  operations, the SQLite database and the update checks. `WORKBENCH_DB=/path/file.db` uses another database.
- **`index.html`** — the whole UI (plain JavaScript, no build step; `marked` + `DOMPurify` from a CDN for
  markdown). The page is read from disk on every load, so UI changes only need a browser reload; changes
  to `server.py` need a restart (⏻).
- **`install-service.sh` / `workbench.service`** — the systemd installer and its template.
- **Keeping the update checks accurate:** when the dashboard starts using new Claude Code features, update
  `DASHBOARD_BRIEF`, `USED_FLAGS`, `KNOWN_RECORDS` / `KNOWN_BLOCKS` and `RELEVANT_RE` in `server.py`.
