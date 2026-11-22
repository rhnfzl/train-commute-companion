import ActivityKit
import AlarmKit
import AppIntents
import Foundation

struct WakeAlarmMetadata: AlarmMetadata {}

struct TripActivityAttributes: ActivityAttributes {
  struct ContentState: Codable, Hashable {
    let estimatedArrival: Date
    let timetableCheckedAt: Date
  }

  let connectionID: UUID
  let profileID: String
  let direction: String
  let serviceDate: String
  let startedAt: Date
}

struct StopWakeCountdown: LiveActivityIntent {
  static let title: LocalizedStringResource = "Stop wake alarm"

  @Parameter(title: "Alarm ID")
  var alarmID: String

  init() {}

  init(id: UUID) {
    alarmID = id.uuidString
  }

  func perform() throws -> some IntentResult {
    guard let id = UUID(uuidString: alarmID) else { return .result() }
    try AlarmManager.shared.stop(id: id)
    return .result()
  }
}
