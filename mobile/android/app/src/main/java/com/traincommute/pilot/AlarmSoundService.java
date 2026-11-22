package com.traincommute.pilot;

import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import java.io.IOException;
import java.time.Duration;
import java.time.Instant;

public final class AlarmSoundService extends Service {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private PowerManager.WakeLock wakeLock;
    private Ringtone ringtone;
    private String playingId;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        AndroidAlarm device = new AndroidAlarm(this);
        String requestedId = intent == null ? null : intent.getStringExtra("alarm_id");
        try {
            WakeAlarm.Entry alarm = device.controller().current();
            if (alarm == null || !alarm.ringing()) {
                releasePlayback();
                stopSelf(startId);
                return START_NOT_STICKY;
            }
            if (alarm.id().equals(playingId)) {
                return START_STICKY;
            }
            if (intent != null && !alarm.id().equals(requestedId)) {
                if (playingId != null) {
                    return START_STICKY;
                }
                stopSelf(startId);
                return START_NOT_STICKY;
            }
            requestedId = alarm.id();
            releasePlayback();
            playingId = requestedId;
            long remaining = Duration.between(Instant.now(),
                    alarm.when().plus(WakeAlarm.SOUND_LIMIT)).toMillis();
            if (remaining <= 0) {
                expire(device, alarm.id());
                return START_NOT_STICKY;
            }
            // Bound the audio and CPU wake time even if the wall clock moved backward.
            long playbackMs = Math.min(remaining, WakeAlarm.SOUND_LIMIT.toMillis());
            AndroidAlarm.createChannel(this);
            startForeground(AndroidAlarm.NOTIFICATION_ID,
                    AndroidAlarm.ringingNotification(this, alarm.id(),
                            R.string.alarm_notification_body));
            handler.postDelayed(() -> expire(device, alarm.id()), playbackMs);
            keepAwake(playbackMs);
            if (!playSound()) {
                fail(device, alarm.id(), R.string.alarm_notification_no_sound);
                return START_NOT_STICKY;
            }
            return START_STICKY;
        } catch (IOException | RuntimeException failure) {
            fail(device, requestedId, R.string.alarm_start_failed);
            return START_NOT_STICKY;
        }
    }

    private void keepAwake(long timeoutMs) {
        wakeLock = getSystemService(PowerManager.class)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, getPackageName() + ":wake-alarm");
        wakeLock.acquire(timeoutMs);
    }

    private boolean playSound() {
        try {
            ringtone = RingtoneManager.getRingtone(this,
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM));
            if (ringtone != null) {
                ringtone.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
                ringtone.setLooping(true);
                ringtone.play();
                return true;
            }
        } catch (RuntimeException failure) {
            // The caller reports failure and releases the audio resources.
        }
        return false;
    }

    private void fail(AndroidAlarm device, String id, int message) {
        if (id != null && id.equals(playingId)) {
            expire(device, id);
        } else if (playingId == null) {
            stopSelf();
        }
        try {
            AndroidAlarm.notifyDeliveryFailure(this, message);
        } catch (RuntimeException failure) {
            // Notification access may have changed after the alarm was scheduled.
        }
    }

    private void expire(AndroidAlarm device, String id) {
        if (!id.equals(playingId)) {
            return;
        }
        try {
            device.controller().stop(id);
        } catch (IOException | RuntimeException failure) {
            // Still stop playback at the local time limit.
        }
        releasePlayback();
        AndroidAlarm.changed();
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void releasePlayback() {
        playingId = null;
        handler.removeCallbacksAndMessages(null);
        if (ringtone != null) {
            try {
                ringtone.stop();
            } catch (RuntimeException ignored) {
                // Release the wake lock even when the audio service has stopped.
            }
            ringtone = null;
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        wakeLock = null;
    }

    @Override
    public void onDestroy() {
        releasePlayback();
        super.onDestroy();
    }
}
