# CacheDash: Content Cache dashboard for macOS 27 (v1.21)

> **Built with Claude.** CacheDash was written with [Claude](https://claude.ai), Anthropic's AI assistant, directed and tested by a school IT administrator. Read the code before you run it as root: [SECURITY.md](SECURITY.md) and [About this project](#about-this-project) explain what it does and how it was made.

A dashboard for Apple Content Caching where you pick which widgets appear. It reads the per-cache usage database that Apple documents for macOS 27:

`/Library/Application Support/Apple/AssetCache/Metrics/Metrics.db`

That database holds one row per active minute and keeps 30 days. CacheDash also calls the built-in `AssetCacheManagerUtil` to fill the storage, health and peer widgets.

Free and open source under the MIT License. It uses only the Python standard library, sends no telemetry, and the dashboard loads nothing from the internet. **[SECURITY.md](SECURITY.md)** lists every command it runs, every connection it makes and every file it keeps.

## Files

| File | What it does |
|---|---|
| `cachedash.py` | Small web server (Python standard library only). Reads Metrics.db and serves the dashboard and a JSON API. |
| `clientmon.py` | Device monitoring: live bandwidth per device and what each device downloads. |
| `alerts.py` | Watches the cache around the clock, records incidents, and can send notifications. |
| `alerts.example.json` | Copy to `alerts.json` to set a chat webhook, email and thresholds. |
| `incident.py` | Explains traffic drops: records connection, limit, error and system counters, and works out why traffic fell. |
| `logs.py` | Reads the AssetCache log for the dashboard's log viewer: groups repeated errors, traces one request, and exports `.log` files. |
| `appupdates.py` | App updates waiting: reads your MDM's app inventory (an uploaded CSV report from any MDM, or optional automatic sync) and checks each app against the App Store. |
| `mosyle.example.json` | Optional: settings for automatic app-inventory sync (see App updates). |
| `history.py` | Saves one summary per day in `history.db` so reports can cover more than Apple's 30 days. |
| `catalog.py` | Looks up real product names (for example "Safari 26.1" or "iPadOS 18.7") in Apple's public update catalogs. |
| `clients.example.csv` | Optional. Rename it to `clients.csv` to give devices friendly names. |
| `AssetCache-PrivateLogging.mobileconfig` | Optional profile, only needed if the device widgets show `<private>`. |
| `dashboard.html` | The dashboard. If no server is reachable, it shows sample data. |
| `SECURITY.md` | What CacheDash runs, connects to and stores, and who can reach it. |
| `tests/` | Automated checks of the access rules and report maths: `python3 -m unittest discover -s tests -v` |
| `LICENSE` | MIT License. |
| `install.sh` | Installs CacheDash to `/usr/local/cachedash` as a LaunchDaemon that starts at boot. Run again with no arguments to update: it keeps every setting, checks the new version starts, and puts the old one back if not. |
| `cluster.py` | Connects the caching Macs: finds the others, checks addresses, and tells each Mac about the others. |
| `selfupdate.py`, `updater.sh` | The dashboard's Update button: PIN check, and the small service that runs `git up` when you click it. |
| `cachestore.py` | Finds where a download is stored in the cache (read-only). |
| `views.py` | Saved views: named widget layouts kept on the caching Macs, the same on every computer. |
| `update.sh` | What `git up` runs: pulls the latest version, runs the tests, installs it. |
| `publish.sh` | Puts a new release zip into your GitHub repo (tests, copy, commit, push). |

## Install on the caching Mac

1. Make sure Python 3 is available: `xcode-select --install` installs the Command Line Tools.
2. Copy the CacheDash folder to the Mac, then run:
   ```
   cd CacheDash
   sudo ./install.sh "High School" 8027
   ```
3. Open `http://<cache-mac-ip>:8027` from a computer on your network.

**Who can open it:** by default, only computers with private addresses (10.x, 172.16–31.x, 192.168.x). The dashboard has no login and shows device names and downloads, so it's best to limit it to your IT subnet:

```
sudo CACHEDASH_ALLOW="10.0.5.0/24" zsh install.sh "High School" 8027
```

Use spaces between several networks. See [SECURITY.md](SECURITY.md) for details.

To try it once without installing:

```
sudo python3 cachedash.py --name "High School"
```

## Updating: `git up`

Install from a git clone of your CacheDash repository, and updating any caching Mac is one command.

**Once per caching Mac:**

```
git clone https://github.com/YOUR-ACCOUNT/CacheDash.git ~/CacheDash-repo
cd ~/CacheDash-repo
sudo zsh install.sh "Cache 1" 8027 "Cache 2=http://10.0.1.16:8027"
```

Use your own repository address. If CacheDash is already installed, leave out the name, port and other caches (`sudo zsh install.sh`): an existing install keeps its settings.

**From then on, to update:**

```
cd ~/CacheDash-repo && git up
```

`git up` (set up by `install.sh`):

1. downloads the latest version from GitHub and lists what's new
2. runs the built-in tests, and stops if they fail
3. installs it, keeping the name, port, allowed networks, other caches, extra options, `alerts.json`, sync settings, `clients.csv` and all history
4. checks the new version answers, and **puts the previous version back automatically** if it doesn't start

If nothing changed, it says so and does nothing. `sudo FORCE=1 zsh update.sh` reinstalls anyway.

**Or update from the dashboard: the Update button.** When a new version is published to your repository, click **Update now** on the banner (or **Widgets → Settings → Updates → Update…**), type your **update PIN**, and every caching Mac on the dashboard updates itself: the same `git up` steps, with progress shown for each Mac. The page reloads with the new version when it's done. If a Mac's new version doesn't start, that Mac goes back to its previous version and the dialog says so.

- **Set the PIN** when the installer asks, or any time with `sudo zsh install.sh --pin` (Return leaves the button off). Use the **same PIN on every caching Mac**, so one click updates them all.
- The button only works on Macs installed from a git clone, because it installs from that clone's repository. It can't install anything else.
- Five wrong PINs lock the button for 10 minutes. The update's output is in `/var/log/cachedash-update.log` on each Mac.

**The dashboard tells you when to update.** Every 2 hours (or when you click **Check now** in Widgets → Settings → Updates) each caching Mac checks the version on GitHub (public repositories only; turn off with `--no-update-check`). When a newer one exists, a banner says so, shows the exact `git up` command for each Mac, and links to what's new. **Not now** hides it until the next version.

**Publishing a new release** (for example a zip from Claude) to your repository, from any Mac with your GitHub login:

```
cd ~/CacheDash-repo && zsh publish.sh ~/Downloads/CacheDash_1.17.zip
```

It unzips the release, runs the tests, copies it over your clone (settings and data files are never copied or uploaded, and it stops if one would be), shows the changes, commits and pushes. Then run `git up` on each caching Mac.

## Several caching Macs on one dashboard

Install CacheDash on **every** caching Mac. The installer asks which other caching Macs to show:

```
cd ~/CacheDash-repo && sudo zsh install.sh
```

1. **Name** and **port** (press Return to accept the suggestion).
2. It **looks for the others by itself**: any of this cache's peers or parents that run CacheDash is offered, so you just answer **Y**.
3. You can **type an address** for any it didn't find, such as `10.0.1.16`. Each one is checked straight away (✓ connected, or ✗ with the reason). If CacheDash isn't installed there yet, you can add it anyway.
4. Once it's running, it **tells each of those Macs about this one**, so you only enter each Mac once. Install the first Mac, then the second: the second finds or adds the first, and the first adds the second automatically.

Open either Mac's dashboard and it shows every cache and switches to **All caches (combined)**. This works in any browser on any computer; the list is saved in `servers.json` on each Mac.

**Later changes:** `sudo zsh install.sh --setup` asks the questions again (keep or clear the list, find, add). `git up` never asks anything and keeps the list. While running, each Mac also checks its peers every 30 minutes and re-announces itself every 6 hours, so a Mac that was offline during setup catches up on its own. Turn this off with `--no-cluster`.

Scripted installs still work: `sudo zsh install.sh "Cache 1" 8027 "Cache 2=http://10.0.1.16:8027"`.

The browser reads each Mac directly, so port 8027 must be reachable from your computer to every caching Mac, and the caching Macs must be able to reach each other on that port (if you used `--allow`, include the other caching Macs' addresses).

| Widget | What it shows |
|---|---|
| **Caching servers** | One card per Mac, side by side: healthy, alerts, or caching off; traffic and devices right now; data served and hit ratio; storage used; cache pressure; peers seen; CacheDash version. If the dashboard can't reach a Mac, its card turns red and says so. |
| **Throughput by server** | One line per Mac, so you can see whether the load is shared |
| **Data served by server** | Each Mac's share of the data in the selected window |

The **All servers** preset puts these together with alerts and live device bandwidth. In the combined view, every other widget adds the Macs together. Device lists, alerts and incident reports label which Mac each row came from. **Show only this server** on a card, or the server menu in the header, switches to a single Mac.

The browser reads each Mac directly, so port 8027 must be reachable from your computer to every caching Mac.

## Choosing widgets

Click **Widgets** to turn widgets on or off. You can also start from a preset: Essentials, Capacity planning, Bandwidth savings, Who's downloading, or Everything.

Hover over any widget to see its controls in the top-right corner:

- **⠿ Drag** to move the widget anywhere on the page. While you drag:
  - every tile gets a faint dashed outline
  - the tile you're moving turns into a blue dashed **Drop here** box that follows the pointer, so you see exactly where it will land
  - you can drop it over another tile or into empty space, such as the end of a row or below the last tile
  - **Esc** cancels the move

  You can also click ⠿ and use the arrow keys.
- **S / M / L** changes the size: small, half width, or full width. Widgets adapt to their size: at half width and small, lists and tables drop their less important columns (for example Connections or Served from) instead of overflowing.
- **✕** hides the widget.

You can also drag rows in the **Widgets** panel to reorder them.

### Saved views (the same on every computer)

Your layout normally lives in the browser you arranged it in, so another computer starts with a different one. **Saved views** fix that: they're kept on the caching Macs, so every computer sees the same list.

- **Save:** arrange the widgets, click **Views** in the header, then **Save current layout as a new view…**, name it (for example *Rollout day* or *Morning check*) and click **Save**. Saving with an existing name replaces that view.
- **Switch:** click **Views** and pick one. The widgets, their order and sizes, the stacking options and the time range switch to that view.
- **Change one:** after switching to a view and moving things around, **Views → Save changes to “…”** updates it.
- **Default for new computers:** tick the box when saving, or use **Make default** in **Widgets → Settings → Saved views**. A computer opening the dashboard for the first time starts with that view.
- **Rename, delete or change the default** in **Widgets → Settings → Saved views**.

A view only holds the layout, never server addresses or device information. Views are saved on every caching Mac the dashboard knows about, in `views.json`, and are kept by `git up` and the Update button.


**Stacking (on by default):** half-width tiles are two rows tall, so two small tiles sit on top of each other beside them instead of stretching to match. Put small tiles just before or after a half-width tile to pair them up. A small tile with nothing under it stretches to fill the height. Turn this off in **Widgets → Settings → Stack small tiles beside larger ones**. It switches itself off on screens narrower than 1100 pixels, where tiles wrap anyway.

Tiles fill rows in order, so a half-width tile next to a full-width one leaves a gap. To pack small tiles into those gaps automatically, turn on **Widgets → Settings → Fill empty gaps automatically**. Your layout is saved in your browser, and **Settings → Copy layout as JSON** lets you move it to another computer.

| Group | Widgets | Source |
|---|---|---|
| Headline numbers | Data served, hit ratio, bandwidth saved, fetched upstream, requests, peak throughput, rejected (no space), cache pressure, purged, imported, dropped, active minutes | Metrics.db |
| Traffic over time | Served by source, served by destination, throughput, hit ratio, requests, rejections, purges, imports, dropped bytes | Metrics.db |
| Breakdowns | Source mix, destination mix, bytes source→destination matrix, replies matrix, purged content by age, imports by method, busiest-hours heatmap | Metrics.db |
| Capacity & storage | Storage used vs. limit, cached content by type | AssetCacheManagerUtil |
| Live status | Cache health (service, registration, addresses), peers & parents | AssetCacheManagerUtil |
| Devices | Live device bandwidth, top devices (with search), recent downloads, downloads by type, and compact versions of top devices and recent downloads | clientmon.py (nettop + AssetCache log) |
| Data | Interval table, database coverage | Metrics.db |

**Busiest hours** has its own range buttons:

- **Today:** hour-by-hour bars for today, with the busiest hour called out.
- **7 days** (default) or **30 days:** a weekday × hour heat grid showing the *average per day*, which weekday and hour is usually busiest, the busiest single day, and a "data served per day" bar chart.
- **Time range:** follows the date picker in the header. One day or less shows hourly bars; longer ranges show the grid.

**Cache pressure is an estimate.** It uses Apple's documented rule over the last 24 hours. The first match sets the level:

- Any request rejected for lack of space: **100%**
- Purged content less than 1 day old: **80%**
- Less than 7 days old: **60%**
- Less than 30 days old: **40%**
- Any purge: **20%**
- Otherwise: **0%**

Lower is better. A sustained 60% or higher means the cache is too small for your workload.

## Reports

Click **Report** in the header for a usage report covering whole days. Pick a preset (Yesterday, Last 7 days, Last 30 days, Last month, This month, This school year since Aug 1) or set **From** and **To** and click **Create report**. With several caching Macs, choose one Mac or **All caches (combined)**. **Print / Save PDF** prints a clean, light-coloured version; choose *Save as PDF* in the print dialog to keep a file.

A report shows:

- **Headline numbers:** data served, devices served (unique), internet bandwidth saved, hit ratio, data downloaded from the internet, downloads recorded, requests, and peak throughput (busiest 15 minutes).
- **Data served each day** (each week for ranges over 3 months), split into from the cache and from the internet.
- **By caching server** (combined reports): each Mac's data, savings, hit ratio, devices and share.
- **Devices & downloads:** content by type, top devices, and the most downloaded apps and updates.
- **Health & incidents:** busiest time of day and day of week, requests turned away, content purged, storage now, and every alert in the period.

**History.** Apple's Metrics.db and the device log only keep about 30 days. From version 1.13, CacheDash saves a summary of each finished day in `history.db` (about an hour after midnight) and fills in the last 29 days the first time it starts. Reports use that history plus live data, so longer ranges (a semester or school year) fill in as history builds up. Days before history started are marked "no data". Turn it off with `--no-history`; move the file with `--history-db`.

API: `GET /api/report?from=YYYY-MM-DD&to=YYYY-MM-DD`.

## Looking back at a specific time

Click the date button in the header to choose what time period the graphs show.

- **Quick ranges:** last 15 min, 30 min, 1 h, 3 h, 6 h, 12 h, 24 h, 7 days or 30 days. These views are live and refresh every 60 seconds.
- **Custom range:** pick an exact *From* and *To* date and time.
- **Around a moment:** pick the time something happened, such as the moment bandwidth spiked, plus a window from ±15 min to ±6 h.
- **Interval:** sets how much time each point on the graph covers, from 1 minute to 1 day. *Auto* picks one based on the range. Metrics.db records once per minute, so 1 minute is the finest detail available. Each option shows how many points it would draw, and options that would draw more than 2,000 points are greyed out.
- **Drag across any graph** to zoom into that stretch. **‹ ›** step one window back or forward. **Zoom out** triples the window. **Back to live** returns to the live view.

Auto-refresh pauses while you're looking at a past window, and all widgets use the selected window. Two exceptions:

- Busiest hours uses its own Today, 7 days and 30 days buttons, unless you set it to **Time range**.
- Cache pressure covers the 24 hours that end at the end of your window.


## Alerts: knowing when something goes wrong

CacheDash checks the cache every 15 seconds, even when nobody has the dashboard open. It records every problem it finds in `alerts.db`, kept for 180 days.

| Alert | Level | What triggers it |
|---|---|---|
| Cache full: devices are being turned away | Critical | Any request rejected with HTTP 503 for lack of space in the last 10 min |
| Traffic to devices dropped suddenly | Critical | Traffic to devices falls by 75% or more within a few minutes after being busy (at least 100 Mbps or 10 devices). This is the pattern you see when a rollout abandons the cache. It stays open until traffic recovers to half its earlier level, or for 1 hour at most. |
| Caching service is not running | Critical | The AssetCache process stops, or the service reports it isn't active or OK |
| Cache is not registered with Apple | Critical | Registration is lost, so devices can't discover the cache |
| Cache is evicting brand-new content | Warning | Content less than a day old purged in the last 30 min, meaning the cache is too small |
| Cache is nearly full | Warning | 95% of the cache limit used, or less than 20 GB free on the volume |
| Network link is near its limit | Warning | 85% of the detected link speed (e.g. 10 Gbps) for 2 minutes |
| Burst of errors in the caching log | Warning | 30 or more error lines in the AssetCache log in a minute |

On the dashboard, alerts show up in four places:

- **Banner:** an alert that's active right now appears at the top of the page, with a **View graphs** button that jumps the time range to that moment.
- **Bell icon:** shows how many alerts are active right now.
- **Alert status** tile and **Alerts & incidents** list: the list shows every incident in the selected time range, how long it lasted, and what was measured. Click **View graphs** to jump to the time it happened.
- **Graphs:** any graph over time gets shaded red (critical) or amber (warning) wherever an alert was active, so you can see what the traffic was doing at that moment.

**Acknowledge or mute alerts.** Every active alert, in the banner and in Alerts & incidents, has two buttons:

- **Acknowledge** — "I've seen this one." That occurrence leaves the banner and bell count, and is marked ✓ Acknowledged in the history. If the problem clears and comes back later, it alerts again.
- **Mute…** — stops a whole alert type for **1 day, 7 days, 30 days, or until you turn it back on**. In the combined view it's applied to all caching servers by default; untick **On all caching servers** for just one. Muted alerts are still detected and kept in the history (marked Muted), but they don't show the banner, count on the bell or send chat/email notifications. Use this for "Cache is nearly full" if your cache is meant to run full.

Acknowledgements and mutes are saved on the caching Mac, so they apply to everyone who opens the dashboard. **Widgets → Settings → Alerts → Muted alerts** lists what's muted, with an **Unmute** button for each.

The **Rollout watch** preset puts the most useful widgets for a big update push on one screen.

### Why did traffic drop?

A traffic drop isn't always a problem. When a batch of downloads finishes, traffic falls just as sharply. To tell the two apart, CacheDash records these measurements at every check and keeps them for 14 days:

- Connections and new connections
- The Mac's TCP **listen-queue overflow** counter and accept backlog (the "connection limit reached" signal)
- Open files compared with the per-process limit
- The caching service's process ID (so restarts are caught) and its CPU use
- Network-interface errors and link state
- Timeout, reset and "refused" errors in the caching log
- Download completions

When traffic drops, it analyses the minutes around the drop and gives a verdict:

| Verdict | Level | What it means |
|---|---|---|
| Downloads finished | Info | Most devices that stopped had completed their downloads, with no errors, limits or rejections |
| Traffic dropped as downloads ended | Info | Devices stopped asking for content and nothing went wrong. Used when the log doesn't report completions. |
| Connection limit reached | Critical | Connection attempts were dropped: listen-queue overflows, a full accept backlog, or the open-file limit reached |
| Cache ran out of space | Critical | HTTP 503 rejections |
| Caching service restarted | Critical | The AssetCache process stopped or changed |
| Network problem | Critical | The link went down, had errors, or was saturated |
| CPU maxed out | Critical | The caching service used all the CPU |
| Device downloads were failing | Critical | A spike in timeouts or resets, or most devices stopped part-way through their downloads |
| Cause unclear | Warning | None of the above, but devices kept requesting content |

**Normal drops don't page you.** Info-level verdicts are recorded but don't send a notification or show the banner. Set `"notifyInfo": true` in `alerts.json` if you want those messages too. The verdict is checked again 3 minutes later with more data. If it changes, for example from "failing" to "finished", you get an update message.

Click **Why did this happen?** on any alert to open its report:

- the verdict
- every check, marked Found, Possible, Ruled out or No data
- graphs of traffic, devices and connections, and errors and dropped connection attempts, from 15 minutes before to 10 minutes after the drop
- the devices that stopped, and whether each one had finished its download
- caching-log errors from around the drop

Records start when v1.7 is installed, so older alerts show "No data" for most checks.

**Notifications.** Copy `alerts.example.json` to `alerts.json`, fill in one or both channels, and reinstall:

- `"webhook"`: an incoming-webhook URL from Google Chat, Slack or Microsoft Teams. Each alert is posted as a message.
- `"email"`: your SMTP relay (`host`, `port`, optional `starttls`, `username` and `password`) and the `to` addresses.
- `"dashboardUrl"`: optional. Adds a link to the dashboard in each message.

You get a message when an alert starts and another when it resolves. To check the setup, use **Widgets → Settings → Alerts → Send a test notification**. Thresholds can be changed in the same file.

## CPU, memory, crashes and restarts

Every 15 seconds, each caching Mac records:

- **CPU** for the whole Mac
- **Memory** used, the macOS memory-pressure level, and swap
- the load average and thermal state
- how much CPU and memory the caching service (AssetCache) uses, and CacheDash's own usage, so you can rule it out
- the busiest processes
- crash reports for AssetCache and kernel panics (from `/Library/Logs/DiagnosticReports`)
- the Mac's boot time

These measurements are kept for 14 days. The widgets that show them:

| Widget | What it shows |
|---|---|
| **Server resources** | One card per Mac: CPU and memory now with the peak in the window, swap, load, temperature, uptime, the caching service's share, the busiest processes, crashes and restarts in the window, and recent crash reports |
| **CPU over time** | Whole-Mac CPU per server, or for one server the whole Mac and the caching service. A dashed line marks the alert threshold. Crashes and restarts show as red markers. |
| **Memory over time** | Memory in use per server |

The **Server health** preset puts them together with the servers overview, alerts and log errors. The servers overview cards also show CPU and memory now.

New alerts:

| Alert | Level | Trigger |
|---|---|---|
| CPU usage is high | Warning | Whole-Mac CPU averages at least 80% for 2 minutes (`cpuPercent` in `alerts.json`) |
| Memory is under pressure | Warning | macOS reports memory pressure as warning or critical |
| Caching service crashed or restarted | Critical | The AssetCache process ID changes. The alert says whether a crash report was written, and records the CPU, memory and AssetCache size just before. |
| Caching Mac restarted | Critical | The Mac's boot time changes, including when a kernel panic report exists |

The incident report's CPU check also uses whole-Mac CPU now.

## Caching log viewer

Click the **document icon** in the header, **Open log viewer** on the **Log errors** widget, or **View log** on a "Burst of errors" alert. The log viewer reads the AssetCache system log on every caching Mac at once.

- **Server:** All servers, or one Mac.
- **Time:** last 5 min, 15 min, 1 h or 4 h, the dashboard's time range, or 10 minutes either side of an alert.
- **Errors only / All lines.**
- **Search:** filters by any text. Typing a request ID such as `#EPjRH9oiohMl` jumps straight to that request. Typing a device address such as `10.0.40.23` matches that address exactly (not `10.0.40.230`), switches to **All lines** so you see its downloads and not just errors, and highlights it in every line.
- **Error summary:** the same error repeated hundreds of times is grouped into one line, for example `708× Failed with status code 403, reason code 26`. Each group shows a count per server, when it started and stopped, and a plain-English meaning of the HTTP status. **Show these** lists just those lines.
- **Error lines:** newest at the bottom, with each line tagged by server. Click any request ID (`#…`) to see every line for that one request: which device asked, which Apple address the cache tried, and how it ended.
- **Download .log:** saves the lines in the selected window as a text file, one per server.

**Explain an error.** Click **Explain** on any row of the error summary. A request trace also explains its failing line automatically. You get:

1. **What it means**, in plain English: HTTP status codes (401, 403, 404, 407, 416, 429, 5xx), timeouts, resets, refused connections, certificate and DNS problems, out-of-space, content-verification and registration errors.
2. **What CacheDash found** on your Macs:
   - how often the error happened and on which server
   - a few sample requests traced end to end, showing which Apple addresses and what kind of content (App Store apps, OS updates, iCloud …)
   - how many different devices were affected
   - a **live check**: the caching Mac requests one byte of the failing address again and reports **who answered**. That's either Apple's servers or CDN, or a filter, firewall or proxy (it recognises common school filters and block pages), or no answer at all.
3. **A verdict**, for example: *"Your network is blocking these: the answer came from your web filter, not from Apple."* Or: *"Apple's servers themselves returned 403 … usually an expired link."*
4. **Most likely causes** and **what to do**.

Apple doesn't publish what its "reason code" numbers mean, so CacheDash doesn't guess at them. The live check works out where the error actually came from. Only Apple's own domains can be checked, and the result is kept for 5 minutes.

Recent lines (about the last 20,000 per Mac) come from CacheDash's live buffer. Older times are read from the Mac's own system log with `log show`, up to 4 hours per view.

The "Burst of errors" alert now names the most common error, so you know what it is before opening the log.

## App updates: which apps are your devices updating?

The cache can't name App Store apps (their download addresses are random strings), but your MDM knows every app on every device and its version. CacheDash combines the two:

| Widget | What it shows |
|---|---|
| **App updates waiting** | Every App Store app with a newer version than some of your devices have: the latest version and release date, how many devices need it, how many are current, roughly how much data that is, and how many devices updated in the last 24 hours. Search, or untick **Only apps with updates** to see everything. Click an app for its release notes, the versions on your devices, and which devices still need it. |
| **App updates (summary)** | A small tile: apps with updates waiting and installs waiting. |
| **Rolling out now** | App Store downloads through the cache, grouped by app version. Every device updating the same app downloads from the same folder, so a wave of 200 iPads updating one app shows as one line: devices, how many are downloading right now, size each, and when it started. Most lines are unnamed (Apple doesn't publish the names), so the widget also lists what your MDM says was updated in the last 24 hours, to put names to the waves. |

The **App updates** preset puts them together.

**Data** is an upper limit: the App Store's listed size × devices that need the update. iPads download a smaller version made for their model, and updates are often smaller still.

### Getting the app inventory (any MDM)

Export an **installed-apps report** from your MDM as a CSV file, then click **Upload your MDM's app report…** (or **Upload CSV…**) in the App updates widget. It works with any MDM: Jamf, Intune, Kandji, Mosyle, Workspace ONE, Meraki, Addigy and others. Columns are detected by name, and a title line above the header is fine:

- **app**: bundle ID (best) or app name
- **version**
- **device**: serial number, device name or UDID. This is optional, but without it you won't see which devices need each update. A summary report with a count column (such as `Installs`) per app and version also works.

Upload a new report whenever you want fresh numbers. Each upload is compared with the previous one, which is what fills in **Updated 24 h**. The newest inventory replaces the previous one, so nothing is counted twice.

#### Optional: automatic sync

Instead of uploading reports, CacheDash can fetch the inventory from your MDM by itself every 6 hours. This is currently available for **Mosyle Manager**. On **one** caching Mac:

1. In Mosyle Manager, turn on the API integration in your organization settings and copy the **access token** (Mosyle's help centre shows where it is for your account). If you use an admin login as well, make a dedicated admin account for CacheDash with the least access that can view devices.
2. Copy `mosyle.example.json` to `mosyle.json` and fill in `accessToken`. If your Mosyle account also requires an admin login for the API, fill in `email` and `password` too; otherwise leave them empty and only the token is used. Leave `"os"` as `["ios", "macos"]` (or remove one).
3. Reinstall (`sudo zsh install.sh`). The file is copied to `/usr/local/cachedash` and readable only by root.
4. Check it works:
   ```
   sudo python3 /usr/local/cachedash/cachedash.py --check-mosyle
   ```
   It logs in, reads one page of devices, and shows which field holds the installed apps. Mosyle doesn't document this field publicly, so if the app list is found under a different name, add that name as `"appsColumn"` in `mosyle.json` and reinstall. If no app list comes back, upload reports instead.

The widget then shows **Sync now**, which starts a sync straight away (`"refreshHours"` changes the schedule).

**App Store versions** come from Apple's public lookup service, checked every 12 hours, for the US store by default (`--app-store-country gb` and so on). Custom, in-house and some education apps aren't listed publicly and show "not in the public App Store". Apps matched by name (because an export had no bundle IDs) are marked so you can check the match. The caching Mac needs to reach `itunes.apple.com` over HTTPS.

API: `GET /api/apps`, `GET /api/apps/devices?key=`, `POST /api/apps/import {csv, filename}`, `POST /api/apps/sync`, `GET /api/rollouts?start=&end=`. Turn the feature off with `--no-apps`.

## Devices: who is downloading what

The **Devices** widgets show which devices are downloading through the cache, what they're downloading, and how fast.

- **Live device bandwidth** refreshes every 5 seconds. It lists every device currently pulling from the cache with its current rate and how many connections it has open. It also shows how fast the cache itself is downloading from Apple.
- **Top devices**, **Recent downloads** and **Downloads by type** follow the time range you pick.
- **Track one device:** type in the search box on **Top devices**. A full address (`10.0.40.23`) finds exactly that device; part of one (`10.0.40.`), a name or a model (`iPad13`) finds every match. The search covers every device seen in the time range, not just the top 15. **Open** shows that device's details (or press Enter after typing an address), and **Search the log** opens the log viewer filtered to it. The device panel also has a **Search the log for …** button.
- **Compact tiles:** **Top devices (compact)** and **Recent downloads (compact)** fit the same information into a small tile, one line per device or download, for stacking beside larger widgets. Click a row for that device's details.
- **Click any device** in Live device bandwidth, Top devices or Recent downloads to open its details panel for the selected time range. The panel shows:
  - total data, number of downloads, number of distinct apps/updates, and its current rate
  - a graph of the data it pulled over time
  - **Apps & updates:** each app or update it downloaded, how many times, total size, and when
  - **Every download:** the full list, one row per download
  - **Download CSV:** exports that device's downloads, for a ticket or a parent/teacher conversation
  - **Filter dashboard to this device:** narrows the device widgets on the page to that device. Click ✕ on the "Device:" chip to clear it.

How it works, and what it can't see:

- **Live rates** come from `nettop`, which is built into macOS. It measures the cache's own network connections, one per device. These numbers are reliable.
- **"What" comes from the AssetCache system log.** Apple doesn't document that log's format, so CacheDash matches patterns in it. It can usually tell the type of download: App Store app, iOS/iPadOS update, macOS update, macOS installer, iCloud, and so on. It also gets the device model and OS version, and the file name, which often names the app or OS version. Apple's download addresses don't always include a readable app name.
- **Real names for updates and .pkg files.** CacheDash downloads Apple's public update catalogs and turns file names into product names. It refreshes them every 12 hours and saves a copy to `catalog-cache.json`.

  | You'd otherwise see | Shown as |
  |---|---|
  | `…/093-55555/…/Safari26.1SequoiaAuto.pkg` | **Safari 26.1** (macOS software update) |
  | `…/XProtectPayloads_10_15-157.pkg` | **XProtectPayloads 157** (macOS security data) |
  | `…/InstallAssistant.pkg` | **macOS Tahoe 26.4** (macOS installer) |
  | `…/com_apple_MobileAsset_SoftwareUpdate/9f3c….zip` | **iPadOS 18.7 (22H20)**, update from 18.6 |

  These names work for everything delivered through Software Update: Safari, XProtect, Command Line Tools, Rosetta, full macOS installers, and iOS, iPadOS and macOS updates. The original file name stays visible underneath.

  **App Store apps can't be named this way.** Apple doesn't publish a catalog for App Store downloads, and their addresses are random strings, so apps keep showing their file name. See **App updates** above for how CacheDash uses your MDM's app inventory instead.

  The caching Mac needs to reach `swscan.apple.com` and `mesu.apple.com` over HTTPS. Content caching already relies on Apple hosts, so they're usually allowed. To check, run:

  ```
  sudo python3 /usr/local/cachedash/cachedash.py --check-catalogs
  ```

  It reports how many Software Update products (several hundred is normal) and update files it found. The macOS update catalog is often refused (HTTP 403), because Apple doesn't always publish it. In that case macOS system updates show as **"macOS system update – device is on macOS 26.4"** instead of a version number. Their .pkg files (Safari, XProtect, installers and so on) are still named through the Software Update catalog. You can also add a download address from the dashboard after the command to see what it resolves to. Some names can't be resolved:

  - older iOS deltas that Apple has dropped from its public catalog
  - beta or AppleSeed builds, unless the Mac is pointed at a beta catalog (CacheDash uses the same catalog the Mac does)

  These show the file name instead.
- **Click a download's name** (in Recent downloads, Live device bandwidth, a device's Apps & updates, Rolling out now, or the compact tiles) to open its details:
  - **What it is:** type, real name and version when Apple's catalogs know it, product ID, file name, size, and the full Apple address.
  - **Where it's stored on the cache:** the exact file on each caching Mac (Apple keeps each item as `…/AssetCache/Data/<ID>/0`), its size on disk and when it was cached, with a **Copy** button and the Terminal command to show it in Finder. If it isn't stored, it says so (removed to make space, or passed straight through).
  - **Who downloaded it:** every device that downloaded that file in the last 30 days, when, how many times, how much, and whether it came from the cache or Apple. For App Store apps, devices that downloaded the same app update in a version for a different model are listed too. Click a device to open it.

  The storage lookup reads a copy of the cache's own index (`AssetInfo.db`) and never changes or moves cached files. Apple doesn't document that index, so check it on your Mac with `sudo python3 /usr/local/cachedash/cachedash.py --find-cached`, optionally followed by a file name or address.
- **Data and Size use measured traffic.** CacheDash counts the bytes the cache actually sends to each device, every few seconds, and saves them per minute. Top devices' **Data** is the larger of that measurement and the sizes reported in the log. In Recent downloads, a size marked **≈** was measured from traffic because the log didn't give one. Measurement starts when v1.3.2 is installed, so older downloads may still show "–".
- **Download history** is saved to `clients.db` next to the program and kept for 30 days. Recording starts when v1.2 starts running, so earlier history isn't available.
- **Device names** come from reverse DNS. If your DHCP server registers hostnames, you'll see names like `CART-A-IPAD07`. To label devices yourself, rename `clients.example.csv` to `clients.csv`, fill in `ip,name` rows, and reinstall.
- **Devices that skip the cache are invisible here.** CacheDash only sees traffic that passes through the cache. Devices that can't find the cache (the public-IP issue) download straight from Apple, and only your firewall or web filter can see that traffic. To find what's using your internet bandwidth overall, use the firewall's per-device or per-app reports.

**Check what your Mac shows.** After installing, run this on the caching Mac:

```
sudo python3 /usr/local/cachedash/cachedash.py --diagnose-clients 60
```

While it runs, download an app on an iPad. It prints what `nettop` and the log can see, then saves a raw log sample to `assetcache-log-sample.ndjson`. If downloads don't show up as RECORD lines, send that file to whoever maintains CacheDash and the log matching can be adjusted. The file contains device IP addresses, so share it internally only.

**If you see `<private>`:** macOS can hide details in system logs. Install `AssetCache-PrivateLogging.mobileconfig` on the caching Mac only, either through your MDM or by double-clicking it and approving in System Settings → Privacy & Security → Profiles. Then restart CacheDash:

```
sudo launchctl kickstart -k system/local.cachedash
```

**To turn device monitoring off:** add `--no-clients` to the plist arguments.

## Notes and troubleshooting

- **Permission denied reading Metrics.db.** The server has to run as root, which the LaunchDaemon does. If macOS privacy controls still block it, give `/usr/bin/python3` Full Disk Access. Do this in System Settings → Privacy & Security, or with an MDM PPPC profile.
- **"Metrics database not found".** Content caching has to be on, and the Mac has to be on macOS 27.
- **Apple may change the format.** Apple notes that the Metrics.db model can change without notice. CacheDash finds the table and columns by name, so new columns are picked up and missing ones show as zero. If an update breaks it, `python3 cachedash.py --check` prints what the database contains.
- **Network access.** The server listens on all interfaces at port 8027 but only answers private addresses, and only lets dashboards served from private addresses read it from a browser. Narrow it with `--allow CIDR` (or `CACHEDASH_ALLOW` at install), add a specific dashboard address with `--allow-origin URL`, or limit it to the Mac itself with `--bind 127.0.0.1`. There is no login, so keep it on a trusted VLAN. Opening `dashboard.html` straight from disk shows sample data only.
- **Not used by this version.** Unique, peak and connected client counts exist only in macOS 27's HTTPS metrics report, which CacheDash doesn't read. The live device widget gives a direct view of connected devices instead.
- **Device names and download history are visible to anyone who can open the dashboard.** Keep port 8027 on a staff or management VLAN.
- **An update didn't start.** `install.sh` puts the previous version back by itself and shows the end of `/var/log/cachedash.log`. The previous version's files are kept in `/usr/local/cachedash/.previous`.
- **Uninstall.**
  ```
  sudo launchctl bootout system /Library/LaunchDaemons/local.cachedash.plist
  sudo launchctl bootout system /Library/LaunchDaemons/local.cachedash.updater.plist
  sudo rm /Library/LaunchDaemons/local.cachedash.plist /Library/LaunchDaemons/local.cachedash.updater.plist
  sudo rm -r /usr/local/cachedash
  ```

## About this project

CacheDash was written with an AI assistant (Claude, by Anthropic), directed by a school IT administrator. Treat it like any code you find online: read it before you run it as root. It's kept small and plain on purpose (about 6,000 lines, no dependencies, no build step, nothing minified), and [SECURITY.md](SECURITY.md) maps out what to look at.

Bug reports, fixes and reviews are welcome. Please report security problems privately (GitHub → Security → Report a vulnerability) rather than in a public issue.
