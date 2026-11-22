package com.traincommute.pilot;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;

public final class StopFollowReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Uri data = intent.getData();
        if (data != null && data.getLastPathSegment() != null) {
            new TripFollow(context).stop(data.getLastPathSegment());
            AndroidAlarm.changed();
        }
    }
}
