package com.traincommute.pilot;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

record JourneyStatus(String text, Instant asOf, List<Journey> journeys,
        String endpoint, String profile) {
    private static final Duration FRESH_FOR = Duration.ofMinutes(5);

    record Key(String endpoint, String profile, String direction, String serviceDate) {
        JSONObject toJson() throws JSONException {
            return new JSONObject().put("endpoint", endpoint).put("profile", profile)
                    .put("direction", direction).put("service_date", serviceDate);
        }

        static Key fromJson(JSONObject object) throws JSONException {
            if (object == null) {
                return null;
            }
            return new Key(object.getString("endpoint"), object.getString("profile"),
                    object.getString("direction"), object.getString("service_date"));
        }

        String label() {
            return ("outbound".equals(direction) ? "Outbound" : "Return") + " · " + serviceDate;
        }
    }

    record AlarmReview(String alarmId, Key journey, Instant when) {
        Instant confirmedTime(JourneyStatus status, WakeAlarm.Entry alarm, Instant now) {
            if (status != null && alarm != null && alarmId.equals(alarm.id())
                    && journey.equals(alarm.journey()) && when.equals(status.earlierTime(alarm, now))) {
                return when;
            }
            return null;
        }
    }

    record Journey(Key key, String state, boolean unreadableOrSkipped, Instant checked,
            String health, Instant leaveBy, boolean usable, boolean awaitingDeparture) {
        boolean canFollow(Instant asOf, Instant now) {
            return !unreadableOrSkipped && usable && freshUntil(asOf, now) != null
                    && ("locked".equals(state) || "riding".equals(state));
        }

        Instant freshUntil(Instant asOf, Instant now) {
            if (!"healthy".equals(health) || !recentlyChecked(asOf, now)
                    || !recentlyChecked(checked, asOf) || !recentlyChecked(checked, now)) {
                return null;
            }
            return checked.plus(FRESH_FOR);
        }

        Instant earlierTime(Instant asOf, WakeAlarm.Entry alarm, Instant now) {
            if (alarm == null || alarm.ringing() || !key.equals(alarm.journey())
                    || unreadableOrSkipped || !"locked".equals(state)
                    || !usable || !awaitingDeparture || freshUntil(asOf, now) == null
                    || leaveBy == null
                    || !leaveBy.isAfter(now) || !leaveBy.isBefore(alarm.when())) {
                return null;
            }
            return leaveBy;
        }
    }

    boolean fresh(Instant now) {
        return recentlyChecked(asOf, now);
    }

    Journey find(Key key) {
        Journey match = null;
        for (Journey journey : journeys) {
            if (journey.key().equals(key)) {
                if (match != null) {
                    return null;
                }
                match = journey;
            }
        }
        return match;
    }

    Instant earlierTime(WakeAlarm.Entry alarm, Instant now) {
        Journey journey = alarm == null ? null : find(alarm.journey());
        return journey == null ? null : journey.earlierTime(asOf, alarm, now);
    }

    static JourneyStatus parse(JSONObject data, String endpoint) throws JSONException {
        String text = requiredText(data, "text");
        String profile = requiredText(data, "profile");
        JSONArray rows = data.getJSONArray("journeys");
        List<Journey> journeys = new ArrayList<>();
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = rows.getJSONObject(i);
            String direction = requiredText(row, "direction");
            if (!"outbound".equals(direction) && !"return".equals(direction)) {
                continue;
            }
            String date = requiredText(row, "service_date");
            try {
                LocalDate.parse(date);
            } catch (DateTimeParseException e) {
                continue;
            }
            JSONArray legs = row.optJSONArray("legs");
            JSONArray transfers = row.optJSONArray("transfers");
            journeys.add(new Journey(new Key(endpoint, profile, direction, date),
                    row.optString("state", "unknown"),
                    !Boolean.FALSE.equals(row.opt("not_travelling")),
                    readInstant(row, "timetable_last_ok_at"),
                    row.optString("timetable_health", "unknown"), readInstant(row, "leave_by"),
                    !"no_train".equals(row.optString("arrival_target_status"))
                            && usableRoute(legs, transfers),
                    allMatch(legs, "progress", List.of("pending", "boarding"))));
        }
        return new JourneyStatus(text, readInstant(data, "as_of"),
                Collections.unmodifiableList(journeys), endpoint, profile);
    }

    private static boolean usableRoute(JSONArray legs, JSONArray transfers) {
        if (legs == null || legs.length() == 0
                || !allMatch(legs, "state", List.of("ok", "delayed"))
                || !allMatch(legs, "progress", List.of("pending", "boarding", "riding", "done"))) {
            return false;
        }
        return transfers == null ? legs.length() == 1
                : transfers.length() == legs.length() - 1
                        && allMatch(transfers, "state", List.of("ok"));
    }

    private static boolean allMatch(JSONArray rows, String key, List<String> allowed) {
        if (rows == null) {
            return false;
        }
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = rows.optJSONObject(i);
            if (row == null || !allowed.contains(row.optString(key))) {
                return false;
            }
        }
        return true;
    }

    private static Instant readInstant(JSONObject object, String key) {
        try {
            return Instant.parse(object.optString(key));
        } catch (DateTimeParseException e) {
            return null;
        }
    }

    private static String requiredText(JSONObject object, String key) throws JSONException {
        Object value = object.get(key);
        if (!(value instanceof String) || ((String) value).trim().isEmpty()) {
            throw new JSONException("Expected status text");
        }
        return (String) value;
    }

    private static boolean recentlyChecked(Instant checked, Instant now) {
        return checked != null && now != null && !checked.isAfter(now)
                && Duration.between(checked, now).compareTo(FRESH_FOR) < 0;
    }
}
