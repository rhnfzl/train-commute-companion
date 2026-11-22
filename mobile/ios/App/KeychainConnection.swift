import Foundation
import Security

struct KeychainConnection {
  private var query: [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: Bundle.main.bundleIdentifier ?? "com.rhnfzl.TrainCommuteCompanion",
      kSecAttrAccount as String: "mcp-connection",
    ]
  }

  func load() throws -> MCPConnection? {
    var lookup = query
    lookup[kSecReturnData as String] = true
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    var item: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &item)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = item as? Data else { throw ConnectionStoreError() }
    let stored = try JSONDecoder().decode(MCPConnection.self, from: data)
    return try MCPConnection(
      id: stored.id,
      endpoint: stored.endpoint, profile: stored.profile, tokenName: stored.tokenName,
      token: stored.token
    )
  }

  func save(_ connection: MCPConnection) throws {
    let data = try JSONEncoder().encode(connection)
    let updated = SecItemUpdate(
      query as CFDictionary, [kSecValueData as String: data] as CFDictionary
    )
    if updated == errSecSuccess { return }
    guard updated == errSecItemNotFound else { throw ConnectionStoreError() }

    var item = query
    item[kSecValueData as String] = data
    item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
      throw ConnectionStoreError()
    }
  }

  func remove() throws {
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw ConnectionStoreError()
    }
  }
}

private struct ConnectionStoreError: LocalizedError {
  var errorDescription: String? { "The iPhone Keychain could not open or save the connection." }
}
