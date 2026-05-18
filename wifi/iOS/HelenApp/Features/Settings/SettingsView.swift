import SwiftUI

struct SettingsView: View {
    @State private var notifications = true
    @State private var readReceipts  = true
    @State private var darkOverride: AppearanceChoice = .system
    @State private var confirmSignOut = false

    enum AppearanceChoice: Hashable { case system, light, dark }

    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()
            ScrollView {
                VStack(spacing: HelenSpace.xl) {
                    HelenNavBar(title: "Settings")

                    section(title: "Appearance") {
                        HelenSegmented(
                            options: [
                                (.system, "System"),
                                (.light,  "Light"),
                                (.dark,   "Dark"),
                            ],
                            selection: $darkOverride
                        )
                        .padding(.horizontal, HelenSpace.lg)
                        .padding(.vertical,   HelenSpace.md)
                    }

                    section(title: "Privacy") {
                        toggleRow(symbol: "bell.badge", title: "Notifications",
                                  subtitle: "Sounds, banners, badges",
                                  isOn: $notifications)
                        Divider().overlay(theme.colors.divider).padding(.leading, 60)
                        toggleRow(symbol: "checkmark.message", title: "Read receipts",
                                  subtitle: "Let others see when you read",
                                  isOn: $readReceipts)
                    }

                    section(title: "About") {
                        infoRow(symbol: "info.circle", title: "Version",       value: "1.0.0 (build 42)")
                        Divider().overlay(theme.colors.divider).padding(.leading, 60)
                        infoRow(symbol: "doc.text",    title: "Terms",          value: "")
                        Divider().overlay(theme.colors.divider).padding(.leading, 60)
                        infoRow(symbol: "hand.raised", title: "Privacy policy", value: "")
                    }

                    HelenButton(title: "Sign out", icon: "rectangle.portrait.and.arrow.right",
                                variant: .destructive) {
                        confirmSignOut = true
                    }
                    .padding(.top, HelenSpace.sm)
                }
                .padding(.horizontal, HelenSpace.pageH)
                .padding(.bottom, HelenSpace.huge)
            }
        }
        .confirmationDialog("Sign out",
                            isPresented: $confirmSignOut,
                            titleVisibility: .visible) {
            Button("Sign out", role: .destructive) {
                Task { await session.logout() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("You'll need to sign back in to use Helen.")
        }
    }

    @ViewBuilder
    private func section<Content: View>(
        title: LocalizedStringKey,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            HelenSectionHeader(title: title)
            VStack(spacing: 0, content: content)
                .helenCardSurface()
        }
    }

    private func toggleRow(symbol: String,
                           title: LocalizedStringKey,
                           subtitle: LocalizedStringKey,
                           isOn: Binding<Bool>) -> some View {
        HStack(spacing: HelenSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: HelenRadius.sm, style: .continuous)
                    .fill(theme.colors.accentMuted).frame(width: 36, height: 36)
                Image(systemName: symbol)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(HelenFont.bodyMed)
                    .foregroundStyle(theme.colors.textPrimary)
                Text(subtitle).font(HelenFont.footnote)
                    .foregroundStyle(theme.colors.textSecondary)
            }
            Spacer()
            Toggle("", isOn: isOn).labelsHidden().tint(theme.colors.accent)
        }
        .padding(.horizontal, HelenSpace.lg).padding(.vertical, HelenSpace.md)
    }

    private func infoRow(symbol: String, title: LocalizedStringKey, value: String) -> some View {
        HStack(spacing: HelenSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: HelenRadius.sm, style: .continuous)
                    .fill(theme.colors.surfaceAlt).frame(width: 36, height: 36)
                Image(systemName: symbol)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.textSecondary)
            }
            Text(title).font(HelenFont.bodyMed)
                .foregroundStyle(theme.colors.textPrimary)
            Spacer()
            if !value.isEmpty {
                Text(value).font(HelenFont.subhead.monospacedDigit())
                    .foregroundStyle(theme.colors.textSecondary)
            } else {
                Image(systemName: "chevron.forward")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(theme.colors.textTertiary)
            }
        }
        .padding(.horizontal, HelenSpace.lg).padding(.vertical, HelenSpace.md)
        .contentShape(Rectangle())
    }
}

#Preview { SettingsView().environmentObject(HelenSession()) }
