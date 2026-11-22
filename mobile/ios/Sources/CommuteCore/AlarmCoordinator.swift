import Foundation
import Observation

struct AlarmRecord: Codable, Equatable {
  let id: UUID
  let time: Date
}

struct AlarmState: Codable, Equatable {
  var primary: AlarmRecord?
  var additional: AlarmRecord?
}

struct SystemAlarm: Identifiable {
  enum Phase: String {
    case scheduled = "Scheduled"
    case countdown = "Counting down"
    case paused = "Paused"
    case alerting = "Ringing"
    case unknown = "State unknown"
  }

  let id: UUID
  let time: Date?
  let phase: Phase
}

@MainActor
protocol AlarmSystem {
  func alarms() throws -> [SystemAlarm]
  func schedule(_ alarm: AlarmRecord) async throws
  func cancel(id: UUID) throws
}

@MainActor
protocol AlarmStoring {
  func load() throws -> AlarmState
  func save(_ state: AlarmState) throws
}

enum WakeAlarmError: LocalizedError {
  case busy
  case notFuture
  case additionalAlarms
  case notConfirmed
  case access

  var errorDescription: String? {
    switch self {
    case .busy: "An alarm change is already in progress."
    case .notFuture: "Choose a time in the future."
    case .additionalAlarms: "Resolve the additional alarm before setting another."
    case .notConfirmed:
      "The iPhone could not confirm the new alarm. Check active alarms before trying again."
    case .access: "Allow this app to schedule alarms in Settings."
    }
  }
}

@MainActor
@Observable
final class AlarmCoordinator {
  private let system: any AlarmSystem
  private let store: any AlarmStoring
  private(set) var state: AlarmState
  private(set) var inventory: [SystemAlarm] = []
  private(set) var isVerified = false
  private(set) var busy = false

  init(system: any AlarmSystem, store: any AlarmStoring) throws {
    self.system = system
    self.store = store
    state = try store.load()
  }

  var primaryAlarm: SystemAlarm? { inventory.first { $0.id == state.primary?.id } }
  var additionalAlarms: [SystemAlarm] { inventory.filter { $0.id != state.primary?.id } }

  func reconcile() throws {
    guard !busy else { return }
    try readInventory()
  }

  func replace(on time: Date, validate: @MainActor () async throws -> Void) async throws {
    guard !busy else { throw WakeAlarmError.busy }
    busy = true
    defer { busy = false }

    try readInventory()
    guard additionalAlarms.isEmpty else { throw WakeAlarmError.additionalAlarms }
    try await validate()
    try Task.checkCancellation()
    guard time > Date() else { throw WakeAlarmError.notFuture }
    try readInventory()
    guard additionalAlarms.isEmpty else { throw WakeAlarmError.additionalAlarms }
    if primaryAlarm?.phase == .scheduled, state.primary?.time == time { return }

    let previous = state.primary
    let next = AlarmRecord(id: UUID(), time: time)
    // Persist the ID before scheduling so a restart can find any alarm left by an interrupted save.
    try save(AlarmState(primary: previous, additional: next))
    do {
      try await system.schedule(next)
    } catch {
      try? readInventory()
      throw error
    }

    isVerified = false
    guard (try? system.alarms().contains(where: { $0.id == next.id })) == true else {
      throw WakeAlarmError.notConfirmed
    }

    do {
      try save(AlarmState(primary: next, additional: previous))
    } catch {
      // Keep the previous alarm if the new primary could not be saved.
      try? system.cancel(id: next.id)
      try? readInventory()
      throw error
    }

    if let previous {
      // A failed cancellation leaves the previous ID recorded for reconciliation.
      try? system.cancel(id: previous.id)
    }
    try readInventory()
  }

  func removePrimary() throws {
    guard !busy else { throw WakeAlarmError.busy }
    try readInventory()
    guard let primary = state.primary else { return }
    try remove([primary.id])
  }

  func removeAdditionalAlarms() throws {
    guard !busy else { throw WakeAlarmError.busy }
    try readInventory()
    try remove(additionalAlarms.map(\.id))
  }

  private func remove(_ ids: [UUID]) throws {
    var cancellationError: (any Error)?
    for id in ids {
      do { try system.cancel(id: id) } catch { cancellationError = error }
    }
    try readInventory()
    if let cancellationError, inventory.contains(where: { ids.contains($0.id) }) {
      throw cancellationError
    }
  }

  private func readInventory() throws {
    isVerified = false
    let alarms = try system.alarms()
    let ids = Set(alarms.map(\.id))
    let confirmed = AlarmState(
      primary: state.primary.flatMap { ids.contains($0.id) ? $0 : nil },
      additional: state.additional.flatMap { ids.contains($0.id) ? $0 : nil }
    )
    if confirmed != state { try save(confirmed) }
    inventory = alarms
    isVerified = true
  }

  private func save(_ state: AlarmState) throws {
    try store.save(state)
    self.state = state
  }
}
