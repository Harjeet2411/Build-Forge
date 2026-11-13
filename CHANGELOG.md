# Changelog

## Unreleased

- The agent now checks its settings at start-up and names the variable that is wrong: an unknown send or deck mode, a Teams alert
  category that is not one of the known ones, an on/off setting that is neither, a `BF_DECK_THRESHOLD` or `BF_DECK_FIRST_THRESHOLD`
  outside 0-1, a negative count, a zero `BF_RUNNER_TIMEOUT`, and a `BF_RUNNER_URL` or `TEAMS_WEBHOOK_URL` that is not http(s).
- Teams cards no longer title a test failure "Test failure failure".
- `python -m buildforge_agent extract` exits with 2 and a message for a log file that cannot be read, and `show` exits with 1 when the incident has no log.
- More tests: the `check` command, the runner client, the command line and the export helpers.

## 2.2.1 - 2026-10-24

- New logo: a brushed-steel diamond plate with rivets and a green gem-cut tick (docs/branding). Replaces the hexagon mark; the old
  one is kept as `docs/branding/logo-ember-hex-alt.svg`.
- Upgrade: build and copy the plugin only (`bash plugin/build.sh <jenkins.war>`, copy to `buildforge.jpi`, restart Jenkins). The
  service is unchanged apart from its version number.

## 2.2.0 - 2026-10-24

**Alerts (the bell)**
- A bell on the dashboard lists pipelines that keep failing, with a live unread count, a toast and a ringing bell for new ones.
  Critical: a production pipeline failed more than 3 builds in a row. High: UAT or staging, more than 5. Low: any other
  pipeline, more than 5. One green build discards the alert. All limits and the production/UAT name patterns are settings.
- Each alert says who started the failing builds and who committed the code (name, Jenkins id, e-mail when known), so an admin
  knows whom to contact; an **Email** button opens a message with the facts filled in.
- **Mark read** (per person), **Dismiss** (for everyone, until a green build), **Restore**.
- The plugin now sends who started a failed build and its commit authors. The dashboard fills the whole browser window.
- New logo (docs/branding has the concepts and how to switch).

**Upgrade from 2.1**
1. Same as 2.1: `sudo bash agent/deploy/install.sh` (the database gains two columns and two tables on start), then the new plugin
   (`bash plugin/build.sh <jenkins.war>`, copy to `buildforge.jpi`, restart Jenkins). Service and plugin go together; the JDK that builds
   the plugin must be at least as new as the Java Jenkins runs on.
2. Optional: the `BF_ALERT*` and `BF_ENV_*` settings from `agent/agent.env.example`. Alerts for failures that happened before the
   upgrade appear at the first start (commit and starter details only exist for builds after the plugin upgrade).

## 2.1.0 - 2026-10-23

**Dashboard**
- Shows **every** failed build. The job include/exclude regexes now only decide which failures are sent to Kiro; the rest are
  listed as "Excluded from analysis" and can be analysed on request (**Analyse again**).
- Filters: search, period (presets, a month, or a custom range), pipelines (multi-select with `*` wildcards), category,
  outcome, analysed or excluded, rebuild. Sortable columns. The filter state is shown as chips and is shared by the table, the
  numbers, the charts and the reports.
- **Reports** for admins: CSV, XLSX or PDF, for the current filters (month, period, chosen pipelines), as the list, the summary
  or both.
- **Live updates**: the page polls every 4 s, flashes new or changed rows, shows a toast, animates the numbers and charts.
  A Live toggle pauses it.
- New look: minimal, built on Jenkins' own colour variables so it follows the Jenkins theme (light and dark), works on narrow
  screens.

**Solutions deck**
- Every Kiro analysis is filed on a card per kind of error (recurrence, pipelines, how often the next build went green).
- Cards can be edited, approved, retired, added by hand, exported and imported.
- When Kiro is not available, a matching card answers the failure (`BF_DECK_MODE=fallback`, the default). `first` mode answers
  before Kiro for close matches of approved cards; `off` disables it.
- The deck is built from the existing history on first start.

**Upgrade from 2.0**
1. Copy the new files to the Jenkins server and run `sudo bash agent/deploy/install.sh` (keeps your `/etc/buildforge/buildforge.env`;
   the database is migrated automatically on start).
2. Build the plugin again (`bash plugin/build.sh <jenkins.war>`), copy `buildforge.hpi` over
   `/var/lib/jenkins/plugins/buildforge.jpi`, restart Jenkins. The plugin and the service must be upgraded together.
3. Optional: add the `BF_DECK_*` settings from `agent/agent.env.example`.

Behaviour change: jobs matching an exclude regex used to disappear from Build Forge. They now appear on the dashboard.
