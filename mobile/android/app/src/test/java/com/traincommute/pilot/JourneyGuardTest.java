package com.traincommute.pilot;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.time.Instant;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public class JourneyGuardTest {
    private static final Instant NOW = Instant.parse("2026-11-23T06:00:00Z");
    private static final Instant LEAVE = NOW.plusSeconds(600);
    private static final JourneyStatus.Key TRIP = new JourneyStatus.Key(
            "https://train.example/mcp", "alice", "outbound", "2026-11-23");
    private static final WakeAlarm.Entry ALARM = new WakeAlarm.Entry("original",
            NOW.plusSeconds(1200), TRIP, false);

    @Test
    public void offersAnEarlierTimeOnlyForTheSameAlarmJourney() {
        assertEquals(LEAVE, status(TRIP, NOW, NOW, LEAVE, "healthy").earlierTime(ALARM, NOW));
        JourneyStatus.Key otherEndpoint = new JourneyStatus.Key(
                "https://other.example/mcp", "alice", "outbound", "2026-11-23");
        assertNull(status(otherEndpoint, NOW, NOW, LEAVE, "healthy").earlierTime(ALARM, NOW));
        JourneyStatus.Key otherDate = new JourneyStatus.Key(
                "https://train.example/mcp", "alice", "outbound", "2026-11-24");
        assertNull(status(otherDate, NOW, NOW, LEAVE, "healthy").earlierTime(ALARM, NOW));
        WakeAlarm.Entry unlinked = new WakeAlarm.Entry("original", ALARM.when(), null, false);
        assertNull(status(TRIP, NOW, NOW, LEAVE, "healthy").earlierTime(unlinked, NOW));
    }

    @Test
    public void rejectsOldUnknownDegradedAndFutureDatedEvidence() {
        assertNull(status(TRIP, NOW.minusSeconds(300), NOW, LEAVE, "healthy")
                .earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, NOW.minusSeconds(300), LEAVE, "healthy")
                .earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, NOW, LEAVE, "degraded").earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, null, LEAVE, "healthy").earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, NOW.plusSeconds(1), LEAVE, "healthy").earlierTime(ALARM, NOW));
    }

    @Test
    public void rejectsTimesThatArePastOrEqualToOrLaterThanTheAlarm() {
        assertNull(status(TRIP, NOW, NOW, NOW, "healthy").earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, NOW, ALARM.when(), "healthy").earlierTime(ALARM, NOW));
        assertNull(status(TRIP, NOW, NOW, ALARM.when().plusSeconds(1), "healthy")
                .earlierTime(ALARM, NOW));
    }

    @Test
    public void rejectsSkippedAndRidingJourneysEvenWithAValidLeaveBy() {
        assertNull(new JourneyStatus.Journey(TRIP, "locked", true, NOW, "healthy", LEAVE, true, true)
                .earlierTime(NOW, ALARM, NOW));
        assertNull(new JourneyStatus.Journey(TRIP, "riding", false, NOW, "healthy", LEAVE, true, false)
                .earlierTime(NOW, ALARM, NOW));
    }

    @Test
    public void confirmsOnlyTheTimeTheUserReviewedForTheSameAlarm() {
        JourneyStatus.AlarmReview review = new JourneyStatus.AlarmReview(ALARM.id(), TRIP, LEAVE);
        JourneyStatus current = status(TRIP, NOW, NOW, LEAVE, "healthy");
        assertEquals(LEAVE, review.confirmedTime(current, ALARM, NOW));
        assertNull(review.confirmedTime(null, ALARM, NOW));
        assertNull(review.confirmedTime(
                status(TRIP, NOW, NOW, LEAVE.minusSeconds(60), "healthy"), ALARM, NOW));
        assertNull(review.confirmedTime(current,
                new WakeAlarm.Entry("other", ALARM.when(), TRIP, false), NOW));
        assertNull(review.confirmedTime(current, ALARM, NOW.plusSeconds(300)));
    }

    @Test
    public void checksEveryLegAndRequiresTransferEvidenceForActions() throws Exception {
        JSONObject direct = new JSONObject("""
                {"direction":"outbound","service_date":"2026-11-23","state":"locked",
                "not_travelling":false,"timetable_last_ok_at":"2026-11-23T05:59:00Z",
                "timetable_health":"healthy","leave_by":"2026-11-23T06:10:00Z",
                "legs":[{"state":"ok","progress":"pending"}]}
                """);
        JourneyStatus good = parse(direct);
        assertEquals(LEAVE, good.earlierTime(ALARM, NOW));
        assertEquals(NOW.plusSeconds(240), good.find(TRIP).freshUntil(good.asOf(), NOW));

        direct.getJSONArray("legs").put(new JSONObject()
                .put("state", "ok").put("progress", "pending"));
        assertNull(parse(direct).earlierTime(ALARM, NOW));
        direct.put("transfers", new JSONArray().put(new JSONObject().put("state", "broken")));
        JourneyStatus broken = parse(direct);
        assertFalse(broken.find(TRIP).canFollow(broken.asOf(), NOW));
        assertNull(broken.earlierTime(ALARM, NOW));
        direct.getJSONArray("transfers").getJSONObject(0).put("state", "ok");
        JourneyStatus valid = parse(direct);
        assertTrue(valid.find(TRIP).canFollow(valid.asOf(), NOW));
        assertEquals(LEAVE, valid.earlierTime(ALARM, NOW));
        for (String unavailable : new String[] {"cancelled", "cut_short"}) {
            direct.getJSONArray("legs").getJSONObject(1).put("state", unavailable);
            assertNull(parse(direct).earlierTime(ALARM, NOW));
        }
    }

    private static JourneyStatus parse(JSONObject row) throws Exception {
        return JourneyStatus.parse(new JSONObject().put("text", "Trip").put("profile", TRIP.profile())
                .put("as_of", NOW.toString()).put("journeys", new JSONArray().put(row)),
                TRIP.endpoint());
    }

    private static JourneyStatus status(JourneyStatus.Key key, Instant asOf, Instant checked,
            Instant leave, String health) {
        return new JourneyStatus("Trip", asOf, java.util.Collections.singletonList(
                new JourneyStatus.Journey(key, "locked", false, checked, health, leave, true, true)),
                key.endpoint(), key.profile());
    }
}
