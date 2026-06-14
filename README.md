# AutoTorrent

A self-contained, Dockerized torrent search-and-download tool. It runs
**Jackett** (multi-tracker search) and **qBittorrent** (the download client)
in containers, and gives you a rich interactive terminal app to search across
your indexers and send torrents straight to qBittorrent — no manual copying of
magnet links.

```
╭──────────────────────────────────────────────╮
│   AutoTorrent  search → download              │
╰────────────────────── Jackett + qBittorrent ──╯
  ✓ Jackett      Connected
  ✓ qBittorrent  Connected

Search: your search here
```

---

## Features

- 🔎 **Aggregated search** across every indexer you've configured in Jackett
- 🧠 **Smart grouping** — collapses the same release across trackers/qualities into one row
- 🎚️ **Filters** — quality (4K / 1080p / 720p / SD), minimum seeders, max size
- ↕️ **Sorting** — by seeders, size, or date
- 📊 **Live download view** — progress, speed, ETA, state
- ⚡ **Result caching** so repeat searches are instant
- 🐳 **Fully containerized** — the only thing you install is Docker

---

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose)

That's it. You do **not** need Python, Jackett, or qBittorrent installed on your
machine — they all run in containers.

---

## Getting started

### 1. Clone the repo

```bash
git clone https://github.com/<your-username>/autotorrent.git
cd autotorrent
```

### 2. Create your config file

```bash
cp .env.example .env
```

Leave it as-is for now — you'll fill in the API key and password in the next steps.

### 3. Start the services

```bash
docker compose up -d
```

This launches Jackett and qBittorrent in the background. Give them ~10 seconds
to boot on first run.

### 4. Configure Jackett

1. Open **http://localhost:9117**
2. Click **+ Add indexer** and add the trackers you want to search (e.g. public
   ones like TorrentGalaxy).
3. Copy the **API Key** from the top-right of the page.
4. Paste it into your `.env`:
   ```
   JACKETT_API_KEY=the_key_you_just_copied
   ```

### 5. Configure qBittorrent

1. On first launch, qBittorrent generates a **temporary password**. Find it with:
   ```bash
   docker compose logs qbittorrent | grep password
   ```
2. Open **http://localhost:8080** and log in with `admin` / *(temporary password)*.
3. Go to **Tools → Options → Web UI** and set a **permanent password** so it
   stops changing on every restart. Click **Save**.
4. Put that password into your `.env`:
   ```
   QBIT_USER=admin
   QBIT_PASS=your_permanent_password
   ```

### 6. Run the app

```bash
docker compose run --rm script
```

You'll get the interactive search prompt. Type a title, apply filters, pick a
result by number, and it's sent to qBittorrent. Finished downloads appear in the
folder set by `DOWNLOADS_PATH` (default `~/Downloads/torrents`).

---

## Using the app

At the **search prompt**:

| Input | Action |
|-------|--------|
| *any text* | Search for that title |
| `downloads` | Show active downloads (progress / speed / ETA) |
| `history` | Show recent searches |
| `clearcache` | Clear the search cache |
| `exit` | Quit |

In the **results view**:

| Input | Action |
|-------|--------|
| `1`, `2`, … | Download that numbered result |
| `1,3,5` | Download multiple at once |
| `b` | Grab the single best (highest-seeded) result |
| `n` / `p` | Next / previous page |
| `d` | Show active downloads |
| `q` | Back to a new search |

---

## Managing the stack

```bash
docker compose up -d        # start Jackett + qBittorrent
docker compose stop         # stop (keeps containers)
docker compose down         # stop and remove containers (config volumes kept)
docker compose logs -f      # tail logs
docker compose restart      # restart everything
```

- **Web UIs:** Jackett at http://localhost:9117, qBittorrent at http://localhost:8080
- **Downloads:** land in `DOWNLOADS_PATH` from your `.env` (default `~/Downloads/torrents`)
- Jackett indexers and qBittorrent settings persist in Docker volumes across restarts.

---

## Configuration reference

All settings live in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `JACKETT_API_KEY` | — | **Required.** From the Jackett UI. |
| `DOWNLOADS_PATH` | `~/Downloads/torrents` | Where finished files land on your machine. |
| `QBIT_USER` | `admin` | qBittorrent Web UI username. |
| `QBIT_PASS` | — | qBittorrent Web UI password. |

The Python script also reads these (set in `docker-compose.yml`, rarely changed):
`JACKETT_URL`, `QBIT_URL`, `GROUPS_PER_PAGE`, `RESULTS_PER_GROUP`,
`REQUEST_TIMEOUT`, `SEARCH_CACHE_TTL`.

---

## Troubleshooting

**qBittorrent shows "Unreachable" / login fails**
The temp password changes on every restart. Set a permanent one in the Web UI
(step 5) and update `.env`. The compose file also whitelists the Docker network
so the script can connect even without a password.

**Search returns nothing**
Make sure you've added at least one working indexer in Jackett (http://localhost:9117)
and that your `JACKETT_API_KEY` in `.env` is correct.

**Typing in the app feels laggy**
That's Docker's TTY. The compose file sets `PYTHONUNBUFFERED=1` to minimize it.
If it persists, you can run the script directly with Python instead (it will talk
to the containerized Jackett/qBittorrent on localhost).

---

## Running without Docker (optional)

If you prefer to run the script directly against an existing Jackett/qBittorrent:

```bash
pip install -r requirements.txt
python3 autotv5.py
```

The script auto-loads `.env` from its own directory, so the same config applies.

---

## Disclaimer

This tool is for downloading content you have the legal right to access (Linux
ISOs, public-domain media, your own files, etc.). You are responsible for
complying with the laws in your jurisdiction.
