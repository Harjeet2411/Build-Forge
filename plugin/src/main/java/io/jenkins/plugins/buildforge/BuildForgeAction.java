package io.jenkins.plugins.buildforge;

import hudson.model.Action;
import hudson.model.Cause;
import hudson.model.CauseAction;
import hudson.model.Computer;
import hudson.model.Item;
import hudson.model.Job;
import hudson.model.ParametersAction;
import hudson.model.Queue;
import hudson.model.Run;
import hudson.model.User;
import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import jenkins.model.ParameterizedJobMixIn;
import jenkins.model.RunAction2;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.HttpResponse;
import org.kohsuke.stapler.interceptor.RequirePOST;

/**
 * Attached only to failed builds: the Build Forge panel on the build page (and its sidebar link). It shows the Kiro
 * agent's analysis under the failed stage and, when the agent says a rebuild with the same parameters can fix it,
 * a Rebuild button. Browser calls go through Jenkins, with Jenkins permissions.
 */
public class BuildForgeAction implements RunAction2 {

    private static final Logger LOG = Logger.getLogger(BuildForgeAction.class.getName());

    private transient Run<?, ?> run;

    @Override
    public void onAttached(Run<?, ?> r) {
        this.run = r;
    }

    @Override
    public void onLoad(Run<?, ?> r) {
        this.run = r;
    }

    public Run<?, ?> getRun() {
        return run;
    }

    @Override
    public String getIconFileName() {
        return BuildForgeConfig.get().isActive() ? "/plugin/buildforge/images/buildforge.svg" : null;
    }

    @Override
    public String getDisplayName() {
        return "Build Forge";
    }

    @Override
    public String getUrlName() {
        return "buildforge";
    }

    public boolean isShowOnBuildPage() {
        BuildForgeConfig cfg = BuildForgeConfig.get();
        return cfg.isActive() && cfg.isShowOnBuildPage();
    }

    public boolean isCanRebuild() {
        return run.getParent().hasPermission(Item.BUILD);
    }

    public boolean isCanSeeAdmin() {
        return BuildForgeAdmin.canSee();
    }

    /** GET buildforge/analysis: the incident of this build (Kiro's analysis, stages, rebuilds), as JSON. */
    public HttpResponse doAnalysis() {
        run.checkPermission(Item.READ);
        return ServiceClient.proxy("/api/v2/analysis?job=" + ServiceClient.enc(run.getParent().getFullName())
                + "&build=" + run.getNumber());
    }

    /**
     * POST buildforge/rebuild: start the job again with the same parameters. Needs Build permission, and only works
     * when the Kiro agent said a rebuild can fix this failure.
     */
    @RequirePOST
    public HttpResponse doRebuild() {
        Job<?, ?> job = run.getParent();
        job.checkPermission(Item.BUILD);
        if (!(job instanceof Queue.Task)) {
            return ServiceClient.error(400, "This kind of job can't be rebuilt from here.");
        }
        JSONObject incident;
        try {
            ServiceClient.Reply r = ServiceClient.get("/api/v2/analysis?job=" + ServiceClient.enc(job.getFullName())
                    + "&build=" + run.getNumber());
            incident = r.json();
        } catch (IOException e) {
            return ServiceClient.error(502, "The Build Forge service is unreachable: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ServiceClient.error(503, "interrupted");
        }
        JSONObject analysis = incident.optJSONObject("analysis");
        if (analysis == null || analysis.isNullObject() || !analysis.optBoolean("rebuild_can_fix", false)) {
            return ServiceClient.error(409, "Build Forge did not find this failure fixable by a rebuild.");
        }
        List<Action> actions = new ArrayList<>();
        actions.add(new CauseAction(new Cause.UserIdCause(), new RebuildCause(run, incident.optString("id", ""))));
        ParametersAction params = run.getAction(ParametersAction.class);
        if (params != null) {
            actions.add(new ParametersAction(params.getParameters()));
        }
        Queue.Item item = ParameterizedJobMixIn.scheduleBuild2(job, 0, actions.toArray(new Action[0]));
        if (item == null) {
            return ServiceClient.error(409, "Jenkins did not queue the build (the job may be disabled or already queued).");
        }
        JSONObject event = new JSONObject();
        event.put("job", job.getFullName());
        event.put("build", run.getNumber());
        event.put("phase", "requested");
        event.put("user", currentUser());
        Computer.threadPoolForRemoting.submit(() -> {
            try {
                ServiceClient.post("/api/v2/rebuild-event", event, Duration.ofSeconds(15));
            } catch (Exception e) {
                LOG.log(Level.FINE, "could not record the rebuild request", e);
            }
        });
        JSONObject ok = new JSONObject();
        ok.put("ok", true);
        ok.put("message", "Rebuild queued with the same parameters. It will appear in the build history in a moment.");
        ok.put("queue", Jenkins.get().getRootUrl() == null ? "" : Jenkins.get().getRootUrl() + item.getUrl());
        return ServiceClient.json(200, ok.toString());
    }

    private static String currentUser() {
        User u = User.current();
        return u == null ? "anonymous" : u.getId();
    }

    /** Shows "Rebuilt by Build Forge from #N (entry BF-...)" on the new build. */
    public static class RebuildCause extends Cause {
        private final String upstreamUrl;
        private final int upstreamBuild;
        private final String incidentId;

        public RebuildCause(Run<?, ?> from, String incidentId) {
            this.upstreamUrl = from.getUrl();
            this.upstreamBuild = from.getNumber();
            this.incidentId = incidentId;
        }

        public String getUpstreamUrl() {
            return upstreamUrl;
        }

        public int getUpstreamBuild() {
            return upstreamBuild;
        }

        public String getIncidentId() {
            return incidentId;
        }

        @Override
        public String getShortDescription() {
            return "Rebuilt by Build Forge from #" + upstreamBuild
                    + (incidentId == null || incidentId.isEmpty() ? "" : " (entry " + incidentId + ")");
        }
    }
}
