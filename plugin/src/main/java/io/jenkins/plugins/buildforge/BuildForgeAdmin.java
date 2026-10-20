package io.jenkins.plugins.buildforge;

import hudson.Extension;
import hudson.model.RootAction;
import java.io.IOException;
import java.time.Duration;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.HttpResponse;
import org.kohsuke.stapler.QueryParameter;
import org.kohsuke.stapler.StaplerProxy;
import org.kohsuke.stapler.interceptor.RequirePOST;

/**
 * https://your-jenkins/buildforge/admin : every failed build Build Forge analysed, a stats panel (failed, fixed on the next
 * build, rebuilds...), and each incident's log. Jenkins administrators only, unless the System setting opens it to
 * every signed-in user.
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

    /** GET buildforge/stats?days=30 */
    public HttpResponse doStats(@QueryParameter String days) {
        check();
        return ServiceClient.proxy("/api/v2/stats?days=" + num(days, 30));
    }

    /** GET buildforge/incidents?days=&limit=&offset=&q=&category=&outcome= */
    public HttpResponse doIncidents(@QueryParameter String days, @QueryParameter String limit,
                                    @QueryParameter String offset, @QueryParameter String q,
                                    @QueryParameter String category, @QueryParameter String outcome) {
        check();
        String query = "?days=" + num(days, 30)
                + "&limit=" + num(limit, 50)
                + "&offset=" + num(offset, 0)
                + "&q=" + ServiceClient.enc(q)
                + "&category=" + ServiceClient.enc(category)
                + "&outcome=" + ServiceClient.enc(outcome);
        return ServiceClient.proxy("/api/v2/incidents" + query);
    }

    /** GET buildforge/incident?id=BF-... : one incident with its log file. */
    public HttpResponse doIncident(@QueryParameter String id) {
        check();
        return ServiceClient.proxy("/api/v2/incident?id=" + ServiceClient.enc(id));
    }

    /** POST buildforge/reanalyze?id=BF-... : ask the Kiro agent again (after fixing the runner, for example). */
    @RequirePOST
    public HttpResponse doReanalyze(@QueryParameter String id) {
        check();
        JSONObject body = new JSONObject();
        body.put("id", id);
        try {
            ServiceClient.Reply r = ServiceClient.post("/api/v2/reanalyze", body, Duration.ofSeconds(15));
            return ServiceClient.json(r.status, r.body);
        } catch (IOException e) {
            return ServiceClient.error(502, "The Build Forge service is unreachable: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ServiceClient.error(503, "interrupted");
        }
    }

    private static int num(String raw, int dflt) {
        try {
            return raw == null || raw.isEmpty() ? dflt : Math.max(0, Integer.parseInt(raw.trim()));
        } catch (NumberFormatException e) {
            return dflt;
        }
    }
}
