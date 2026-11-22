package com.traincommute.pilot;

import java.io.IOException;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

final class WakeAlarm {
    static final Duration SOUND_LIMIT = Duration.ofMinutes(5);

    record Entry(String id, Instant when, JourneyStatus.Key journey, boolean ringing) {}
    record State(Entry active, String obsolete) {}

    interface Store {
        State read() throws IOException;

        void write(State state) throws IOException;
    }

    interface Scheduler {
        void schedule(Entry entry);

        void cancel(String id);
    }

    private final Store store;
    private final Scheduler scheduler;

    WakeAlarm(Store store, Scheduler scheduler) {
        this.store = store;
        this.scheduler = scheduler;
    }

    Entry current() throws IOException {
        return store.read().active();
    }

    Entry current(Instant now, Runnable stopSound) throws IOException {
        Entry active = current();
        if (active != null && !now.isBefore(active.when().plus(SOUND_LIMIT))) {
            try {
                stop(active.id());
            } finally {
                stopSound.run();
            }
            return null;
        }
        return active;
    }

    Entry replace(Instant when, JourneyStatus.Key journey, Instant now) throws IOException {
        Instant trigger = Instant.ofEpochMilli(when.toEpochMilli());
        if (!trigger.isAfter(now)) {
            throw new IllegalArgumentException("Choose a future alarm time.");
        }
        Entry previous = recover().active();
        Entry next = new Entry(UUID.randomUUID().toString(), trigger, journey, false);
        // Keep the old alarm authoritative until the new schedule and its identity are saved.
        store.write(new State(previous, next.id()));
        try {
            scheduler.schedule(next);
            store.write(new State(next, previous == null ? null : previous.id()));
        } catch (IOException | RuntimeException failure) {
            discardObsolete();
            throw failure;
        }
        discardObsolete();
        return next;
    }

    boolean stop(String expectedId) throws IOException {
        Entry active = recover().active();
        if (active == null || !active.id().equals(expectedId)) {
            return false;
        }
        store.write(new State(null, active.id()));
        discardObsolete();
        return true;
    }

    boolean trigger(String id, Instant now, Runnable startSound) throws IOException {
        State saved = store.read();
        Entry active = saved.active();
        if (active == null || active.ringing() || !active.id().equals(id)
                || now.isBefore(active.when())) {
            return false;
        }
        if (!now.isBefore(active.when().plus(SOUND_LIMIT))) {
            stop(id);
            return false;
        }
        try {
            store.write(new State(new Entry(id, active.when(), active.journey(), true),
                    saved.obsolete()));
            startSound.run();
        } catch (IOException | RuntimeException failure) {
            try {
                stop(id);
            } catch (IOException | RuntimeException cleanupFailure) {
                failure.addSuppressed(cleanupFailure);
            }
            throw failure;
        }
        return true;
    }

    State recover() throws IOException {
        State saved = store.read();
        if (saved.obsolete() != null) {
            scheduler.cancel(saved.obsolete());
            saved = new State(saved.active(), null);
            store.write(saved);
        }
        return saved;
    }

    void restore(Instant now) throws IOException {
        Entry active = recover().active();
        if (active == null) {
            return;
        }
        if (!active.ringing() && active.when().isAfter(now)) {
            scheduler.schedule(active);
        } else {
            stop(active.id());
        }
    }

    private void discardObsolete() {
        try {
            recover();
        } catch (IOException | RuntimeException ignored) {
            // Keep the cleanup record for the next app launch. Delivery checks the active identity.
        }
    }
}
