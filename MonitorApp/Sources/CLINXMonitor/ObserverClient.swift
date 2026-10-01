import Foundation
import Security

public enum MonitorError: Error, Equatable {
    case invalidEndpoint, credentialUnavailable, invalidResponse, incompatibleSchema
    case server(Int)
}

public protocol ObserverServing: Sendable {
    func health() async throws -> ObserverHealth
    func tasks(active: Bool, offset: Int) async throws -> TaskPage
    func task(_ ref: String) async throws -> ObservedTask
    func events(_ ref: String, after: String?) async throws -> EventPage
}

public enum ObserverKeychain {
    private static let service = "com.pvxlabs.clinx.monitor.observer"
    private static let account = "p620-observer"
    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service, kSecAttrAccount as String: account,
         kSecAttrSynchronizable as String: false]
    }
    public static func read() throws -> String {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data, let value = String(data: data, encoding: .utf8)
        else { throw MonitorError.credentialUnavailable }
        return value
    }
    public static func replace(_ credential: String) throws {
        guard credential.range(of: "^[A-Za-z0-9_-]{32,256}$", options: .regularExpression) != nil
        else { throw MonitorError.credentialUnavailable }
        let attributes: [String: Any] = [
            kSecValueData as String: Data(credential.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        ]
        let updated = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updated == errSecItemNotFound {
            let added = query.merging(attributes) { _, new in new }
            guard SecItemAdd(added as CFDictionary, nil) == errSecSuccess
            else { throw MonitorError.credentialUnavailable }
        } else if updated != errSecSuccess {
            throw MonitorError.credentialUnavailable
        }
    }
    public static func revokeLocal() throws {
        let result = SecItemDelete(query as CFDictionary)
        guard result == errSecSuccess || result == errSecItemNotFound
        else { throw MonitorError.credentialUnavailable }
    }
}

// Reject redirects so Authorization is never forwarded to another endpoint.
private final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public actor ObserverClient: ObserverServing {
    private let baseURL: URL
    private let session: URLSession
    private let credential: @Sendable () throws -> String
    public init(baseURL: URL) throws {
        try self.init(baseURL: baseURL, session: Self.makeSession(), credential: ObserverKeychain.read)
    }
    // Internal injection permits transport tests without a real endpoint or credential.
    init(baseURL: URL, session: URLSession, credential: @escaping @Sendable () throws -> String) throws {
        guard baseURL.scheme == "https", baseURL.host != nil,
              baseURL.user == nil, baseURL.password == nil,
              baseURL.query == nil, baseURL.fragment == nil,
              (baseURL.path.isEmpty || baseURL.path == "/")
        else { throw MonitorError.invalidEndpoint }
        self.baseURL = baseURL
        self.session = session
        self.credential = credential
    }
    private static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        // The private Observer must use the Tailnet route, not a system web proxy.
        configuration.connectionProxyDictionary = [:]
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = 15
        configuration.waitsForConnectivity = false
        configuration.httpMaximumConnectionsPerHost = 1
        return URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }
    private func get<T: Decodable & Sendable>(_ path: String,
                                              query: [URLQueryItem] = []) async throws -> T {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        else { throw MonitorError.invalidEndpoint }
        components.path = "/" + path
        if !query.isEmpty { components.queryItems = query }
        guard let url = components.url else { throw MonitorError.invalidEndpoint }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer " + (try credential()), forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Stream a bounded response; data(for:) could allocate an unbounded hostile response.
        let (bytes, response) = try await session.bytes(for: request)
        guard let response = response as? HTTPURLResponse else { throw MonitorError.invalidResponse }
        guard response.statusCode == 200 else { throw MonitorError.server(response.statusCode) }
        guard response.mimeType == "application/json" else { throw MonitorError.invalidResponse }
        var data = Data()
        for try await byte in bytes {
            guard data.count < 2 * 1024 * 1024 else { throw MonitorError.invalidResponse }
            data.append(byte)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do { return try decoder.decode(T.self, from: data) }
        catch { throw MonitorError.invalidResponse }
    }
    public func health() async throws -> ObserverHealth {
        let value: ObserverHealth = try await get("v1/health")
        guard value.schemaVersion == "1", value.readOnly else { throw MonitorError.incompatibleSchema }
        return value
    }
    public func tasks(active: Bool, offset: Int = 0) async throws -> TaskPage {
        guard (0...100000).contains(offset) else { throw MonitorError.invalidEndpoint }
        let value: TaskPage = try await get("v1/tasks", query: [
            URLQueryItem(name: "state", value: active ? "active" : "recent"),
            URLQueryItem(name: "offset", value: String(offset))])
        guard value.schemaVersion == "1", value.items.allSatisfy({
            $0.schemaVersion == "1" && $0.mutationBoundary.observerReadOnly &&
            $0.mutationBoundary.allowedActions.isEmpty
        }) else { throw MonitorError.incompatibleSchema }
        return value
    }
    private func checkedRef(_ ref: String) throws -> String {
        guard ref.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil
        else { throw MonitorError.invalidEndpoint }
        return ref
    }
    public func task(_ ref: String) async throws -> ObservedTask {
        let value: ObservedTask = try await get("v1/tasks/" + checkedRef(ref))
        guard value.schemaVersion == "1", value.mutationBoundary.observerReadOnly,
              value.mutationBoundary.allowedActions.isEmpty else { throw MonitorError.incompatibleSchema }
        return value
    }
    public func events(_ ref: String, after: String? = nil) async throws -> EventPage {
        if let after, after.range(of: "^[a-f0-9]{16}\\.[0-9]{1,20}$", options: .regularExpression) == nil {
            throw MonitorError.invalidEndpoint
        }
        let value: EventPage = try await get("v1/tasks/" + checkedRef(ref) + "/events",
            query: after.map { [URLQueryItem(name: "after", value: $0)] } ?? [])
        guard value.schemaVersion == "1", value.taskRef == ref else { throw MonitorError.incompatibleSchema }
        return value
    }
}
