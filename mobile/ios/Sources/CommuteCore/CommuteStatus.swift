import Foundation

struct CommuteStatus: Decodable, Sendable {
  let profile: String
  let name: String
  let text: String
  let asOf: Date
  let journeys: [CommuteJourney]

  // The companion considers the timetable fresh for five minutes after its last successful poll.
  static let freshnessWindow: TimeInterval = 5 * 60
  private static let clockTolerance: TimeInterval = 30

  func freshUntil(for journey: CommuteJourney, now: Date) -> Date? {
    guard journey.timetableHealth == "healthy",
      let checked = journey.timetableLastOKAt,
      checked <= asOf.addingTimeInterval(Self.clockTolerance),
      max(asOf, checked) <= now.addingTimeInterval(Self.clockTolerance)
    else { return nil }

    let expires = min(asOf, checked).addingTimeInterval(Self.freshnessWindow)
    return expires > now ? expires : nil
  }

  func wakeProposal(for id: CommuteJourney.ID, leadMinutes: Int, now: Date) -> WakeProposal? {
    let matching = journeys.filter { $0.id == id }
    guard let journey = matching.only,
      ["idle", "locked"].contains(journey.state),
      journey.isUsable,
      journey.legs.allSatisfy({ ["pending", "boarding"].contains($0.progress) }),
      freshUntil(for: journey, now: now) != nil,
      let leaveBy = journey.leaveBy,
      (0...180).contains(leadMinutes)
    else { return nil }

    let alarmTime = leaveBy.addingTimeInterval(-Double(leadMinutes) * 60)
    guard alarmTime > now else { return nil }
    return WakeProposal(
      profile: profile, journey: id, leaveBy: leaveBy, leadMinutes: leadMinutes,
      alarmTime: alarmTime
    )
  }

  func confirms(_ proposal: WakeProposal, now: Date) -> Bool {
    wakeProposal(for: proposal.journey, leadMinutes: proposal.leadMinutes, now: now) == proposal
  }

  enum CodingKeys: String, CodingKey {
    case profile, name, text, journeys
    case asOf = "as_of"
  }
}

struct CommuteJourney: Decodable, Identifiable, Sendable {
  struct ID: Hashable, Sendable {
    let direction: String
    let serviceDate: String
  }

  let direction: String
  let serviceDate: String
  let state: String
  let notTravelling: Bool
  let timetableLastOKAt: Date?
  let timetableHealth: String
  let leaveBy: Date?
  let legs: [CommuteLeg]
  let transfers: [CommuteTransfer]?
  let arrivalTargetStatus: String?

  var id: ID { ID(direction: direction, serviceDate: serviceDate) }
  var title: String { "\(direction.capitalized) · \(serviceDate)" }

  var isUsable: Bool {
    ["outbound", "return"].contains(direction)
      && !notTravelling && !legs.isEmpty
      && arrivalTargetStatus != "no_train"
      && legs.allSatisfy { ["ok", "delayed"].contains($0.state) }
      && (transfers?.count ?? 0) == legs.count - 1
      && (transfers?.allSatisfy { $0.state == "ok" } ?? true)
  }

  var estimatedArrival: Date? {
    guard ["idle", "locked", "riding"].contains(state), isUsable,
      legs.allSatisfy({ ["pending", "boarding", "riding", "done"].contains($0.progress) })
    else { return nil }
    return legs.last?.estimatedArrival
  }

  enum CodingKeys: String, CodingKey {
    case direction, state, legs, transfers
    case serviceDate = "service_date"
    case notTravelling = "not_travelling"
    case timetableLastOKAt = "timetable_last_ok_at"
    case timetableHealth = "timetable_health"
    case leaveBy = "leave_by"
    case arrivalTargetStatus = "arrival_target_status"
  }
}

struct CommuteTransfer: Decodable, Sendable {
  let state: String
}

struct CommuteLeg: Decodable, Sendable {
  let state: String
  let progress: String
  let plannedArrival: Date?
  let arrival: Date?

  var estimatedArrival: Date? { arrival ?? plannedArrival }

  enum CodingKeys: String, CodingKey {
    case state, progress, arrival
    case plannedArrival = "planned_arrival"
  }
}

struct WakeProposal: Equatable, Sendable {
  let profile: String
  let journey: CommuteJourney.ID
  let leaveBy: Date
  let leadMinutes: Int
  let alarmTime: Date
}

extension Array {
  fileprivate var only: Element? { count == 1 ? first : nil }
}
