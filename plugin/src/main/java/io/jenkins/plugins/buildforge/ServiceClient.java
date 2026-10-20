package io.jenkins.plugins.buildforge;

import java.io.IOException;
import java.io.PrintWriter;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.StaplerRequest;
import org.kohsuke.stapler.StaplerResponse;

/** Talks to the Build Forge service (incidents, stats, the link to the Kiro runner). Browsers never call it directly. */
final class ServiceClient {

    static final class Reply {
        final int status;
        final String body;

        Reply(int status, String body) {
            this.status = status;
            this.body = body;
        }

        JSONObject json() {
            try {
                return JSONObject.fromObject(body);
            } catch (RuntimeException e) {
                return new JSONObject();
            }
        }
    }

    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    private ServiceClient() {}

    static Reply post(String path, JSONObject body, Duration timeout) throws IOException, InterruptedException {
        BuildForgeConfig cfg = BuildForgeConfig.get();
        HttpRequest req = HttpRequest.newBuilder(URI.create(cfg.getServiceUrl() + path))
                .timeout(timeout)
                .header("Content-Type", "application/json")
                .header("X-BuildForge-Secret", cfg.plainSecret())
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> rsp = HTTP.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return new Reply(rsp.statusCode(), rsp.body());
    }

    static Reply get(String pathAndQuery) throws IOException, InterruptedException {
        BuildForgeConfig cfg = BuildForgeConfig.get();
        HttpRequest req = HttpRequest.newBuilder(URI.create(cfg.getServiceUrl() + pathAndQuery))
                .timeout(Duration.ofSeconds(20))
                .header("X-BuildForge-Secret", cfg.plainSecret())
                .GET()
                .build();
        HttpResponse<String> rsp = HTTP.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return new Reply(rsp.statusCode(), rsp.body());
    }

    static String enc(String s) {
        return URLEncoder.encode(s == null ? "" : s, StandardCharsets.UTF_8);
    }

    /** GET the service and hand its JSON answer to the browser unchanged (or a JSON error). */
    static org.kohsuke.stapler.HttpResponse proxy(String pathAndQuery) {
        try {
            Reply r = get(pathAndQuery);
            return json(r.status, r.body);
        } catch (IOException e) {
            return error(502, "The Build Forge service is unreachable: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return error(503, "interrupted");
        }
    }

    static org.kohsuke.stapler.HttpResponse error(int status, String message) {
        JSONObject o = new JSONObject();
        o.put("error", message);
        return json(status, o.toString());
    }

    static org.kohsuke.stapler.HttpResponse json(int status, String body) {
        return new org.kohsuke.stapler.HttpResponse() {
            @Override
            public void generateResponse(StaplerRequest req, StaplerResponse rsp, Object node) throws IOException {
                rsp.setStatus(status);
                rsp.setContentType("application/json;charset=UTF-8");
                rsp.setHeader("Cache-Control", "no-store");
                try (PrintWriter w = rsp.getWriter()) {
                    w.write(body == null ? "{}" : body);
                }
            }
        };
    }
}
