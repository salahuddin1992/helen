//
//  ServerDiscovery.swift
//  HelenApp
//
//  Find a Helen-Server on the local network without typing an IP.
//
//  Discovery strategy (in order, first hit wins):
//    1. Last-used URL from UserDefaults — instant.
//    2. Bonjour / mDNS query for `_helen-server._tcp.` services on
//       the LAN. Requires NSLocalNetworkUsageDescription in Info.plist
//       (already declared) and the user accepting the LAN permission
//       prompt that iOS 14+ shows on first scan.
//    3. HTTP probe of common gateway IPs (192.168.{0,1}.{1,100}).
//
//  Result is a list of reachable candidates sorted by latency.
//

import Foundation
import Network

public struct DiscoveredServer: Identifiable, Equatable {
    public let id: UUID = UUID()
    public let url: URL
    public let host: String
    public let port: Int
    public let latencyMs: Double
    public let source: Source
    public let metadata: [String: String]

    public enum Source: String { case lastUsed, bonjour, lanProbe, manual }
}

public actor ServerDiscovery {

    private let lastUsedKey = "helen.lastServerURL"
    private var browser: NWBrowser?

    public init() {}

    // MARK: - Public API

    /// Run all probes in parallel and yield the first reachable server,
    /// continuing in the background to populate the rest.
    public func scan(timeout seconds: TimeInterval = 5) async -> [DiscoveredServer] {
        var found: [DiscoveredServer] = []

        // 1. Last-used URL — instant.
        if let last = getLastUsedURL(), let r = await probe(last, source: .lastUsed) {
            found.append(r)
        }

        // 2/3 in parallel
        async let bonjour = bonjourScan(timeout: seconds)
        async let probes  = lanProbeScan(timeout: seconds)
        let (b, p) = await (bonjour, probes)
        found.append(contentsOf: b)
        found.append(contentsOf: p)

        // De-dupe by host:port; keep the lowest-latency record.
        var byHost: [String: DiscoveredServer] = [:]
        for s in found {
            let key = "\(s.host):\(s.port)"
            if let existing = byHost[key], existing.latencyMs <= s.latencyMs { continue }
            byHost[key] = s
        }
        return byHost.values.sorted { $0.latencyMs < $1.latencyMs }
    }

    /// Probe a specific URL — used when the user types one manually.
    public func probe(_ url: URL,
                      source: DiscoveredServer.Source = .manual) async -> DiscoveredServer? {
        let start = Date()
        var req = URLRequest(url: url.appendingPathComponent("/api/health"))
        req.timeoutInterval = 3
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  body["service"] as? String == "Helen Server" else {
                return nil
            }
            let latency = Date().timeIntervalSince(start) * 1000
            return DiscoveredServer(
                url:       url,
                host:      url.host ?? "?",
                port:      url.port ?? 3000,
                latencyMs: latency,
                source:    source,
                metadata:  ["version": (body["version"] as? String) ?? "?"]
            )
        } catch {
            return nil
        }
    }

    public func rememberLastUsed(_ url: URL) {
        UserDefaults.standard.set(url.absoluteString, forKey: lastUsedKey)
    }

    public func forgetLastUsed() {
        UserDefaults.standard.removeObject(forKey: lastUsedKey)
    }

    nonisolated public func getLastUsedURL() -> URL? {
        guard let s = UserDefaults.standard.string(forKey: lastUsedKey),
              let u = URL(string: s) else { return nil }
        return u
    }

    // MARK: - Bonjour

    private func bonjourScan(timeout seconds: TimeInterval) async -> [DiscoveredServer] {
        await withCheckedContinuation { (cont: CheckedContinuation<[DiscoveredServer], Never>) in
            var found: [DiscoveredServer] = []
            let params = NWParameters()
            params.includePeerToPeer = true
            let browser = NWBrowser(
                for: .bonjour(type: "_helen-server._tcp.", domain: nil),
                using: params
            )
            self.browser = browser

            browser.browseResultsChangedHandler = { results, _ in
                for r in results {
                    if case let .service(name, _, _, _) = r.endpoint {
                        // We only have the service name + endpoint here;
                        // resolving requires another round-trip. Use the
                        // endpoint host directly via NWConnection probe.
                        if let host = self.extractHost(from: r) {
                            let url = URL(string: "http://\(host):3000")!
                            found.append(DiscoveredServer(
                                url: url, host: host, port: 3000,
                                latencyMs: 0, source: .bonjour,
                                metadata: ["service_name": name]))
                        }
                    }
                }
            }

            browser.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + seconds) {
                browser.cancel()
                cont.resume(returning: found)
            }
        }
    }

    private nonisolated func extractHost(from result: NWBrowser.Result) -> String? {
        let mirror = Mirror(reflecting: result.metadata)
        for child in mirror.children {
            if let host = child.value as? String, host.contains(".") { return host }
        }
        return nil
    }

    // MARK: - LAN HTTP probe

    private func lanProbeScan(timeout seconds: TimeInterval) async -> [DiscoveredServer] {
        // Cover the most-common LAN gateways first; if none answer, the
        // user can type the URL manually. We don't enumerate /24 because
        // 254 parallel probes flood the link and fail iOS rate limits.
        let candidates: [URL] = [
            "http://192.168.1.1:3000", "http://192.168.0.1:3000",
            "http://192.168.1.100:3000", "http://192.168.0.100:3000",
            "http://192.168.1.10:3000",  "http://192.168.0.10:3000",
            "http://10.0.0.1:3000",      "http://10.0.0.100:3000",
            "http://10.0.1.1:3000",      "http://172.16.0.1:3000",
        ].compactMap { URL(string: $0) }

        return await withTaskGroup(of: DiscoveredServer?.self) { group in
            for url in candidates {
                group.addTask { await self.probe(url, source: .lanProbe) }
            }
            var found: [DiscoveredServer] = []
            for await r in group {
                if let r = r { found.append(r) }
            }
            return found
        }
    }
}
