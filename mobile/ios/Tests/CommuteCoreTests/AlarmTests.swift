import Foundation

@MainActor
struct AlarmTests {
  func oldAlarmSurvivesScheduleFailure() async throws {
    let (coordinator, system, store, old) = fixture()
    system.failSchedule = true

    await expectAsyncError(FakeError.schedule) {
      try await coordinator.replace(on: Date().addingTimeInterval(3_600)) {}
    }

    expect(system.alarmsByID.keys.sorted() == [old.id])
    expect(store.state.primary == old)
    expect(system.operations == ["schedule"])
  }

  func successfulSchedulePrecedesCancelAndResidualSurvivesRestart() async throws {
    let (coordinator, system, store, old) = fixture()
    system.failCancellation = true

    try await coordinator.replace(on: Date().addingTimeInterval(3_600)) {}

    expect(system.operations == ["schedule", "cancel"])
    expect(store.state.primary?.id != old.id)
    let restarted = try AlarmCoordinator(system: system, store: store)
    try restarted.reconcile()
    expect(restarted.additionalAlarms.map(\.id) == [old.id])

    system.failCancellation = false
    try restarted.removeAdditionalAlarms()
    expect(restarted.additionalAlarms.isEmpty)
    expect(system.alarmsByID.count == 1)
    expect(store.state.primary == restarted.state.primary)
  }

  func staleConfirmationMakesNoOSChange() async throws {
    let (coordinator, system, store, old) = fixture()

    await expectAsyncError(FakeError.stale) {
      try await coordinator.replace(on: Date().addingTimeInterval(3_600)) {
        throw FakeError.stale
      }
    }

    expect(system.operations.isEmpty)
    expect(store.state.primary == old)
  }

  func oldAlarmSurvivesUnconfirmedSchedule() async throws {
    let (coordinator, system, store, old) = fixture()
    system.omitNewAlarm = true

    await expectAsyncError(WakeAlarmError.notConfirmed) {
      try await coordinator.replace(on: Date().addingTimeInterval(3_600)) {}
    }

    expect(system.operations == ["schedule"])
    expect(store.state.primary == old)
    expect(store.state.additional != nil)
  }

  func stoppedAlarmIsRemovedOnlyAfterSuccessfulInventoryRead() throws {
    let (coordinator, system, store, old) = fixture()
    system.alarmsByID.removeAll()
    system.failRead = true

    expectError(FakeError.read) { try coordinator.reconcile() }
    expect(store.state.primary == old)
    system.failRead = false
    try coordinator.reconcile()
    expect(store.state.primary == nil)
  }

  private func fixture() -> (AlarmCoordinator, TestAlarmSystem, TestAlarmStore, AlarmRecord) {
    let old = AlarmRecord(id: UUID(), time: Date().addingTimeInterval(1_800))
    let system = TestAlarmSystem()
    system.alarmsByID[old.id] = SystemAlarm(id: old.id, time: old.time, phase: .scheduled)
    let store = TestAlarmStore(state: AlarmState(primary: old))
    let coordinator = try! AlarmCoordinator(system: system, store: store)
    return (coordinator, system, store, old)
  }
}

private enum FakeError: Error { case schedule, cancel, read, stale }

@MainActor
private final class TestAlarmSystem: AlarmSystem {
  var alarmsByID: [UUID: SystemAlarm] = [:]
  var operations: [String] = []
  var failSchedule = false
  var omitNewAlarm = false
  var failCancellation = false
  var failRead = false

  func alarms() throws -> [SystemAlarm] {
    if failRead { throw FakeError.read }
    return Array(alarmsByID.values)
  }

  func schedule(_ alarm: AlarmRecord) async throws {
    operations.append("schedule")
    if failSchedule { throw FakeError.schedule }
    if omitNewAlarm { return }
    alarmsByID[alarm.id] = SystemAlarm(id: alarm.id, time: alarm.time, phase: .scheduled)
  }

  func cancel(id: UUID) throws {
    operations.append("cancel")
    if failCancellation { throw FakeError.cancel }
    alarmsByID.removeValue(forKey: id)
  }
}

@MainActor
private final class TestAlarmStore: AlarmStoring {
  var state: AlarmState

  init(state: AlarmState) { self.state = state }

  func load() throws -> AlarmState { state }

  func save(_ state: AlarmState) throws { self.state = state }
}
