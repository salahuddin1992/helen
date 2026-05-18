//
//  HelenSession.swift
//  HelenApp
//
//  Top-level observable state for the running session — wires the
//  REST client + Socket client + persistence + auto-discovery into
//  one object the UI binds to.
//

import Foundation
import SwiftUI
import Combine

@MainActor
public final class HelenSession: ObservableObject {

    // MARK: - Persistent state

    @Published public private(set) var serverURL: URL?
    @Published public private(set) var currentUser: HelenUser?
    @Published public private(set) var isAuthenticated = false
    @Published public private(set) var isConnected = false
    @Published public var lastError: String?

    // MARK: - Live data

    @Published public private(set) var channels: [HelenChannel] = []
    @Published public private(set) var messagesByChannel: [String: [HelenMessage]] = [:]
    @Published public private(set) var users: [HelenUser] = []

    // MARK: - Internals

    private var api: HelenAPIClient?
    private var socket: HelenSocketClient?
    private let discovery = ServerDiscovery()
    private let keychain = KeychainStore(service: "com.helen.mobile")
    private var socketDelegateProxy: SocketDelegateProxy?

    // MARK: - Init / restore

    public init() {}

    public func restoreFromDisk() async {
        // Try last-used URL + cached tokens. If both present, validate
        // and resume the session silently.
        if let url = discovery.getLastUsedURL() {
            self.serverURL = url
            let api = HelenAPIClient(baseURL: url)
            self.api = api
            if let access = keychain.get("access_token"),
               let refresh = keychain.get("refresh_token") {
                let tokens = AuthTokens(
                    accessToken: access, refreshToken: refresh,
                    tokenType: "bearer", expiresIn: 3600,
                )
                await api.setTokens(tokens)
                do {
                    let user = try await api.getMe()
                    self.currentUser = user
                    self.isAuthenticated = true
                    await connectSocket(token: access)
                } catch {
                    // Tokens may have expired — try refresh once.
                    do {
                        try await api.refresh()
                        let user = try await api.getMe()
                        self.currentUser = user
                        self.isAuthenticated = true
                    } catch {
                        // Hard fail — clear and force re-login.
                        keychain.delete("access_token")
                        keychain.delete("refresh_token")
                    }
                }
            }
        }
    }

    // MARK: - Server selection

    public func discoverServers() async -> [DiscoveredServer] {
        await discovery.scan()
    }

    public func chooseServer(_ url: URL) async throws {
        let probe = await discovery.probe(url)
        guard probe != nil else {
            throw HelenAPIError.networkFailure(
                underlying: NSError(domain: "Helen", code: 404,
                                    userInfo: [NSLocalizedDescriptionKey:
                                               "Server not reachable at \(url)"]))
        }
        await discovery.rememberLastUsed(url)
        self.serverURL = url
        self.api = HelenAPIClient(baseURL: url)
    }

    /// Drop the cached server so the user is sent back to ServerSelectView.
    /// Also signs out if currently authenticated.
    public func forgetServer() async {
        if isAuthenticated { await logout() }
        await discovery.forgetLastUsed()
        self.serverURL = nil
        self.api = nil
    }

    // MARK: - Auth

    public func register(username: String, password: String,
                         displayName: String) async throws {
        guard let api = api else { throw HelenAPIError.invalidURL }
        let res = try await api.register(username: username, password: password,
                                         displayName: displayName)
        await persistTokens(res.tokens)
        self.currentUser = res.user
        self.isAuthenticated = true
        await connectSocket(token: res.tokens.accessToken)
    }

    public func login(username: String, password: String) async throws {
        guard let api = api else { throw HelenAPIError.invalidURL }
        let res = try await api.login(username: username, password: password)
        await persistTokens(res.tokens)
        self.currentUser = res.user
        self.isAuthenticated = true
        await connectSocket(token: res.tokens.accessToken)
    }

    public func logout() async {
        socket?.disconnect()
        socket = nil
        if let api = api { try? await api.logout() }
        keychain.delete("access_token")
        keychain.delete("refresh_token")
        currentUser = nil
        isAuthenticated = false
        isConnected = false
        channels = []
        messagesByChannel = [:]
    }

    // MARK: - Channels

    public func reloadChannels() async {
        guard let api = api else { return }
        do { self.channels = try await api.listChannels() }
        catch { self.lastError = error.localizedDescription }
    }

    public func reloadUsers(search: String? = nil) async {
        guard let api = api else { return }
        do {
            let res = try await api.listUsers(search: search)
            self.users = res.users
        } catch {
            self.lastError = error.localizedDescription
        }
    }

    /// Find or create a 1-on-1 DM channel with the given user. The server
    /// returns the existing DM if one already exists, so this is idempotent.
    /// Side-effect: appends the channel to `channels` if it wasn't there.
    public func openOrCreateDM(with userId: String) async -> HelenChannel? {
        guard let api = api else { return nil }
        do {
            let channel = try await api.createChannel(
                name: "DM", type: "dm", memberIds: [userId],
            )
            if !channels.contains(where: { $0.id == channel.id }) {
                channels.append(channel)
            }
            return channel
        } catch {
            self.lastError = error.localizedDescription
            return nil
        }
    }

    public func loadMessages(channelId: String) async {
        guard let api = api else { return }
        do {
            let m = try await api.listMessages(in: channelId)
            self.messagesByChannel[channelId] = m
        } catch {
            self.lastError = error.localizedDescription
        }
    }

    public func sendMessage(channelId: String, content: String) async {
        guard let api = api, !content.isEmpty else { return }
        // Prefer socket emit so the server fan-outs to the channel
        // members in real time. Falls back to REST POST if the socket
        // is down.
        if let socket = socket, isConnected {
            try? await socket.emit(event: "v2_chat_send_message", payload: [
                "channel_id":         channelId,
                "content":            content,
                "type":               "text",
                "client_message_id":  UUID().uuidString,
            ])
        } else {
            do {
                let m = try await api.sendMessage(in: channelId, content: content)
                var arr = self.messagesByChannel[channelId] ?? []
                arr.append(m)
                self.messagesByChannel[channelId] = arr
            } catch {
                self.lastError = error.localizedDescription
            }
        }
    }

    // MARK: - Socket

    private func connectSocket(token: String) async {
        guard let url = serverURL else { return }
        let s = HelenSocketClient(baseURL: url, token: token)
        let proxy = SocketDelegateProxy(session: self)
        s.delegate = proxy
        self.socketDelegateProxy = proxy
        self.socket = s
        s.connect()
    }

    fileprivate func socketDidChangeState(_ connected: Bool) {
        Task { @MainActor in self.isConnected = connected }
    }

    fileprivate func socketEventReceived(_ event: String, payload: Any) {
        Task { @MainActor in
            switch event {
            case "v2_chat:new_message", "chat:new_message":
                guard let dict = payload as? [String: Any],
                      let cid = dict["channel_id"] as? String else { return }
                if var arr = self.messagesByChannel[cid] {
                    if let m = self.parseMessage(dict) { arr.append(m); self.messagesByChannel[cid] = arr }
                } else {
                    if let m = self.parseMessage(dict) { self.messagesByChannel[cid] = [m] }
                }
            default:
                break
            }
        }
    }

    private func parseMessage(_ dict: [String: Any]) -> HelenMessage? {
        guard let id = dict["id"] as? String,
              let senderId = dict["sender_id"] as? String,
              let content = dict["content"] as? String else { return nil }
        return HelenMessage(
            id: id,
            channelId: dict["channel_id"] as? String,
            senderId: senderId,
            content: content,
            type: (dict["type"] as? String) ?? "text",
            createdAt: dict["created_at"] as? String,
        )
    }

    // MARK: - Token persistence

    private func persistTokens(_ tokens: AuthTokens) async {
        keychain.set(tokens.accessToken,  forKey: "access_token")
        keychain.set(tokens.refreshToken, forKey: "refresh_token")
    }
}

// Bridges the (non-MainActor) socket callbacks back onto the actor.
private final class SocketDelegateProxy: HelenSocketDelegate {
    weak var session: HelenSession?
    init(session: HelenSession) { self.session = session }
    func socketDidConnect(_: HelenSocketClient) { session?.socketDidChangeState(true) }
    func socketDidDisconnect(_: HelenSocketClient, error _: Error?) { session?.socketDidChangeState(false) }
    func socket(_: HelenSocketClient, didReceiveEvent event: String, payload: Any) {
        session?.socketEventReceived(event, payload: payload)
    }
}

// MARK: - Tiny Keychain wrapper (avoids pulling in a third-party dep)

import Security

final class KeychainStore {
    let service: String
    init(service: String) { self.service = service }

    func set(_ value: String, forKey key: String) {
        guard let data = value.data(using: .utf8) else { return }
        delete(key)
        let q: [String: Any] = [
            kSecClass as String:        kSecClassGenericPassword,
            kSecAttrService as String:  service,
            kSecAttrAccount as String:  key,
            kSecValueData as String:    data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        SecItemAdd(q as CFDictionary, nil)
    }

    func get(_ key: String) -> String? {
        let q: [String: Any] = [
            kSecClass as String:       kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecMatchLimit as String:  kSecMatchLimitOne,
            kSecReturnData as String:  true,
        ]
        var ref: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &ref) == errSecSuccess,
              let data = ref as? Data, let s = String(data: data, encoding: .utf8) else {
            return nil
        }
        return s
    }

    func delete(_ key: String) {
        let q: [String: Any] = [
            kSecClass as String:       kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(q as CFDictionary)
    }
}
