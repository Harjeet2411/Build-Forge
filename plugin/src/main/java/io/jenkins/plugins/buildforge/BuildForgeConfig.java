package io.jenkins.plugins.buildforge;

import hudson.Extension;
import hudson.util.FormValidation;
import hudson.util.Secret;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;
import jenkins.model.GlobalConfiguration;
import org.kohsuke.stapler.DataBoundSetter;
import org.kohsuke.stapler.QueryParameter;

/** Manage Jenkins > System > Build Forge. */
@Extension
public class BuildForgeConfig extends GlobalConfiguration {

    private boolean enabled = true;
    private String serviceUrl = "http://127.0.0.1:8791";
    private Secret secret;
    private boolean includeUnstable = false;
    private int maxLogKb = 2048;
    private boolean showOnBuildPage = true;
    private boolean adminForAllUsers = false;
    private String jobInclude = "";
    private String jobExclude = "";

    public BuildForgeConfig() {
        load();
    }

    public static BuildForgeConfig get() {
        return GlobalConfiguration.all().get(BuildForgeConfig.class);
    }

    public boolean isEnabled() { return enabled; }
    @DataBoundSetter public void setEnabled(boolean enabled) { this.enabled = enabled; save(); }

    public String getServiceUrl() { return serviceUrl; }
    @DataBoundSetter public void setServiceUrl(String serviceUrl) {
        this.serviceUrl = serviceUrl == null ? "" : serviceUrl.trim().replaceAll("/+$", "");
        save();
    }

    public Secret getSecret() { return secret; }
    @DataBoundSetter public void setSecret(Secret secret) { this.secret = secret; save(); }

    public boolean isIncludeUnstable() { return includeUnstable; }
    @DataBoundSetter public void setIncludeUnstable(boolean v) { this.includeUnstable = v; save(); }

    public int getMaxLogKb() { return maxLogKb <= 0 ? 2048 : maxLogKb; }
    @DataBoundSetter public void setMaxLogKb(int v) { this.maxLogKb = v; save(); }

    public boolean isShowOnBuildPage() { return showOnBuildPage; }
    @DataBoundSetter public void setShowOnBuildPage(boolean v) { this.showOnBuildPage = v; save(); }

    public boolean isAdminForAllUsers() { return adminForAllUsers; }
    @DataBoundSetter public void setAdminForAllUsers(boolean v) { this.adminForAllUsers = v; save(); }

    public String getJobInclude() { return jobInclude; }
    @DataBoundSetter public void setJobInclude(String v) { this.jobInclude = v == null ? "" : v.trim(); save(); }

    public String getJobExclude() { return jobExclude; }
    @DataBoundSetter public void setJobExclude(String v) { this.jobExclude = v == null ? "" : v.trim(); save(); }

    String plainSecret() {
        return secret == null ? "" : Secret.toString(secret);
    }

    boolean isActive() {
        return enabled && serviceUrl != null && !serviceUrl.isEmpty();
    }

    boolean jobAllowed(String fullName) {
        try {
            if (!jobInclude.isEmpty() && !Pattern.compile(jobInclude).matcher(fullName).find()) {
                return false;
            }
            return jobExclude.isEmpty() || !Pattern.compile(jobExclude).matcher(fullName).find();
        } catch (PatternSyntaxException e) {
            return true;
        }
    }

    public FormValidation doCheckJobInclude(@QueryParameter String value) {
        return checkRegex(value);
    }

    public FormValidation doCheckJobExclude(@QueryParameter String value) {
        return checkRegex(value);
    }

    public FormValidation doCheckServiceUrl(@QueryParameter String value) {
        if (value == null || value.trim().isEmpty()) {
            return FormValidation.warning("Build Forge stays off until this is set.");
        }
        return value.startsWith("http://") || value.startsWith("https://")
                ? FormValidation.ok() : FormValidation.error("Use a full URL, e.g. http://127.0.0.1:8791");
    }

    private static FormValidation checkRegex(String value) {
        try {
            if (value != null && !value.trim().isEmpty()) {
                Pattern.compile(value.trim());
            }
            return FormValidation.ok();
        } catch (PatternSyntaxException e) {
            return FormValidation.error("Not a valid regular expression: " + e.getDescription());
        }
    }
}
