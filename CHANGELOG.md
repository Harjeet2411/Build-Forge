# Changelog

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
