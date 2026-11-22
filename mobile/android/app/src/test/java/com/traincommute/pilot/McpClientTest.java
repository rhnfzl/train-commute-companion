package com.traincommute.pilot;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONObject;
import org.junit.Test;

public class McpClientTest {
    private static final String ENDPOINT = "https://train.example/mcp";

    @Test
    public void handshakesAndCallsOnlyReadOnlyStatusWithTheProvidedProfile() throws Exception {
        McpClient.Connection configuration = McpClient.Connection.create(
                ENDPOINT, "android-pilot", "my-profile", "a".repeat(32));
        List<String> methods = new ArrayList<>();
        JourneyStatus snapshot = new McpClient((connection, version, message) -> {
            assertEquals(configuration, connection);
            String method = message.getString("method");
            methods.add(method);
            return switch (method) {
                case "initialize" -> {
                    assertNull(version);
                    yield response(1, new JSONObject().put("protocolVersion", "2025-11-25"));
                }
                case "notifications/initialized" -> {
                    assertEquals("2025-11-25", version);
                    yield null;
                }
                case "tools/call" -> {
                    assertEquals("2025-11-25", version);
                    JSONObject params = message.getJSONObject("params");
                    assertEquals("get_status", params.getString("name"));
                    assertEquals("my-profile", params.getJSONObject("arguments")
                            .getString("profile"));
                    yield response(2, new JSONObject().put("structuredContent",
                            new JSONObject("""
                            {"profile":"actual-id","as_of":"2026-11-23T07:00:00+01:00",
                            "journeys":[{"direction":"outbound","service_date":"2026-11-23",
                            "state":"locked","not_travelling":false,
                            "timetable_last_ok_at":"2026-11-23T06:58:00+01:00",
                            "timetable_health":"healthy","leave_by":"2026-11-23T07:30:00+01:00"}],
                            "text":"Data: NS"}
                            """)));
                }
                default -> throw new AssertionError("Unexpected method " + method);
            };
        }).getStatus(configuration);

        assertEquals(List.of("initialize", "notifications/initialized", "tools/call"), methods);
        assertEquals("Data: NS", snapshot.text());
        assertEquals(Instant.parse("2026-11-23T06:00:00Z"), snapshot.asOf());
        assertEquals("actual-id", snapshot.journeys().get(0).key().profile());
        assertEquals(Instant.parse("2026-11-23T06:30:00Z"), snapshot.journeys().get(0).leaveBy());
    }

    @Test
    public void rejectsCleartextAmbiguousUrisAndUnsafeHeaderValues() {
        for (String endpoint : List.of("http://train.example/mcp", "https://id@train.example/mcp",
                "https://train.example/mcp?token=secret", "https://train.example/mcp#x",
                "https://train.example/other", "https://train.example/%6Dcp")) {
            assertThrows(IllegalArgumentException.class, () -> McpClient.Connection.create(
                    endpoint, "android-pilot", "profile", "a".repeat(32)));
        }
        assertThrows(IllegalArgumentException.class, () -> McpClient.Connection.create(
                ENDPOINT, "android-pilot", "profile", "a".repeat(32) + "\r\n"));
        assertThrows(IllegalArgumentException.class, () -> McpClient.Connection.create(
                ENDPOINT, "android-pilot", "profile", "a".repeat(31)));
    }

    @Test
    public void stopsOnAnUnsupportedProtocolWithoutMakingAStatusCall() {
        List<String> methods = new ArrayList<>();
        McpClient client = new McpClient((connection, version, message) -> {
            methods.add(message.getString("method"));
            return response(1, new JSONObject().put("protocolVersion", "unsupported"));
        });

        assertThrows(IOException.class, () -> client.getStatus(McpClient.Connection.create(
                ENDPOINT, "android-pilot", "profile", "a".repeat(32))));
        assertEquals(List.of("initialize"), methods);
    }

    @Test
    public void rejectsMcpToolErrorWithoutDisplayingRemoteErrorText() {
        McpClient client = new McpClient((connection, version, message) -> {
            if (!message.has("id")) {
                return null;
            }
            if (version == null) {
                return response(1, new JSONObject().put("protocolVersion", "2025-11-25"));
            }
            return response(2, new JSONObject().put("isError", true)
                    .put("structuredContent", new JSONObject().put("text", "private server error")));
        });
        IOException failure = assertThrows(IOException.class, () -> client.getStatus(
                McpClient.Connection.create(ENDPOINT, "android-pilot", "profile", "a".repeat(32))));
        assertTrue(!failure.getMessage().contains("private server error"));
    }

    private static JSONObject response(int id, JSONObject result) throws org.json.JSONException {
        return new JSONObject().put("jsonrpc", "2.0").put("id", id).put("result", result);
    }
}
