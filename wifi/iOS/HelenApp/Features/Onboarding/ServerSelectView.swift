//
//  ServerSelectView.swift
//  HelenApp
//
//  First-run / re-onboarding screen — pick the Helen-Server to connect
//  to before signing in. Runs `HelenSession.discoverServers()` (Bonjour
//  + LAN probes) on appear, lets the user tap a discovered server, and
//  falls back to a manual URL field for non-broadcast networks.
//

import SwiftUI

struct ServerSelectView: View {
    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    @State private var discovered: [DiscoveredServer] = []
    @State private var manualHost: String = ""
    @State private var isScanning = false
    @State private var isConnecting = false
    @State private var error: LocalizedStringKey? = nil

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()

            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: HelenSpace.xxl) {
                    header
                    discoveredCard
                    manualCard
                    Spacer(minLength: HelenSpace.lg)
                }
                .padding(.horizontal, HelenSpace.pageH)
                .padding(.top, HelenSpace.huge)
                .padding(.bottom, HelenSpace.xl)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task { await scan() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: HelenRadius.lg, style: .continuous)
                    .fill(theme.colors.accent)
                    .frame(width: 64, height: 64)
                    .helenShadow(.md)
                Image(systemName: "antenna.radiowaves.left.and.right")
                    .font(.system(size: 28, weight: .bold))
                    .foregroundStyle(.white)
            }
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: HelenSpace.xs) {
                Text("Find your Helen server").font(HelenFont.display)
                    .foregroundStyle(theme.colors.textPrimary)
                Text("We'll scan your Wi-Fi for Helen-Server. Pick the one to use, or enter its address.")
                    .font(HelenFont.subhead)
                    .foregroundStyle(theme.colors.textSecondary)
            }
        }
    }

    private var discoveredCard: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            HStack {
                HelenSectionHeader(title: "On your network")
                Spacer()
                if isScanning {
                    ProgressView().controlSize(.small)
                } else {
                    Button { Task { await scan() } } label: {
                        Image(systemName: "arrow.clockwise")
                            .foregroundStyle(theme.colors.accent)
                    }
                    .accessibilityLabel("Rescan")
                }
            }

            if discovered.isEmpty && !isScanning {
                HelenCard {
                    HelenEmptyState(
                        symbol: "wifi.slash",
                        title: "No servers found",
                        message: "Make sure Helen-Server is running on the same Wi-Fi, or enter its address below."
                    )
                }
            } else {
                VStack(spacing: 0) {
                    ForEach(discovered) { server in
                        HelenListRow(
                            title: server.host,
                            subtitle: "\(Int(server.latencyMs)) ms · \(server.source.rawValue)",
                            leading: {
                                Image(systemName: "server.rack")
                                    .font(.body.weight(.semibold))
                                    .foregroundStyle(theme.colors.accent)
                            },
                            trailing: {
                                Image(systemName: "chevron.forward")
                                    .font(.footnote.weight(.semibold))
                                    .foregroundStyle(theme.colors.textTertiary)
                            },
                            onTap: { connect(to: server.url) }
                        )
                        if server.id != discovered.last?.id {
                            Divider().overlay(theme.colors.divider).padding(.leading, 60)
                        }
                    }
                }
                .helenCardSurface()
            }
        }
    }

    private var manualCard: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            HelenSectionHeader(title: "Or enter address")
            HelenTextField(
                label: "Server address",
                text: $manualHost,
                placeholder: "192.168.1.50:3000",
                icon: "globe",
                keyboard: .URL,
                contentType: .URL,
                error: error,
                submitLabel: .go,
                onSubmit: connectManual
            )
            HelenButton(
                title: "Connect",
                icon: "arrow.right.circle",
                variant: .primary,
                isLoading: isConnecting,
                isDisabled: manualHost.isEmpty,
                action: connectManual
            )
        }
    }

    // MARK: - Actions

    @MainActor
    private func scan() async {
        isScanning = true
        let results = await session.discoverServers()
        self.discovered = results
        self.isScanning = false
    }

    private func connect(to url: URL) {
        Task { @MainActor in
            isConnecting = true
            error = nil
            do {
                try await session.chooseServer(url)
            } catch {
                self.error = LocalizedStringKey(error.localizedDescription)
            }
            isConnecting = false
        }
    }

    private func connectManual() {
        let trimmed = manualHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let normalised = trimmed.contains("://") ? trimmed : "http://\(trimmed)"
        guard let url = URL(string: normalised) else {
            error = "Server address"
            return
        }
        connect(to: url)
    }
}

#Preview {
    ServerSelectView().environmentObject(HelenSession())
}
