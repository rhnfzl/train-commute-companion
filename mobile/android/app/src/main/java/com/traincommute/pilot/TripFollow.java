package com.traincommute.pilot;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import org.json.JSONException;
import org.json.JSONObject;

final class TripFollow {
    static final String CHANNEL = "followed-trip";
    private static final int NOTIFICATION_ID = 2;
    private static final Duration FOLLOW_LIMIT = Duration.ofHours(4);

    record Active(String id, JourneyStatus.Key journey, Instant until) {}

    private final Context context;
    private final SharedPreferences settings;
    private final NotificationManager manager;

    TripFollow(Context context) {
        this.context = context;
        settings = context.getSharedPreferences("trip-follow", Context.MODE_PRIVATE);
        manager = context.getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(CHANNEL,
                context.getString(R.string.trip_channel), NotificationManager.IMPORTANCE_LOW);
        channel.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
        manager.createNotificationChannel(channel);
    }

    static boolean allowed(Context context, String channelId) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(
                Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        NotificationChannel channel = manager.getNotificationChannel(channelId);
        return manager.areNotificationsEnabled() && channel != null
                && channel.getImportance() != NotificationManager.IMPORTANCE_NONE;
    }

    Active active(Instant now) {
        Active current = read();
        if (current != null && !current.until().isAfter(now)) {
            stop(current.id());
            return null;
        }
        return current;
    }

    boolean start(JourneyStatus.Journey journey, Instant asOf, Instant now) {
        if (!journey.canFollow(asOf, now)) {
            return false;
        }
        Active next = new Active(UUID.randomUUID().toString(), journey.key(),
                now.plus(FOLLOW_LIMIT));
        try {
            JSONObject json = new JSONObject().put("id", next.id())
                    .put("journey", next.journey().toJson()).put("until", next.until().toString());
            if (!settings.edit().putString("active", json.toString()).commit()) {
                return false;
            }
            show(next, journey, asOf, now);
            return true;
        } catch (JSONException | RuntimeException failure) {
            stop(next.id());
            return false;
        }
    }

    boolean update(JourneyStatus snapshot, Instant now) {
        try {
            Active current = active(now);
            if (current == null || !snapshot.fresh(now) || !allowed(context, CHANNEL)) {
                return true;
            }
            JourneyStatus.Journey journey = snapshot.find(current.journey());
            if (journey != null && journey.canFollow(snapshot.asOf(), now)) {
                show(current, journey, snapshot.asOf(), now);
            } else if (journey != null) {
                manager.cancel(NOTIFICATION_ID);
            } else if (current.journey().endpoint().equals(snapshot.endpoint())
                    && current.journey().profile().equals(snapshot.profile())) {
                stop(current.id());
            }
            return true;
        } catch (RuntimeException error) {
            return false;
        }
    }

    void stop(String expectedId) {
        Active current = read();
        if (current != null && current.id().equals(expectedId)) {
            settings.edit().remove("active").commit();
            manager.cancel(NOTIFICATION_ID);
        }
    }

    private Active read() {
        String serialized = settings.getString("active", null);
        if (serialized == null) {
            return null;
        }
        try {
            JSONObject json = new JSONObject(serialized);
            return new Active(json.getString("id"),
                    JourneyStatus.Key.fromJson(json.getJSONObject("journey")),
                    Instant.parse(json.getString("until")));
        } catch (JSONException | RuntimeException failure) {
            return null;
        }
    }

    private void show(Active current, JourneyStatus.Journey journey, Instant asOf, Instant now) {
        Instant expires = journey.freshUntil(asOf, now);
        if (expires == null || !allowed(context, CHANNEL)) {
            return;
        }
        Instant until = expires.isBefore(current.until()) ? expires : current.until();
        Intent action = new Intent(context, StopFollowReceiver.class)
                .setData(Uri.parse("tcc-follow://local/" + current.id()));
        PendingIntent stop = PendingIntent.getBroadcast(context, 0, action,
                PendingIntent.FLAG_IMMUTABLE);

        Notification publicNotice = new Notification.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_train)
                .setContentTitle(context.getString(R.string.trip_channel))
                .setContentText(context.getString(R.string.trip_private))
                .setContentIntent(AndroidAlarm.openApp(context))
                .build();

        Instant leaveBy = journey.leaveBy();
        boolean showLeaveBy = leaveBy != null && leaveBy.isAfter(now);
        if (showLeaveBy && leaveBy.isBefore(until)) {
            until = leaveBy;
        }
        String leave = showLeaveBy
                ? context.getString(R.string.leave_by, PhoneTime.format(context, leaveBy))
                : context.getString(R.string.leave_by_unavailable);
        String text = context.getString(R.string.trip_content,
                context.getString(R.string.timetable, journey.health(),
                        PhoneTime.format(context, journey.checked())),
                leave, PhoneTime.format(context, asOf));
        manager.notify(NOTIFICATION_ID, new Notification.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_train)
                .setContentTitle(context.getString(R.string.trip_title, journey.key().label()))
                .setContentText(leave)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setCategory(Notification.CATEGORY_STATUS)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setPublicVersion(publicNotice)
                .setContentIntent(AndroidAlarm.openApp(context))
                .setDeleteIntent(stop)
                .addAction(new Notification.Action.Builder(null,
                        context.getString(R.string.stop_follow), stop).build())
                .setOnlyAlertOnce(true)
                .setOngoing(true)
                .setWhen(asOf.toEpochMilli())
                .setTimeoutAfter(Math.max(1, Duration.between(now, until).toMillis()))
                .build());
    }
}
