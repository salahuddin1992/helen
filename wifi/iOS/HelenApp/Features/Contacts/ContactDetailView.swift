import SwiftUI

struct ContactDetailView: View {
    let contact: Contact
    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss

    @State private var presentedChat: Conversation?
    @State private var isOpeningChat = false

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()
            ScrollView {
                VStack(spacing: HelenSpace.xl) {
                    hero
                    quickActions
                    infoCard
                    aboutCard
                    Spacer(minLength: HelenSpace.huge)
                }
                .padding(.horizontal, HelenSpace.pageH)
                .padding(.top, HelenSpace.xl)
            }
        }
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button { dismiss() } label: {
                    Image(systemName: "chevron.backward")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(theme.colors.accent)
                }
                .accessibilityLabel("Back")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button { } label: { Label("Share contact", systemImage: "square.and.arrow.up") }
                    Button { } label: { Label("Mute", systemImage: "bell.slash") }
                    Button(role: .destructive) {} label: { Label("Block", systemImage: "hand.raised") }
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(theme.colors.accent)
                }
                .accessibilityLabel("More")
            }
        }
    }

    private var hero: some View {
        VStack(spacing: HelenSpace.md) {
            HelenAvatar(name: contact.displayName, size: .xl, presence: contact.presence)
            VStack(spacing: HelenSpace.xs) {
                Text(contact.displayName)
                    .font(HelenFont.title)
                    .foregroundStyle(theme.colors.textPrimary)
                Text(contact.username)
                    .font(HelenFont.subhead)
                    .foregroundStyle(theme.colors.textSecondary)
            }
            HelenBadge(text: presenceLabel, icon: presenceIcon, tone: presenceTone)
        }
    }

    private var quickActions: some View {
        HStack(spacing: HelenSpace.md) {
            QuickAction(icon: "message.fill", title: "Message",
                        isLoading: isOpeningChat, action: openChat)
            QuickAction(icon: "phone.fill",   title: "Voice")
            QuickAction(icon: "video.fill",   title: "Video")
            QuickAction(icon: "envelope.fill", title: "Email")
        }
        .sheet(item: $presentedChat) { conversation in
            NavigationStack {
                ChatView(conversation: conversation)
                    .environmentObject(session)
            }
        }
    }

    private func openChat() {
        guard !isOpeningChat else { return }
        isOpeningChat = true
        Task { @MainActor in
            if session.isAuthenticated,
               let channel = await session.openOrCreateDM(with: contact.id) {
                presentedChat = Conversation(from: channel)
            } else {
                // Offline / preview — open a synthetic conversation so the
                // chat UI still renders for the user.
                presentedChat = Conversation(
                    id: contact.id, title: contact.displayName,
                    subtitle: "", lastActivity: .now, unreadCount: 0,
                    pinned: false, muted: false, isGroup: false,
                    presence: contact.presence,
                )
            }
            isOpeningChat = false
        }
    }

    private var infoCard: some View {
        HelenCard {
            VStack(spacing: 0) {
                InfoRow(icon: "phone",   title: "Phone",    value: contact.phone ?? "—")
                Divider().overlay(theme.colors.divider)
                InfoRow(icon: "at",      title: "Username", value: contact.username)
                Divider().overlay(theme.colors.divider)
                InfoRow(icon: "clock",   title: "Last seen", value: lastSeenText)
            }
        }
    }

    private var aboutCard: some View {
        HelenCard {
            VStack(alignment: .leading, spacing: HelenSpace.sm) {
                Text("About")
                    .font(HelenFont.headline)
                    .foregroundStyle(theme.colors.textPrimary)
                Text(contact.status ?? "—")
                    .font(HelenFont.body)
                    .foregroundStyle(theme.colors.textSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: – derived
    private var presenceLabel: String {
        switch contact.presence {
        case .online:  return "Online now"
        case .away:    return "Away"
        case .offline: return "Offline"
        }
    }
    private var presenceIcon: String {
        switch contact.presence {
        case .online:  return "circle.fill"
        case .away:    return "moon.fill"
        case .offline: return "circle"
        }
    }
    private var presenceTone: HelenBadge.Tone {
        switch contact.presence {
        case .online:  return .success
        case .away:    return .warning
        case .offline: return .neutral
        }
    }
    private var lastSeenText: String {
        guard let last = contact.lastSeen else { return "Active now" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .full
        return f.localizedString(for: last, relativeTo: .now)
    }
}

private struct QuickAction: View {
    let icon: String
    let title: LocalizedStringKey
    var isLoading: Bool = false
    var action: (() -> Void)? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        Button { action?() } label: {
            VStack(spacing: HelenSpace.xs) {
                ZStack {
                    RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous)
                        .fill(theme.colors.accentMuted)
                    if isLoading {
                        ProgressView()
                            .controlSize(.small)
                            .tint(theme.colors.accent)
                    } else {
                        Image(systemName: icon)
                            .font(.body.weight(.semibold))
                            .foregroundStyle(theme.colors.accent)
                    }
                }
                .frame(height: 56)
                Text(title)
                    .font(HelenFont.caption.weight(.semibold))
                    .foregroundStyle(theme.colors.textSecondary)
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(PressableScaleStyle())
        .disabled(action == nil || isLoading)
        .accessibilityLabel(title)
    }
}

private struct InfoRow: View {
    let icon: String
    let title: LocalizedStringKey
    let value: String
    @Environment(\.theme) private var theme
    var body: some View {
        HStack(spacing: HelenSpace.md) {
            Image(systemName: icon)
                .font(.body)
                .foregroundStyle(theme.colors.accent)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(HelenFont.caption.weight(.medium))
                    .foregroundStyle(theme.colors.textSecondary)
                Text(value).font(HelenFont.body)
                    .foregroundStyle(theme.colors.textPrimary)
            }
            Spacer()
        }
        .padding(.vertical, HelenSpace.sm)
    }
}

#Preview {
    NavigationStack {
        ContactDetailView(contact: Contact.samples[1])
    }
    .environmentObject(HelenSession())
}
