//
//  HelenSocketClient.swift
//  HelenApp
//
//  Real-time channel for Socket.IO events (chat fanout, presence,
//  call signalling). Uses URLSessionWebSocketTask directly rather than
//  the heavy Socket.IO Swift client — Helen-Server's events are simple
//  enough that a hand-rolled implementation keeps the dependency
//  surface small and lets us reuse the auth + tokens we already have.
//
//  Event format (matches Socket.IO's framing):
//      ["event_name", { ...payload... }]
//
//  Lifecycle:
//      let socket = HelenSocketClient(baseURL: ..., token: ...)
//      socket.delegate = ...
//      try await socket.connect()
//      try await socket.emit(event: "v2_chat_send_message", payload: [...])
//

import Foundation

public protocol HelenSocketDelegate: AnyObject {
    func socketDidConnect(_ socket: HelenSocketClient)
    func socketDidDisconnect(_ socket: HelenSocketClient, error: Error?)
    func socket(_ socket: HelenSocketClient, didReceiveEvent event: String, payload: Any)
}

public final class HelenSocketClient: NSObject {

    // MARK: - State

    public weak var delegate: HelenSocketDelegate?

    private let baseURL: URL
    private let token: String
    private var task: URLSessionWebSocketTask?
    private var session: URLSession!

    private var heartbeatTimer: Timer?
    private var connected = false
    private var explicitDisconnect = false

    // Reconnect with exponential backoff (capped — not infinite).
    private var reconnectAttempts = 0
    private let maxReconnectAttempts = 10
    private let baseReconnectDelay: TimeInterval = 1.0
    private let maxReconnectDelay:  TimeInterval = 30.0

    // MARK: - Init

    public init(baseURL: URL, token: String) {
        self.baseURL = baseURL
        self.token = token
        super.init()
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 15
        self.session = URLSession(configuration: cfg, delegate: nil, delegateQueue: .main)
    }

    deinit {
        explicitDisconnect = true
        heartbeatTimer?.invalidate()
        task?.cancel()
    }

    // MARK: - Public API

    public func connect() {
        guard !connected else { return }
        explicitDisconnect = false
        openSocket()
    }

    public func disconnect() {
        explicitDisconnect = true
        heartbeatTimer?.invalidate()
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        connected = false
    }

    public func emit(event: String, payload: [String: Any]) async throws {
        guard let task = task, connected else {
            throw NSError(domain: "HelenSocket", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Not connected"])
        }
        // Socket.IO v4 frame: 42<json-array> for "message" (event)
        let envelope: [Any] = [event, payload]
        let payloadData = try JSONSerialization.data(withJSONObject: envelope)
        guard let json = String(data: payloadData, encoding: .utf8) else {
            throw NSError(domain: "HelenSocket", code: 2, userInfo: nil)
        }
        let frame = "42\(json)"
        try await task.send(.string(frame))
    }

    // MARK: - Internals

    private func openSocket() {
        guard let wsURL = makeWebSocketURL() else {
            delegate?.socketDidDisconnect(self,
                error: NSError(domain: "HelenSocket", code: 3,
                               userInfo: [NSLocalizedDescriptionKey: "bad URL"]))
            return
        }
        var req = URLRequest(url: wsURL)
        // Socket.IO accepts `auth` via query string; we also send the
        // bearer in case the server's middleware checks Authorization.
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let t = session.webSocketTask(with: req)
        self.task = t
        t.resume()
        receive()
    }

    private func makeWebSocketURL() -> URL? {
        // ws(s)://host[:port]/socket.io/?EIO=4&transport=websocket&token=...
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        comps?.scheme = (baseURL.scheme == "https") ? "wss" : "ws"
        comps?.path = "/socket.io/"
        comps?.queryItems = [
            URLQueryItem(name: "EIO",       value: "4"),
            URLQueryItem(name: "transport", value: "websocket"),
            URLQueryItem(name: "token",     value: token),
        ]
        return comps?.url
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                self.handleDisconnect(error: err)
            case .success(let msg):
                switch msg {
                case .string(let s): self.handleFrame(s)
                case .data(let d):   if let s = String(data: d, encoding: .utf8) { self.handleFrame(s) }
                @unknown default:    break
                }
                self.receive()
            }
        }
    }

    /// Socket.IO frames begin with a numeric prefix: 0=open, 2=ping, 3=pong,
    /// 4=message, 40=connect, 42=event, 41=disconnect.
    private func handleFrame(_ frame: String) {
        guard let first = frame.first else { return }
        switch first {
        case "0":
            // engine.io OPEN — server tells us session info. We immediately
            // upgrade to the namespace by sending "40".
            sendRaw("40")
        case "2":
            // ping — respond pong.
            sendRaw("3")
        case "4":
            // message frame; second char tells us what kind.
            let rest = String(frame.dropFirst())
            guard let kind = rest.first else { return }
            let payload = String(rest.dropFirst())
            switch kind {
            case "0":
                // namespace connect ack
                connected = true
                reconnectAttempts = 0
                startHeartbeat()
                delegate?.socketDidConnect(self)
            case "1":
                // namespace disconnect
                handleDisconnect(error: nil)
            case "2":
                // event: "42<json>"
                handleEventPayload(payload)
            default:
                break
            }
        default: break
        }
    }

    private func handleEventPayload(_ json: String) {
        guard let data = json.data(using: .utf8),
              let arr = try? JSONSerialization.jsonObject(with: data) as? [Any],
              let event = arr.first as? String else { return }
        let body = arr.count > 1 ? arr[1] : [:]
        delegate?.socket(self, didReceiveEvent: event, payload: body)
    }

    private func sendRaw(_ s: String) {
        task?.send(.string(s)) { _ in }
    }

    private func startHeartbeat() {
        heartbeatTimer?.invalidate()
        heartbeatTimer = Timer.scheduledTimer(withTimeInterval: 25, repeats: true) {
            [weak self] _ in self?.sendRaw("2")  // engine.io ping
        }
    }

    private func handleDisconnect(error: Error?) {
        connected = false
        heartbeatTimer?.invalidate()
        delegate?.socketDidDisconnect(self, error: error)
        // Auto-reconnect unless caller asked us to stop.
        guard !explicitDisconnect else { return }
        guard reconnectAttempts < maxReconnectAttempts else { return }
        let delay = min(maxReconnectDelay,
                        baseReconnectDelay * pow(2.0, Double(reconnectAttempts)))
        reconnectAttempts += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.openSocket()
        }
    }
}
