package io.jenkins.plugins.buildforge;

import hudson.Extension;
import hudson.model.RootAction;
import hudson.model.User;
import java.io.BufferedReader;
import java.io.IOException;
import java.time.Duration;
import java.util.Arrays;
import java.util.List;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.HttpResponse;
import org.kohsuke.stapler.QueryParameter;
import org.kohsuke.stapler.Stapler;
import org.kohsuke.stapler.StaplerProxy;
import org.kohsuke.stapler.StaplerRequest;
import org.kohsuke.stapler.interceptor.RequirePOST;

/**
 * https://your-jenkins/buildforge/admin : the Build Forge dashboard. Every failed build (analysed or not), live numbers,
 * filters, sorting, report downloads (CSV, XLSX, PDF), each incident's log and the solutions deck. Jenkins administrators
 * only, unless the System setting opens it to every signed-in user. The page talks to the service through this class,
 * with Jenkins' permission check on every call.
 */
@Extension
public class BuildForgeAdmin implements RootAction, StaplerProxy {

    /** Every URL under /buildforge/ (the page and its JSON calls) goes through this permission check. */
    @Override
    public Object getTarget() {
        check();
        return this;
    }

    static boolean canSee() {
        Jenkins j = Jenkins.get();
        return j.hasPermission(Jenkins.ADMINISTER)
                || (BuildForgeConfig.get().isAdminForAllUsers() && j.hasPermission(Jenkins.READ));
    }

    static void check() {
        if (BuildForgeConfig.get().isAdminForAllUsers()) {
            Jenkins.get().checkPermission(Jenkins.READ);
        } else {
            Jenkins.get().checkPermission(Jenkins.ADMINISTER);
        }
    }

    @Override
    public String getIconFileName() {
        return BuildForgeConfig.get().isActive() && canSee() ? "/plugin/buildforge/images/buildforge.svg" : null;
    }

    @Override
    public String getDisplayName() {
        return "Build Forge";
    }

    @Override
    public String getUrlName() {
        return "buildforge";
    }

    public boolean isActive() {
        return BuildForgeConfig.get().isActive();
    }

    /** The query parameters the dashboard may pass on to the service (filters, sorting, paging, report options). */
    private static final List<String> PARAMS = Arrays.asList("days", "from", "to", "q", "jobs", "category", "outcome",
            "analysis", "rebuild", "result", "sort", "dir", "limit", "offset", "tz", "since", "format", "scope",
            "status", "id", "text", "min", "all");

    private static String query() {
        StaplerRequest req = Stapler.getCurrentRequest();
        StringBuilder sb = new StringBuilder();
        for (String name : PARAMS) {
            String v = req.getParameter(name);
            if (v != null && !v.isEmpty()) {
                sb.append(sb.length() == 0 ? '?' : '&').append(name).append('=').append(ServiceClient.enc(v));
            }
        }
        return sb.toString();
    }

    /** GET buildforge/stats?from=&to=&jobs=&... : the cards and charts for the current filter. */
    public HttpResponse doStats() {
        check();
        return ServiceClient.proxy("/api/v2/stats" + query());
    }

    /** GET buildforge/incidents?from=&to=&jobs=&category=&sort=&dir=&limit=&offset=... : the failed builds. */
    public HttpResponse doIncidents() {
        check();
        return ServiceClient.proxy("/api/v2/incidents" + query());
    }

    /** GET buildforge/incident?id=BF-... : one incident with its log file. */
    public HttpResponse doIncident() {
        check();
        return ServiceClient.proxy("/api/v2/incident" + query());
    }

    /** GET buildforge/pipelines : every pipeline with failures, for the pipeline picker. */
    public HttpResponse doPipelines() {
        check();
        return ServiceClient.proxy("/api/v2/pipelines" + query());
    }

    /** GET buildforge/pulse?since= : changes since the last poll, so the dashboard updates itself live. */
    public HttpResponse doPulse() {
        check();
        return ServiceClient.proxy("/api/v2/pulse" + query());
    }

    /** GET buildforge/export?format=csv|xlsx|pdf&scope=list|summary|full&...filters : download a report. */
    public HttpResponse doExport() {
        check();
        return ServiceClient.download("/api/v2/export" + query());
    }

    /** GET buildforge/solutions : the solutions deck. */
    public HttpResponse doSolutions() {
        check();
        return ServiceClient.proxy("/api/v2/solutions" + query());
    }

    /** GET buildforge/solution?id=SD-... */
    public HttpResponse doSolution() {
        check();
        return ServiceClient.proxy("/api/v2/solution" + query());
    }

    /** GET buildforge/solutionMatch?text=... : which card would answer this error. */
    public HttpResponse doSolutionMatch() {
        check();
        return ServiceClient.proxy("/api/v2/solutions/match" + query());
    }

    /** GET buildforge/solutionsExport?format=csv|xlsx|pdf|json : download the deck. */
    public HttpResponse doSolutionsExport() {
        check();
        return ServiceClient.download("/api/v2/solutions/export" + query());
    }

    /** POST buildforge/reanalyze?id=BF-... : ask the Kiro agent again (after fixing the runner, for example). */
    @RequirePOST
    public HttpResponse doReanalyze(@QueryParameter String id) {
        check();
        JSONObject body = new JSONObject();
        body.put("id", id);
        return relay("/api/v2/reanalyze", body);
    }

    private static final String[] CARD_FIELDS = {"id", "title", "category", "error_observed", "proposed_solution",
            "rebuild_can_fix", "rebuild_reason", "status", "notes", "signature", "error_text", "pattern"};

    /** POST buildforge/solutionUpdate (JSON body): edit, approve or retire a card. */
    @RequirePOST
    public HttpResponse doSolutionUpdate(StaplerRequest req) throws IOException {
        check();
        return relay("/api/v2/solution/update", pick(readJson(req), CARD_FIELDS));
    }

    /** POST buildforge/solutionCreate (JSON body): add a card by hand. */
    @RequirePOST
    public HttpResponse doSolutionCreate(StaplerRequest req) throws IOException {
        check();
        return relay("/api/v2/solution/create", pick(readJson(req), CARD_FIELDS));
    }

    /** POST buildforge/solutionsImport (JSON body: an exported deck). */
    @RequirePOST
    public HttpResponse doSolutionsImport(StaplerRequest req) throws IOException {
        check();
        return relay("/api/v2/solutions/import", pick(readJson(req), "format", "solutions"));
    }

    private static JSONObject readJson(StaplerRequest req) throws IOException {
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = req.getReader()) {
            char[] buf = new char[8192];
            int n;
            while ((n = r.read(buf)) > 0) {
                sb.append(buf, 0, n);
                if (sb.length() > 20 * 1024 * 1024) {
                    throw new IOException("request too large");
                }
            }
        }
        try {
            return JSONObject.fromObject(sb.toString());
        } catch (RuntimeException e) {
            return new JSONObject();
        }
    }

    private static JSONObject pick(JSONObject in, String... keys) {
        JSONObject out = new JSONObject();
        for (String k : keys) {
            if (in.has(k)) {
                out.put(k, in.get(k));
            }
        }
        return out;
    }

    private static HttpResponse relay(String path, JSONObject body) {
        User u = User.current();
        body.put("user", u == null ? "anonymous" : u.getId());
        try {
            ServiceClient.Reply r = ServiceClient.post(path, body, Duration.ofSeconds(30));
            return ServiceClient.json(r.status, r.body);
        } catch (IOException e) {
            return ServiceClient.error(502, "The Build Forge service is unreachable: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ServiceClient.error(503, "interrupted");
        }
    }
}
