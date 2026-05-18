import SwiftUI

struct ProfileView: View {
    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    private var displayName: String {
        session.currentUser?.displayName ?? session.currentUser?.username ?? "Helen User"
    }
    private var username: String {
        guard let u = session.currentUser?.username else { return "@you" }
        return u.hasPrefix("@") ? u : "@\(u)"
    }
    private var status: String {
        session.currentUser?.status ?? (session.isConnected ? "Online" : "Offline")
    }

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()
            ScrollView {
                VStack(spacing: HelenSpace.xl) {
                    HelenNavBar(title: "Profile")

                    HelenCard {
                        VStack(spacing: HelenSpace.lg) {
                            HelenAvatar(name: displayName, size: .xl, presence: .online)
                            VStack(spacing: HelenSpace.xs) {
                                Text(displayName)
                                    .font(HelenFont.title2)
                                    .foregroundStyle(theme.colors.textPrimary)
                                Text(username)
                                    .font(HelenFont.subhead)
                                    .foregroundStyle(theme.colors.textSecondary)
                            }
                            HelenButton(
                                title: "Edit profile",
                                icon: "pencil",
                                variant: .secondary,
                                fullWidth: false
                            ) {}
                        }
                        .frame(maxWidth: .infinity)
                    }

                    StatsCard()

                    settingsCard
                }
                .padding(.horizontal, HelenSpace.pageH)
                .padding(.bottom, HelenSpace.huge)
            }
        }
    }

    private var settingsCard: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            HelenSectionHeader(title: "Account")
            VStack(spacing: 0) {
                HelenListRow(
                    title: "Privacy & Security",
                    subtitle: "Two-factor, app lock, sessions",
                    leading: { Icon(symbol: "lock.shield", tone: .accent) },
                    trailing: { Image(systemName: "chevron.forward")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(theme.colors.textTertiary) },
                    onTap: {}
                )
                Divider().overlay(theme.colors.divider).padding(.leading, 60)
                HelenListRow(
                    title: "Notifications",
                    subtitle: "Sounds, alerts, badges",
                    leading: { Icon(symbol: "bell.badge", tone: .warning) },
                    trailing: { Image(systemName: "chevron.forward")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(theme.colors.textTertiary) },
                    onTap: {}
                )
                Divider().overlay(theme.colors.divider).padding(.leading, 60)
                HelenListRow(
                    title: "Storage & Data",
                    subtitle: "Manage cache and downloads",
                    leading: { Icon(symbol: "internaldrive", tone: .accent) },
                    trailing: { Image(systemName: "chevron.forward")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(theme.colors.textTertiary) },
                    onTap: {}
                )
            }
            .helenCardSurface()
        }
    }
}

private struct StatsCard: View {
    @Environment(\.theme) private var theme
    var body: some View {
        HelenCard {
            HStack(spacing: 0) {
                Stat(value: "1,284", label: "Messages")
                Divider().frame(height: 32).overlay(theme.colors.divider)
                Stat(value: "47",    label: "Contacts")
                Divider().frame(height: 32).overlay(theme.colors.divider)
                Stat(value: "12",    label: "Channels")
            }
        }
    }
}

private struct Stat: View {
    let value: String
    let label: LocalizedStringKey
    @Environment(\.theme) private var theme
    var body: some View {
        VStack(spacing: 4) {
            Text(value)
                .font(HelenFont.title3.monospacedDigit())
                .foregroundStyle(theme.colors.textPrimary)
            Text(label)
                .font(HelenFont.caption)
                .foregroundStyle(theme.colors.textSecondary)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct Icon: View {
    let symbol: String
    enum Tone { case accent, warning, danger }
    var tone: Tone = .accent
    @Environment(\.theme) private var theme
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: HelenRadius.sm, style: .continuous)
                .fill(bg)
                .frame(width: 36, height: 36)
            Image(systemName: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(fg)
        }
    }
    private var bg: Color {
        switch tone {
        case .accent:  return theme.colors.accentMuted
        case .warning: return theme.colors.warning.opacity(0.15)
        case .danger:  return theme.colors.danger.opacity(0.12)
        }
    }
    private var fg: Color {
        switch tone {
        case .accent:  return theme.colors.accent
        case .warning: return theme.colors.warning
        case .danger:  return theme.colors.danger
        }
    }
}

#Preview { ProfileView().environmentObject(HelenSession()) }
