import AlarmKit
import Foundation
import SwiftUI

@MainActor
struct PhoneAlarmSystem: AlarmSystem {
  func alarms() throws -> [SystemAlarm] {
    guard AlarmManager.shared.authorizationState == .authorized else { throw WakeAlarmError.access }
    return try AlarmManager.shared.alarms.map { alarm in
      let time: Date?
      if case .fixed(let scheduledDate)? = alarm.schedule {
        time = scheduledDate
      } else {
        time = nil
      }

      let phase: SystemAlarm.Phase
      switch alarm.state {
      case .scheduled: phase = .scheduled
      case .countdown: phase = .countdown
      case .paused: phase = .paused
      case .alerting: phase = .alerting
      @unknown default: phase = .unknown
      }
      return SystemAlarm(id: alarm.id, time: time, phase: phase)
    }
  }

  func schedule(_ alarm: AlarmRecord) async throws {
    let manager = AlarmManager.shared
    guard manager.authorizationState == .authorized else { throw WakeAlarmError.access }

    let snooze = AlarmButton(text: "Snooze 5 min", textColor: .white, systemImageName: "zzz")
    let alert: AlarmPresentation.Alert
    if #available(iOS 26.1, *) {
      alert = AlarmPresentation.Alert(
        title: "Commute wake alarm", secondaryButton: snooze, secondaryButtonBehavior: .countdown
      )
    } else {
      alert = AlarmPresentation.Alert(
        title: "Commute wake alarm",
        stopButton: AlarmButton(text: "Stop", textColor: .white, systemImageName: "stop.fill"),
        secondaryButton: snooze, secondaryButtonBehavior: .countdown
      )
    }
    let attributes = AlarmAttributes(
      presentation: AlarmPresentation(
        alert: alert, countdown: AlarmPresentation.Countdown(title: "Snoozing")
      ),
      metadata: WakeAlarmMetadata(),
      tintColor: .indigo
    )
    let configuration = AlarmManager.AlarmConfiguration(
      countdownDuration: Alarm.CountdownDuration(preAlert: nil, postAlert: 5 * 60),
      schedule: .fixed(alarm.time),
      attributes: attributes
    )
    _ = try await manager.schedule(id: alarm.id, configuration: configuration)
  }

  func cancel(id: UUID) throws {
    try AlarmManager.shared.cancel(id: id)
  }
}

@MainActor
struct AlarmFile: AlarmStoring {
  private let folder = URL.applicationSupportDirectory.appending(path: "WakeAlarm")
  private var file: URL { folder.appending(path: "state.json") }

  func load() throws -> AlarmState {
    do {
      return try JSONDecoder().decode(AlarmState.self, from: Data(contentsOf: file))
    } catch CocoaError.fileReadNoSuchFile {
      return AlarmState()
    }
  }

  func save(_ state: AlarmState) throws {
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let data = try JSONEncoder().encode(state)
    try data.write(
      to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
  }
}
