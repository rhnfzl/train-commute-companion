import AlarmKit
import SwiftUI

@main
struct CommuteApp: App {
  @Environment(\.scenePhase) private var scenePhase
  @State private var model = PilotModel()

  var body: some Scene {
    WindowGroup {
      PilotView(model: model)
        .onChange(of: scenePhase, initial: true) { _, phase in
          model.setForeground(phase == .active)
        }
        .task(id: scenePhase) {
          guard scenePhase == .active else { return }
          for await _ in AlarmManager.shared.alarmUpdates {
            guard !Task.isCancelled else { break }
            model.reconcileAlarms()
          }
        }
    }
  }
}

private struct PilotView: View {
  let model: PilotModel
  @State private var showConnection = false
  @State private var wakeReview: WakeReview?
  @State private var leadMinutes = 30

  var body: some View {
    NavigationStack {
      Form {
        Section {
          if let connection = model.connection {
            LabeledContent("Profile", value: connection.profile)
            Button {
              model.refresh()
            } label: {
              HStack {
                Text("Refresh commute")
                if model.refreshing { ProgressView() }
              }
            }
            .disabled(model.refreshing || model.alarms?.busy == true)
          } else {
            Button("Configure connection") { showConnection = true }
          }

          if let error = model.connectionError {
            Label(error, systemImage: "exclamationmark.circle")
              .foregroundStyle(.secondary)
          }
        } header: {
          Text("Commute status")
        } footer: {
          Text("Sync runs while the app is open. A scheduled alarm may use an older timetable.")
        }

        if let status = model.status {
          Section("Latest snapshot") {
            LabeledContent("Snapshot", value: formatDate(status.asOf))
            if !model.latestReadSucceeded {
              Text("Showing the previous snapshot.")
                .foregroundStyle(.secondary)
            }
            Text(verbatim: status.text)
              .textSelection(.enabled)
          }

          TimelineView(.periodic(from: .now, by: 15)) { context in
            journeySections(status, now: context.date)
          }
        }

        AlarmSection(model: model) { wakeReview = $0 }

        if model.tripActive || model.tripMessage != nil {
          Section("Trip Live Activity") {
            if model.tripActive {
              Button("End trip display") { Task { await model.endTrip() } }
                .disabled(model.endingTrip)
            }
            if let message = model.tripMessage {
              Text(message).foregroundStyle(.secondary)
            }
          }
        }
      }
      .navigationTitle("Train Commute")
      .toolbar {
        ToolbarItem(placement: .topBarTrailing) {
          Button("Connection settings", systemImage: "gearshape") { showConnection = true }
            .disabled(model.alarms?.busy == true)
        }
      }
      .sheet(isPresented: $showConnection) { ConnectionForm(model: model) }
      .sheet(item: $wakeReview) { review in
        WakeReviewSheet(review: review, current: model.alarms?.state.primary?.time) {
          model.arm(review)
        }
      }
      .onChange(of: model.isForeground) { _, active in
        if !active { wakeReview = nil }
      }
    }
  }

  @ViewBuilder
  private func journeySections(_ status: CommuteStatus, now: Date) -> some View {
    Section("Journeys") {
      if status.journeys.isEmpty {
        Text("No journeys to display.")
      } else {
        ForEach(status.journeys) { journey in
          VStack(alignment: .leading, spacing: 10) {
            Text(journey.title).font(.headline)
            LabeledContent("Timetable health", value: healthLabel(journey.timetableHealth))
            LabeledContent(
              "Last good check", value: journey.timetableLastOKAt.map(formatDate) ?? "Unavailable"
            )
            LabeledContent("Leave by", value: journey.leaveBy.map(formatDate) ?? "Unavailable")
            if status.freshUntil(for: journey, now: now) == nil {
              Text("Timetable unavailable or out of date.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            }
            if let proposal = model.wakeProposal(for: journey, leadMinutes: leadMinutes, now: now) {
              Button("Review wake time") { wakeReview = .journey(proposal) }
                .buttonStyle(.borderless)
            }
            if model.canStartTrip(journey, now: now) {
              Button("Start trip display") { model.startTrip(journey) }
                .buttonStyle(.borderless)
            }
          }
          .padding(.vertical, 6)
        }
      }
    }

    if model.alarmAuthorization == .authorized {
      Section("Wake lead time") {
        Stepper(value: $leadMinutes, in: 0...180, step: 5) {
          Text("\(leadMinutes) min before leave-by")
        }
      }
    }
  }

  private func healthLabel(_ health: String) -> String {
    switch health {
    case "healthy": "Healthy"
    case "stale": "Stale"
    case "degraded": "Last check failed"
    case "unseen": "No successful check"
    case "auth_rejected": "Provider authentication failed"
    default: "Unknown"
    }
  }
}

private struct AlarmSection: View {
  let model: PilotModel
  let review: (WakeReview) -> Void

  @State private var manualDate =
    Calendar.current.dateInterval(of: .minute, for: .now.addingTimeInterval(3_600))?.start
    ?? .now.addingTimeInterval(3_600)
  @State private var removingPrimary = false
  @State private var removingAdditional = false

  var body: some View {
    Section {
      if let alarms = model.alarms {
        switch model.alarmAuthorization {
        case .authorized:
          alarmControls(alarms)
        case .notDetermined:
          Button("Allow wake alarms") { Task { await model.requestAlarmAuthorization() } }
            .disabled(model.requestingAlarmAuthorization)
        case .denied:
          Text("Allow this app to schedule alarms in iPhone Settings.")
        @unknown default:
          Text("Alarm access is unavailable.")
        }
      }
      if let message = model.alarmMessage {
        Text(message).foregroundStyle(.secondary)
      }
    } header: {
      Text("Wake alarm")
    } footer: {
      Text(
        "iOS handles the alarm and its five-minute snooze. Leave-by changes require your review while the app is open."
      )
    }
  }

  @ViewBuilder
  private func alarmControls(_ alarms: AlarmCoordinator) -> some View {
    if let primary = alarms.state.primary {
      LabeledContent("Original alarm time", value: formatDate(primary.time))
      if alarms.isVerified {
        LabeledContent("Alarm state", value: alarms.primaryAlarm?.phase.rawValue ?? "Unknown")
      }
    } else if alarms.isVerified || alarms.busy {
      Text("No primary alarm set.")
        .foregroundStyle(.secondary)
    }

    if alarms.busy {
      ProgressView("Checking and setting alarm…")
    } else if !alarms.isVerified {
      Button("Check active alarms") { model.reconcileAlarms() }
    } else if alarms.additionalAlarms.isEmpty {
      DatePicker("Manual wake time", selection: $manualDate)
      Button("Review manual time") { review(.manual(manualDate)) }
        .disabled(manualDate <= Date())
    } else {
      Label("Additional iPhone alarm active", systemImage: "exclamationmark.triangle")
        .foregroundStyle(.orange)
      ForEach(alarms.additionalAlarms) { alarm in
        LabeledContent(
          alarm.phase.rawValue, value: alarm.time.map(formatDate) ?? "Time unavailable")
      }
      Button("Remove additional alarms", role: .destructive) { removingAdditional = true }
        .confirmationDialog(
          "Remove the additional iPhone alarms?", isPresented: $removingAdditional
        ) {
          Button("Remove additional alarms", role: .destructive) {
            model.removeAlarm(additional: true)
          }
        } message: {
          Text("Your primary wake alarm stays active.")
        }
    }

    if alarms.isVerified && !alarms.busy && alarms.state.primary != nil {
      Button("Remove wake alarm", role: .destructive) { removingPrimary = true }
        .confirmationDialog("Remove the primary wake alarm?", isPresented: $removingPrimary) {
          Button("Remove wake alarm", role: .destructive) { model.removeAlarm(additional: false) }
        }
    }
  }
}

private struct WakeReviewSheet: View {
  @Environment(\.dismiss) private var dismiss
  let review: WakeReview
  let current: Date?
  let arm: () -> Void

  var body: some View {
    NavigationStack {
      Form {
        Section("New alarm") {
          LabeledContent("Wake time", value: formatDate(review.time))
          LabeledContent("Time zone", value: TimeZone.current.identifier)
          if case .journey(let proposal) = review {
            LabeledContent("Leave by", value: formatDate(proposal.leaveBy))
            LabeledContent("Wake before leave-by", value: "\(proposal.leadMinutes) min")
            Text("The app checks this leave-by time again when you confirm.")
              .foregroundStyle(.secondary)
          }
        }
        if let current {
          Section("Current alarm") {
            Text(formatDate(current))
            Text("The app schedules the new alarm before removing the current one.")
              .foregroundStyle(.secondary)
          }
        }
        Button(current == nil ? "Set wake alarm" : "Replace wake alarm") {
          arm()
          dismiss()
        }
      }
      .navigationTitle("Review wake alarm")
      .navigationBarTitleDisplayMode(.inline)
      .toolbar {
        ToolbarItem(placement: .cancellationAction) {
          Button("Cancel") { dismiss() }
        }
      }
    }
  }
}

private struct ConnectionForm: View {
  @Environment(\.dismiss) private var dismiss
  let model: PilotModel

  @State private var endpoint = ""
  @State private var profile = ""
  @State private var tokenName = ""
  @State private var token = ""
  @State private var error: String?
  @State private var removing = false

  var body: some View {
    NavigationStack {
      Form {
        Section {
          TextField("Private HTTPS URL ending in /mcp", text: $endpoint)
            .keyboardType(.URL)
            .textContentType(.URL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
          TextField("Profile ID or name", text: $profile)
            .autocorrectionDisabled()
          TextField("Registered token name", text: $tokenName)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
          SecureField(
            model.connection == nil ? "Bearer token" : "Bearer token (leave blank to keep)",
            text: $token
          )
          .textInputAutocapitalization(.never)
          .autocorrectionDisabled()
        } footer: {
          Text(
            "Use a dedicated token registered on your private companion. This app uses it only to read status."
          )
        }

        if let error {
          Text(error).foregroundStyle(.secondary)
        }

        if model.connection != nil {
          Button("Remove connection", role: .destructive) { removing = true }
            .confirmationDialog("Remove the saved connection?", isPresented: $removing) {
              Button("Remove connection", role: .destructive) { remove() }
            } message: {
              Text("Any scheduled wake alarm continues to run.")
            }
        }
      }
      .navigationTitle("Connection")
      .navigationBarTitleDisplayMode(.inline)
      .toolbar {
        ToolbarItem(placement: .cancellationAction) {
          Button("Cancel") { dismiss() }
        }
        ToolbarItem(placement: .confirmationAction) {
          Button("Save") { save() }
        }
      }
      .onAppear {
        endpoint = model.connection?.endpoint.absoluteString ?? ""
        profile = model.connection?.profile ?? ""
        tokenName = model.connection?.tokenName ?? ""
      }
    }
  }

  private func save() {
    do {
      try model.saveConnection(
        endpoint: endpoint, profile: profile, tokenName: tokenName, token: token)
      token = ""
      dismiss()
    } catch {
      self.error =
        (error as? LocalizedError)?.errorDescription ?? "The connection could not be saved."
    }
  }

  private func remove() {
    do {
      try model.removeConnection()
      token = ""
      dismiss()
    } catch {
      self.error = "The connection could not be removed from the iPhone Keychain."
    }
  }
}

private func formatDate(_ date: Date) -> String {
  date.formatted(date: .abbreviated, time: .shortened)
}
