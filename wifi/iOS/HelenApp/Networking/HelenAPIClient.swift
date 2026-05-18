//
//  HelenAPIClient.swift
//  HelenApp
//
//  REST client that talks to Helen-Server. Server-agnostic: the same
//  client works against the Windows .exe, the Linux Docker image, or
//  any future packaging — the wire protocol is plain HTTP + JSON.
//
//  Usage:
//      let api = HelenAPIClient(baseURL: URL(string: "http://192.168.1.50:3000")!)
//      let me = try await api.getMe()
//

import Foundation

/// Errors the API surface can raise. `.serverError` carries the parsed
/// `{"detail": "..."}` body when the server returned one.
public enum HelenAPIError: Error, LocalizedError {
    case invalidURL
    case notAuthenticated
    case serverError(status: Int, detail: String)
    case decodingFailed(underlying: Error)
    case networkFailure(underlying: Error)

    public var errorDescription: String? {
        switch self {
        case .invalidURL:                 return "Invalid server URL"
        case .notAuthenticated:           return "Not signed in"
        case .serverError(let s, let d):  return "Server \(s): \(d)"
        case .decodingFailed(let e):      return "Decoding error: \(e)"
        case .networkFailure(let e):      return "Network error: \(e)"
        }
    }
}

/// Token pair returned by `/api/auth/login` and `/api/auth/register`.
public struct AuthTokens: Codable, Equatable {
    public let accessToken: String
    public let refreshToken: String
    public let tokenType: String
    public let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case accessToken  = "access_token"
        case refreshToken = "refresh_token"
        case tokenType    = "token_type"
        case expiresIn    = "expires_in"
    }
}

/// Server's representation of a user.
public struct HelenUser: Codable, Identifiable, Equatable {
    public let id: String
    public let username: String
    public let displayName: String?
    public let avatarUrl: String?
    public let shareCode: String?
    public let status: String?

    enum CodingKeys: String, CodingKey {
        case id, username, status
        case displayName = "display_name"
        case avatarUrl   = "avatar_url"
        case shareCode   = "share_code"
    }
}

public struct AuthResponse: Codable {
    public let user: HelenUser
    public let tokens: AuthTokens
}

/// Channel returned by `/api/channels` endpoints.
public struct HelenChannel: Codable, Identifiable, Equatable {
    public let id: String
    public let name: String
    public let type: String
    public let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name, type
        case createdAt = "created_at"
    }
}

public struct HelenMessage: Codable, Identifiable, Equatable {
    public let id: String
    public let channelId: String?
    public let senderId: String
    public let content: String
    public let type: String
    public let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, content, type
        case channelId = "channel_id"
        case senderId  = "sender_id"
        case createdAt = "created_at"
    }
}

/// `/api/turn/ice-config` payload — what the renderer feeds to RTCPeerConnection.
public struct ICEConfig: Codable {
    public let iceServers: [ICEServer]
    public let iceTransportPolicy: String?
    public let ttlSeconds: Int?

    public struct ICEServer: Codable {
        public let urls: [String]
        public let username: String?
        public let credential: String?
    }

    enum CodingKeys: String, CodingKey {
        case iceServers         = "ice_servers"
        case iceTransportPolicy = "ice_transport_policy"
        case ttlSeconds         = "ttl_seconds"
    }
}


public actor HelenAPIClient {

    // MARK: - State

    private(set) public var baseURL: URL
    private var accessToken: String?
    private var refreshToken: String?

    private let session: URLSession
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        // Server uses snake_case; the per-type CodingKeys handle it
        // explicitly so we don't need a global key strategy.
        return d
    }()
    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        // Server's pydantic models accept either case; emit camelCase
        // and rely on per-type CodingKeys mapping when needed.
        return e
    }()

    // MARK: - Init

    public init(baseURL: URL,
                session: URLSession? = nil) {
        self.baseURL = baseURL
        self.session = session ?? Self.makeSession()
    }

    private static func makeSession() -> URLSession {
        let cfg = URLSessionConfiguration.default
        cfg.waitsForConnectivity = false
        cfg.timeoutIntervalForRequest = 15
        cfg.timeoutIntervalForResource = 60
        cfg.httpAdditionalHeaders = [
            "User-Agent": "HelenMobile-iOS/1.0",
        ]
        return URLSession(configuration: cfg)
    }

    /// Switch to a different server URL at runtime — used after onboarding.
    public func setBaseURL(_ url: URL) {
        self.baseURL = url
    }

    // MARK: - Token storage

    public func setTokens(_ tokens: AuthTokens) {
        self.accessToken = tokens.accessToken
        self.refreshToken = tokens.refreshToken
    }

    public func clearTokens() {
        self.accessToken = nil
        self.refreshToken = nil
    }

    public var isAuthenticated: Bool {
        accessToken != nil
    }

    // MARK: - Auth

    public func register(username: String, password: String,
                         displayName: String) async throws -> AuthResponse {
        let body: [String: Any] = [
            "username":     username,
            "password":     password,
            "display_name": displayName,
        ]
        let res: AuthResponse = try await post("/api/auth/register",
                                               body: body, authed: false)
        setTokens(res.tokens)
        return res
    }

    public func login(username: String, password: String) async throws -> AuthResponse {
        let body: [String: Any] = ["username": username, "password": password]
        let res: AuthResponse = try await post("/api/auth/login",
                                               body: body, authed: false)
        setTokens(res.tokens)
        return res
    }

    public func logout() async throws {
        let _: EmptyResponse = try await post("/api/auth/logout",
                                              body: [:], authed: true)
        clearTokens()
    }

    public func refresh() async throws {
        guard let rt = refreshToken else { throw HelenAPIError.notAuthenticated }
        let body: [String: Any] = ["refresh_token": rt]
        let tokens: AuthTokens = try await post("/api/auth/refresh",
                                                body: body, authed: false)
        setTokens(tokens)
    }

    public func getMe() async throws -> HelenUser {
        try await get("/api/users/me", authed: true)
    }

    // MARK: - Users / contacts

    public struct UserListResponse: Codable {
        public let users: [HelenUser]
        public let total: Int
    }

    public func listUsers(skip: Int = 0, limit: Int = 100,
                          search: String? = nil) async throws -> UserListResponse {
        var path = "/api/users?skip=\(skip)&limit=\(limit)"
        if let s = search?.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
           !s.isEmpty {
            path += "&search=\(s)"
        }
        return try await get(path, authed: true)
    }

    public func getUser(id: String) async throws -> HelenUser {
        try await get("/api/users/\(id)", authed: true)
    }

    // MARK: - Channels + messages

    public func listChannels() async throws -> [HelenChannel] {
        try await get("/api/channels", authed: true)
    }

    public func createChannel(name: String, type: String = "group",
                              memberIds: [String]) async throws -> HelenChannel {
        let body: [String: Any] = [
            "name":       name,
            "type":       type,
            "member_ids": memberIds,
        ]
        return try await post("/api/channels", body: body, authed: true)
    }

    public func listMessages(in channelId: String,
                             limit: Int = 50) async throws -> [HelenMessage] {
        struct Wrapper: Codable { let messages: [HelenMessage] }
        // Some builds return {messages: [...]}, others a bare array.
        // Try the wrapper first; fall back to array.
        let req = try makeRequest(path: "/api/channels/\(channelId)/messages?limit=\(limit)",
                                  method: "GET", body: nil, authed: true)
        let (data, response) = try await sendRequest(req)
        try check(response: response, data: data)
        if let w = try? decoder.decode(Wrapper.self, from: data) {
            return w.messages
        }
        return try decoder.decode([HelenMessage].self, from: data)
    }

    public func sendMessage(in channelId: String,
                            content: String,
                            type: String = "text") async throws -> HelenMessage {
        let body: [String: Any] = ["content": content, "type": type]
        return try await post("/api/channels/\(channelId)/messages",
                              body: body, authed: true)
    }

    // MARK: - Calls / WebRTC

    public func getICEConfig() async throws -> ICEConfig {
        try await get("/api/turn/ice-config", authed: true)
    }

    // MARK: - Health / discovery

    public struct Health: Codable {
        public let status: String
        public let service: String
        public let version: String
    }

    public func health() async throws -> Health {
        try await get("/api/health", authed: false)
    }

    // MARK: - HTTP plumbing

    private func get<T: Codable>(_ path: String, authed: Bool) async throws -> T {
        let req = try makeRequest(path: path, method: "GET", body: nil, authed: authed)
        let (data, response) = try await sendRequest(req)
        try check(response: response, data: data)
        do { return try decoder.decode(T.self, from: data) }
        catch { throw HelenAPIError.decodingFailed(underlying: error) }
    }

    private func post<T: Codable>(_ path: String,
                                  body: [String: Any],
                                  authed: Bool) async throws -> T {
        let bodyData = try JSONSerialization.data(withJSONObject: body)
        let req = try makeRequest(path: path, method: "POST", body: bodyData, authed: authed)
        let (data, response) = try await sendRequest(req)
        try check(response: response, data: data)
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do { return try decoder.decode(T.self, from: data) }
        catch { throw HelenAPIError.decodingFailed(underlying: error) }
    }

    private func makeRequest(path: String, method: String,
                             body: Data?, authed: Bool) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw HelenAPIError.invalidURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if authed {
            guard let t = accessToken else { throw HelenAPIError.notAuthenticated }
            req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization")
        }
        if let body = body {
            req.httpBody = body
        }
        return req
    }

    private func sendRequest(_ req: URLRequest) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(for: req)
        } catch {
            throw HelenAPIError.networkFailure(underlying: error)
        }
    }

    private func check(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            // Try to surface server's {"detail": "..."} message.
            var detail = "HTTP \(http.statusCode)"
            if let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let d = body["detail"] as? String {
                detail = d
            } else if let s = String(data: data, encoding: .utf8) {
                detail = s
            }
            throw HelenAPIError.serverError(status: http.statusCode, detail: detail)
        }
    }
}

private struct EmptyResponse: Codable {}
