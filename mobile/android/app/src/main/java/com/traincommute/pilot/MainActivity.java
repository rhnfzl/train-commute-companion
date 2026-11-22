package com.traincommute.pilot;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DatePickerDialog;
import android.app.TimePickerDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.text.format.DateFormat;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.IOException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

public final class MainActivity extends Activity {
    private static final int NOTIFICATION_REQUEST = 1;
    private static final long REFRESH_INTERVAL_MS = 60_000;
    private static final int BACKGROUND = Color.rgb(239, 244, 242);
    private static final int TEXT = Color.rgb(20, 39, 35);
    private static final int MUTED = Color.rgb(66, 89, 83);

    // Secret and connection live here only until the Android process exits.
    private static McpClient.Connection connection;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final McpClient client = new McpClient();
    private final Runnable periodicRefresh = this::refreshNormally;

    private AndroidAlarm device;
    private TripFollow follow;
    private SharedPreferences settings;
    private boolean visible;
    private boolean editingFields;
    private boolean statusUsable;
    private int requestSequence;
    private Future<?> request;
    private JourneyStatus status;
    private EditText endpoint;
    private EditText tokenName;
    private EditText profile;
    private EditText token;
    private TextView syncMessage;
    private TextView alarmMessage;
    private LinearLayout alarmControls;
    private LinearLayout statusContent;
    private Button refreshButton;
    private Button disconnectButton;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        device = new AndroidAlarm(this);
        AndroidAlarm.createChannel(this);
        follow = new TripFollow(this);
        settings = getSharedPreferences("connection-settings", MODE_PRIVATE);
        buildScreen();
        try {
            device.controller().recover();
        } catch (IOException | RuntimeException error) {
            alarmMessage.setText(R.string.alarm_unreadable);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        AndroidAlarm.listen(this::renderState);
        renderState();
        refreshNormally();
    }

    @Override
    protected void onPause() {
        visible = false;
        AndroidAlarm.listen(null);
        abandonRefresh();
        statusUsable = false;
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        network.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
            int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTIFICATION_REQUEST) {
            renderState();
        }
    }

    private void buildScreen() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(BACKGROUND);
        scroll.setClipToPadding(false);
        if (Build.VERSION.SDK_INT >= 30) {
            scroll.setOnApplyWindowInsetsListener((view, window) -> {
                Insets insets = window.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                view.setPadding(insets.left, insets.top, insets.right, insets.bottom);
                return window;
            });
        }
        LinearLayout page = column();
        page.setPadding(dp(18), dp(18), dp(18), dp(20));
        scroll.addView(page);
        heading(page, R.string.app_name, 30);

        LinearLayout alarm = card(page);
        heading(alarm, R.string.wake_alarm, 22);
        text(alarm, getString(R.string.alarm_time_zone), 14, true);
        alarmMessage = text(alarm, "", 14, false);
        alarmMessage.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        alarmControls = column();
        alarm.addView(alarmControls);

        LinearLayout conn = card(page);
        heading(conn, R.string.connection, 22);
        text(conn, getString(R.string.connection_hint), 14, true);
        endpoint = field(conn, R.string.endpoint, InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_URI, "endpoint");
        tokenName = field(conn, R.string.token_name, InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS, "token_name");
        profile = field(conn, R.string.profile, InputType.TYPE_CLASS_TEXT, "profile");
        token = field(conn, R.string.token, InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_PASSWORD, null);
        token.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        token.setImeOptions(EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
                | EditorInfo.IME_ACTION_DONE);
        token.setSelectAllOnFocus(false);
        if (connection != null) {
            token.setHint(R.string.token_in_memory);
        }
        button(conn, R.string.connect, this::connect);
        disconnectButton = button(conn, R.string.disconnect, this::disconnect);
        syncMessage = text(conn, getString(R.string.connect_prompt), 14, true);
        syncMessage.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);

        LinearLayout statusCard = card(page);
        heading(statusCard, R.string.status_heading, 22);
        text(statusCard, getString(R.string.foreground_hint), 14, true);
        refreshButton = button(statusCard, R.string.refresh, this::refreshNormally);
        statusContent = column();
        statusCard.addView(statusContent);

        LinearLayout permissions = card(page);
        heading(permissions, R.string.device_permissions, 22);
        text(permissions, getString(R.string.device_permissions_hint), 14, true);
        button(permissions, R.string.grant_notifications, this::requestNotifications);
        if (Build.VERSION.SDK_INT >= 31) {
            button(permissions, R.string.grant_exact, this::requestExact);
        }
        button(permissions, R.string.open_settings, this::openAppSettings);

        for (EditText input : new EditText[] {endpoint, tokenName, profile, token}) {
            input.addTextChangedListener(new TextWatcher() {
                @Override
                public void beforeTextChanged(CharSequence text, int start, int count, int after) {}

                @Override
                public void onTextChanged(CharSequence text, int start, int before, int count) {}

                @Override
                public void afterTextChanged(Editable text) {
                    if (!editingFields && connection != null) {
                        disconnect();
                        syncMessage.setText(R.string.connection_edited);
                    }
                }
            });
        }
        page.setFocusableInTouchMode(true);
        page.requestFocus();
        setContentView(scroll);
    }

    private void connect() {
        String userToken = token.getText().toString();
        if (userToken.isEmpty() && connection != null) {
            refreshNormally();
            return;
        }
        McpClient.Connection next;
        try {
            next = McpClient.Connection.create(endpoint.getText().toString(),
                    tokenName.getText().toString(), profile.getText().toString(), userToken);
        } catch (IllegalArgumentException error) {
            syncMessage.setText(error.getMessage());
            return;
        }
        abandonRefresh();
        connection = next;
        settings.edit().putString("endpoint", next.endpoint().toString())
                .putString("token_name", next.tokenName()).putString("profile", next.profile()).apply();
        status = null;
        setTokenHint(R.string.token_in_memory);
        ((android.view.inputmethod.InputMethodManager) getSystemService(INPUT_METHOD_SERVICE))
                .hideSoftInputFromWindow(token.getWindowToken(), 0);
        refreshNormally();
    }

    private void disconnect() {
        abandonRefresh();
        connection = null;
        status = null;
        statusUsable = false;
        token.setHint(R.string.token);
        syncMessage.setText(R.string.connect_prompt);
        renderState();
    }

    private void setTokenHint(int hint) {
        editingFields = true;
        token.setText("");
        token.setHint(hint);
        editingFields = false;
    }

    private void abandonRefresh() {
        requestSequence++;
        main.removeCallbacks(periodicRefresh);
        if (request != null) {
            request.cancel(true);
            request = null;
        }
    }

    private void refreshNormally() {
        refresh(null);
    }

    private void refresh(JourneyStatus.AlarmReview review) {
        McpClient.Connection captured = connection;
        if (!visible || captured == null || request != null) {
            return;
        }
        main.removeCallbacks(periodicRefresh);
        statusUsable = false;
        syncMessage.setText(R.string.connecting);
        int sequence = ++requestSequence;
        request = network.submit(() -> {
            try {
                JourneyStatus updated = client.getStatus(captured);
                main.post(() -> finishRefresh(sequence, review, updated, null));
            } catch (IOException | RuntimeException failure) {
                String message = failure instanceof McpClient.Failure
                        ? failure.getMessage() : getString(R.string.connect_failed);
                main.post(() -> finishRefresh(sequence, review, null, message));
            }
        });
        renderState();
    }

    private void finishRefresh(int sequence, JourneyStatus.AlarmReview review, JourneyStatus updated, String error) {
        if (!visible || sequence != requestSequence) {
            return;
        }
        request = null;
        if (updated == null) {
            syncMessage.setText(getString(R.string.connect_failure_format, error));
        } else {
            status = updated;
            statusUsable = true;
            syncMessage.setText(R.string.connected);
            if (review != null) {
                verifyReview(review, updated);
            }
            if (!follow.update(updated, Instant.now())) {
                syncMessage.setText(R.string.notification_unavailable);
            }
        }
        renderState();
        main.postDelayed(periodicRefresh, REFRESH_INTERVAL_MS);
    }

    private void renderState() {
        Instant now = Instant.now();
        WakeAlarm.Entry alarm = currentAlarm();
        renderAlarm(alarm, now);
        renderStatus(alarm, now);
        refreshButton.setEnabled(connection != null && request == null);
        disconnectButton.setEnabled(connection != null);
    }

    private void renderAlarm(WakeAlarm.Entry alarm, Instant now) {
        alarmControls.removeAllViews();
        if (alarm == null) {
            text(alarmControls, getString(R.string.alarm_none), 18, false);
            button(alarmControls, R.string.alarm_set, () -> chooseTime(null, null));
            return;
        }
        int summary = alarm.ringing() ? R.string.alarm_ringing
                : !device.canSchedule() ? R.string.alarm_needs_permission
                : !alarm.when().isAfter(now) ? R.string.alarm_due : R.string.alarm_armed;
        text(alarmControls, getString(summary, PhoneTime.format(this, alarm.when())), 18, false);
        text(alarmControls, alarm.journey() == null ? getString(R.string.alarm_local)
                : getString(R.string.alarm_linked, alarm.journey().label()), 14, true);
        button(alarmControls, alarm.ringing() ? R.string.alarm_stop : R.string.alarm_cancel,
                () -> stopAlarm(alarm));
        if (alarm.ringing()) {
            button(alarmControls, R.string.alarm_snooze, () -> snooze(alarm));
            return;
        }
        button(alarmControls, R.string.alarm_replace, () -> chooseTime(alarm, alarm.journey()));
        if (statusUsable && status != null && status.earlierTime(alarm, now) != null) {
            button(alarmControls, R.string.alarm_review, () -> reviewEarlier(alarm));
        }
    }

    private void renderStatus(WakeAlarm.Entry alarm, Instant now) {
        statusContent.removeAllViews();
        TripFollow.Active following = follow.active(now);
        if (following != null) {
            text(statusContent, getString(R.string.following, following.journey().label()), 16, false);
            button(statusContent, R.string.stop_follow, () -> {
                follow.stop(following.id());
                renderState();
            });
        }
        if (status == null) {
            text(statusContent, getString(R.string.no_status), 14, true);
            return;
        }
        boolean fresh = statusUsable && status.fresh(now);
        text(statusContent, getString(fresh ? R.string.snapshot : R.string.old_snapshot,
                PhoneTime.format(this, status.asOf())), 14, true);
        TextView statusText = text(statusContent, status.text(), 16, false);
        statusText.setTextIsSelectable(true);
        for (JourneyStatus.Journey journey : status.journeys()) {
            TextView heading = text(statusContent,
                    getString(R.string.journey_heading, journey.key().label(), journey.state()),
                    16, false);
            heading.setTypeface(null, Typeface.BOLD);
            text(statusContent, getString(R.string.timetable, journey.health(),
                    PhoneTime.format(this, journey.checked())), 14, true);
            text(statusContent, journey.leaveBy() == null ? getString(R.string.leave_by_unavailable)
                    : getString(R.string.leave_by, PhoneTime.format(this, journey.leaveBy())),
                    14, false);
            if (fresh && journey.canFollow(status.asOf(), now)
                    && journey.equals(status.find(journey.key()))) {
                if (following == null || !journey.key().equals(following.journey())) {
                    button(statusContent, R.string.follow, () -> startFollow(journey));
                }
                if (alarm == null || !alarm.ringing()) {
                    button(statusContent, R.string.alarm_link, () -> chooseTime(alarm, journey.key()));
                }
            }
        }
    }

    private void startFollow(JourneyStatus.Journey journey) {
        Instant now = Instant.now();
        if (!statusUsable || status == null || !status.fresh(now)
                || !journey.equals(status.find(journey.key()))
                || !journey.canFollow(status.asOf(), now)) {
            return;
        }
        if (!TripFollow.allowed(this, TripFollow.CHANNEL)) {
            syncMessage.setText(R.string.trip_notifications_denied);
            requestNotifications();
        } else if (!follow.start(journey, status.asOf(), now)) {
            syncMessage.setText(R.string.notification_unavailable);
        }
        renderState();
    }

    private void chooseTime(WakeAlarm.Entry existing, JourneyStatus.Key key) {
        if (!allowAlarm()) {
            return;
        }
        ZoneId zone = ZoneId.systemDefault();
        LocalDateTime initial = Instant.now().plusSeconds(600).atZone(zone).toLocalDateTime();
        if (existing != null && existing.when().isAfter(Instant.now())) {
            initial = existing.when().atZone(zone).toLocalDateTime();
        }
        LocalTime initialClock = initial.toLocalTime();
        DatePickerDialog dialog = new DatePickerDialog(this, (picker, year, month, day) ->
                new TimePickerDialog(this, (clock, hour, minute) -> {
                    LocalDate date = LocalDate.of(year, month + 1, day);
                    LocalDateTime local = date.atTime(hour, minute);
                    List<ZoneOffset> offsets = zone.getRules().getValidOffsets(local);
                    if (offsets.size() != 1) {
                        alarmMessage.setText(R.string.alarm_ambiguous);
                        return;
                    }
                    Instant when = local.toInstant(offsets.get(0));
                    if (!when.isAfter(Instant.now())) {
                        alarmMessage.setText(R.string.alarm_future);
                        return;
                    }
                    confirmManual(existing, key, when);
                }, initialClock.getHour(), initialClock.getMinute(),
                        DateFormat.is24HourFormat(this)).show(),
                initial.getYear(), initial.getMonthValue() - 1, initial.getDayOfMonth());
        dialog.getDatePicker().setMinDate(System.currentTimeMillis());
        dialog.show();
    }

    private void confirmManual(WakeAlarm.Entry existing, JourneyStatus.Key key, Instant when) {
        new AlertDialog.Builder(this)
                .setTitle(R.string.wake_alarm)
                .setMessage(getString(R.string.alarm_confirm_body, PhoneTime.format(this, when)))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.alarm_confirm, (dialog, which) -> {
                    if (Objects.equals(existing, currentAlarm())) {
                        arm(when, key);
                    } else {
                        alarmMessage.setText(R.string.alarm_changed);
                    }
                }).show();
    }

    private void reviewEarlier(WakeAlarm.Entry alarm) {
        Instant earlier = statusUsable && status != null ? status.earlierTime(alarm, Instant.now())
                : null;
        if (earlier == null) {
            alarmMessage.setText(R.string.alarm_changed);
            return;
        }
        JourneyStatus.AlarmReview review = new JourneyStatus.AlarmReview(alarm.id(),
                alarm.journey(), earlier);
        new AlertDialog.Builder(this)
                .setTitle(R.string.alarm_review)
                .setMessage(getString(R.string.alarm_review_body,
                        PhoneTime.format(this, alarm.when()), PhoneTime.format(this, earlier),
                        alarm.journey().label()))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.alarm_review_confirm, (dialog, which) -> {
                    if (request != null) {
                        alarmMessage.setText(R.string.alarm_changed);
                    } else {
                        refresh(review);
                    }
                }).show();
    }

    private void verifyReview(JourneyStatus.AlarmReview review, JourneyStatus updated) {
        WakeAlarm.Entry alarm = currentAlarm();
        Instant when = review.confirmedTime(updated, alarm, Instant.now());
        if (when != null) {
            arm(when, review.journey());
        } else {
            alarmMessage.setText(R.string.alarm_changed);
        }
    }

    private void arm(Instant when, JourneyStatus.Key key) {
        if (!allowAlarm()) {
            return;
        }
        try {
            device.controller().replace(when, key, Instant.now());
            alarmMessage.setText("");
        } catch (IllegalArgumentException past) {
            alarmMessage.setText(R.string.alarm_future);
        } catch (IOException | RuntimeException error) {
            alarmMessage.setText(R.string.alarm_failed);
        }
        renderState();
    }

    private boolean allowAlarm() {
        if (!device.canSchedule()) {
            alarmMessage.setText(R.string.exact_denied);
            requestExact();
            return false;
        }
        if (!TripFollow.allowed(this, AndroidAlarm.CHANNEL)) {
            alarmMessage.setText(R.string.notifications_denied);
            requestNotifications();
            return false;
        }
        return true;
    }

    private void stopAlarm(WakeAlarm.Entry alarm) {
        try {
            AlarmReceiver.stop(this, device, alarm.id());
            alarmMessage.setText("");
        } catch (IOException | RuntimeException error) {
            alarmMessage.setText(R.string.alarm_stop_failed);
        }
        renderState();
    }

    private void snooze(WakeAlarm.Entry alarm) {
        try {
            AlarmReceiver.snooze(this, device, alarm.id());
            alarmMessage.setText("");
        } catch (IOException | RuntimeException error) {
            alarmMessage.setText(R.string.alarm_snooze_failed);
        }
        renderState();
    }

    private WakeAlarm.Entry currentAlarm() {
        try {
            return device.controller().current(Instant.now(), () -> AndroidAlarm.stopSound(this));
        } catch (IOException | RuntimeException error) {
            alarmMessage.setText(R.string.alarm_unreadable);
            return null;
        }
    }

    private void requestExact() {
        if (Build.VERSION.SDK_INT >= 31) {
            openSettings(new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                    Uri.parse("package:" + getPackageName())));
        }
    }

    private void requestNotifications() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED
                && (!settings.getBoolean("notification_requested", false)
                        || shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS))) {
            settings.edit().putBoolean("notification_requested", true).apply();
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_REQUEST);
        } else {
            openSettings(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName()));
        }
    }

    private void openAppSettings() {
        openSettings(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getPackageName())));
    }

    private void openSettings(Intent settingsIntent) {
        try {
            startActivity(settingsIntent);
        } catch (RuntimeException unsupported) {
            alarmMessage.setText(R.string.permissions_blocked);
        }
    }

    private EditText field(LinearLayout parent, int label, int inputType, String setting) {
        EditText field = new EditText(this);
        field.setId(View.generateViewId());
        field.setSaveEnabled(false);
        field.setHint(label);
        field.setSingleLine(true);
        field.setInputType(inputType);
        field.setTextSize(16);
        field.setTextColor(TEXT);
        field.setHintTextColor(MUTED);
        if (setting != null) {
            field.setText(settings.getString(setting, ""));
        }
        parent.addView(field);
        return field;
    }

    private LinearLayout column() {
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        return column;
    }

    private LinearLayout card(LinearLayout page) {
        LinearLayout card = column();
        GradientDrawable panel = new GradientDrawable();
        panel.setColor(Color.WHITE);
        panel.setCornerRadius(dp(14));
        card.setBackground(panel);
        card.setPadding(dp(16), dp(12), dp(16), dp(12));
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        layout.topMargin = dp(14);
        page.addView(card, layout);
        return card;
    }

    private void heading(LinearLayout parent, int label, int size) {
        TextView heading = text(parent, getString(label), size, false);
        heading.setTypeface(null, Typeface.BOLD);
        heading.setAccessibilityHeading(true);
    }

    private TextView text(LinearLayout parent, String value, int size, boolean muted) {
        TextView line = new TextView(this);
        line.setText(value);
        line.setTextSize(size);
        line.setTextColor(muted ? MUTED : TEXT);
        line.setPadding(0, dp(4), 0, dp(4));
        parent.addView(line);
        return line;
    }

    private Button button(LinearLayout parent, int label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setOnClickListener(view -> action.run());
        parent.addView(button);
        return button;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
