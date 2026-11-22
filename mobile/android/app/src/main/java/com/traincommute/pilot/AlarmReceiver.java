package com.traincommute.pilot;

import android.app.AlarmManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import java.io.IOException;
import java.time.Instant;

public final class AlarmReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        AndroidAlarm device = new AndroidAlarm(context);
        String action = intent.getAction();
        try {
            if (Intent.ACTION_BOOT_COMPLETED.equals(action)
                    || AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED
                            .equals(action)) {
                if (device.canSchedule()) {
                    device.controller().restore(Instant.now());
                    AndroidAlarm.changed();
                }
                return;
            }
            Uri data = intent.getData();
            if (data == null || data.getLastPathSegment() == null) {
                return;
            }
            String id = data.getLastPathSegment();
            if (AndroidAlarm.TRIGGER.equals(action)) {
                Intent ring = new Intent(context, AlarmSoundService.class).putExtra("alarm_id", id);
                try {
                    device.controller().trigger(id, Instant.now(),
                            () -> context.startForegroundService(ring));
                } catch (IOException | RuntimeException failure) {
                    AndroidAlarm.notifyDeliveryFailure(context, R.string.alarm_start_failed);
                }
            } else if (AndroidAlarm.STOP.equals(action)) {
                stop(context, device, id);
            } else if (AndroidAlarm.SNOOZE.equals(action)) {
                snooze(context, device, id);
            }
            AndroidAlarm.changed();
        } catch (IOException | RuntimeException failure) {
            AndroidAlarm.changed();
        }
    }

    static void stop(Context context, AndroidAlarm device, String id) throws IOException {
        if (device.controller().stop(id)) {
            AndroidAlarm.stopSound(context);
        }
    }

    static void snooze(Context context, AndroidAlarm device, String id) throws IOException {
        WakeAlarm.Entry alarm = device.controller().current();
        if (alarm != null && alarm.ringing() && alarm.id().equals(id)) {
            Instant now = Instant.now();
            device.controller().replace(now.plusSeconds(300), alarm.journey(), now);
            AndroidAlarm.stopSound(context);
        }
    }
}
