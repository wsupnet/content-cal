# ContentCal — Self-Hosted YouTube Content Calendar

A lightweight, self-hosted Docker app for planning and tracking video content
across multiple YouTube channels. Multi-user and multi-workspace, with a
six-stage pipeline (Idea → Filming → Editing → Ready → Scheduled → Published),
calendar / board / list / year views, goals, comments, and an activity feed.
It's made for solo creators and small crews self-hosting at home, and it stays
simple on purpose: one container, one database file.

Data is stored in a single **SQLite** database file (`calendar.db`) on your own machine.
(Older installs that used a `calendar.json` file are imported automatically on
first start and renamed to `calendar.json.migrated`.)

[![ContentCal demo — click to watch the full 90-second tour](docs/contentcal-demo-preview.gif)](docs/contentcal-demo.mp4)

▶ **[Watch the 90-second demo](docs/contentcal-demo.mp4)** — sign in, add a video, drag it across the board, comment, and switch views.

📚 **Full documentation lives in [`docs/`](docs/):**
[Overview](docs/OVERVIEW.md) · [User Guide](docs/USER_GUIDE.md) · [Developer Guide](docs/DEVELOPER_GUIDE.md)

---

## Features

- 📅 Six views: Month, List, Kanban (drag to change status), Backlog, Quarter, Year
- 🎬 Per-video status pipeline, short/long-form tagging, content pillars, links, notes
- 🎯 Goal tracking with a live progress bar
- 🟣 Up to 6 channels per workspace, each with its own color and profile image
- 🤝 Workspaces with owner/member roles, invite links, comments, and an activity feed
- ⏱️ Timezone-aware auto-publish for scheduled videos
- 📥 CSV import (with column mapping + duplicate detection) and export
- 🌙 Dark mode and responsive mobile layout
- 💾 Automatic daily database snapshots, with backup, download and one-click restore for the admin
- 🔒 Account-based auth (bcrypt + signed session cookies); all data stays on your server

---

## Quick Start

ContentCal runs on anything with Docker: Docker Desktop on Windows or Mac,
Docker on Linux, or a NAS with Docker support. These commands are the same on
all of them.

### 1. Get the code and start it

Clone or download this repo, open a terminal in its folder, then run:

```bash
docker compose up -d --build
```

You don't need to create any files first. Your data goes in a `data` folder
next to `docker-compose.yml`. To keep it somewhere else, see
[Choosing the data folder](#choosing-the-data-folder).

### 2. Open the app

- On the same machine: `http://localhost:8765`
- From another device: `http://<server-ip>:8765`

### 3. Create your admin account

The **first** person to open the app runs a one-time setup wizard that creates
the admin account and the first workspace. The wizard asks for a **setup
code**, which the server prints to its log on startup so a stranger can't claim
your install first:

```bash
docker logs contentcal
```

Look for the line with `First-run setup code`. (In a container manager, open
the container's logs.) The code changes on every restart until setup is done.
After that, sign in with your account; invite teammates from **Workspace
settings**. Sign-up is **invite-only**: people join through an invite link (set
`ALLOW_OPEN_SIGNUP=1` in `docker-compose.yml` to open it).

> ⚠️ Plain `http://` sends logins unencrypted, so use HTTPS (a reverse proxy or
> Tailscale) when exposing this beyond your home network, and then set
> `COOKIE_SECURE=1`. See [Security notes](#security-notes) before exposing it
> to the internet.
>
> The container runs as root unless you uncomment the `user:` line in
> `docker-compose.yml` (instructions are next to it).

### Choosing the data folder

To keep your data somewhere other than `./data`, create a file named `.env` next
to `docker-compose.yml` with one of these lines (`.env.example` has the same
examples):

```bash
# Linux
CONTENTCAL_DATA=/srv/contentcal
# NAS
CONTENTCAL_DATA=/volume1/docker/appdata/contentcal
# Windows (use forward slashes)
CONTENTCAL_DATA=C:/ContentCal/data
```

`data/` is ignored by Git, so `git pull` won't touch it, but deleting the app's
folder deletes it too. For a long-term install, a folder outside the repo is
safer.

### Moving an existing install

Stop the app, then copy your `calendar.db` and `.session_secret` into the new
data folder **before** starting it. If you see the setup screen afterwards, the
app isn't looking in the right folder: stop and check `CONTENTCAL_DATA`. Your
data hasn't been touched.

Once an install holds real data, you can also add `REQUIRE_EXISTING_DB=1` to
`.env`. The app then refuses to start if `calendar.db` is missing, instead of
creating an empty one. That's most useful when the folder is on a drive that
might not be mounted.

### Upgrading from a version that used `APPDATA`

`APPDATA` was replaced because Windows already has a variable with that name,
which overrode the one in `.env`. **Before you update**, change your `.env`.
`CONTENTCAL_DATA` is the folder holding `calendar.db`, so it now includes the
`contentcal` part. For example, this:

```bash
APPDATA=/volume1/docker/appdata
```

becomes:

```bash
CONTENTCAL_DATA=/volume1/docker/appdata/contentcal
REQUIRE_EXISTING_DB=1
```

If you set `APPDATA` as a stack variable in a container manager, change it there
too. `docker-compose.yml` no longer requires `REQUIRE_EXISTING_DB`, so add it
yourself if you want to keep that protection.

---

## Configuration

All settings are environment variables in `docker-compose.yml` (most are there,
commented out, with notes):

| Variable | Default | What it does |
|---|---|---|
| `CONTENTCAL_DATA` (in `.env`) | `./data` | Folder holding `calendar.db`, `.session_secret` and `backups/` |
| `REQUIRE_EXISTING_DB` (in `.env`) | off | `1` refuses to start without an existing database |
| `ALLOW_OPEN_SIGNUP` | off | `1` lets anyone create an account; otherwise invite-only |
| `COOKIE_SECURE` | off | `1` when served over HTTPS: HTTPS-only cookie + HSTS |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Your reverse proxy's IP/subnet, so each user gets their own rate limits. Only set it when **every** request goes through the proxy (see the compose file) |
| `BACKUP_KEEP` | `14` | How many daily database snapshots to keep in `backups/` (see [Data Backup](#data-backup)); `0` turns them off |
| `MAX_REQUEST_BYTES` | 2.5 MB | Largest channel save / CSV import chunk |
| `SETUP_CODE` | random per start | Fixed first-run setup code, if you'd rather set one |
| `SESSION_SECRET` | generated in the data folder | Signs login cookies; changing it signs everyone out |
| `ENABLE_API_DOCS` | off | `1` serves `/docs` (development only; loads from a CDN) |

---

## Security notes

- **One process, one container.** Login limits and the setup code live in
  memory, so run a single container with a single uvicorn process. Multiple
  workers or replicas aren't supported.
- **Give each app its own hostname** when exposing it. Browsers share cookies
  between every port on the same host, so other web apps on your server's IP
  receive ContentCal's login cookie too. Serving ContentCal on its own name
  (e.g. `calendar.example.com` through a reverse proxy, with
  `COOKIE_SECURE=1`) is the only way to keep it to itself. The cookie name is
  unique per install, so two ContentCal installs on one host no longer log each
  other out.
- **Reverse proxies:** set `FORWARDED_ALLOW_IPS` as described in the compose
  file, or every user shares one rate-limit bucket. Never trust an IP that
  other machines can also connect from.
- **Shared IPs** (NAT, Docker Desktop, Tailscale Serve): all users behind one IP
  share the per-IP limits, which are set generously for that reason. Login
  lockouts are per account and IP, and a browser that has signed in before
  keeps its own allowance, so a stranger can't lock you out of your account.
- The content security policy blocks the page from fetching or loading
  anything from other servers. It can't stop a page from navigating away, so
  it limits the damage of a script-injection bug rather than ruling it out.

---

## NAS and Container Managers

ContentCal's `docker-compose.yml` mounts `./frontend` and `./backend/main.py`
relative to its own folder, so it needs the whole repo on the machine. Pasting
the compose file into a container manager on its own won't work.

1. Put the repo in a folder on your server, for example
   `/volume1/docker/apps/contentcal`
2. Set `CONTENTCAL_DATA` to a folder for your data, either in a `.env` next to
   `docker-compose.yml` or as a stack variable in your container manager
3. In your container manager (Dockhand, for example), add the stack from that
   folder's `docker-compose.yml`. The project name is `contentcal`
4. Deploy, then get the setup code from the container's logs

---

## Updating

From the app's folder:

```bash
git pull
docker compose up -d --build
```

Your data folder is untouched by `git pull`.

---

## Data Backup

Your entire calendar — every workspace, channel, video, comment, and account —
lives in one SQLite file in your data folder: `calendar.db`.

Don't back up `calendar.db` itself: a copy taken while someone is saving can
be corrupt. Instead, the app writes a consistent snapshot to the `backups`
folder next to it when it starts and once a day after that, and keeps the 14
newest (`BACKUP_KEEP` changes how many), named like
`backups/calendar-20261102T031500Z.db`.

Point your normal backup tool (File History, Time Machine, rsync, Hyper Backup,
etc.) at that `backups` folder. A snapshot only appears once it's fully written, so it's
safe to copy at any time. Also back up the `.session_secret` file in the data
folder; restoring it keeps users logged in.

### Restoring

Sign in with the admin account (the one created at first-run setup) and open
**Server backups** from the user menu. From there you can:

- **Back up now** — take a snapshot on demand, e.g. before a big CSV import.
- **Download** a snapshot to keep a copy off the server.
- **Restore** any snapshot in the list, or **Restore from file** to upload one
  you downloaded earlier or pulled from your own backup.

A restore replaces everything on the server (every workspace, account, video
and comment) and signs everyone out, so people sign in again with their
accounts as they were in the backup. The current data is saved as a snapshot
first, so a restore can itself be undone from the same list. Only the admin
sees this menu.

If the app won't start at all, restore by hand instead: stop the container,
replace `calendar.db` with a snapshot, delete any `calendar.db-journal` next
to it, and start the container again:

```bash
docker compose stop contentcal
cp backups/calendar-20261102T031500Z.db calendar.db   # run in the data folder
rm -f calendar.db-journal
docker compose start contentcal
```

On Windows, do the copy and delete in File Explorer instead.

Snapshots and the session key are readable only by the account the container runs as (root by
default) because they hold password hashes and the session key. Run backups as
an admin, or set the compose `user:` line to your own account so the files are
yours.

---

## Port

Default: `8765` — change the left side of the port mapping in `docker-compose.yml`:
```yaml
ports:
  - "NEW_PORT:8000"
```

---

## API Endpoints (optional direct access)

The core data routes (all scoped to your active workspace via the session cookie):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/data` | Get channels + entries + goal for the active workspace |
| POST | `/api/entries` | Add a new entry |
| PUT | `/api/entries/{id}` | Update an entry |
| DELETE | `/api/entries/{id}` | Delete an entry |
| PUT | `/api/channels` | Update all channels (send `X-Channels-Version` header) |

There are many more routes for auth, workspaces, members, invites, and comments.
See the **[full API reference](docs/DEVELOPER_GUIDE.md#15-api-reference)** in the
Developer Guide. Interactive OpenAPI docs (`/docs`) are off unless you set
`ENABLE_API_DOCS=1`.

---

## Development

The backend has a pytest regression suite (~140 tests, ~60s). It runs inside
the app's Docker image, so the only requirement is Docker, on any OS. Common
dev tasks are wrapped in a top-level `Makefile`:

```bash
make help            # list all targets
make setup           # one-time: build the test image + wire up the pre-push hook
make test            # run the regression suite (in Docker)
make test-local      # run it on your own Python 3.11+ instead
make install-hooks   # (re-)wire the pre-push hook on a fresh clone
```

After `make install-hooks`, every `git push` runs the test suite first and
blocks if anything fails. Bypass in a pinch with `git push --no-verify`.

Tests live in `backend/tests/` — see `backend/tests/README.md` for details.

