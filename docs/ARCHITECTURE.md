# Build Forge architecture

This is the reference for how Build Forge is built: components, interfaces, data model and the design decisions behind
them. For installing it see [INSTALL.md](INSTALL.md); for day-to-day debugging see [DEVELOPER-GUIDE.md](DEVELOPER-GUIDE.md);
to rebuild it from scratch with an AI coding tool see [RECREATE-WITH-AI.md](RECREATE-WITH-AI.md).

## 1. Goals and non-goals

Goals

- Every failed Jenkins build, in every job, gets an analysis (error observed, proposed fix) with **no Jenkinsfile changes**.
- The analysis is written by an AI agent that reads only the relevant part of the log and answers through a **structured,
  validated tool call**, never free text.
- A **Rebuild** button appears only when the agent says a rebuild with identical parameters can fix the failure.
- Every failure and its analysis has a unique, human-quotable id and a durable **incident log** on the Jenkins server.
- An admin page shows every failure and statistics, including how many pipelines failed and were fixed by the next build.

Non-goals

- No chat with the agent on the build page (analysis only).
- No automatic rebuilds. A human clicks; Jenkins checks that user's Build permission.
- No changes to the pipelines, and no credentials for the AI agent: it has one tool, which only records its own answer.

## 2. Components

```
┌───────────────────────── Jenkins server ─────────────────────────┐         ┌──────── Kiro / AI instance ────────┐
│ Jenkins                                                          │         │                                    │
│  └ Build Forge plugin (Java)                                     │         │  buildforge-runner (Python, :8796) │
│      RunListener    ── every build ──────────┐                   │         │    one headless AI turn per failure│
│      RunAction      panel on failed builds   │ HTTP + shared     │         │      └ AI agent (Claude Opus 4.5)  │
│      RootAction     /buildforge/admin        │ secret            │ POST    │          └ MCP server (stdio)      │
│                                              ▼                   │ /analyze│              tool submit_analysis  │
│  buildforge service (Python, 127.0.0.1:8791) ───────────────────┼────────►│                                    │
│    SQLite · incident logs (.md + .jsonl) · Teams alerts          │ ◄───────┼── {analysis: {...}}                │
└──────────────────────────────────────────────────────────────────┘         └────────────────────────────────────┘
```

| Component | Language | Responsibility | Talks to |
|---|---|---|---|
| Plugin | Java 11, Jelly, vanilla JS | Hook every build; show the panel and admin page; queue rebuilds; enforce Jenkins permissions | the service (never the AI side) |
| Service | Python 3 stdlib | Create incidents, cut the log down, ask the runner, store results, write incident logs, compute outcomes and stats, send Teams cards | runner, SQLite, filesystem |
| Runner | Python 3 stdlib | Run one headless AI turn per request with the agent definition; return the structured result | AI CLI, MCP server |
| MCP server | Python 3 stdlib | Expose one tool, `submit_analysis`; validate its arguments; write them to a result file | runner (via the result file) |
| Agent definition | JSON + Markdown prompt | Model, prompt, the single allowed tool | AI CLI |

Why two Python services and not one: the AI CLI needs a logged-in user and credits, so it lives on its own instance; the
service lives next to Jenkins and keeps the data. The only network link between them is one authenticated HTTP call.

## 3. Interfaces

All HTTP bodies are JSON. Secrets travel in the header `X-BuildForge-Secret` and are compared in constant time.

### 3.1 Runner (`kiro-runner/runner.py`, default port 8796)

| Call | Meaning |
|---|---|
| `GET /healthz` | `{"ok": true, "agent": "...", "kiro": "<path of the CLI>"}` (no secret needed) |
| `POST /analyze` | Body `{job, build, build_url, stage, node, params[], error, history{...}}` → `200 {"analysis": {...}, "seconds": n, "agent": "...", "text": "..."}` |

Errors: `401` wrong secret, `413` body over 512 KB, `500` CLI not found, `502` the agent finished without calling the tool
(body has `stderr_tail`), `504` over `BF_TIMEOUT`.

The runner builds a prompt from the body (job, build, URL, failed stage, node, parameter **names**, history of the same
error, and the error text wrapped in `<log>…</log>`), runs

```
<cli> chat --no-interactive --agent buildforge-fixer --require-mcp-startup --trust-tools=@buildforge/submit_analysis "<prompt>"
```

with `BF_RESULT_FILE=<temp file>` in the environment, and returns the content of that file.

### 3.2 The tool: `submit_analysis` (`kiro-runner/buildforge_mcp.py`)

| Field | Type | Notes |
|---|---|---|
| `error_observed` | string (≤1500) | What failed, 1 to 3 sentences, quoting the key text |
| `proposed_solution` | string, Markdown (≤6000) | Numbered options, root-cause fix first |
| `category` | enum | `application` `infrastructure` `configuration` `dependency` `test_failure` `transient` `unknown` |
| `severity` | enum | `low` `medium` `high` |
| `confidence` | number 0..1 | |
| `rebuild_can_fix` | boolean | true only for transient problems; false for anything deterministic |
| `rebuild_reason` | string | One sentence |
| `signature` | string | Short stable label, lower-case with dashes (`npm-e401-codeartifact`); used to group the same error |
| `affected_component` | string, optional | |

Invalid arguments make the tool return an error to the model, which then calls it again. Transport is MCP over
newline-delimited JSON-RPC on stdio (protocol `2025-03-26` or newer, tools only).

### 3.3 Service (`agent/buildforge_agent/server.py`, default `127.0.0.1:8791`)

| Call | Used by | Meaning |
|---|---|---|
| `GET /healthz` | anyone | version, runner configured?, Teams configured?, log dir |
| `POST /api/v2/failure` | plugin | A failed build: `{job, build, result, build_url, timestamp, duration_ms, node, params[], log, rebuild_of?, triggered_by?: {kind, id, name}, commits?: [{id, author, author_id, email, message}]}` → `202 {id, status}` (the analysis runs in the background; a repeat returns `duplicate: true`) |
| `POST /api/v2/build-result` | plugin | Every finished build: `{job, build, result, timestamp, duration_ms, rebuild_of?}` (feeds "fixed on the next build") |
| `POST /api/v2/rebuild-event` | plugin | `{job, build, phase: "requested"\|"started", user, new_build?}` |
| `POST /api/v2/reanalyze` | plugin (admin page) | `{id}`: queue the analysis again |
| `GET /api/v2/analysis?job=&build=` | plugin (panel) | What the panel shows: status, stages, analysis, outcome, rebuilds |
| `GET /api/v2/incidents?…&sort=&dir=&limit=&offset=` | plugin (admin) | Paged list. Filters (shared by every call marked †): `days` or `from`/`to` (epoch s, `to` exclusive), `q` (search), `jobs` (`\|`-separated names, `*` wildcards), `category`, `outcome`, `analysis` (`analysed`/`skipped`), `rebuild`, `result`. Sort keys: `when`, `job`, `build`, `category`, `signature`, `outcome`, `seen` |
| `GET /api/v2/incident?id=` | plugin (admin) | Full detail including the log text |
| `GET /api/v2/stats` † (+ `tz`) | plugin (admin) | The numbers behind the stat cards and charts; `tz` (minutes east of UTC) puts the day buckets in the viewer's time |
| `GET /api/v2/pipelines` † | plugin (admin) | Pipeline names for the picker, with failure counts |
| `GET /api/v2/pulse?since=` | plugin (admin) | `{rev, now, changes[]}`: a counter that moves on every write, and the entries changed since `since`. The dashboard polls it every 4 s |
| `GET /api/v2/export?format=csv\|xlsx\|pdf&scope=list\|summary\|full` † | plugin (admin) | A report for the current filters (and `sort`/`dir`), returned as a download |
| `GET /api/v2/alerts?status=open\|dismissed\|resolved\|all&user=` | plugin (admin) | The bell: alerts (critical first) with `contacts` and the latest error, per-user `read`, and `counts` (open, unread, per severity) |
| `POST /api/v2/alerts/read` `{ids[], all, unread, user}` | plugin (admin) | Mark alerts read (or unread) for `user` |
| `POST /api/v2/alerts/dismiss`, `/alerts/restore` `{id, user}` | plugin (admin) | Remove an alert from the bell for everyone, or bring it back |
| `GET /api/v2/solutions?q=&status=&category=&sort=&dir=` | plugin (admin) | The solutions deck: cards with sightings, pipelines and how often the next build went green |
| `GET /api/v2/solution?id=` | plugin (admin) | One card with the incidents it covers |
| `GET /api/v2/solutions/match?text=&min=&all=1` | plugin (admin) | Best card for an error text (without `all=1` only usable cards) |
| `GET /api/v2/solutions/export?format=json\|csv\|xlsx\|pdf&status=` | plugin (admin) | The deck as a download; JSON can be imported on another server |
| `POST /api/v2/solution/update`, `/solution/create`, `/solutions/import` | plugin (admin) | Edit a card (title, answer, status, notes, rebuild advice), add one by hand, or import a JSON export |

### 3.4 Plugin (Jenkins URLs)

| URL | Method | Notes |
|---|---|---|
| `/job/<name>/<n>/buildforge/analysis` | GET | Proxy of `/api/v2/analysis` for the panel; needs Read |
| `/job/<name>/<n>/buildforge/rebuild` | POST + crumb | Needs Build on the job; refused unless the analysis says `rebuild_can_fix`; queues the job with the same parameters and a `RebuildCause` |
| `/buildforge/admin` | GET | Admin page; needs Administer (or Read when the option "every signed-in user" is on) |
| `/buildforge/stats`, `/incidents`, `/incident`, `/pipelines`, `/pulse`, `/export`, `/solutions`, `/solution`, `/solutionMatch`, `/solutionsExport` | GET | Proxies of the service for the admin page. Only whitelisted query parameters are forwarded; the browser never talks to the service. Reports and the deck export need the same permission as the page (Administer by default) |
| `/buildforge/alerts` | GET | Proxy of the bell; the plugin adds the signed-in user, so "read" is per person |
| `/buildforge/alertsRead`, `/alertDismiss`, `/alertRestore` | POST + crumb | Mark read, dismiss, restore (the user comes from the Jenkins session, never from the browser) |
| `/buildforge/reanalyze`, `/solutionUpdate`, `/solutionCreate`, `/solutionsImport` | POST + crumb | Proxies for re-analysis and for editing the deck |

## 4. Data model

SQLite file `buildforge.db` (schema in `agent/buildforge_agent/store.py`):

- `incidents`: one row per failed build, unique on `(job, build)`. Holds the id (`BF-YYYYMMDD-XXXXXX`), the status
  (`queued → analysing → done | error`), the cut-down excerpt sent to the agent, a **fingerprint** (hash of the error lines with
  numbers blanked, used to find the same error again), the analysis JSON, the extracted fields (`signature`, `category`,
  `rebuild_can_fix`), timings, and the outcome fields `next_build`, `next_result`, `next_is_rebuild`, `fixed_build`, `fixed_at`.
- `builds`: every finished build (green ones too), so a later success can be matched to an earlier failure.
- `rebuilds`: each Build Forge rebuild: who asked, the new build number and its result.
- `alerts` and `alert_reads`: one alert (`AL-XXXXXX`) per streak of failed builds of a pipeline (environment, severity, the limit it
  exceeded, streak length, first and last build, status `open`/`dismissed`/`resolved`, who to contact as JSON) and which person
  has read which alert.
- `incidents.triggered_by` and `incidents.commits`: who started the build and the commit authors, as the plugin sent them.
- `solutions`: the solutions deck, one card (`SD-XXXXXX`) per kind of error: signature, title, category, the answer, whether a
  rebuild can fix it, the normalised error lines it was written for, the fingerprints it covers, `status`
  (`draft`/`approved`/`retired`), `source` (`auto`/`manual`/`import`) and notes.

An incident has the status `skipped` when its job is excluded from analysis. It is stored and listed like any other failure.

Files on disk (`BF_LOG_DIR`): `incidents/<id>.md`, a human-readable report rewritten when the outcome changes (see
[sample-incident.md](sample-incident.md)); and `incidents.jsonl`, one JSON line per event (`analysed`, `analysis_failed`,
`rebuild`, `outcome`) for log shippers. Both contain the build URL.

## 5. How the main behaviours work

**Cutting the log** (`errors.py`): strip ANSI and timestamps; find the failing `[Pipeline] { (Stage)` block, the node
(`Running on …`), the output of the failing `sh`/`bat`/`powershell` step including stack traces, and Jenkins' own `ERROR:` and
`Finished:` lines; drop package-manager noise; cap at `BF_MAX_ERROR_CHARS`. This is what makes the analysis cheap and focused.

**History in the prompt**: before asking, the service looks up earlier incidents with the same fingerprint (same job and
overall), including whether the error **came back after a rebuild**. The agent is told, and the prompt says that if it did,
`rebuild_can_fix` must be false.

**Fixed on the next build**: every finished build is reported. For each earlier failed build of the job the service records
the first later build (`next_build`, `next_result`) and the first later green build (`fixed_build`). A build started by Build
Forge carries `rebuild_of`, so a rebuild that goes green or fails again is counted separately.

**Rebuild**: the panel shows the button only when `rebuild_can_fix` is true, no later build has passed and no rebuild is
running. The plugin re-checks the analysis and the user's Build permission on the server, queues the job with the original
`ParametersAction` plus a `RebuildCause` ("Rebuilt by Build Forge from #N (entry BF-…)"), and tells the service.

**Under the failed stage**: the plugin cannot inject content into the Pipeline Graph View, so the panel draws its own stage
strip (from the service's parsed stages) with the failed stage marked and the analysis attached beneath it.

**Every failure is recorded, only some are analysed**: the plugin sends every failed build with an `analyse` flag (its job
regexes) and the service applies `BF_JOB_INCLUDE`/`BF_JOB_EXCLUDE` as well. Analysed failures are queued for Kiro; the rest are
stored as `skipped` and never leave Jenkins. **Analyse again** works on a skipped entry. Period filters use the failure time
(`COALESCE(failed_at, created)`), so a month report is about when builds failed, not when they were analysed.

**One filter, three consumers** (`store.where`): the table, the stat cards/charts and the exports all take the same filter
dict, so a report always matches what the dashboard shows. Exports are built with the standard library only: CSV with a BOM
(opens correctly in Excel) and a guard against formulas, XLSX written as raw OOXML, and a PDF written by hand (capped at 3000
rows; CSV/XLSX at 50000).

**Live updates**: every store write bumps a counter (`rev`). The page polls `/pulse` every 4 s (paused in hidden tabs); when
`rev` moved it fetches the changes, flashes the rows, shows a toast and refreshes the numbers. Polling was chosen over
server-sent events because it works behind any proxy and needs no long-lived connections in Jetty.

**Alerts** (`alerts.py`): after every finished build of a pipeline the service counts the failed builds in a row, newest
first, until a successful one (aborted and not-built runs are skipped; they neither count nor end the streak). The pipeline name
decides the environment (`BF_ENV_PROD`, `BF_ENV_UAT`) and so the severity and the limit; when the streak is *more than* the
limit one alert is opened for that streak (and updated as it grows; a dismissed alert stays dismissed). A green build resolves
it. The contacts are built from the incidents of the streak: who started the first and the newest failing build, and every
commit author in those builds with their builds, the authors of the first failing build first. The plugin collects them in
`BuildContacts.java` (`Cause.UserIdCause` and the other causes; `RunWithSCM.getChangeSets()`; the Git plugin's author e-mail,
else the Mailer plugin's address for the Jenkins user, read by reflection so neither plugin is required). Commit messages are
cut to the first line; only the first 20 commits are sent. On start the service evaluates every pipeline once, so failures from
before the upgrade raise their alerts.

**Solutions deck** (`deck.py`): when Kiro's analysis is stored, `Deck.record` files it under its error signature. A repeat
of the same error adds a sighting to the same card; a similar error (same signature, or the same error fingerprint) joins it.
Matching a new failure: an exact fingerprint hit wins; otherwise the words of the normalised error lines are compared with
every usable card (IDF-weighted Jaccard, so rare words count more). A card is usable when it is approved, or a draft seen at
least `BF_DECK_MIN_SEEN` times; retired cards are never used. Modes: `fallback` (default) answers only when Kiro is
unreachable or fails, with a threshold of 0.55; `first` answers before Kiro for a match of 0.85 or more from approved cards
only. A card edited by a person is never overwritten by Kiro's later answers. A rebuild is never suggested by a card if the
same error came back after a rebuild. The panel says "From the solutions deck" with a link to the card, and the incident log records which card answered.

## 6. Security model

- The AI agent has exactly one tool and no other MCP servers (`includeMcpJson: false`, `allowedTools` lists only
  `submit_analysis`). The log is wrapped in `<log>` tags and the prompt tells the agent to treat it as untrusted evidence; a
  `</log>` inside the log is broken so it cannot close the wrapper.
- Only the failing stage's output, job and stage names, node and parameter **names** leave Jenkins. Parameter values never do.
- Two shared secrets (plugin ↔ service, service ↔ runner). The service listens on loopback; the runner is plain HTTP and must
  stay inside the network, restricted to the Jenkins server.
- Rebuilds are human-initiated and permission-checked by Jenkins.
- The service runs as an unprivileged user with a read-only filesystem except its data and log folders.

## 7. Design decisions

| Decision | Reason |
|---|---|
| Plugin built with `javac` only (`plugin/build.sh`), no Maven | Works on a server with just a JDK and the war; no internet; no dependency drift |
| Python standard library only | Nothing to `pip install` on locked-down servers |
| Agent answers through a validated tool call, not free text | Structured fields for the UI, stats and the Rebuild decision; the schema rejects bad answers |
| Agent decides `rebuild_can_fix` itself (no regex rule table) | Rules drift; the model reads the actual error and the history |
| No automatic rebuild | A wrong automatic retry costs builds and hides real defects; a button keeps a human accountable |
| Separate runner process | The AI CLI needs its own login, credits and trust boundary |
| SQLite + Markdown/JSONL logs | One file to back up; logs readable by humans and shippable by agents |
| Panel on the build page, not inside the graph view | The graph view has no extension point for adding content inside its stages |

## 8. Extension points

- **Another AI backend**: replace `run_kiro()` in `kiro-runner/runner.py` (see [RECREATE-WITH-AI.md](RECREATE-WITH-AI.md#6-use-a-different-ai-backend)).
- **Prompt and judgement**: `kiro-runner/kiro/prompts/buildforge-fixer.md`.
- **Fields or categories**: `kiro-runner/buildforge_mcp.py`, `engine.ANALYSIS_FIELDS`, `incidents.py`, `buildforge.js`.
- **Notifications**: `agent/buildforge_agent/teams.py` (an adaptive-card post to a Workflows webhook); add another sender next to it
  and call it from `Engine._maybe_notify`.
- **Which logs are analysed**: `BF_JOB_INCLUDE` / `BF_JOB_EXCLUDE`, or the plugin's job filters (analysis only; all failures are recorded).
- **Report formats**: `agent/buildforge_agent/export.py` (`build()` and `build_deck()`; add a writer next to `to_csv`/`to_xlsx`/`to_pdf`).
- **Deck matching or modes**: `agent/buildforge_agent/deck.py` (`match`, `answer`) and `Engine._finish_from_deck`.
