package com.traincommute.pilot;

import android.content.Context;
import android.text.format.DateFormat;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Locale;

final class PhoneTime {
    private PhoneTime() {}

    static String format(Context context, Instant value) {
        if (value == null) {
            return context.getString(R.string.unknown);
        }
        Locale locale = context.getResources().getConfiguration().getLocales().get(0);
        String hour = DateFormat.is24HourFormat(context) ? "Hm" : "hm";
        String pattern = DateFormat.getBestDateTimePattern(locale, "EEE MMM d " + hour);
        return DateTimeFormatter.ofPattern(pattern, locale)
                .withZone(ZoneId.systemDefault()).format(value);
    }
}
