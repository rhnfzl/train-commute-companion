import Foundation

enum MCPError: LocalizedError, Equatable {
  case invalidEndpoint
  case invalidSettings
  case invalidToken
  case unauthorized
  case redirected
  case requestFailed(Int)
  case invalidResponse
  case toolFailed

  var errorDescription: String? {
    switch self {
    case .invalidEndpoint: "Enter the private HTTPS URL ending in /mcp."
    case .invalidSettings: "Enter a profile and a registered token name."
    case .invalidToken: "Enter the token registered for this URL and name."
    case .unauthorized: "The companion rejected the token."
    case .redirected: "The companion redirected the request. Check the HTTPS URL."
    case .requestFailed(let status): "The companion returned HTTP \(status)."
    case .invalidResponse: "The companion returned an unsupported response."
    case .toolFailed: "The companion could not read this profile. Check the profile ID or name."
    }
  }
}

struct MCPConnection: Codable, Equatable, Sendable {
  let id: UUID
  let endpoint: URL
  let profile: String
  let tokenName: String
  let token: String

  static func endpoint(from input: String) throws -> URL {
    guard let parts = URLComponents(string: input.trimmingCharacters(in: .whitespacesAndNewlines)),
      parts.scheme?.lowercased() == "https",
      let host = parts.host, !host.isEmpty,
      parts.user == nil, parts.password == nil,
      parts.query == nil, parts.fragment == nil,
      parts.percentEncodedPath == "/mcp",
      let url = parts.url
    else { throw MCPError.invalidEndpoint }
    return url
  }

  init(id: UUID = UUID(), endpoint: URL, profile: String, tokenName: String, token: String) throws {
    self.id = id
    self.endpoint = try Self.endpoint(from: endpoint.absoluteString)
    self.profile = profile.trimmingCharacters(in: .whitespacesAndNewlines)
    self.tokenName = tokenName.trimmingCharacters(in: .whitespacesAndNewlines)
    self.token = token
    guard !self.profile.isEmpty, (1...24).contains(self.tokenName.utf8.count),
      self.tokenName.utf8.allSatisfy({
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0)
          || $0 == 45 || $0 == 95
      })
    else { throw MCPError.invalidSettings }
    guard (32...128).contains(token.utf8.count), token.utf8.allSatisfy({ (33...126).contains($0) })
    else { throw MCPError.invalidToken }
  }
}

struct MCPClient: Sendable {
  private static let protocolVersion = "2025-11-25"

  private static let session: URLSession = {
    let config = URLSessionConfiguration.ephemeral
    config.httpCookieStorage = nil
    config.httpShouldSetCookies = false
    config.urlCache = nil
    config.urlCredentialStorage = nil
    config.requestCachePolicy = .reloadIgnoringLocalCacheData
    config.timeoutIntervalForRequest = 10
    config.timeoutIntervalForResource = 15
    return URLSession(configuration: config, delegate: RejectRedirects(), delegateQueue: nil)
  }()

  func status(for connection: MCPConnection) async throws -> CommuteStatus {
    let initialization = try await post(
      [
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": [
          "protocolVersion": Self.protocolVersion,
          "capabilities": [:],
          "clientInfo": ["name": "Train Commute Companion for iPhone", "version": "0.1.0"],
        ],
      ], to: connection)
    let version = try Self.decode(InitializeResult.self, from: initialization, id: 1)
      .protocolVersion
    guard version == Self.protocolVersion else { throw MCPError.invalidResponse }

    try Task.checkCancellation()
    _ = try await post(
      ["jsonrpc": "2.0", "method": "notifications/initialized"],
      to: connection, version: version, expectedStatus: 202
    )

    try Task.checkCancellation()
    let reply = try await post(
      [
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": ["name": "get_status", "arguments": ["profile": connection.profile]],
      ], to: connection, version: version)
    return try Self.decodeStatus(reply)
  }

  static func decodeStatus(_ data: Data) throws -> CommuteStatus {
    let result = try decode(ToolResult.self, from: data, id: 2)
    if result.isError == true { throw MCPError.toolFailed }
    guard let status = result.structuredContent else { throw MCPError.invalidResponse }
    return status
  }

  private func post(
    _ payload: [String: Any],
    to connection: MCPConnection,
    version: String? = nil,
    expectedStatus: Int = 200
  ) async throws -> Data {
    let endpoint = try MCPConnection.endpoint(from: connection.endpoint.absoluteString)
    var request = URLRequest(url: endpoint)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("application/json, text/event-stream", forHTTPHeaderField: "Accept")
    request.setValue("Bearer \(connection.token)", forHTTPHeaderField: "Authorization")
    request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
    if let version { request.setValue(version, forHTTPHeaderField: "MCP-Protocol-Version") }
    request.httpBody = try JSONSerialization.data(withJSONObject: payload)

    let (body, response) = try await Self.session.data(for: request)
    guard let http = response as? HTTPURLResponse else { throw MCPError.invalidResponse }
    if (300..<400).contains(http.statusCode) { throw MCPError.redirected }
    if http.statusCode == 401 || http.statusCode == 403 { throw MCPError.unauthorized }
    guard http.statusCode == expectedStatus else { throw MCPError.requestFailed(http.statusCode) }
    if expectedStatus == 200, http.mimeType?.lowercased() != "application/json" {
      throw MCPError.invalidResponse
    }
    return body
  }

  private static func decode<T: Decodable>(_ type: T.Type, from data: Data, id: Int) throws -> T {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .custom { decoder in
      let text = try decoder.singleValueContainer().decode(String.self)
      let formatter = ISO8601DateFormatter()
      formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
      if let date = formatter.date(from: text) { return date }
      formatter.formatOptions = [.withInternetDateTime]
      guard let date = formatter.date(from: text) else { throw MCPError.invalidResponse }
      return date
    }
    guard let envelope = try? decoder.decode(Envelope<T>.self, from: data),
      envelope.jsonrpc == "2.0", envelope.id == id,
      !envelope.hasError, let result = envelope.result
    else { throw MCPError.invalidResponse }
    return result
  }

  private struct InitializeResult: Decodable {
    let protocolVersion: String
  }

  private struct ToolResult: Decodable {
    let isError: Bool?
    let structuredContent: CommuteStatus?
  }

  private struct Envelope<T: Decodable>: Decodable {
    let jsonrpc: String
    let id: Int
    let result: T?
    let hasError: Bool

    enum CodingKeys: CodingKey { case jsonrpc, id, result, error }

    init(from decoder: any Decoder) throws {
      let values = try decoder.container(keyedBy: CodingKeys.self)
      jsonrpc = try values.decode(String.self, forKey: .jsonrpc)
      id = try values.decode(Int.self, forKey: .id)
      result = try values.decodeIfPresent(T.self, forKey: .result)
      hasError = values.contains(.error)
    }
  }
}

private final class RejectRedirects: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession,
    task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest,
    completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
