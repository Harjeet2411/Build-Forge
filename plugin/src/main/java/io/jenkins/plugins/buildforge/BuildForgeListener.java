package io.jenkins.plugins.buildforge;

import hudson.Extension;
import hudson.model.AbstractBuild;
import hudson.model.Computer;
import hudson.model.ParameterValue;
import hudson.model.ParametersAction;
import hudson.model.Result;
import hudson.model.Run;
import hudson.model.TaskListener;
import hudson.model.listeners.RunListener;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/**
 * The global hook: runs for every build of every job (freestyle, pipeline, multibranch) without touching any
 * Jenkinsfile. A failed build gets the Build Forge panel and its log goes to the Build Forge service; every other completed
 * build is reported too, so Build Forge knows when a failure was fixed on the next build.
 */
@Extension
public class BuildForgeListener extends RunListener<Run<?, ?>> {

    private static final Logger LOG = Logger.getLogger(BuildForgeListener.class.getName());

    @Override
    public void onStarted(Run<?, ?> run, TaskListener listener) {
        BuildForgeConfig cfg = BuildForgeConfig.get();
        BuildForgeAction.RebuildCause cause = run.getCause(BuildForgeAction.RebuildCause.class);
        if (cfg == null || !cfg.isActive() || cause == null) {
            return;
        }
        JSONObject event = new JSONObject();
        event.put("job", run.getParent().getFullName());
        event.put("build", cause.getUpstreamBuild());
        event.put("phase", "started");
        event.put("new_build", run.getNumber());
        Computer.threadPoolForRemoting.submit(() -> send("/api/v2/rebuild-event", run.getFullDisplayName(), event));
    }

    @Override
    public void onCompleted(Run<?, ?> run, TaskListener listener) {
        BuildForgeConfig cfg = BuildForgeConfig.get();
        if (cfg == null || !cfg.isActive()) {
            return;
        }
        String job = run.getParent().getFullName();
        if (!cfg.jobAllowed(job)) {
            return;
        }
        Result result = run.getResult();
        boolean failed = result == Result.FAILURE || (cfg.isIncludeUnstable() && result == Result.UNSTABLE);
        JSONObject payload = base(run);
        if (!failed) {
            Computer.threadPoolForRemoting.submit(() -> send("/api/v2/build-result", run.getFullDisplayName(), payload));
            return;
        }
        if (run.getAction(BuildForgeAction.class) == null) {
            run.addAction(new BuildForgeAction());
            try {
                run.save();
            } catch (IOException e) {
                LOG.log(Level.WARNING, "Could not save the Build Forge action on " + run, e);
            }
        }
        try {
            payload.put("log", tail(run.getLog(20000), cfg.getMaxLogKb() * 1024));
        } catch (IOException e) {
            LOG.log(Level.WARNING, "Could not read the log of " + run, e);
        }
        if (run instanceof AbstractBuild) {
            payload.put("node", ((AbstractBuild<?, ?>) run).getBuiltOnStr());
        }
        JSONArray params = new JSONArray();
        ParametersAction pa = run.getAction(ParametersAction.class);
        if (pa != null) {
            for (ParameterValue p : pa.getParameters()) {
                params.add(p.getName());  // names only: values can hold secrets
            }
        }
        payload.put("params", params);
        // Off the build thread: a slow or missing service must never hold up a build.
        Computer.threadPoolForRemoting.submit(() -> send("/api/v2/failure", run.getFullDisplayName(), payload));
    }

    private static JSONObject base(Run<?, ?> run) {
        JSONObject p = new JSONObject();
        p.put("job", run.getParent().getFullName());
        p.put("build", run.getNumber());
        String root = Jenkins.get().getRootUrl();
        p.put("build_url", (root == null ? "/" : root) + run.getUrl());
        p.put("result", String.valueOf(run.getResult()));
        p.put("timestamp", run.getTimeInMillis());
        p.put("duration_ms", run.getDuration() > 0 ? run.getDuration()
                : Math.max(0, System.currentTimeMillis() - run.getStartTimeInMillis()));
        BuildForgeAction.RebuildCause cause = run.getCause(BuildForgeAction.RebuildCause.class);
        if (cause != null) {
            p.put("rebuild_of", cause.getUpstreamBuild());
        }
        return p;
    }

    static String tail(List<String> lines, int maxBytes) {
        StringBuilder sb = new StringBuilder();
        int size = 0;
        int start = lines.size();
        while (start > 0) {
            int len = lines.get(start - 1).getBytes(StandardCharsets.UTF_8).length + 1;
            if (size + len > maxBytes) {
                break;
            }
            size += len;
            start--;
        }
        for (int i = start; i < lines.size(); i++) {
            sb.append(lines.get(i)).append('\n');
        }
        return sb.toString();
    }

    private static void send(String path, String what, JSONObject payload) {
        for (int attempt = 1; attempt <= 3; attempt++) {
            try {
                ServiceClient.Reply r = ServiceClient.post(path, payload, Duration.ofSeconds(30));
                if (r.status < 300) {
                    return;
                }
                LOG.warning("Build Forge service answered " + r.status + " to " + path + " for " + what + ": " + r.body);
                if (r.status < 500) {
                    return;
                }
            } catch (IOException e) {
                LOG.log(Level.WARNING, "Build Forge service unreachable for " + what + " (attempt " + attempt + ")", e);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            try {
                Thread.sleep(5000L * attempt);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }
}
