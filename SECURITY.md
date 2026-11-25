# Security and privacy

CacheDash is meant to be read before you run it. This page lists everything it touches, so you can check each claim against the code.

## At a glance

- **Python standard library only.** No third-party packages, nothing downloaded at install or run time.
- **No telemetry.** CacheDash never contacts the author or any analytics service.
- **The dashboard is one self-contained HTML file.** It loads no external scripts, fonts or trackers, and only talks to your own caching Macs.
- **Read-only toward macOS.** It reads Apple's databases and logs (including a snapshot copy of the cache's index, AssetInfo.db, and the names and sizes of cached files) and runs status commands. It never changes Content Caching settings, deletes cached content or modifies the system. Its only writes are its own files in `/usr/local/cachedash` and its log at `/var/log/cachedash.log`.

## Why it runs as root

`install.sh` installs a LaunchDaemon that runs `cachedash.py` as root, because:

- Apple's `Metrics.db` is readable only by root.
- Per-device byte counts come from `nettop` on the `AssetCache` process, which needs root.
- `lsof` on the `AssetCache` process (open-connection counts for incident diagnosis) needs root.

If you'd rather not run it as root, you can start it by hand with `--no-clients` and point `--db` at a copy of `Metrics.db`. Device and incident features then stop working.

## Commands it runs

Every command is run directly (never through a shell) with fixed arguments:

| Command | Purpose | File |
|---|---|---|
| `/usr/bin/AssetCacheManagerUtil -j status` | Storage, health and peer widgets | cachedash.py |
| `/usr/bin/log stream` / `log show` with a predicate limited to `com.apple.AssetCache` | Device downloads, log viewer | clientmon.py, logs.py |
| `/usr/bin/nettop -p <AssetCache pid>` | Bytes sent to each device | clientmon.py |
| `pgrep`, `ps`, `top`, `vm_stat`, `sysctl`, `pmset -g therm` | CPU, memory, uptime, temperature | incident.py, clientmon.py |
| `lsof -p <AssetCache pid>`, `netstat -s / -L / -I` | Connection limits and link errors when traffic drops | incident.py |
| `git pull`, `python3 -m unittest`, `install.sh` (via `updater.sh`, launchd job `local.cachedash.updater`) | Only when you run `git up` or click Update with the right PIN | update.sh, updater.sh |
| `defaults read /Library/Preferences/com.apple.AssetCache.plist DataPath` | Where the cache keeps its files (for download details) | cachestore.py |
| `ifconfig`, `route -n get default` | Find the network interface and its speed | alerts.py, clientmon.py |
| `defaults read /Library/Preferences/com.apple.SoftwareUpdate CatalogURL` | Use your Software Update catalog if you set one | catalog.py |

The only user input that reaches a command is a request ID from the log viewer's trace, which must be 6–20 letters and digits (`logs.py`, `trace`). Ordinary log searches, including device addresses, are matched in Python after the lines are read.

## Outgoing network connections

| To | When | Turn off |
|---|---|---|
| Apple's public update catalogs (`swscan.apple.com`, `mesu.apple.com`) | About once a day, to show real names for updates | `--no-catalogs` |
| Apple download addresses | Only when someone clicks **Check who answers** in the log viewer. One request for 1 byte, refused for any non-Apple host (`logs.py`, `APPLE_DOMAINS`) | Don't click it |
| Apple's App Store lookup (`itunes.apple.com/lookup`) | Every 12 hours, once you've loaded an app inventory: the bundle IDs of your apps (app names only if an export has no bundle IDs). No device information is sent. | `--no-apps` |
| Mosyle Manager API (`managerapi.mosyle.com`) | Every 6 hours, only if `mosyle.json` exists. Logs in with your API token and admin account and reads the device list with installed apps. | Don't create `mosyle.json` |
| GitHub (`raw.githubusercontent.com`) | Twice a day, only if CacheDash was installed from a git clone: reads the `VERSION` line of `cachedash.py` in your repository to say whether an update is available. Nothing is downloaded or installed automatically; `git up` is always run by you. | `--no-update-check` |
| Your other caching Macs (port 8027, never through a proxy) | When the installer checks an address you type; every 30 minutes for this cache's peers and parents (does CacheDash answer?); every 6 hours to tell the Macs in `servers.json` about this one | `--no-cluster` |
| Your webhook / SMTP server | Only if you configure them in `alerts.json` | Leave them empty |
| Your other caching Macs | Your browser fetches their API when you add them to the dashboard | Don't add them |

## Who can reach the dashboard

The server listens on port 8027 (change with `--port`, or bind to one address with `--bind`). It has **no login**, so it limits who can connect:

- **Only computers on private networks** (10.x, 172.16–31.x, 192.168.x, loopback, IPv6 ULA/link-local) may connect. Use `--allow CIDR` (repeatable) to narrow this to, say, your IT subnet, or widen it. The caching Mac itself (127.0.0.1) is always allowed, so the installer's start-up check and the desktop widget keep working. With the installer: `sudo CACHEDASH_ALLOW="10.0.5.0/24" zsh install.sh "Cache 1"`.
- **Browsers only let pages served from private addresses** (another caching Mac's dashboard) read the API. A website a staff member has open can't read device data or mute alerts. Add an exact origin with `--allow-origin https://dash.example.org`.
- **Another caching Mac can add itself to this dashboard** (`POST /api/cluster/join`), and only itself: the address added is the one the request came from, it's added only after this Mac checks that CacheDash answers there, browser requests are refused, and the list is capped at 12. It only changes which Macs the dashboard shows. Narrow `--allow` if other computers on your network shouldn't be able to do this.
- **The Update button needs a PIN** set on the caching Mac (`sudo zsh install.sh --pin`). Only a salted PBKDF2-SHA256 hash is stored (`update-pin.json`, mode 600); five wrong PINs lock it for 10 minutes. A correct PIN can only do one thing: run `git up` on the clone CacheDash was installed from (its path comes from `source.json`, written by the installer), as the owner of that clone, with the usual tests, start-up check and rollback. The person clicking can't choose what gets installed. Anyone who can push to your GitHub repository can therefore change what the button installs, so protect that repository (2FA, fine-grained tokens), as you would for `git up`. No PIN set means the button is off.
- **Changes (acknowledge, mute, test alert, app-inventory upload) must be sent as JSON**, uploads are capped at 30 MB, which stops form-based cross-site requests.

Anyone allowed to connect can see device addresses, device names and download names, can acknowledge or mute alerts, and can save or delete saved views (layouts only; they're checked against the list of real widgets before being stored). If that's too broad, use `--allow` to limit it to your IT staff's subnet, or a firewall rule on port 8027. Don't expose port 8027 to the internet.

Known limitation: these checks don't defend against DNS-rebinding attacks. Restricting `--allow` to an IT subnet is the strongest protection.

## Data it stores

All in `/usr/local/cachedash`:

| File | Contents | Kept |
|---|---|---|
| `clients.db` | Device IP, model, OS, what it downloaded, bytes | 30 days |
| `history.db` | Daily totals, bytes and downloads per device IP, download names | Until you delete it |
| `alerts.db` | Incidents and system samples | Incidents 180 days, samples 14 days |
| `apps.db` | App inventory from your MDM: device serial numbers and names, app bundle IDs and versions, and version changes | Inventory until the next sync; changes 60 days |
| `mosyle.json` | Mosyle API token, admin email and password (mode 600, never sent to the browser). Use a dedicated Mosyle admin account with read-only rights. | — |
| `update-pin.json` | Salted hash of the Update button PIN (mode 600) | Until changed |
| `views.json` | Saved dashboard layouts (widget names, order, sizes) | Until deleted |
| `update-status.json` | Progress of the last update from the dashboard | Until the next one |
| `alerts.json` | Webhook URL and SMTP password, if you set them (mode 600, never sent to the browser) | — |
| `clients.csv`, `servers.json` | Your device names and other caches, if you create them | — |

Device records are tied to network addresses, not to people. If your district's privacy rules limit how long you keep device data, delete `history.db` on a schedule or run with `--no-history`.

## Escaping

Device names, download names and log lines can be influenced by anyone on the network, so the dashboard escapes all of them before display (`esc()` in `dashboard.html`). This was tested by putting script payloads in device names, download names, the server name and alert text, then opening every view. None ran.

## How this was built

CacheDash was written with an AI assistant (Claude, by Anthropic), directed by a school IT administrator. That's why this page, the tests in `tests/` and plain, unminified source are here: so you don't have to take anyone's word for it, human or AI. Please review it yourself, and report problems through the repository's issue tracker or a private security advisory.

## Checking it yourself

```sh
python3 -m unittest discover -s tests -v      # access rules and report maths, no cache needed
grep -n "subprocess\|urlopen" *.py            # every command and network call
grep -n "<script src\|https://" dashboard.html # the dashboard loads nothing external
```
