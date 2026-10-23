# Build Forge: developer guide

Setup, debugging and troubleshooting for anyone who has to install, run, change or fix Build Forge. For the click-by-click
install on the real servers, `INSTALL.md` is the long form; this guide is the one to keep open while working.

- [1. What it is and how it fits together](#1-what-it-is-and-how-it-fits-together)
- [2. Setup guide](#2-setup-guide)
- [3. Configuration reference](#3-configuration-reference)
- [4. Debugging guide](#4-debugging-guide)
- [5. Troubleshooting guide](#5-troubleshooting-guide)
- [6. Changing the behaviour](#6-changing-the-behaviour)
- [7. Operations: upgrade, roll back, remove](#7-operations-upgrade-roll-back-remove)
- [8. Security notes](#8-security-notes)

---

## 1. What it is and how it fits together

Build Forge reads every failed Jenkins build with a Kiro agent (Claude Opus 4.5) and shows the cause and the proposed fix
on the failed build. A **Rebuild** button appears only when the agent says a rebuild with the same parameters can fix it.
Every failure gets a unique entry id (`BF-YYYYMMDD-XXXXXX`), an incident log file on the Jenkins server, and a row on the
admin page `/buildforge/admin`. It has no chat mode and never rebuilds by itself.

### Three parts

| Part | Runs on | Port | Installed as | Code |
|---|---|---|---|---|
| **Plugin** (panel, Rebuild button, admin page) | Jenkins | none | `buildforge.jpi` in `/var/lib/jenkins/plugins` | `plugin/` (Java, Jelly, JS) |
| **Service** (incidents, stats, incident logs, Teams) | Jenkins server | 8791 (127.0.0.1 only) | systemd `buildforge` | `agent/` (Python, standard library) |
| **Runner + MCP server + Kiro agent** | Kiro CLI instance | 8796 | systemd `buildforge-runner` | `kiro-runner/` (Python, standard library) |

### Data flow of one failure

```
 Jenkins                          buildforge service                    buildforge-runner (Kiro instance)
 ───────                          ──────────────────                    ─────────────────────────────────
 build fails
 BuildForgeListener ──POST /api/v2/failure──► create incident BF-…  (status queued)
   (RunListener)       console log, params,    cut out the failed stage's error, fingerprint it,
                       build url, result       look up earlier occurrences
                                               status analysing ──POST /analyze──► kiro-cli chat --agent buildforge-fixer
                                                                                     │ the agent calls ONE MCP tool:
                                                                                     ▼ submit_analysis (buildforge_mcp.py)
                                               status done ◄──── {analysis: {...}} ◄── writes BF_RESULT_FILE, runner returns it
                                               writes incidents/BF-….md + incidents.jsonl, maybe Teams card
 build page panel ──GET /api/v2/analysis──►    returns the analysis for that build
 Rebuild click ───► plugin queues the job with the same parameters (cause "Rebuilt by Build Forge from #N")
 every finished build ──POST /api/v2/build-result──► "fixed on the next build" bookkeeping
```

Browsers never call the service. The panel and admin page call the plugin (`/job/<name>/<n>/buildforge/analysis`,
`/buildforge/stats`, …), which calls the service with the shared secret.

### Incident lifecycle

`queued` → `analysing` → `done` (analysis stored) or `error` (runner down, timeout, no analysis; the reason is in the
`error` column and shown in the panel). An `error` incident can be retried with **Analyse again** on the admin page
(`POST /api/v2/reanalyze`). Incidents left `queued` or `analysing` by a service restart are resumed on start-up.

Outcomes (admin filters): **fixed-next** (the next build of the job passed), **fixed-later** (a later build passed),
**open** (no green build since), **rebuilt** (a Build Forge rebuild was requested).

### Where things live

| What | Where |
|---|---|
| Plugin settings | Manage Jenkins › System › Build Forge (`$JENKINS_HOME/io.jenkins.plugins.buildforge.BuildForgeConfig.xml`, secret encrypted) |
| Service env / data / logs | `/etc/buildforge/buildforge.env`, `/var/lib/buildforge/buildforge.db`, `/var/log/buildforge/incidents/<id>.md`, `/var/log/buildforge/incidents.jsonl` |
| Service process log | `journalctl -u buildforge` |
| Runner env / code | `/etc/buildforge-runner/runner.env`, `/opt/buildforge-runner/` |
| Kiro agent definition | `~<run user>/.kiro/agents/buildforge-fixer.json`, prompt `/opt/buildforge-runner/kiro/prompts/buildforge-fixer.md` |
| Runner process log | `journalctl -u buildforge-runner` |

### Source layout

```
Build-Forge/
├── plugin/        build.sh (writes buildforge.hpi), src/main/java/io/jenkins/plugins/buildforge/ (BuildForgeListener, BuildForgeAction,
│                  BuildForgeAdmin, BuildForgeConfig, ServiceClient), src/main/resources/ (Jelly views),
│                  src/main/webapp/ (buildforge.js, buildforge.css, images/buildforge.svg)
├── agent/         buildforge_agent/ (server, engine, store, deck, export, incidents, errors, kiro, teams, checks, config), deploy/, tests/
├── kiro-runner/   runner.py, buildforge_mcp.py, kiro/agents/buildforge-fixer.json, kiro/prompts/buildforge-fixer.md, deploy/, tests/
├── docs/          INSTALL.md, DEVELOPER-GUIDE.md (this file), ARCHITECTURE.md, RECREATE-WITH-AI.md, screenshots, samples
└── README.md
```

---

## 2. Setup guide

### 2.1 Prerequisites

- `sudo` on the Jenkins server and on the Kiro instance; Python 3.8+ on both (nothing to `pip install`).
- Kiro CLI installed and logged in on the Kiro instance (`kiro-cli --version`), or a Kiro API key.
- Jenkins 2.346 or newer (tested on 2.346.3 and 2.580.1).
- Permission to edit the Kiro instance's security group (inbound TCP 8796 from the Jenkins server only).
- The **whole** repository on both machines (`git clone`, or a release archive). Partial copies cause stale-file problems.

```bash
git clone https://github.com/Harjeet2411/Build-Forge.git && cd Build-Forge
```

### 2.2 Kiro instance

```bash
kiro-cli chat --no-interactive "Reply with the single word: ready"     # must answer
sudo RUN_USER=ubuntu bash kiro-runner/deploy/install.sh                # RUN_USER = the user logged in to Kiro
sudo grep BF_RUNNER_SECRET /etc/buildforge-runner/runner.env           # note it for the Jenkins side
curl -s http://127.0.0.1:8796/healthz                                  # {"ok": true, "agent": "buildforge-fixer", "kiro": "/…/kiro-cli"}
```

Check the agent and its MCP server by hand:

```bash
kiro-cli chat --agent buildforge-fixer
/mcp          # the "buildforge" server must be listed as loaded, with the tool submit_analysis
/quit
```

If Kiro says the model `claude-opus-4.5` is unknown, copy the exact `"model"` value from another Kiro agent file that already works for you
(`~/.kiro/agents/*.json`) into `~/.kiro/agents/buildforge-fixer.json`.

Then one real analysis through the runner (uses one Kiro turn):

```bash
SECRET=$(sudo grep '^BF_RUNNER_SECRET=' /etc/buildforge-runner/runner.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8796/analyze -H "X-BuildForge-Secret: $SECRET" -H "Content-Type: application/json" \
  -d '{"job":"demo","build":1,"stage":"Checkout","error":"+ git fetch origin\nfatal: unable to access https://git.example.com/app.git/: Could not resolve host: git.example.com\nERROR: script returned exit code 128"}'
```

Expected within 20 to 90 seconds: JSON with `"analysis": {"error_observed": …, "proposed_solution": …, "rebuild_can_fix": true, …}`.
(The word `SECRET` in older instructions was a placeholder; the runner answers 401 if you send it literally.)

Open the network path: security group inbound **Custom TCP 8796**, source = the Jenkins server's security group or its
private IP `/32`. Never open it to the internet (plain HTTP, protected only by the shared secret).

### 2.3 Jenkins server: service

```bash
sudo bash agent/deploy/install.sh
sudo nano /etc/buildforge/buildforge.env      # set BF_RUNNER_URL=http://<kiro-private-ip>:8796 and BF_RUNNER_SECRET=<from 2.2>
sudo systemctl restart buildforge
sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; cd /opt/buildforge/agent; python3 -m buildforge_agent check'
```

Expected `check` output:

```
OK   Incident log folder: /var/log/buildforge/incidents
OK   Build Forge runner: http://10.x.x.x:8796 (agent buildforge-fixer, kiro /home/ubuntu/.local/bin/kiro-cli)
OK   Kiro agent: transient, rebuild_can_fix=True, 41s: `git fetch origin` failed ...
```

Options: `--no-kiro` skips the test analysis, `--teams` posts a test card. `FAIL … unreachable … timed out` means a network
block (section 5), not a bad secret.

### 2.4 Jenkins server: plugin

```bash
bash plugin/build.sh /usr/share/java/jenkins.war      # needs a JDK; writes plugin/buildforge.hpi
sudo cp plugin/buildforge.hpi /var/lib/jenkins/plugins/buildforge.jpi
sudo chown jenkins:jenkins /var/lib/jenkins/plugins/buildforge.jpi
# restart Jenkins when no builds run: https://<jenkins>/safeRestart
```

Manage Jenkins › System › **Build Forge**: Enabled on, service URL `http://127.0.0.1:8791`, shared secret =
`sudo grep BF_SHARED_SECRET /etc/buildforge/buildforge.env`, panel on. Save.

### 2.5 Smoke test

Create a pipeline that fails like a network hiccup, build it, open the failed build:

```groovy
pipeline { agent any
  stages {
    stage('Checkout') { steps { sh 'echo "fatal: unable to access https://git.example.com/x.git/: Could not resolve host: git.example.com"; exit 128' } }
    stage('Build') { steps { echo 'never runs' } }
  } }
```

Within about 90 seconds the **Build Forge** panel shows Error observed, Proposed solution, the category, and (transient)
a **Rebuild** button. Then check the admin page and the incident log:

```bash
sudo ls -lt /var/log/buildforge/incidents | head
sudo tail -n 3 /var/log/buildforge/incidents.jsonl
```

### 2.6 Developer setup (no Jenkins servers needed)

Run the tests (standard library only, no network, about 4 seconds):

```bash
cd agent        && python3 -m unittest discover -s tests     # 44 tests: incidents, logs, outcomes, stats, filters, exports, deck, HTTP API
cd kiro-runner  && python3 -m unittest discover -s tests     # 7 tests: runner + the real MCP server with a stand-in kiro-cli
```

Build the plugin from source (JDK 11+ and a `jenkins.war`; no Maven, no internet):

```bash
cd plugin && bash build.sh /path/to/jenkins.war      # writes plugin/buildforge.hpi
```

`build.sh` compiles with `javac` against the war's libraries, writes the plugin manifest into both the `.hpi` and the
inner jar (Jenkins' JEP-200 class filter rejects classes from a jar without it), and handles old and new wars (the servlet
API is in `winstone.jar` at the war root on old wars, under `executable/` on 2.580).

Run a whole stack locally for the UI: start a Jenkins in Docker with the plugin in `plugins/buildforge.jpi`, run the
service (`BF_SHARED_SECRET=… BF_RUNNER_URL=… python3 -m buildforge_agent serve` from `agent/`) and the runner
(`BF_RUNNER_SECRET=… BF_KIRO_BIN=<stand-in> python3 runner.py` from `kiro-runner/`). A stand-in `kiro-cli` only has to
accept the same arguments and call `buildforge_mcp.py` with a canned `submit_analysis`; `kiro-runner/tests/test_runner.py`
contains one.

---

## 3. Configuration reference

### Service: `/etc/buildforge/buildforge.env` (restart `buildforge` after a change)

| Setting | Default | Meaning |
|---|---|---|
| `BF_LISTEN` | `127.0.0.1:8791` | Where the plugin reaches the service |
| `BF_SHARED_SECRET` | generated | Must equal the plugin's Shared secret (the service refuses to start without it) |
| `BF_RUNNER_URL`, `BF_RUNNER_SECRET` | none | The runner on the Kiro instance |
| `BF_RUNNER_TIMEOUT` | 300 | Seconds to wait for one analysis; keep above the runner's `BF_TIMEOUT` |
| `BF_LOG_DIR` | `/var/log/buildforge` | Incident logs (`incidents/<id>.md`, `incidents.jsonl`) |
| `BF_DATA_DIR` | `/var/lib/buildforge` | SQLite database behind the admin page |
| `BF_RETENTION_DAYS` | 365 | Older entries and log files are deleted daily; 0 keeps everything |
| `BF_SEND` | `errors` | `errors` = only the failed stage's output goes to Kiro; `full` = the end of the console log |
| `BF_MAX_ERROR_CHARS` | 16000 | Upper limit of what is sent to Kiro |
| `TEAMS_WEBHOOK_URL`, `BF_NOTIFY_ON` | none, `infrastructure` | Teams card (Workflows webhook) for these categories (comma list or `all`) |
| `BF_JOB_INCLUDE`, `BF_JOB_EXCLUDE` | none | Regular expressions on the full job name (`folder/job`). Only decide which failures are **analysed**; every failure is recorded and listed on the dashboard (status `skipped`, "Excluded") |
| `BF_DECK_MODE` | `fallback` | `off`, `fallback` (deck answers when Kiro is unreachable or fails) or `first` (deck answers before Kiro for a very close match) |
| `BF_DECK_THRESHOLD`, `BF_DECK_FIRST_THRESHOLD` | 0.55, 0.85 | Minimum similarity for a fallback answer, and for answering first (approved cards only) |
| `BF_DECK_MIN_SEEN` | 2 | Sightings after which a draft card may be used (approved cards: immediately) |

### Runner: `/etc/buildforge-runner/runner.env` (restart `buildforge-runner` after a change)

| Setting | Default | Meaning |
|---|---|---|
| `BF_RUNNER_LISTEN` | `0.0.0.0:8796` | Where the runner listens |
| `BF_RUNNER_SECRET` | generated | Same value as on the Jenkins side |
| `KIRO_API_KEY` | none | Kiro login for a headless instance |
| `BF_AGENT` | `buildforge-fixer` | Kiro agent name |
| `BF_TIMEOUT` | 240 | Seconds one Kiro turn may take |
| `BF_MAX_PARALLEL` | 3 | Analyses at the same time; the rest wait |
| `BF_MAX_ERROR_CHARS` | 16000 | Cuts what the agent is shown (head 2000 + tail) |
| `BF_KIRO_BIN` | found by install.sh | Full path of `kiro-cli` (systemd's PATH is minimal) |
| `BF_TRUST_TOOLS` | `--trust-tools=@buildforge/submit_analysis` | Tools kiro-cli may run without asking |
| `BF_PASS_ENV` | none | Extra env names passed on to kiro-cli (it gets `PATH`, `HOME`, `LANG`, `KIRO_API_KEY` only) |

### Plugin (Manage Jenkins › System › Build Forge)

Enabled; service URL; shared secret; show the panel on failed builds; max console log sent (KB); Advanced: let every
signed-in user open `/buildforge/admin` (off = Jenkins administrators only); only/skip jobs matching (regular expressions).
Permissions: seeing the panel needs Read on the job; **Rebuild** needs Build on the job (checked again on the server);
the admin page needs Administer unless the option above is on.

---

## 4. Debugging guide

### 4.1 Find the broken hop first

Follow one failed build left to right and stop at the first hop that has no trace:

| # | Hop | Check | Healthy sign |
|---|---|---|---|
| 1 | Jenkins → service | Jenkins log (4.5), `journalctl -u buildforge -n 50` | a line `<job>#<n>: incident BF-… (stage …, X of Y chars to Kiro)` |
| 2 | service → runner | `journalctl -u buildforge`, `curl http://<kiro-ip>:8796/healthz` from the Jenkins server | `BF-…: <category>, rebuild_can_fix=…` |
| 3 | runner → kiro-cli | `journalctl -u buildforge-runner -n 50` | a `POST /analyze` line, no `no analysis submitted` warning |
| 4 | kiro-cli → MCP tool | run the agent by hand (4.3) | `/mcp` shows `buildforge` loaded |
| 5 | service → panel | `curl` the service API (4.2), then the browser network tab (4.5) | `status: done` with an `analysis` object |

### 4.2 Call the APIs by hand

Service (on the Jenkins server; every call except `/healthz` needs the header):

```bash
. /etc/buildforge/buildforge.env 2>/dev/null || true       # or paste the secret
H="X-BuildForge-Secret: $BF_SHARED_SECRET"
curl -s http://127.0.0.1:8791/healthz                                           # version, runner configured?, teams configured?, log dir
curl -s -H "$H" "http://127.0.0.1:8791/api/v2/analysis?job=payments-api&build=7"   # what the panel shows
curl -s -H "$H" "http://127.0.0.1:8791/api/v2/incidents?days=7&limit=20"         # list (also q=, category=, outcome=, offset=)
curl -s -H "$H" "http://127.0.0.1:8791/api/v2/incident?id=BF-20261012-ABC123"    # full detail + log text + rebuilds
curl -s -H "$H" "http://127.0.0.1:8791/api/v2/stats?days=30"                     # the admin stat cards
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"id":"BF-20261012-ABC123"}' http://127.0.0.1:8791/api/v2/reanalyze
```

Inject a failure without Jenkins (creates a real incident and spends a Kiro turn):

```bash
curl -s -X POST -H "$H" -H 'Content-Type: application/json' http://127.0.0.1:8791/api/v2/failure -d '{
  "job":"demo","build":1,"result":"FAILURE","build_url":"http://jenkins/job/demo/1/","params":["ENV"],
  "log":"[Pipeline] stage\n[Pipeline] { (Checkout)\n+ git fetch origin\nfatal: Could not resolve host: git.example.com\nERROR: script returned exit code 128"}'
```

Runner (on the Kiro instance or from the Jenkins server): `GET /healthz`, `POST /analyze` (section 2.2). Status codes:
401 wrong secret, 413 body over 512 KB, 500 `kiro-cli` not found, 502 the agent did not call the tool, 504 over `BF_TIMEOUT`.

### 4.3 See what Kiro sees, and run the agent by hand

What would be sent for a saved console log (no Kiro turn):

```bash
sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; cd /opt/buildforge/agent; python3 -m buildforge_agent extract /tmp/console.log'
```

It prints the failed stage, node, stage list and the excerpt, with the character counts. If the excerpt is wrong the
analysis will be wrong; the cut is done in `agent/buildforge_agent/errors.py`.

Run the agent exactly like the runner does (on the Kiro instance, as the run user):

```bash
export BF_RESULT_FILE=/tmp/analysis.json
kiro-cli chat --no-interactive --agent buildforge-fixer --require-mcp-startup \
  --trust-tools=@buildforge/submit_analysis "Pipeline: demo
Build: #1
Failed stage: Checkout
<log>
fatal: Could not resolve host: git.example.com
</log>
Analyse it and call submit_analysis."
cat /tmp/analysis.json
```

Test the MCP server alone (it speaks newline-delimited JSON-RPC on stdin/stdout):

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | BF_RESULT_FILE=/tmp/x.json python3 /opt/buildforge-runner/buildforge_mcp.py
```

It must list `submit_analysis`. Started with no input it just waits (Ctrl+C); that is normal. If the file named by
`BF_RESULT_FILE` is not written, the runner answers 502. The server falls back to reading `BF_RESULT_FILE` from the parent
kiro-cli process (`/proc/$PPID/environ`) when Kiro does not pass the variable on.

### 4.4 Read the data

Incident logs: `/var/log/buildforge/incidents/<id>.md` (rewritten when the outcome changes) and one JSON line per event
in `incidents.jsonl` (`analysed`, `analysis_failed`, `rebuild`, `outcome`, …). Print one with the CLI:

```bash
cd /opt/buildforge/agent && sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; python3 -m buildforge_agent show BF-20261012-ABC123'
```

The database (`/var/lib/buildforge/buildforge.db`) has three tables: `incidents` (one per failed build, with `status`,
`error`, `analysis` JSON, `fingerprint`, `next_build`/`next_result`/`fixed_build`), `builds` (every finished build, for
"fixed on the next build") and `rebuilds`. Query it without the sqlite3 CLI:

```bash
sudo python3 - <<'EOF'
import sqlite3
c = sqlite3.connect("file:/var/lib/buildforge/buildforge.db?mode=ro", uri=True)
for r in c.execute("select id, job, build, status, category, rebuild_can_fix, substr(coalesce(error,''),1,80) from incidents order by created desc limit 15"):
    print(r)
EOF
```

### 4.5 Debug the plugin

- **Jenkins log:** Manage Jenkins › System Log › Add new log recorder for `io.jenkins.plugins.buildforge` at FINE. The
  listener logs `Build Forge service answered <status> …` and `unreachable … (attempt n)` (it retries), and
  `Could not save the Build Forge action …`.
- **Is it loaded?** Manage Jenkins › Plugins › Installed › Build Forge (short name `buildforge`). If missing, check
  `$JENKINS_HOME/logs` or `journalctl -u jenkins` for the load error (a wrong Jenkins version, or a damaged file).
- **Is it configured?** The Script Console prints the effective settings (the secret is not shown):

  ```groovy
  def c = io.jenkins.plugins.buildforge.BuildForgeConfig.get()
  println "enabled=${c.enabled} url=${c.serviceUrl} secretSet=${c.secret != null}"
  ```
- **Browser:** open the failed build with the developer tools Network tab. The panel calls
  `…/job/<name>/<n>/buildforge/analysis` (GET, JSON) and, on click, `…/buildforge/rebuild` (POST with the Jenkins
  crumb). A 502 body carries the service's error text. The admin page calls `/buildforge/stats`, `/buildforge/incidents`,
  `/buildforge/incident`, `/buildforge/reanalyze`. 404s on `…/stages/tree`, `…/pipeline-overview/tree`,
  `…/pipeline-graph/tree` and `…/wfapi/describe` are harmless probes for the stage list ("Open stage" link).
- **JS/CSS changes:** `buildforge.js` and `buildforge.css` are served from the plugin (`/plugin/buildforge/…`); after
  rebuilding the `.hpi` Jenkins must be restarted and the browser cache cleared.

### 4.6 "Fixed on the next build" and rebuild bookkeeping

Every finished build is reported (`/api/v2/build-result`). The service marks an incident `next_build/next_result` from the
first later build of the same job, and `fixed_build` from the first later build that succeeded. A Build Forge rebuild is
recognised by the `RebuildCause` on the new build (`rebuild_of`), so "rebuilds that went green" and "came back after a
rebuild" (a failed rebuild counts as that) are counted separately. If stats look wrong, check that `builds` has the rows
you expect for the job.

---

## 5. Troubleshooting guide

### Install and connectivity

| Symptom | Cause | Fix |
|---|---|---|
| `check`: `FAIL … runner … unreachable … timed out` | Network block between Jenkins server and Kiro instance (a wrong secret gives 401, not a timeout) | Security group inbound TCP 8796 from the Jenkins server; then `curl -s --max-time 5 http://<kiro-ip>:8796/healthz` from the Jenkins server. Also check `sudo ss -ltnp \| grep 8796` (must be `0.0.0.0`, not `127.0.0.1`), `ufw status` and network ACLs between the subnets |
| `curl … /analyze` → `missing or wrong X-BuildForge-Secret` | Literal `SECRET` pasted, or the two env files differ | Use `$SECRET` read from `runner.env` (2.2); copy the same value to `BF_RUNNER_SECRET` on the Jenkins side and restart |
| Service exits at start: `BF_SHARED_SECRET is not set` | Empty secret in `buildforge.env` | `openssl rand -hex 32`, put it in the env file and in the plugin settings |
| `kiro-cli not found on this instance` (500) | systemd's PATH does not include `~/.local/bin` | Set `BF_KIRO_BIN=<full path>` in `runner.env`, restart `buildforge-runner` |
| Kiro asks to log in / 502 right away | `kiro-cli` not logged in for the service user | `kiro-cli login` as that user, or `KIRO_API_KEY=` in `runner.env` |
| `/mcp` shows the buildforge server failed | Python path or file missing in the agent JSON | Check `~/.kiro/agents/buildforge-fixer.json` points to existing `__PYTHON__` and `/opt/buildforge-runner/buildforge_mcp.py`; run the MCP test in 4.3 |
| Kiro: model `claude-opus-4.5` unknown | Model id differs in your Kiro version | Copy the `"model"` value from a working agent file into `buildforge-fixer.json` |
| Plugin does not appear after restart | File not named `.jpi`/wrong owner, or Jenkins older than 2.346 | `ls -l /var/lib/jenkins/plugins/buildforge.jpi`, owner `jenkins`; check the Jenkins log |

### Analysis problems

| Symptom | Cause | Fix |
|---|---|---|
| Panel: "Waiting for Build Forge to receive this build" for long | The failure never reached the service: wrong secret, service down, or plugin disabled (a filtered job is still recorded, it only shows "Excluded from analysis") | `journalctl -u buildforge -n 30`; Jenkins log for `Build Forge service answered 401`; compare secrets |
| Panel: "Build Forge has no record of this build" | Build failed before the plugin was installed, or the job is filtered | Expected for old builds; rebuild the job |
| Panel: "Kiro could not analyse this failure: … unreachable" | Runner or network (above) | Fix the path, then **Analyse again** on the admin page |
| `… HTTP 401` | Runner secret mismatch | Same value on both sides, restart both services |
| `… HTTP 502 … did not submit an analysis` | Kiro not logged in, agent file missing, MCP server failed to start, or the model answered in text instead of calling the tool | Use 4.3 to run the agent by hand; the response's `stderr_tail` and `journalctl -u buildforge-runner` show why |
| `… HTTP 504 / took longer than N s` | Opus turn over `BF_TIMEOUT` | Raise `BF_TIMEOUT` on the runner and `BF_RUNNER_TIMEOUT` on the service (keep the second higher) |
| Analyses queue up | `BF_MAX_PARALLEL` (default 3) reached | Raise it if Kiro credits and the instance allow |
| Incident stuck on `analysing` | Service was restarted mid-analysis | It resumes on start; otherwise **Analyse again** |
| Analysis is vague or wrong | The excerpt cut the wrong part of the log, or the log was thin | `extract` (4.3) on the saved log; try `BF_SEND=full`; improve `errors.py`; see section 6 for the prompt |
| Duplicate entries for one build | Not possible by design (unique job+build); a retry returns `duplicate: true` | Check the `jsonl` for repeated events instead |

### Rebuild and stats

| Symptom | Cause | Fix |
|---|---|---|
| No **Rebuild** button | The agent said a rebuild will not help (the panel explains why), a later build already passed, a Build Forge rebuild is running, or you lack Build permission | Read the "rebuild_reason" line; log in as a user with Build on the job |
| Rebuild click gives an error | 400: that kind of job cannot be rebuilt from here. 409: the analysis says a rebuild will not fix it, or Jenkins did not queue the build (job disabled or already queued). 502: the service is unreachable | The message is shown in the panel; the Jenkins log has the detail |
| Rebuild goes green but the incident stays open | The build-result event was lost (service down during that moment) | Check `builds` table (4.4); the next completed build repairs the bookkeeping |
| Admin stat cards are all zero | Wrong period, service has no data, or the plugin's service URL points to another service | Change the Period; `curl` `/api/v2/stats` (4.2) |
| `/buildforge/admin` → 403 | Only Administer users by default | Plugin Advanced option, or log in as an admin |
| Admin page blank, console error | Cached old `buildforge.js` after an upgrade | Hard refresh; confirm Jenkins was restarted after copying the new `.jpi` |
| "Open stage" link missing | Pipeline Graph View / Stage View API does not answer for that build | Harmless; the Console link always works |

### Alerts, disk, housekeeping

| Symptom | Cause | Fix |
|---|---|---|
| No Teams card | `TEAMS_WEBHOOK_URL` empty, or the category is not in `BF_NOTIFY_ON` | `python3 -m buildforge_agent check --teams`; set `BF_NOTIFY_ON=all` to test. check the flow's run history in Teams |
| `/var/log/buildforge` or the DB grows | Retention set to 0, or many failing jobs | Lower `BF_RETENTION_DAYS`; pruning runs daily; use `BF_JOB_EXCLUDE` for noisy jobs |
| A job's log must not go to Kiro | Secrets in console output | `BF_JOB_EXCLUDE=<regex>` (the failure stays on the dashboard, its log is not sent); Jenkins masks bound credentials but not other secrets |
| Dashboard shows a failure as "Excluded" and you want it analysed | The job matches the exclude regex | Open the entry and press **Analyse again**; or fix the regex (Manage Jenkins > System > Build Forge, or `BF_JOB_EXCLUDE`) |
| Dashboard does not update by itself | The browser cannot reach `/buildforge/pulse` (a proxy that buffers or blocks it) or the page was left in a background tab | Open the browser console; `curl` the service's `/api/v2/pulse` (see 2.4); the page polls every 4 s and pauses in hidden tabs |
| Export downloads an empty or error file | Filters match nothing, or the service is down | The first rows of the file repeat the filters used; check `journalctl -u buildforge` |
| A deck card is never used | It is `draft` and seen fewer than `BF_DECK_MIN_SEEN` times, `retired`, or below the similarity threshold | Approve the card in the Solutions deck tab; test with `GET /api/v2/solutions/match?text=<error line>&all=1` |


---

## 6. Changing the behaviour

- **What the agent says and how it judges:** edit `/opt/buildforge-runner/kiro/prompts/buildforge-fixer.md` on the Kiro
  instance (source: `kiro-runner/kiro/prompts/buildforge-fixer.md`). It applies from the next failure; no restart. Keep
  the rule that the log is untrusted evidence and that the only output is the tool call.
- **Which fields the agent returns:** the schema and checks are in `kiro-runner/buildforge_mcp.py` (`TOOL`,
  `validate`). A new field must also be added to `ANALYSIS_FIELDS` in `agent/buildforge_agent/engine.py`, shown in
  `plugin/src/main/webapp/buildforge.js` (panel and admin detail), and written in `agent/buildforge_agent/incidents.py`
  (the Markdown log). Add a test in `kiro-runner/tests/test_runner.py` and `agent/tests/test_service.py`.
- **A new category:** add it to `CATEGORIES` in `buildforge_mcp.py`, describe it in the prompt, add a colour/label in
  `buildforge.js`/`buildforge.css`, and to the admin filter list.
- **Which failures are analysed:** `BF_JOB_INCLUDE` / `BF_JOB_EXCLUDE`, or the plugin's job filters (analysis only; the dashboard always lists everything).
- **What leaves Jenkins:** only the failing stage's output (or, with `BF_SEND=full`, the log tail), the job and stage
  names, the node, and the parameter **names**. Never parameter values.
- **The model:** `"model"` in `kiro/agents/buildforge-fixer.json` (installed copy: `~/.kiro/agents/`).
- After a code change: run both test suites (2.6), rebuild the plugin if Java/Jelly/JS changed.

Conventions: Python is standard library only (keep it deployable with no `pip`); the service and runner share the header
name `X-BuildForge-Secret` and compare secrets in constant time; every HTTP handler returns JSON.

---

## 7. Operations: upgrade, roll back, remove

**Upgrade** (new version): `git pull` in the repo folder, then
- service only: `sudo bash agent/deploy/install.sh` (keeps `/etc/buildforge/buildforge.env` and the data);
- runner only: `sudo RUN_USER=ubuntu bash kiro-runner/deploy/install.sh` (keeps `runner.env`; rewrites the agent JSON and prompt);
- plugin: copy the new `.hpi` over `buildforge.jpi` and restart Jenkins when idle.
The old service works with a new plugin and vice versa for patch changes; when the API version changes, deploy all three.

**Roll back:** check out the previous tag and reinstall the same way; the schema has so far only gained columns (new ones are added by a
migration at start-up), but take a copy of `/var/lib/buildforge/buildforge.db` before you roll back.

**Remove:**

```bash
# Jenkins server
sudo systemctl disable --now buildforge
# Manage Jenkins › Plugins › Installed › Build Forge › Uninstall, restart Jenkins
sudo rm -rf /opt/buildforge /etc/buildforge /var/lib/buildforge /var/log/buildforge   # deletes all history
# Kiro instance
sudo systemctl disable --now buildforge-runner
sudo rm -rf /opt/buildforge-runner /etc/buildforge-runner ~ubuntu/.kiro/agents/buildforge-fixer.json
```

---

## 8. Security notes

- The Kiro agent has exactly one tool (`@buildforge/submit_analysis`), which only records its answer; `includeMcpJson`
  is false, so no other MCP servers or tools are available to it. Log text that tries to instruct it cannot do anything.
- Rebuilds are never automatic. The button exists only when the agent said a rebuild can fix the failure, and Jenkins
  checks the clicking user's Build permission again on the server.
- The service listens on `127.0.0.1` only; the runner speaks plain HTTP, so keep port 8796 inside the VPC, limited to the
  Jenkins server. Both secrets are shared secrets: generate with `openssl rand -hex 32`, never commit them, rotate by
  changing the env file and the plugin setting together.
- Env files are `0600`; the service runs as an unprivileged `buildforge` user with `ProtectSystem=strict`.
- Incident logs contain error text and build URLs: restrict `/var/log/buildforge` (it is `0750`) and keep retention in
  line with your policy.

Plugin developed by Harjeet L.
