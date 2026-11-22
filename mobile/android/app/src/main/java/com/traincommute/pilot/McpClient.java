package com.traincommute.pilot;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONException;
import org.json.JSONObject;

final class McpClient {
    private static final String PROTOCOL = "2025-11-25";
    private static final int TIMEOUT_MS = 8000;
    private static final int MAX_RESPONSE_BYTES = 256 * 1024;

    record Connection(URI endpoint, String tokenName, String profile, String token) {
        static Connection create(String url, String tokenName, String profile, String token) {
            try {
                URI uri = new URI(url.trim());
                if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                        || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                        || uri.getRawFragment() != null || !"/mcp".equals(uri.getRawPath())
                        || uri.getPort() == 0 || uri.getPort() > 65535) {
                    throw new IllegalArgumentException("Enter a private HTTPS URL ending in /mcp.");
                }
                int port = uri.getPort() == 443 ? -1 : uri.getPort();
                URI normalized = new URI("https", null, uri.getHost().toLowerCase(Locale.ROOT),
                        port, "/mcp", null, null);
                if (!tokenName.trim().matches("[a-zA-Z0-9_-]{1,24}")) {
                    throw new IllegalArgumentException("Enter the registered token name.");
                }
                if (!token.matches("[!-~]{32,128}")) {
                    throw new IllegalArgumentException("Enter the registered bearer token.");
                }
                if (profile.trim().isEmpty()) {
                    throw new IllegalArgumentException("Enter a profile ID or name.");
                }
                return new Connection(normalized, tokenName.trim(), profile.trim(), token);
            } catch (URISyntaxException e) {
                throw new IllegalArgumentException("Enter a private HTTPS URL ending in /mcp.");
            }
        }

        @Override
        public String toString() {
            return "MCP connection";
        }
    }

    static final class Failure extends IOException {
        Failure(String message) {
            super(message);
        }
    }

    interface Transport {
        JSONObject post(Connection connection, String version, JSONObject message)
                throws IOException, JSONException;
    }

    private final Transport transport;

    McpClient() {
        this(McpClient::postJson);
    }

    McpClient(Transport transport) {
        this.transport = transport;
    }

    JourneyStatus getStatus(Connection connection) throws IOException {
        try {
            JSONObject clientInfo = new JSONObject().put("name", "train-commute-android")
                    .put("version", "0.1.0");
            JSONObject init = message("initialize").put("id", 1).put("params", new JSONObject()
                    .put("protocolVersion", PROTOCOL).put("capabilities", new JSONObject())
                    .put("clientInfo", clientInfo));
            JSONObject initialized = result(transport.post(connection, null, init), 1);
            String version = initialized.getString("protocolVersion");
            if (!PROTOCOL.equals(version)) {
                throw new Failure("The MCP server uses an unsupported protocol version.");
            }
            transport.post(connection, version, message("notifications/initialized"));

            JSONObject params = new JSONObject().put("name", "get_status").put("arguments",
                    new JSONObject().put("profile", connection.profile()));
            JSONObject call = message("tools/call").put("id", 2).put("params", params);
            JSONObject status = result(transport.post(connection, version, call), 2);
            if (status.optBoolean("isError")) {
                throw new Failure("The server could not read status. Check the profile.");
            }
            return JourneyStatus.parse(status.getJSONObject("structuredContent"),
                    connection.endpoint().toString());
        } catch (JSONException e) {
            throw new Failure("The server returned an invalid MCP response.");
        }
    }

    private static JSONObject message(String method) throws JSONException {
        return new JSONObject().put("jsonrpc", "2.0").put("method", method);
    }

    private static JSONObject result(JSONObject response, int expectedId)
            throws IOException, JSONException {
        if (response == null || !"2.0".equals(response.optString("jsonrpc"))
                || !Integer.valueOf(expectedId).equals(response.opt("id"))
                || response.has("error")) {
            throw new Failure("The MCP server rejected the status request.");
        }
        return response.getJSONObject("result");
    }

    private static JSONObject postJson(Connection connection, String version, JSONObject message)
            throws IOException, JSONException {
        if (Thread.currentThread().isInterrupted()) {
            throw new InterruptedIOException();
        }
        HttpsURLConnection request = (HttpsURLConnection) connection.endpoint().toURL()
                .openConnection();
        try {
            request.setInstanceFollowRedirects(false);
            request.setConnectTimeout(TIMEOUT_MS);
            request.setReadTimeout(TIMEOUT_MS);
            request.setUseCaches(false);
            request.setRequestMethod("POST");
            request.setRequestProperty("Authorization", "Bearer " + connection.token());
            request.setRequestProperty("Content-Type", "application/json");
            request.setRequestProperty("Accept", "application/json, text/event-stream");
            if (version != null) {
                request.setRequestProperty("MCP-Protocol-Version", version);
            }
            byte[] body = message.toString().getBytes(StandardCharsets.UTF_8);
            request.setDoOutput(true);
            request.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = request.getOutputStream()) {
                output.write(body);
            }

            int code = request.getResponseCode();
            if (300 <= code && code < 400) {
                throw new Failure("The MCP server redirected this request. Check the URL.");
            }
            if (code == 401 || code == 403) {
                throw new Failure("The MCP server refused access. Check the named token.");
            }
            if (message.has("id") ? code != 200 : code != 202) {
                throw new Failure("The MCP server returned HTTP " + code + ".");
            }
            if (!message.has("id")) {
                return null;
            }
            String contentType = request.getContentType();
            if (contentType == null || !"application/json".equalsIgnoreCase(
                    contentType.split(";", 2)[0].trim())) {
                throw new Failure("This pilot requires the companion's JSON MCP response.");
            }
            try (InputStream input = request.getInputStream()) {
                return new JSONObject(readBody(input));
            }
        } finally {
            request.disconnect();
        }
    }

    private static String readBody(InputStream input) throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int read;
        while ((read = input.read(buffer)) != -1) {
            if (body.size() + read > MAX_RESPONSE_BYTES) {
                throw new Failure("The MCP response exceeded the pilot's size limit.");
            }
            body.write(buffer, 0, read);
        }
        return new String(body.toByteArray(), StandardCharsets.UTF_8);
    }
}
