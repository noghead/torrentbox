import os
import re
import sys
import math
import time
import logging
from difflib import SequenceMatcher
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.prompt import Prompt
from rich.text import Text
from rich.rule import Rule
from rich.live import Live
from rich.spinner import Spinner

# ─────────────────────────────────────────────
# .ENV LOADER  (so direct runs work like Docker)
# ─────────────────────────────────────────────
def load_dotenv(path: str = None):
    """Minimal .env loader: sets vars not already present in the environment."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)  # real env wins over .env

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
JACKETT_URL     = os.environ.get("JACKETT_URL",     "http://127.0.0.1:9117")
JACKETT_API_KEY = os.environ.get("JACKETT_API_KEY", "")
QBIT_URL        = os.environ.get("QBIT_URL",        "http://127.0.0.1:8080")
QBIT_USER       = os.environ.get("QBIT_USER",       "admin")
QBIT_PASS       = os.environ.get("QBIT_PASS",       "adminadmin")

GROUPS_PER_PAGE   = int(os.environ.get("GROUPS_PER_PAGE", "5"))
RESULTS_PER_GROUP = int(os.environ.get("RESULTS_PER_GROUP", "4"))
REQUEST_TIMEOUT   = int(os.environ.get("REQUEST_TIMEOUT", "30"))
SEARCH_CACHE_TTL  = int(os.environ.get("SEARCH_CACHE_TTL", "600"))  # seconds

# Quality detection: ordered best→worst, first hit wins.
QUALITY_DEFS = [
    ("4K",    ("2160p", "4k", "uhd"),                 "bold magenta", 4),
    ("1080p", ("1080p", "1080i", "fhd"),              "cyan",         3),
    ("720p",  ("720p", "hd "),                        "blue",         2),
    ("SD",    ("480p", "576p", "dvdrip", "hdtv", "xvid"), "dim",      1),
]
QUALITY_FILTERS = {"4k": ("2160p", "4k", "uhd"),
                   "1080p": ("1080p", "1080i", "fhd"),
                   "720p": ("720p",),
                   "sd": ("480p", "576p", "dvdrip", "hdtv")}

QBIT_CATEGORIES = ["", "movies", "tv", "music", "books", "software", "other"]

# Tokens that are noise for grouping (codecs, sources, release groups, etc.)
_NOISE_RE = re.compile(
    r"\b(2160p|1080p|1080i|720p|480p|576p|4k|uhd|hdr10\+?|hdr|dolby|vision|dv|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|aac|ac3|eac3|dts|truehd|"
    r"atmos|ddp?5\.?1|bluray|blu-ray|brrip|bdrip|webrip|web-?dl|web|hdtv|dvdrip|"
    r"remux|proper|repack|extended|internal|limited|amzn|nf|dsnp|hmax|atvp|"
    r"multi|dual|complete|season)\b", re.I)
_SXXEYY_RE = re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})", re.I)
_SEASON_RE = re.compile(r"(?:\bs(\d{1,2})\b|season\s*(\d{1,2}))", re.I)
_YEAR_RE   = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SEP_RE    = re.compile(r"[._\-\[\]()]+")
_WS_RE     = re.compile(r"\s+")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), "autotorrent.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("autotorrent")

# ─────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────
console = Console(force_terminal=True, width=int(os.environ.get("COLUMNS", "160")))
search_history: list[str] = []
qbit_session = requests.Session()
_qbit_authed = False
_search_cache: dict[str, tuple[float, list[dict]]] = {}


# ─────────────────────────────────────────────
# HTTP SESSION (retry + connection pooling)
# ─────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

http = make_session()


# ─────────────────────────────────────────────
# FORMATTING
# ─────────────────────────────────────────────
def format_size(size_bytes: int, styled: bool = True) -> str:
    if not size_bytes or size_bytes <= 0:
        return "[dim]N/A[/dim]" if styled else "N/A"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    val = round(size_bytes / (1024 ** i), 2)
    text = f"{val} {units[i]}"
    if not styled:
        return text
    color = "green" if i >= 3 else "yellow" if i == 2 else "white"
    return f"[{color}]{text}[/{color}]"


def format_seeders(n: int) -> str:
    if n >= 100: return f"[bold green]{n}[/bold green]"
    if n >= 20:  return f"[green]{n}[/green]"
    if n >= 5:   return f"[yellow]{n}[/yellow]"
    return f"[red]{n}[/red]"


def quality_label(title: str) -> str:
    """Return a plain quality label: 4K / 1080p / 720p / SD / ?."""
    t = title.lower()
    for label, keywords, _color, _rank in QUALITY_DEFS:
        if any(k in t for k in keywords):
            return label
    return "?"


def style_quality(label: str) -> str:
    for lab, _kw, color, _rank in QUALITY_DEFS:
        if lab == label:
            return f"[{color}]{label}[/{color}]"
    return "[dim]?[/dim]"


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


# ─────────────────────────────────────────────
# GROUPING  (O(n) canonical-key, fuzzy fallback)
# ─────────────────────────────────────────────
@lru_cache(maxsize=4096)
def canonical_key(title: str) -> str:
    """Derive a stable grouping key from a release title in O(len).

    Same show+episode (or movie+year) across trackers/qualities collapses to
    one key, so grouping is a single dict pass instead of O(n²) fuzzy compares.
    """
    t = title.lower()
    t = _SEP_RE.sub(" ", t)

    m = _SXXEYY_RE.search(t)
    if m:
        name = t[:m.start()]
        marker = f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    else:
        ms = _SEASON_RE.search(t)
        my = _YEAR_RE.search(t)
        if ms:
            name = t[:ms.start()]
            num = ms.group(1) or ms.group(2)
            marker = f"s{int(num):02d}"
        elif my:
            name = t[:my.start()]
            marker = my.group(1)
        else:
            # No structural anchor: strip noise tokens, keep first 6 words.
            cleaned = _NOISE_RE.sub(" ", t)
            words = _WS_RE.sub(" ", cleaned).strip().split()
            return " ".join(words[:6])

    name = _WS_RE.sub(" ", _NOISE_RE.sub(" ", name)).strip()
    return f"{name} {marker}".strip()


def group_results(results: list[dict]) -> list[list[dict]]:
    """Group by canonical key (O(n)). Falls back to fuzzy merge only for the
    small set of keyless leftovers, keeping the expensive path bounded."""
    keyed: dict[str, list[dict]] = {}
    leftovers: list[dict] = []

    for res in results:
        key = res.get("_key", "")
        if key:
            keyed.setdefault(key, []).append(res)
        else:
            leftovers.append(res)

    groups = list(keyed.values())

    # Fuzzy-merge only the keyless remainder against existing group heads.
    for res in leftovers:
        best, score = None, 0.0
        title = res["Title"].lower()
        for g in groups:
            s = SequenceMatcher(None, title, g[0]["Title"].lower()).quick_ratio()
            if s > score:
                best, score = g, s
        if best is not None and score > 0.70:
            best.append(res)
        else:
            groups.append([res])

    # Order groups by their strongest seeder count.
    groups.sort(key=lambda g: max(r.get("_seeders", 0) for r in g), reverse=True)
    return groups


# ─────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────
def annotate(results: list[dict]) -> list[dict]:
    """Precompute derived fields once so filtering/sorting/display never recompute."""
    for r in results:
        title = r.get("Title", "")
        r["_seeders"] = r.get("Seeders") or 0
        r["_size"]    = r.get("Size") or 0
        r["_ql"]      = quality_label(title)
        r["_key"]     = canonical_key(title)
    return results


def fetch_results(query: str, category: str = "") -> list[dict]:
    """Query Jackett (aggregate indexers). Cached by query+category for TTL."""
    cache_key = f"{query}|{category}"
    now = time.time()
    hit = _search_cache.get(cache_key)
    if hit and now - hit[0] < SEARCH_CACHE_TTL:
        return hit[1]

    params = {"apikey": JACKETT_API_KEY, "Query": query}
    if category:
        params["Category[]"] = category  # narrows tracker payloads server-side
    resp = http.get(f"{JACKETT_URL}/api/v2.0/indexers/all/results",
                    params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    results = annotate(resp.json().get("Results", []))
    _search_cache[cache_key] = (now, results)
    return results


def apply_filters(results, quality, min_seeders, max_size_gb):
    max_bytes = max_size_gb * (1024 ** 3) if max_size_gb > 0 else 0
    kws = QUALITY_FILTERS.get(quality) if quality != "any" else None
    out = []
    for r in results:
        if r["_seeders"] < min_seeders:
            continue
        if max_bytes and r["_size"] > max_bytes:
            continue
        if kws and not any(k in r["Title"].lower() for k in kws):
            continue
        out.append(r)
    return out


SORTERS = {
    "seeders": lambda r: r["_seeders"],
    "size":    lambda r: r["_size"],
    "date":    lambda r: r.get("PublishDate", ""),
}


# ─────────────────────────────────────────────
# HEALTH CHECKS  (lightweight endpoints)
# ─────────────────────────────────────────────
def check_jackett() -> tuple[bool, str]:
    """Ping the Jackett UI — just check it responds, don't parse JSON."""
    try:
        r = http.get(f"{JACKETT_URL}/UI/Dashboard",
                     timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return True, "reachable"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        log.warning(f"Jackett health check failed: {e}")
        return False, str(e)[:40]


def check_qbittorrent() -> tuple[bool, str]:
    ok = qbit_login(force=True)
    return (ok, "authenticated") if ok else (False, "login failed")


def run_health_checks():
    console.print()
    overall = True
    for name, fn in (("Jackett", check_jackett), ("qBittorrent", check_qbittorrent)):
        with Live(Spinner("dots", text=f"  Connecting to {name}…"),
                  refresh_per_second=12, console=console):
            ok, detail = fn()
        overall &= ok
        mark = "[bold green]✓[/bold green]" if ok else "[bold red]✗[/bold red]"
        state = "[green]Connected[/green]" if ok else "[red]Unreachable[/red]"
        console.print(f"  {mark} [dim]{name:<12}[/dim] {state}  [dim]{detail}[/dim]")

    if not overall:
        console.print(Panel(
            "[yellow]A service is unreachable. Start the stack with:[/yellow]\n\n"
            "  [bold]docker compose up -d[/bold]",
            title="[red]Startup Warning[/red]", border_style="red"))


# ─────────────────────────────────────────────
# QBITTORRENT
# ─────────────────────────────────────────────
def qbit_login(force: bool = False) -> bool:
    global _qbit_authed
    if _qbit_authed and not force:
        return True
    try:
        r = qbit_session.post(f"{QBIT_URL}/api/v2/auth/login",
                              data={"username": QBIT_USER, "password": QBIT_PASS},
                              headers={"Referer": QBIT_URL}, timeout=REQUEST_TIMEOUT)
        _qbit_authed = r.status_code in (200, 204) and r.text.strip() not in ("Fails.", "Ban.")
    except Exception as e:
        log.warning(f"qBit login error: {e}")
        _qbit_authed = False
    return _qbit_authed


def qbit_post(path: str, data: dict):
    """POST with lazy auth + single retry on session expiry (403)."""
    qbit_login()
    headers = {"Referer": QBIT_URL}
    r = qbit_session.post(f"{QBIT_URL}{path}", data=data, headers=headers,
                          timeout=REQUEST_TIMEOUT)
    if r.status_code == 403:
        qbit_login(force=True)
        r = qbit_session.post(f"{QBIT_URL}{path}", data=data, headers=headers,
                              timeout=REQUEST_TIMEOUT)
    return r


def qbit_get(path: str):
    qbit_login()
    headers = {"Referer": QBIT_URL}
    r = qbit_session.get(f"{QBIT_URL}{path}", headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 403:
        qbit_login(force=True)
        r = qbit_session.get(f"{QBIT_URL}{path}", headers=headers, timeout=REQUEST_TIMEOUT)
    return r


def qbit_add(link: str, category: str = "", save_path: str = "") -> bool:
    payload = {"urls": link}
    if category:  payload["category"] = category
    if save_path: payload["savepath"] = save_path
    res = qbit_post("/api/v2/torrents/add", payload)
    body = res.text.strip()
    log.info(f"qBit add response: {res.status_code} – {body[:120]}")
    if res.status_code in (200, 202, 204) and body not in ("Fails.", "Ban."):
        return True
    return False


def show_active_downloads():
    try:
        torrents = qbit_get("/api/v2/torrents/info?sort=added_on&reverse=true").json()
    except Exception as e:
        console.print(f"[red]Could not fetch downloads: {e}[/red]")
        return
    if not torrents:
        console.print("[dim]No active downloads.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan",
                  border_style="dim", expand=True)
    for col, w in (("Name", None), ("Size", 11), ("Progress", 16),
                   ("Speed", 12), ("State", 14), ("ETA", 9)):
        table.add_column(col, width=w, no_wrap=bool(w), ratio=4 if w is None else None)

    state_colors = {"downloading": "green", "uploading": "cyan", "stalledUP": "cyan",
                    "pausedDL": "yellow", "pausedUP": "dim", "stalledDL": "red",
                    "checkingDL": "blue", "error": "bold red", "missingFiles": "bold red"}
    for t in torrents:
        state = t.get("state", "unknown")
        pct = t.get("progress", 0) * 100
        filled = int(pct / 10)
        bar = f"{'█' * filled}{'░' * (10 - filled)} {pct:>3.0f}%"
        dl = t.get("dlspeed", 0)
        speed = f"{format_size(dl, styled=False)}/s" if dl > 0 else "—"
        eta = t.get("eta", -1)
        if eta is None or eta <= 0 or eta >= 8640000:
            eta_str = "∞"
        else:
            h, m, s = eta // 3600, (eta % 3600) // 60, eta % 60
            eta_str = f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
        table.add_row(truncate(t.get("name", "?"), 60),
                      format_size(t.get("size", 0)), bar, speed,
                      f"[{state_colors.get(state, 'white')}]{state}[/]", eta_str)

    console.print(Panel(table, title="[cyan]Active Downloads[/cyan]", border_style="cyan"))


# ─────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────
def print_banner():
    console.print(Panel("[bold cyan]AutoTorrent[/bold cyan]  [dim]search → download[/dim]",
                        subtitle="[dim]Jackett + qBittorrent[/dim]",
                        border_style="cyan", padding=(0, 4)))


def prompt_filters():
    console.print("\n[bold]Filters[/bold] [dim](Enter to accept defaults)[/dim]")
    quality = Prompt.ask("  Quality", choices=["any", "4k", "1080p", "720p", "sd"],
                         default="any", show_choices=True).lower()
    try:    min_seeders = max(0, int(Prompt.ask("  Min seeders", default="1")))
    except ValueError: min_seeders = 1
    try:    max_size_gb = max(0.0, float(Prompt.ask("  Max size GB [dim](0=∞)[/dim]", default="0")))
    except ValueError: max_size_gb = 0.0
    sort = Prompt.ask("  Sort by", choices=["seeders", "size", "date"], default="seeders")
    return quality, min_seeders, max_size_gb, sort


def build_results_table(batch):
    flat_map: dict[str, dict] = {}
    counter = 1
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="dim", expand=True)
    table.add_column("#", style="bold", width=4, no_wrap=True)
    table.add_column("Title", min_width=30, ratio=4)
    table.add_column("Qual", width=7, no_wrap=True)
    table.add_column("Size", width=11, no_wrap=True)
    table.add_column("Seeds", width=7, no_wrap=True)
    table.add_column("Tracker", width=16, no_wrap=True)

    for group in batch:
        table.add_row("", f"[bold white]📂 {truncate(group[0]['_key'] or group[0]['Title'], 80)}[/bold white]",
                      f"[dim]{len(group)}×[/dim]", "", "", "", style="on grey11")
        for res in group[:RESULTS_PER_GROUP]:
            idx = str(counter)
            tracker = truncate(res.get("Tracker") or res.get("TrackerId") or "?", 15)
            table.add_row(f"[bold]{idx}[/bold]", truncate(res.get("Title", ""), 70),
                          style_quality(res["_ql"]), format_size(res["_size"]),
                          format_seeders(res["_seeders"]), f"[dim]{tracker}[/dim]")
            flat_map[idx] = res
            counter += 1
    return table, flat_map


def print_torrent_detail(res):
    lines = [
        f"[bold]{res.get('Title', 'Unknown')}[/bold]", "",
        f"  Size     : {format_size(res['_size'])}   [dim]({style_quality(res['_ql'])})[/dim]",
        f"  Seeders  : {format_seeders(res['_seeders'])}   Leechers: {res.get('Leechers', 'N/A')}",
        f"  Tracker  : {res.get('Tracker') or res.get('TrackerId') or 'Unknown'}",
        f"  Category : {res.get('CategoryDesc') or 'Unknown'}",
        f"  Published: {res.get('PublishDate', 'Unknown')}",
        f"  Link     : {'Magnet' if res.get('MagnetUri') else 'Torrent file'}",
    ]
    console.print(Panel("\n".join(lines), title="[cyan]Details[/cyan]", border_style="cyan"))


# ─────────────────────────────────────────────
# DOWNLOAD FLOW
# ─────────────────────────────────────────────
def handle_download(selected, ask_options=True):
    print_torrent_detail(selected)
    if Prompt.ask("  Download this?", choices=["y", "n"], default="y") != "y":
        console.print("[dim]Skipped.[/dim]")
        return

    category, save_path = "", ""
    if ask_options:
        category = Prompt.ask("  Category", choices=QBIT_CATEGORIES, default="", show_choices=True).strip()
        save_path = Prompt.ask("  Save path [dim](Enter = default)[/dim]", default="").strip()

    link = selected.get("MagnetUri") or selected.get("Link") or ""
    if not link:
        console.print("[red]No download link available.[/red]")
        return

    console.print("[yellow]⌛ Sending to qBittorrent…[/yellow]")
    try:
        if qbit_add(link, category, save_path):
            console.print(f"[bold green]🚀 Added![/bold green]  [dim]{truncate(selected['Title'], 60)}[/dim]")
        else:
            console.print("[red]❌ qBittorrent rejected the request.[/red]")
    except Exception as e:
        console.print(f"[red]❌ Connection error: {e}[/red]")


def search_and_download(query):
    console.print(f"\n[dim]Searching:[/dim] [bold]{query}[/bold]")

    with Live(Spinner("dots", text="  Querying Jackett…"), refresh_per_second=12, console=console):
        t0 = time.time()
        try:
            raw = fetch_results(query)
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            log.error(f"Search error '{query}': {e}")
            return
        elapsed = time.time() - t0

    if not raw:
        console.print("[red]❌ No results found.[/red]")
        return

    tag = "[green](cached)[/green]" if elapsed < 0.05 else f"[dim]{elapsed:.1f}s[/dim]"
    console.print(f"[dim]Found {len(raw)} results[/dim] {tag}")

    quality, min_seeders, max_size_gb, sort = prompt_filters()
    filtered = apply_filters(raw, quality, min_seeders, max_size_gb)
    if not filtered:
        console.print("[yellow]⚠  No results match your filters.[/yellow]")
        return

    filtered.sort(key=SORTERS[sort], reverse=True)
    groups = group_results(filtered)
    console.print(f"[dim]{len(filtered)} results → {len(groups)} groups (sorted by {sort})[/dim]")

    page = 0
    while True:
        start, end = page * GROUPS_PER_PAGE, page * GROUPS_PER_PAGE + GROUPS_PER_PAGE
        batch = groups[start:end]
        total_pages = max(1, math.ceil(len(groups) / GROUPS_PER_PAGE))

        console.print(Rule(f"[dim]Page {page + 1}/{total_pages}[/dim]"))
        table, flat_map = build_results_table(batch)
        console.print(table)

        opts = Text()
        if flat_map:
            hi = max(int(k) for k in flat_map)
            opts.append(f"[1-{hi}]", style="bold cyan"); opts.append(" get  ", style="dim")
            opts.append("[1,3]", style="bold cyan");     opts.append(" multi  ", style="dim")
        opts.append("[b]", style="bold"); opts.append(" best  ", style="dim")
        if page > 0:        opts.append("[p]", style="bold"); opts.append(" prev  ", style="dim")
        if end < len(groups): opts.append("[n]", style="bold"); opts.append(" next  ", style="dim")
        opts.append("[d]", style="bold"); opts.append(" downloads  ", style="dim")
        opts.append("[q]", style="bold"); opts.append(" new search", style="dim")
        console.print(opts)

        choice = Prompt.ask("  Select").strip().lower()
        if choice == "q":
            break
        elif choice == "b":
            handle_download(filtered[0])  # highest-ranked overall
        elif choice == "n":
            if end < len(groups): page += 1
            else: console.print("[dim]🏁 No more pages.[/dim]")
        elif choice == "p":
            page = max(0, page - 1)
        elif choice == "d":
            show_active_downloads()
        elif "," in choice:
            for idx in (c.strip() for c in choice.split(",")):
                if idx in flat_map:
                    console.print(Rule(f"[dim]#{idx}[/dim]"))
                    handle_download(flat_map[idx], ask_options=False)
                else:
                    console.print(f"[red]Unknown index: {idx}[/red]")
        elif choice in flat_map:
            handle_download(flat_map[choice])
        else:
            console.print("[yellow]⚠  Invalid selection.[/yellow]")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print_banner()
    if not JACKETT_API_KEY:
        console.print(Panel(
            "[yellow]JACKETT_API_KEY is not set.\nCopy it from http://localhost:9117 "
            "into your [bold].env[/bold] file.[/yellow]",
            title="[red]Configuration Error[/red]", border_style="red"))
        sys.exit(1)

    run_health_checks()
    console.print("\n[dim]Commands:[/dim] [bold]exit[/bold]  [bold]history[/bold]  "
                  "[bold]downloads[/bold]  [bold]clearcache[/bold]\n")

    while True:
        try:
            query = Prompt.ask("[bold cyan]Search[/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break
        if not query:
            continue

        low = query.lower()
        if low in ("exit", "quit"):
            console.print("[dim]Goodbye.[/dim]"); break
        if low == "history":
            for i, h in enumerate(reversed(search_history[-20:]), 1):
                console.print(f"  [dim]{i}.[/dim] {h}")
            if not search_history:
                console.print("[dim]No history yet.[/dim]")
            continue
        if low == "downloads":
            show_active_downloads(); continue
        if low == "clearcache":
            _search_cache.clear(); canonical_key.cache_clear()
            console.print("[dim]Cache cleared.[/dim]"); continue

        search_history.append(query)
        log.info(f"Search: {query}")
        search_and_download(query)


if __name__ == "__main__":
    main()
