package com.traincommute.pilot;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import org.junit.Test;

public class WakeAlarmTest {
    private static final Instant NOW = Instant.parse("2026-11-23T06:00:00Z");
    private static final WakeAlarm.Entry BEFORE = new WakeAlarm.Entry(
            "original", NOW.plusSeconds(60), null, false);

    @Test
    public void schedulesTheReplacementBeforeCancelingTheExistingAlarm() throws IOException {
        FakeDevice device = new FakeDevice();
        WakeAlarm.Entry next = alarm(device).replace(NOW.plusSeconds(30), null, NOW);

        assertNotEquals(BEFORE.id(), next.id());
        assertEquals(List.of("schedule:" + next.id(), "cancel:" + BEFORE.id()), device.calls);
        assertEquals(next, device.saved.active());
        assertFalse(alarm(device).trigger(BEFORE.id(), BEFORE.when(), () -> {}));
        assertTrue(alarm(device).trigger(next.id(), next.when(), () -> {}));
    }

    @Test
    public void retainsExistingAlarmWhenSchedulingFails() {
        FakeDevice device = new FakeDevice();
        device.failSchedule = true;
        assertThrows(SecurityException.class,
                () -> alarm(device).replace(NOW.plusSeconds(30), null, NOW));
        assertEquals(BEFORE, device.saved.active());
        assertTrue(device.calls.stream().noneMatch(call -> call.equals("cancel:" + BEFORE.id())));
    }

    @Test
    public void retainsExistingAlarmWhenCommittingReplacementFails() {
        FakeDevice device = new FakeDevice();
        device.failCommit = true;
        assertThrows(IOException.class,
                () -> alarm(device).replace(NOW.plusSeconds(30), null, NOW));
        assertEquals(BEFORE, device.saved.active());
        assertTrue(device.calls.stream().noneMatch(call -> call.equals("cancel:" + BEFORE.id())));
    }

    @Test
    public void restartsWithOriginalAlarmAfterInterruptedReplacement() throws IOException {
        FakeDevice device = new FakeDevice();
        device.saved = new WakeAlarm.State(BEFORE, "uncommitted-candidate");
        alarm(device).restore(NOW);
        assertEquals(List.of("cancel:uncommitted-candidate", "schedule:" + BEFORE.id()), device.calls);
        assertEquals(BEFORE, device.saved.active());
        assertNull(device.saved.obsolete());
    }

    @Test
    public void validatesStopAndPreventsDuplicateRingDeliveryAcrossRestarts() throws IOException {
        FakeDevice device = new FakeDevice();
        assertFalse(alarm(device).stop("wrong"));
        assertFalse(alarm(device).trigger(BEFORE.id(), NOW, () -> {}));
        assertTrue(alarm(device).trigger(BEFORE.id(), BEFORE.when(), () -> {}));
        assertFalse(alarm(device).trigger(BEFORE.id(), BEFORE.when(), () -> {}));
        assertTrue(alarm(device).stop(BEFORE.id()));
        assertNull(alarm(device).current());
    }

    @Test
    public void reportsStorageFailureWithoutStartingSoundAndRetiresTheAlarm() throws IOException {
        FakeDevice device = new FakeDevice();
        device.failRingSave = true;
        assertThrows(IOException.class, () -> alarm(device).trigger(
                BEFORE.id(), BEFORE.when(), () -> { throw new AssertionError("Started sound"); }));
        assertNull(alarm(device).current());
        assertEquals(List.of("cancel:" + BEFORE.id()), device.calls);
    }

    @Test
    public void retiresTheAlarmIfAndroidRejectsSoundStartup() throws IOException {
        FakeDevice device = new FakeDevice();
        assertThrows(IllegalStateException.class, () -> alarm(device).trigger(
                BEFORE.id(), BEFORE.when(), () -> { throw new IllegalStateException(); }));
        assertNull(alarm(device).current());
    }

    @Test
    public void clearsAnUnfinishedRingAfterTheSoundWindowOnRestart() throws IOException {
        FakeDevice device = new FakeDevice();
        assertTrue(alarm(device).trigger(BEFORE.id(), BEFORE.when(), () -> {}));
        assertNull(alarm(device).current(BEFORE.when().plus(WakeAlarm.SOUND_LIMIT),
                () -> device.calls.add("stop sound")));
        assertNull(alarm(device).current());
        assertEquals(List.of("cancel:" + BEFORE.id(), "stop sound"), device.calls);
    }

    @Test
    public void armsOnlyAFutureTimeAndDoesNotRestoreAnOverdueAlarm() throws IOException {
        FakeDevice device = new FakeDevice();
        assertThrows(IllegalArgumentException.class,
                () -> alarm(device).replace(NOW, null, NOW));
        assertTrue(device.calls.isEmpty());
        alarm(device).restore(BEFORE.when().plusSeconds(1));
        assertNull(alarm(device).current());
        assertEquals(List.of("cancel:" + BEFORE.id()), device.calls);
    }

    private static WakeAlarm alarm(FakeDevice device) {
        return new WakeAlarm(device, device);
    }

    private static final class FakeDevice implements WakeAlarm.Store, WakeAlarm.Scheduler {
        WakeAlarm.State saved = new WakeAlarm.State(BEFORE, null);
        final List<String> calls = new ArrayList<>();
        boolean failSchedule;
        boolean failCommit;
        boolean failRingSave;

        @Override
        public WakeAlarm.State read() {
            return saved;
        }

        @Override
        public void write(WakeAlarm.State state) throws IOException {
            if (failRingSave && state.active() != null && state.active().ringing()) {
                throw new IOException("Ring save failed");
            }
            if (failCommit && state.active() != null && !state.active().id().equals(BEFORE.id())) {
                throw new IOException("Commit failed");
            }
            saved = state;
        }

        @Override
        public void schedule(WakeAlarm.Entry entry) {
            calls.add("schedule:" + entry.id());
            if (failSchedule) {
                throw new SecurityException();
            }
        }

        @Override
        public void cancel(String id) {
            calls.add("cancel:" + id);
        }
    }
}
