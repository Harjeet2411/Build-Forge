package io.jenkins.plugins.buildforge;

import hudson.model.Cause;
import hudson.model.Run;
import hudson.model.User;
import hudson.scm.ChangeLogSet;
import java.lang.reflect.Method;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import jenkins.scm.RunWithSCM;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/**
 * Who to contact about a failed build: who or what started it, and who committed the code it built. Sent to the
 * Build Forge service so the alerts can name the people behind a pipeline that keeps failing. Only names, user ids,
 * e-mail addresses and the first line of commit messages are read; nothing here ever fails a build.
 */
final class BuildContacts {

    private static final Logger LOG = Logger.getLogger(BuildContacts.class.getName());
    private static final int MAX_COMMITS = 20;

    private BuildContacts() {
    }

    /** {kind: user|scm|timer|upstream|remote|other, id, name} */
    static JSONObject triggeredBy(Run<?, ?> run) {
        try {
            Cause.UserIdCause user = run.getCause(Cause.UserIdCause.class);
            if (user != null) {
                JSONObject o = new JSONObject();
                o.put("kind", "user");
                o.put("id", user.getUserId() == null ? "" : user.getUserId());
                o.put("name", user.getUserName() == null ? "" : user.getUserName());
                return o;
            }
            for (Cause c : run.getCauses()) {
                String text = String.valueOf(c.getShortDescription());
                String kind = "other";
                String cn = c.getClass().getName();
                if (cn.contains("SCMTrigger")) {
                    kind = "scm";
                } else if (cn.contains("TimerTrigger")) {
                    kind = "timer";
                } else if (c instanceof Cause.UpstreamCause) {
                    kind = "upstream";
                } else if (c instanceof Cause.RemoteCause) {
                    kind = "remote";
                }
                if (c instanceof BuildForgeAction.RebuildCause) {
                    kind = "rebuild";
                }
                JSONObject o = new JSONObject();
                o.put("kind", kind);
                o.put("name", text);
                return o;
            }
        } catch (RuntimeException e) {
            LOG.log(Level.FINE, "Could not read the causes of " + run, e);
        }
        return null;
    }

    /** One entry per commit in the changes this build checked out, with the Jenkins user it maps to. */
    static JSONArray commits(Run<?, ?> run) {
        JSONArray out = new JSONArray();
        try {
            if (!(run instanceof RunWithSCM)) {
                return out;
            }
            List<? extends ChangeLogSet<? extends ChangeLogSet.Entry>> sets = ((RunWithSCM<?, ?>) run).getChangeSets();
            for (ChangeLogSet<? extends ChangeLogSet.Entry> set : sets) {
                for (ChangeLogSet.Entry entry : set) {
                    if (out.size() >= MAX_COMMITS) {
                        return out;
                    }
                    JSONObject c = new JSONObject();
                    c.put("id", String.valueOf(entry.getCommitId() == null ? "" : entry.getCommitId()));
                    String msg = entry.getMsg() == null ? "" : entry.getMsg().trim();
                    int nl = msg.indexOf('\n');
                    c.put("message", nl > 0 ? msg.substring(0, nl) : msg);
                    User author = entry.getAuthor();
                    if (author != null) {
                        c.put("author", author.getFullName());
                        c.put("author_id", author.getId());
                    }
                    String email = emailOf(entry, author);
                    if (email != null) {
                        c.put("email", email);
                    }
                    out.add(c);
                }
            }
        } catch (RuntimeException | LinkageError e) {
            LOG.log(Level.FINE, "Could not read the changes of " + run, e);
        }
        return out;
    }

    /** The git plugin's own author e-mail first (that is what was committed), else the address Jenkins knows for the user. */
    private static String emailOf(ChangeLogSet.Entry entry, User author) {
        try {
            Method m = entry.getClass().getMethod("getAuthorEmail");
            Object v = m.invoke(entry);
            if (v instanceof String && !((String) v).isBlank()) {
                return ((String) v).trim();
            }
        } catch (ReflectiveOperationException | RuntimeException e) {
            // not a git change set: try the user's property
        }
        if (author == null) {
            return null;
        }
        try {
            ClassLoader cl = Jenkins.get().getPluginManager().uberClassLoader;
            Class<?> prop = Class.forName("hudson.tasks.Mailer$UserProperty", false, cl);
            Object p = author.getProperty(prop.asSubclass(hudson.model.UserProperty.class));
            if (p != null) {
                Object a = prop.getMethod("getAddress").invoke(p);
                if (a instanceof String && !((String) a).isBlank()) {
                    return ((String) a).trim();
                }
            }
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            // the Mailer plugin is not installed or the user has no address
        }
        return null;
    }
}
