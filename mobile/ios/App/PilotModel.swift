import ActivityKit
import AlarmKit
import Foundation
import Observation

enum WakeReview: Identifiable {
  case manual(Date)
  case journey(WakeProposal)

  var id: Date { time }

  var time: Date {
    switch self {
    case .manual(let time): time
    case .journey(let proposal): proposal.alarmTime
    }
  }
}

@MainActor
@Observable
final class PilotModel {
  private let client = MCPClient()
  private let keychain = KeychainConnection()
  private var refreshTask: Task<Void, Never>?
  private var refreshID: UUID?
  private var wakeTask: Task<Void, Never>?

  private(set) var connection: MCPConnection?
  private(set) var status: CommuteStatus?
  private(set) var isForeground = false
  private(set) var latestReadSucceeded = false
  private(set) var refreshing = false
  private(set) var connectionError: String?
  private(set) var alarmMessage: String?
  private(set) var tripMessage: String?
  private(set) var tripActive = false
  private(set) var endingTrip = false
  private(set) var alarmAuthorization = AlarmManager.shared.authorizationState
  private(set) var requestingAlarmAuthorization = false
  let alarms: AlarmCoordinator?

  init() {
    do {
      alarms = try AlarmCoordinator(system: PhoneAlarmSystem(), store: AlarmFile())
    } catch {
      alarms = nil
      alarmMessage = "The saved alarm record could not be read. Alarm changes are unavailable."
    }
  }

  func setForeground(_ active: Bool) {
    isForeground = active
    if !active {
      stopRefresh()
      wakeTask?.cancel()
      return
    }

    readConnection()
    reconcileAlarms()
    tripActive = !Activity<TripActivityAttributes>.activities.isEmpty
    refresh()
  }

  func saveConnection(
    endpoint input: String, profile: String, tokenName inputName: String, token: String
  ) throws {
    let endpoint = try MCPConnection.endpoint(from: input)
    let tokenName = inputName.trimmingCharacters(in: .whitespacesAndNewlines)
    let matchingCredential = connection?.endpoint == endpoint && connection?.tokenName == tokenName
    let sameProfile = connection?.profile == profile.trimmingCharacters(in: .whitespacesAndNewlines)
    let retainedID = matchingCredential && sameProfile ? connection?.id : nil
    let retainedToken = matchingCredential ? connection?.token : nil
    let updated = try MCPConnection(
      id: retainedID ?? UUID(), endpoint: endpoint, profile: profile, tokenName: tokenName,
      token: token.isEmpty ? (retainedToken ?? "") : token
    )
    try keychain.save(updated)
    stopRefresh()
    connection = updated
    status = nil
    connectionError = nil
    refresh()
  }

  func removeConnection() throws {
    try keychain.remove()
    stopRefresh()
    connection = nil
    status = nil
    connectionError = nil
    Task { await updateTrip(with: nil) }
  }

  func refresh() {
    guard isForeground, !refreshing, alarms?.busy != true, let connection else { return }
    let id = UUID()
    refreshID = id
    refreshing = true
    latestReadSucceeded = false
    refreshTask = Task {
      let updated = await fetchStatus(for: connection)
      guard !Task.isCancelled, refreshID == id, isForeground, self.connection == connection else {
        return
      }
      refreshing = false
      refreshID = nil
      refreshTask = nil
      await updateTrip(with: updated)
    }
  }

  func wakeProposal(for journey: CommuteJourney, leadMinutes: Int, now: Date) -> WakeProposal? {
    guard isForeground, latestReadSucceeded, alarms?.isVerified == true,
      alarms?.busy == false, alarms?.additionalAlarms.isEmpty == true
    else { return nil }
    return status?.wakeProposal(for: journey.id, leadMinutes: leadMinutes, now: now)
  }

  func arm(_ review: WakeReview) {
    guard isForeground, wakeTask == nil, let alarms, !alarms.busy else { return }
    alarmMessage = nil
    wakeTask = Task {
      defer { wakeTask = nil }
      do {
        try await alarms.replace(on: review.time) {
          guard self.isForeground else { throw CancellationError() }
          if case .journey(let proposal) = review {
            guard let connection = self.connection,
              let updated = await self.fetchStatus(for: connection),
              updated.confirms(proposal, now: Date())
            else { throw ReviewError.changed }
          }
          guard self.isForeground else { throw CancellationError() }
        }
        alarmMessage =
          alarms.additionalAlarms.isEmpty
          ? "Wake alarm set."
          : "Wake alarm set. An additional alarm is still active."
      } catch is CancellationError {
        alarmMessage = "Check the alarm status before trying again."
      } catch {
        alarmMessage =
          (error as? LocalizedError)?.errorDescription
          ?? "The alarm change could not be confirmed. Check the current alarm."
      }
    }
  }

  func requestAlarmAuthorization() async {
    guard !requestingAlarmAuthorization else { return }
    requestingAlarmAuthorization = true
    defer { requestingAlarmAuthorization = false }
    do {
      alarmAuthorization = try await AlarmManager.shared.requestAuthorization()
      reconcileAlarms()
    } catch {
      alarmMessage = "iOS could not grant alarm access."
    }
  }

  func reconcileAlarms() {
    alarmAuthorization = AlarmManager.shared.authorizationState
    guard alarmAuthorization == .authorized, let alarms, !alarms.busy else { return }
    do {
      try alarms.reconcile()
    } catch {
      alarmMessage = "The iPhone could not confirm which alarms are active."
    }
  }

  func removeAlarm(additional: Bool) {
    guard isForeground, let alarms else { return }
    alarmMessage = nil
    do {
      if additional { try alarms.removeAdditionalAlarms() } else { try alarms.removePrimary() }
    } catch {
      alarmMessage = "The iPhone could not confirm removal. Check the active alarms."
    }
  }

  func canStartTrip(_ journey: CommuteJourney, now: Date) -> Bool {
    guard isForeground, latestReadSucceeded, !tripActive,
      let arrival = journey.estimatedArrival, arrival > now
    else { return false }
    return status?.freshUntil(for: journey, now: now) != nil
  }

  func startTrip(_ journey: CommuteJourney) {
    let now = Date()
    guard canStartTrip(journey, now: now), let status, let connection,
      let arrival = journey.estimatedArrival,
      let expires = status.freshUntil(for: journey, now: now),
      let checked = journey.timetableLastOKAt
    else { return }
    tripMessage = nil
    guard ActivityAuthorizationInfo().areActivitiesEnabled else {
      tripMessage = "Allow Live Activities in Settings to show the trip on your Lock Screen."
      return
    }
    do {
      let attributes = TripActivityAttributes(
        connectionID: connection.id, profileID: status.profile, direction: journey.direction,
        serviceDate: journey.serviceDate, startedAt: now
      )
      let state = TripActivityAttributes.ContentState(
        estimatedArrival: arrival, timetableCheckedAt: checked)
      _ = try Activity.request(
        attributes: attributes,
        content: ActivityContent(state: state, staleDate: min(expires, arrival)),
        pushType: nil
      )
      tripActive = true
    } catch {
      tripMessage = "The iPhone could not start the trip display."
    }
  }

  func endTrip() async {
    guard !endingTrip else { return }
    endingTrip = true
    for activity in Activity<TripActivityAttributes>.activities {
      await activity.end(nil, dismissalPolicy: .immediate)
    }
    tripActive = false
    endingTrip = false
    tripMessage = nil
  }

  private func readConnection() {
    do {
      connection = try keychain.load()
    } catch {
      connection = nil
      status = nil
      connectionError =
        "The saved connection could not be opened. Open Connection settings to reconnect."
    }
  }

  private func stopRefresh() {
    refreshTask?.cancel()
    refreshTask = nil
    refreshID = nil
    refreshing = false
    latestReadSucceeded = false
  }

  private func fetchStatus(for connection: MCPConnection) async -> CommuteStatus? {
    guard isForeground, !Task.isCancelled else { return nil }
    do {
      let updated = try await client.status(for: connection)
      guard isForeground, !Task.isCancelled, self.connection == connection else { return nil }
      status = updated
      latestReadSucceeded = true
      connectionError = nil
      return updated
    } catch {
      guard isForeground, !Task.isCancelled, self.connection == connection else { return nil }
      latestReadSucceeded = false
      connectionError =
        (error as? MCPError)?.errorDescription
        ?? "The companion could not be reached. Check the private network and HTTPS certificate."
      return nil
    }
  }

  private func updateTrip(with status: CommuteStatus?) async {
    let now = Date()
    let active = Activity<TripActivityAttributes>.activities
    tripActive = !active.isEmpty
    for activity in active {
      let attributes = activity.attributes
      if let status, status.profile == attributes.profileID,
        connection?.id == attributes.connectionID,
        let journey = status.journeys.first(where: {
          $0.direction == attributes.direction && $0.serviceDate == attributes.serviceDate
        }),
        let arrival = journey.estimatedArrival, arrival > attributes.startedAt,
        let checked = journey.timetableLastOKAt,
        let expires = status.freshUntil(for: journey, now: now)
      {
        let state = TripActivityAttributes.ContentState(
          estimatedArrival: arrival, timetableCheckedAt: checked)
        await activity.update(ActivityContent(state: state, staleDate: min(expires, arrival)))
      } else {
        await activity.update(ActivityContent(state: activity.content.state, staleDate: now))
      }
    }
  }
}

private enum ReviewError: LocalizedError {
  case changed

  var errorDescription: String? {
    "The timetable changed or the check failed. Review a fresh wake time before trying again."
  }
}
