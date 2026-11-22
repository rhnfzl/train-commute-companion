import Foundation

struct JourneyTests {
  private let now = Date(timeIntervalSince1970: 1_784_332_800)

  func readsStructuredStatusAndOffsetDates() throws {
    let status = try decodeStatus()
    let journey = require(status.journeys.first)

    expect(status.text == "Your train is on time.")
    expect(status.asOf == now)
    expect(journey.timetableLastOKAt == now.addingTimeInterval(-60))
    expect(journey.leaveBy == now.addingTimeInterval(3_600))
    expect(journey.legs.last?.estimatedArrival == now.addingTimeInterval(7_200))
  }

  func acceptsOnlyFreshUsableJourneyForWakeAlarm() throws {
    let status = try decodeStatus()
    let journey = require(status.journeys.first)
    let proposal = require(status.wakeProposal(for: journey.id, leadMinutes: 30, now: now))

    expect(proposal.alarmTime == now.addingTimeInterval(1_800))
    expect(status.confirms(proposal, now: now))

    let ageLimit = now.addingTimeInterval(240)
    expect(status.wakeProposal(for: journey.id, leadMinutes: 30, now: ageLimit) == nil)
    expect(status.wakeProposal(for: journey.id, leadMinutes: 90, now: now) == nil)
    try expect(!decodeStatus(health: "degraded").confirms(proposal, now: now))
    try expect(!decodeStatus(legState: "cancelled").confirms(proposal, now: now))
    try expect(!decodeStatus(state: "recovery_active").confirms(proposal, now: now))
    try expect(!decodeStatus(notTravelling: true).confirms(proposal, now: now))
    try expect(!decodeStatus(asOf: "2026-07-19T02:00:00+02:00").confirms(proposal, now: now))
    try expect(!decodeStatus(leaveBy: "2026-07-19T03:05:00+02:00").confirms(proposal, now: now))
    try expect(!decodeStatus(profile: "different").confirms(proposal, now: now))
  }

  func checksTransfersBeforeOfferingWakeAndTripActions() throws {
    let direct = try decodeStatus()
    let id = require(direct.journeys.first).id
    let unchecked = try decodeStatus(secondLeg: true)
    let broken = try decodeStatus(secondLeg: true, transfer: "broken")
    let valid = try decodeStatus(secondLeg: true, transfer: "ok")
    for status in [unchecked, broken] {
      expect(status.wakeProposal(for: id, leadMinutes: 30, now: now) == nil)
      expect(status.journeys.first?.estimatedArrival == nil)
    }
    expect(valid.wakeProposal(for: id, leadMinutes: 30, now: now) != nil)
    expect(valid.journeys.first?.estimatedArrival != nil)
  }

  func refusesMCPErrorOrMismatchedID() throws {
    expectError(MCPError.toolFailed) {
      _ = try MCPClient.decodeStatus(
        Data(#"{"jsonrpc":"2.0","id":2,"result":{"isError":true}}"#.utf8)
      )
    }
    expectError(MCPError.invalidResponse) {
      _ = try MCPClient.decodeStatus(
        Data(#"{"jsonrpc":"2.0","id":3,"result":{}}"#.utf8)
      )
    }
  }

  func connectionRejectsCredentialURLsAndNonHTTPS() throws {
    try expect(MCPConnection.endpoint(from: "https://commute.example.invalid/mcp").path == "/mcp")
    for invalid in [
      "http://commute.example.invalid/mcp",
      "https://commute.example.invalid/mcp/",
      "https://commute.example.invalid/mcp?redirect=true",
      "https://commute.example.invalid/mcp#fragment",
      "https://operator@commute.example.invalid/mcp",
      "https://commute.example.invalid/%6Dcp",
    ] {
      expectError(MCPError.invalidEndpoint) {
        _ = try MCPConnection.endpoint(from: invalid)
      }
    }
  }

  func decodeStatus(
    health: String = "healthy",
    state: String = "locked",
    legState: String = "ok",
    notTravelling: Bool = false,
    asOf: String = "2026-07-18T02:00:00.000000+02:00",
    leaveBy: String = "2026-07-18T03:00:00+02:00",
    profile: String = "work",
    secondLeg: Bool = false,
    transfer: String? = nil
  ) throws -> CommuteStatus {
    let onward =
      secondLeg
      ? #", {"state": "ok", "progress": "pending", "planned_arrival": "2026-07-18T04:15:00+02:00"}"#
      : ""
    let transfers = transfer.map { ", \"transfers\": [{\"state\": \"\($0)\"}]" } ?? ""
    let json = """
      {
        "jsonrpc": "2.0", "id": 2,
        "result": {
          "isError": false,
          "content": [{"type": "text", "text": "Non-structured body"}],
          "structuredContent": {
            "profile": "\(profile)", "name": "Work", "text": "Your train is on time.",
            "as_of": "\(asOf)",
            "journeys": [{
              "direction": "outbound", "service_date": "2026-07-18",
              "state": "\(state)", "not_travelling": \(notTravelling),
              "timetable_last_ok_at": "2026-07-18T01:59:00+02:00",
              "timetable_health": "\(health)", "leave_by": "\(leaveBy)",
              "legs": [{
                "state": "\(legState)", "progress": "pending",
                "planned_arrival": "2026-07-18T04:00:00+02:00", "arrival": null
              }\(onward)]\(transfers)
            }]
          }
        }
      }
      """
    return try MCPClient.decodeStatus(Data(json.utf8))
  }
}
