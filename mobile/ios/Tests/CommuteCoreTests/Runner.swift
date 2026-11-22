import Foundation

@main
struct Runner {
  @MainActor
  static func main() async throws {
    let journeys = JourneyTests()
    try journeys.readsStructuredStatusAndOffsetDates()
    try journeys.acceptsOnlyFreshUsableJourneyForWakeAlarm()
    try journeys.checksTransfersBeforeOfferingWakeAndTripActions()
    try journeys.refusesMCPErrorOrMismatchedID()
    try journeys.connectionRejectsCredentialURLsAndNonHTTPS()

    let alarms = AlarmTests()
    try await alarms.oldAlarmSurvivesScheduleFailure()
    try await alarms.successfulSchedulePrecedesCancelAndResidualSurvivesRestart()
    try await alarms.staleConfirmationMakesNoOSChange()
    try await alarms.oldAlarmSurvivesUnconfirmedSchedule()
    try alarms.stoppedAlarmIsRemovedOnlyAfterSuccessfulInventoryRead()
    print("10 checks passed")
  }
}

func expect(
  _ condition: @autoclosure () throws -> Bool,
  file: StaticString = #file,
  line: UInt = #line
) rethrows {
  if try !condition() { fatalError("Check failed", file: file, line: line) }
}

func require<T>(_ value: T?, file: StaticString = #file, line: UInt = #line) -> T {
  guard let value else { fatalError("Expected a value", file: file, line: line) }
  return value
}

func expectError<E: Error & Equatable>(
  _ expected: E,
  _ action: () throws -> Void,
  file: StaticString = #file,
  line: UInt = #line
) {
  do {
    try action()
    fatalError("Expected \(expected)", file: file, line: line)
  } catch {
    expect(error as? E == expected, file: file, line: line)
  }
}

@MainActor
func expectAsyncError<E: Error & Equatable>(
  _ expected: E,
  _ action: @MainActor () async throws -> Void,
  file: StaticString = #file,
  line: UInt = #line
) async {
  do {
    try await action()
    fatalError("Expected \(expected)", file: file, line: line)
  } catch {
    expect(error as? E == expected, file: file, line: line)
  }
}
