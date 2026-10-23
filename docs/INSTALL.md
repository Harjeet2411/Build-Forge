# Installing Build Forge (v2)

Build Forge has three parts:

| Part | Runs on | Port | Installed as |
|---|---|---|---|
| Build Forge runner + MCP server + the `buildforge-fixer` Kiro agent (Claude Opus 4.5) | the Kiro CLI instance | 8796 | systemd service `buildforge-runner` |
| Build Forge service (incidents, incident logs, stats, Teams) | the Jenkins server | 8791 (127.0.0.1 only) | systemd service `buildforge` |
| Build Forge plugin (panel on failed builds, Rebuild button, `/buildforge/admin`) | Jenkins | - | `buildforge.jpi` in `/var/lib/jenkins/plugins` |

```
 Jenkins server                                                    Kiro instance
 ┌──────────────────────────────────────────────┐               ┌──────────────────────────────────────────────┐
 │ Jenkins + Build Forge plugin                 │               │ buildforge-runner (8796)                     │
 │  failed build ─► POST /api/v2/failure        │               │  kiro-cli chat --no-interactive              │
 │  every build  ─► POST /api/v2/build-result   │  POST /analyze│     --agent buildforge-fixer  (Opus 4.5)     │
 │       ▼                                      │               │        │ calls its one MCP tool              │
 │ buildforge service (127.0.0.1:8791)          │               │        ▼                                     │
 │  - cuts out the failed stage's error         │               │  buildforge MCP server: submit_analysis      │
 │  - entry id BF-YYYYMMDD-XXXXXX               │               │    error observed, proposed solution,        │
 │  - /var/log/buildforge/incidents/<id>.md     │               │    category, rebuild_can_fix, signature...   │
 │  - stats for /buildforge/admin, Teams alert  │               │                                              │
 └──────────────────────────────────────────────┘               └──────────────────────────────────────────────┘
```

You need shell access with `sudo` on both machines, Python 3.8+ on both (standard library only, nothing to `pip
install`), and the right to edit the Kiro instance's security group. Get the **whole** repository onto both
machines (clone it, or copy a release archive and unzip it) and work from its top folder:

```bash
git clone https://github.com/Harjeet2411/Build-Forge.git && cd Build-Forge
```

---

## Part A. The Kiro instance

### A1. Check Kiro works headless

As the Linux user that is logged in to Kiro (usually `ubuntu`):

```bash
kiro-cli --version
kiro-cli chat --no-interactive "Reply with the single word: ready"
```

If it asks you to log in, either run `kiro-cli login` as that user, or create a Kiro API key and put it in the runner's
env file in step A3.

### A2. Install the runner, the MCP server and the agent

```bash
cd ~/Build-Forge
sudo RUN_USER=ubuntu bash kiro-runner/deploy/install.sh
```

This:

1. copies `runner.py`, `buildforge_mcp.py` and the agent's instructions to `/opt/buildforge-runner/`;
2. writes the Kiro agent `~ubuntu/.kiro/agents/buildforge-fixer.json` (model `claude-opus-4.5`, one MCP server
   `buildforge`, one allowed tool `@buildforge/submit_analysis`, nothing else);
3. creates `/etc/buildforge-runner/runner.env` with a new secret and the full path of `kiro-cli`;
4. starts the service `buildforge-runner` on port 8796.

Note the secret, you need it in part B:

```bash
sudo grep BF_RUNNER_SECRET /etc/buildforge-runner/runner.env
```

### A3. Only if Kiro needs an API key

```bash
sudo nano /etc/buildforge-runner/runner.env      # add:  KIRO_API_KEY=<your key>
sudo systemctl restart buildforge-runner
```

### A4. Check the agent and its MCP server by hand

Interactive check (no Kiro credits used until you type a question):

```bash
kiro-cli chat --agent buildforge-fixer
/mcp          # should list the "buildforge" server as loaded, with the tool submit_analysis
/quit
```

If `/mcp` shows the server failed: run `python3 /opt/buildforge-runner/buildforge_mcp.py` and press Ctrl+C; it should
wait silently (it speaks MCP over stdin). If Kiro says the model `claude-opus-4.5` is unknown, open another Kiro agent file
that already works for you (`~/.kiro/agents/*.json`), copy its exact `"model"` value into
`~/.kiro/agents/buildforge-fixer.json`, and try again.

Then one real analysis through the runner (uses one Kiro turn):

```bash
curl -s http://127.0.0.1:8796/healthz
SECRET=$(sudo grep '^BF_RUNNER_SECRET=' /etc/buildforge-runner/runner.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8796/analyze -H "X-BuildForge-Secret: $SECRET" -H "Content-Type: application/json" \
  -d '{"job":"demo","build":1,"stage":"Checkout","error":"+ git fetch origin\nfatal: unable to access https://git.example.com/app.git/: Could not resolve host: git.example.com\nERROR: script returned exit code 128"}'
```

Expected after 20 to 90 seconds: JSON with `"analysis": {"error_observed": ..., "proposed_solution": ...,
"rebuild_can_fix": true, ...}`. A `502` means Kiro answered without calling the tool or did not start; its
`stderr_tail` and `sudo journalctl -u buildforge-runner -n 30` say why.

### A5. Let the Jenkins server reach port 8796

In AWS, edit the Kiro instance's security group: inbound **Custom TCP 8796**, source = the Jenkins server's security
group (or its private IP `/32`). Never open it to the internet: it is plain HTTP protected by the shared secret.

---

## Part B. The Jenkins server

### B1. Install the Build Forge service

```bash
cd ~/Build-Forge
sudo bash agent/deploy/install.sh
```

It creates the `buildforge` user, `/opt/buildforge/agent`, `/var/lib/buildforge` (database), `/var/log/buildforge/incidents`
(incident logs) and `/etc/buildforge/buildforge.env` with a generated plugin secret, then starts the service `buildforge`
on `127.0.0.1:8791`.

### B2. Point it at the runner

```bash
sudo nano /etc/buildforge/buildforge.env
```

Set:

```
BF_RUNNER_URL=http://<kiro-instance-private-ip>:8796
BF_RUNNER_SECRET=<the BF_RUNNER_SECRET from step A2>
TEAMS_WEBHOOK_URL=<optional: your Workflows "Send webhook alerts to a channel" URL>
BF_NOTIFY_ON=infrastructure
```

Then restart and check (the check spends one Kiro turn; add `--no-kiro` to skip that, `--teams` to post a test card):

```bash
sudo systemctl restart buildforge
sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; cd /opt/buildforge/agent; python3 -m buildforge_agent check'
```

Expected:

```
OK   Incident log folder: /var/log/buildforge/incidents
OK   Build Forge runner: http://10.x.x.x:8796 (agent buildforge-fixer, kiro /home/ubuntu/.local/bin/kiro-cli)
OK   Kiro agent: transient, rebuild_can_fix=True, 41s: `git fetch origin` failed ...
```

### B3. Install the plugin

```bash
# build the plugin against the war of YOUR Jenkins (needs a JDK at least as new as the Java Jenkins runs on: `java -version` tells you; for Java 21 use sudo apt install openjdk-21-jdk-headless)
bash plugin/build.sh /usr/share/java/jenkins.war      # or wherever your jenkins.war is; writes plugin/buildforge.hpi
sudo cp plugin/buildforge.hpi /var/lib/jenkins/plugins/buildforge.jpi
sudo chown jenkins:jenkins /var/lib/jenkins/plugins/buildforge.jpi
```

Restart Jenkins when no builds are running: open `https://jenkins.example.com/safeRestart`.
(Or upload `buildforge.hpi` in Manage Jenkins › Plugins › Advanced settings › Deploy Plugin; the file picker opens on
the computer your browser runs on, so copy the file there first.)

### B4. Connect the plugin to the service

Manage Jenkins › System › **Build Forge**:

| Field | Value |
|---|---|
| Enabled | on |
| Build Forge service URL | `http://127.0.0.1:8791` |
| Shared secret | the value of `BF_SHARED_SECRET` (`sudo grep BF_SHARED_SECRET /etc/buildforge/buildforge.env`) |
| Show the Build Forge panel on failed builds | on |
| Advanced › Let every signed-in user open /buildforge/admin | off = Jenkins administrators only |
| Advanced › Only / Skip jobs matching | optional regular expressions on the full job name |

Save.

---

## Part C. Try it

1. Make a throwaway pipeline that fails like a network hiccup:

   ```groovy
   pipeline { agent any
     stages {
       stage('Checkout') { steps { sh 'echo "fatal: unable to access https://git.example.com/x.git/: Could not resolve host: git.example.com"; exit 128' } }
       stage('Build') { steps { echo 'never runs' } }
     } }
   ```

2. Build it and open the failed build. The **Build Forge** panel shows the stages with the failed one marked, and under it
   "Kiro is reading the error of this stage…"; 20 to 90 seconds later it shows **Error observed**, **Proposed
   solution**, the category, and, because this one is transient, **A rebuild with the same parameters should fix this**
   with a **Rebuild** button (only for users with Build permission on the job).
3. Click **Rebuild**. The new build carries the cause "Rebuilt by Build Forge from #N (entry BF-…)" and the same
   parameter values. When it finishes, the original incident's outcome becomes "Fixed on the next build (rebuild)" or,
   if it failed again, the new failure is analysed knowing a rebuild did not help (no Rebuild button this time).
4. Open `https://jenkins.example.com/buildforge/admin` (also in the Jenkins menu as **Build Forge**): stats, every failed
   build, and each entry's incident log (click a row).
5. On the server, the same log as a file:

   ```bash
   sudo ls -lt /var/log/buildforge/incidents | head
   sudo cat /var/log/buildforge/incidents/BF-20261012-XXXXXX.md
   sudo tail -n 3 /var/log/buildforge/incidents.jsonl          # one JSON line per event, for log shippers
   cd /opt/buildforge/agent && sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; python3 -m buildforge_agent show BF-20261012-XXXXXX'
   ```

---


## Using the dashboard, the reports and the solutions deck

Open **Build Forge** in the Jenkins sidebar (`/buildforge/admin`, needs Administer).

- **Failures tab**: every failed build. Search (press `/`), pick a period (presets, a month, or a custom range), pipelines
  (type to filter; `payments/*` selects a whole folder), category, outcome, analysed or excluded, rebuild. Click a column to sort,
  click a row for the entry. Jobs matching the exclude regex are listed as "Excluded"; **Analyse again** on one sends it to Kiro.
- **Live**: the page checks for changes every 4 seconds; the **Live** button pauses it.
- **Export**: choose CSV, XLSX or PDF and *list*, *summary* or *both*. The report uses the filters on screen, so set the month and
  the pipelines first. The first rows of the file repeat the filters used.
- **Solutions deck tab**: one card per kind of error. Cards start as `draft`; a draft is used once its error was seen
  `BF_DECK_MIN_SEEN` times. **Approve** a card after checking its answer; edit it if Kiro's wording needs fixing (edited cards are
  never overwritten). **Try an error** shows which card would answer a pasted error. The deck answers failures only when Kiro
  is unavailable, unless `BF_DECK_MODE=first`. After a few months of data, review the cards and consider `first` for approved ones.

## Settings reference

`/etc/buildforge/buildforge.env` (Jenkins server, restart `buildforge` after a change):

| Setting | Default | What it does |
|---|---|---|
| `BF_LISTEN` | `127.0.0.1:8791` | Where the plugin reaches the service |
| `BF_SHARED_SECRET` | generated | Same value as the plugin's Shared secret |
| `BF_RUNNER_URL` / `BF_RUNNER_SECRET` | - | The runner on the Kiro instance |
| `BF_RUNNER_TIMEOUT` | 300 | Seconds to wait for one analysis (keep above the runner's `BF_TIMEOUT`) |
| `BF_LOG_DIR` | `/var/log/buildforge` | Incident logs: `incidents/<ID>.md` + `incidents.jsonl` |
| `BF_DATA_DIR` | `/var/lib/buildforge` | SQLite database behind the admin page |
| `BF_RETENTION_DAYS` | 365 | Older entries and their log files are deleted daily; 0 keeps everything |
| `BF_SEND` | `errors` | `errors` = only the failed stage's output goes to Kiro; `full` = the end of the console log |
| `BF_MAX_ERROR_CHARS` | 16000 | Upper limit of what is sent to Kiro |
| `TEAMS_WEBHOOK_URL` / `BF_NOTIFY_ON` | - / `infrastructure` | Teams card for these categories (comma list, or `all`) |
| `BF_JOB_INCLUDE` / `BF_JOB_EXCLUDE` | - | Regular expressions on the full job name. They only choose which failures **go to Kiro**; every failure is still recorded and shown on the dashboard as "Excluded" |
| `BF_DECK_MODE` | `fallback` | Solutions deck: `off`, `fallback` (a card answers only when Kiro is unavailable) or `first` (a strong match answers before Kiro) |
| `BF_DECK_THRESHOLD` / `BF_DECK_FIRST_THRESHOLD` | 0.55 / 0.85 | Similarity (0-1) needed for a fallback answer / for answering first (approved cards only) |
| `BF_DECK_MIN_SEEN` | 2 | A draft card is used once its error was seen this many times; approved cards are always used |

`/etc/buildforge-runner/runner.env` (Kiro instance, restart `buildforge-runner` after a change):

| Setting | Default | What it does |
|---|---|---|
| `BF_RUNNER_LISTEN` | `0.0.0.0:8796` | Where the runner listens |
| `BF_RUNNER_SECRET` | generated | Same value as `BF_RUNNER_SECRET` on the Jenkins side |
| `KIRO_API_KEY` | - | Kiro login for a headless instance |
| `BF_AGENT` | `buildforge-fixer` | Kiro agent name |
| `BF_TIMEOUT` | 240 | Seconds one Kiro turn may take |
| `BF_MAX_PARALLEL` | 3 | Failures analysed at the same time; the rest wait |
| `BF_KIRO_BIN` | found by install.sh | Full path of `kiro-cli` |

To change what the agent says, edit `/opt/buildforge-runner/kiro/prompts/buildforge-fixer.md` on the Kiro instance; it
applies from the next failure. The fields it must fill are fixed by the MCP tool (`buildforge_mcp.py`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| No Build Forge panel on a failed build | Plugin not enabled or no service URL (B4); the job is filtered out; the build failed before the plugin was installed |
| Panel says "Waiting for Build Forge to receive this build" for long | Wrong secret or service down: `sudo journalctl -u buildforge -n 30`, and Jenkins' log for "Build Forge service answered 401" |
| Panel says "Kiro could not analyse this failure: … unreachable" | Security group / IP for port 8796 (A5); `curl http://<kiro-ip>:8796/healthz` from the Jenkins server |
| "… answered HTTP 401" | `BF_RUNNER_SECRET` differs between the two env files |
| "… HTTP 502 … did not submit an analysis" | Kiro not logged in (A3), agent missing, or the MCP server failed to start (A4, `/mcp`). Then use **Analyse again** on the admin page |
| "… HTTP 504" | Kiro took longer than `BF_TIMEOUT`; raise it and `BF_RUNNER_TIMEOUT` with it |
| No Rebuild button | Kiro judged a rebuild won't help (the panel says why), a later build already passed, or you lack Build permission on the job |
| `/buildforge/admin` gives 403 | Only Jenkins administrators by default; see B4 Advanced |
| "Open stage" link missing | It appears when the Pipeline Graph View or Stage View plugin's API answers for that build; the Console link always works |

## Security notes

- Only the failing stage's output and the parameter **names** leave Jenkins; parameter values never do. Jenkins masks
  bound credentials in the console, but use `BF_JOB_EXCLUDE` for jobs whose logs must not go to Kiro (they stay on the dashboard, but their log text is never sent).
- The Kiro agent has exactly one tool, which only records its answer. Text in a log that tries to instruct it cannot
  make it do anything; the prompt also tells it to treat the log as untrusted evidence.
- Rebuilds are never automatic. The button only exists when the agent said a rebuild can fix the failure, and Jenkins
  checks the clicking user's Build permission again on the server.
- The service listens on 127.0.0.1 only; the runner is plain HTTP, so keep port 8796 inside the VPC.
