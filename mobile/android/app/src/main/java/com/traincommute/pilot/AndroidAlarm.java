package com.traincommute.pilot;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import org.json.JSONException;
import org.json.JSONObject;

final class AndroidAlarm implements WakeAlarm.Store, WakeAlarm.Scheduler {
    static final String TRIGGER = "com.traincommute.pilot.RING";
    static final String STOP = "com.traincommute.pilot.STOP";
    static final String SNOOZE = "com.traincommute.pilot.SNOOZE";
    static final String CHANNEL = "wake-alarm";
    static final int NOTIFICATION_ID = 1;
    private static final int FAILURE_NOTIFICATION_ID = 3;

    private static Runnable changeListener;

    private final Context context;
    private final AlarmManager manager;
    private final AtomicFile file;

    AndroidAlarm(Context context) {
        this.context = context.getApplicationContext();
        manager = this.context.getSystemService(AlarmManager.class);
        file = new AtomicFile(new File(this.context.getFilesDir(), "wake-alarm.json"));
    }

    WakeAlarm controller() {
        return new WakeAlarm(this, this);
    }

    boolean canSchedule() {
        return Build.VERSION.SDK_INT < 31 || manager.canScheduleExactAlarms();
    }

    @Override
    public void schedule(WakeAlarm.Entry entry) {
        if (!canSchedule()) {
            throw new SecurityException("Exact alarm access is required.");
        }
        manager.setAlarmClock(new AlarmManager.AlarmClockInfo(entry.when().toEpochMilli(),
                openApp(context)), action(context, TRIGGER, entry.id(), 0));
    }

    @Override
    public void cancel(String id) {
        PendingIntent trigger = action(context, TRIGGER, id, PendingIntent.FLAG_NO_CREATE);
        if (trigger != null) {
            manager.cancel(trigger);
            trigger.cancel();
        }
    }

    @Override
    public WakeAlarm.State read() throws IOException {
        try {
            JSONObject saved = new JSONObject(new String(file.readFully(), StandardCharsets.UTF_8));
            JSONObject active = saved.optJSONObject("active");
            WakeAlarm.Entry entry = active == null ? null : new WakeAlarm.Entry(
                    active.getString("id"), Instant.ofEpochMilli(active.getLong("when")),
                    JourneyStatus.Key.fromJson(active.optJSONObject("journey")),
                    active.getBoolean("ringing"));
            return new WakeAlarm.State(entry,
                    saved.isNull("obsolete") ? null : saved.getString("obsolete"));
        } catch (FileNotFoundException missing) {
            if (file.getBaseFile().exists()) {
                throw missing;
            }
            return new WakeAlarm.State(null, null);
        } catch (JSONException | RuntimeException error) {
            throw new IOException("Invalid saved wake alarm.", error);
        }
    }

    @Override
    public void write(WakeAlarm.State state) throws IOException {
        JSONObject serialized = new JSONObject();
        WakeAlarm.Entry current = state.active();
        try {
            if (current != null) {
                JSONObject entry = new JSONObject().put("id", current.id())
                        .put("when", current.when().toEpochMilli())
                        .put("ringing", current.ringing());
                if (current.journey() != null) {
                    entry.put("journey", current.journey().toJson());
                }
                serialized.put("active", entry);
            }
            serialized.put("obsolete", state.obsolete());
        } catch (JSONException error) {
            throw new IOException("Could not encode the wake alarm.", error);
        }
        FileOutputStream output = file.startWrite();
        try {
            output.write(serialized.toString().getBytes(StandardCharsets.UTF_8));
            output.getFD().sync();
        } catch (IOException | RuntimeException error) {
            file.failWrite(output);
            throw error;
        }
        file.finishWrite(output);
        if (!state.equals(read())) {
            throw new IOException("Could not save the wake alarm.");
        }
    }

    static void stopSound(Context context) {
        context.stopService(new Intent(context, AlarmSoundService.class));
        context.getSystemService(NotificationManager.class).cancel(NOTIFICATION_ID);
    }

    static PendingIntent openApp(Context context) {
        Intent app = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(context, 0, app, PendingIntent.FLAG_IMMUTABLE);
    }

    static PendingIntent action(Context context, String action, String id, int flags) {
        Intent intent = new Intent(context, AlarmReceiver.class).setAction(action)
                .setData(new Uri.Builder().scheme("tcc-alarm").authority("local")
                        .appendPath(id).build());
        return PendingIntent.getBroadcast(context, 0, intent,
                flags | PendingIntent.FLAG_IMMUTABLE);
    }

    static void createChannel(Context context) {
        NotificationChannel channel = new NotificationChannel(CHANNEL,
                context.getString(R.string.alarm_channel), NotificationManager.IMPORTANCE_HIGH);
        channel.setSound(null, null);
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        context.getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    static Notification ringingNotification(Context context, String id, int text) {
        return new Notification.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_train)
                .setContentTitle(context.getString(R.string.alarm_notification_title))
                .setContentText(context.getString(text))
                .setStyle(new Notification.BigTextStyle().bigText(context.getString(text)))
                .setCategory(Notification.CATEGORY_ALARM)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setContentIntent(openApp(context))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(null,
                        context.getString(R.string.alarm_stop), action(context, STOP, id, 0)).build())
                .addAction(new Notification.Action.Builder(null,
                        context.getString(R.string.alarm_snooze),
                        action(context, SNOOZE, id, 0)).build())
                .build();
    }

    static void notifyDeliveryFailure(Context context, int text) {
        createChannel(context);
        Notification notice = new Notification.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_train)
                .setContentTitle(context.getString(R.string.alarm_notice_failed))
                .setContentText(context.getString(text))
                .setStyle(new Notification.BigTextStyle().bigText(context.getString(text)))
                .setCategory(Notification.CATEGORY_ALARM)
                .setContentIntent(openApp(context))
                .setTimeoutAfter(WakeAlarm.SOUND_LIMIT.toMillis())
                .setAutoCancel(true)
                .build();
        context.getSystemService(NotificationManager.class).notify(FAILURE_NOTIFICATION_ID, notice);
    }

    static void listen(Runnable listener) {
        changeListener = listener;
    }

    static void changed() {
        if (changeListener != null) {
            changeListener.run();
        }
    }
}
