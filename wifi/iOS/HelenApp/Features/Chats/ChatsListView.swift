import SwiftUI

struct ChatsListView: View {
    @State private var search = ""
    @State private var filter: Filter = .all
    @State private var isRefreshing = false

    enum Filter: Hashable { case all, unread, groups }

    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    /// Live channels when authenticated; falls back to samples for previews.
    private var conversations: [Conversation] {
        if session.isAuthenticated {
            return session.channels.map { ch in
                let last = session.messagesByChannel[ch.id]?.last
                return Conversation(from: ch, lastMessage: last)
            }
        }
        return Conversation.samples
    }

    var body: some View {
        ZStack(alignment: .top) {
            theme.colors.background.ignoresSafeArea()
            ScrollView {
                LazyVStack(spacing: HelenSpace.lg) {
                    HelenNavBar(title: "Chats") {
                        Button {} label: {
                            ZStack {
                                Circle().fill(theme.colors.accent).frame(width: 36, height: 36)
                                Image(systemName: "square.and.pencil")
                                    .font(.body.weight(.semibold))
                                    .foregroundStyle(.white)
                            }
                        }
                        .accessibilityLabel("New chat")
                    }

                    VStack(spacing: HelenSpace.md) {
                        HelenSearchBar(text: $search, placeholder: "Search chats")
                        HelenSegmented(
                            options: [(.all, "All"), (.unread, "Unread"), (.groups, "Groups")],
                            selection: $filter
                        )
                    }
                    .padding(.horizontal, HelenSpace.pageH)

                    if filtered.isEmpty {
                        HelenEmptyState(
                            symbol: "bubble.left.and.bubble.right",
                            title: "No conversations",
                            message: "Start a new chat to see it here.",
                            actionTitle: "New chat",
                            action: {}
                        )
                    } else {
                        ForEach(filtered) { c in
                            NavigationLink(value: c) {
                                ChatRow(conversation: c)
                            }
                            .buttonStyle(.plain)
                            .padding(.horizontal, HelenSpace.pageH)
                        }
                    }
                    Spacer(minLength: HelenSpace.huge)
                }
                .padding(.top, HelenSpace.xs)
            }
            .refreshable { await reload() }
        }
        .task { await reload() }
    }

    @MainActor
    private func reload() async {
        guard session.isAuthenticated, !isRefreshing else { return }
        isRefreshing = true
        await session.reloadChannels()
        isRefreshing = false
    }

    private var filtered: [Conversation] {
        let q = search.lowercased()
        return conversations.filter { c in
            let matchesQ = q.isEmpty
                || c.title.lowercased().contains(q)
                || c.subtitle.lowercased().contains(q)
            let matchesFilter: Bool = {
                switch filter {
                case .all:    return true
                case .unread: return c.unreadCount > 0
                case .groups: return c.isGroup
                }
            }()
            return matchesQ && matchesFilter
        }
        .sorted { ($0.pinned, $0.lastActivity) > ($1.pinned, $1.lastActivity) }
    }
}

private struct ChatRow: View {
    let conversation: Conversation
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.md) {
            HelenAvatar(name: conversation.title, size: .lg, presence: conversation.presence)

            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: HelenSpace.xs) {
                    Text(conversation.title)
                        .font(HelenFont.bodyEmph)
                        .foregroundStyle(theme.colors.textPrimary)
                        .lineLimit(1)
                    if conversation.pinned {
                        Image(systemName: "pin.fill")
                            .font(.caption2)
                            .foregroundStyle(theme.colors.textTertiary)
                    }
                    if conversation.muted {
                        Image(systemName: "bell.slash.fill")
                            .font(.caption2)
                            .foregroundStyle(theme.colors.textTertiary)
                    }
                    Spacer()
                    Text(timestamp)
                        .font(HelenFont.caption.monospacedDigit())
                        .foregroundStyle(theme.colors.textTertiary)
                }
                HStack(alignment: .top, spacing: HelenSpace.xs) {
                    Text(conversation.subtitle)
                        .font(HelenFont.subhead)
                        .foregroundStyle(theme.colors.textSecondary)
                        .lineLimit(2)
                    Spacer(minLength: HelenSpace.sm)
                    HelenUnreadDot(count: conversation.unreadCount)
                }
            }
        }
        .padding(HelenSpace.md)
        .helenCardSurface()
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private var timestamp: String {
        let f = DateFormatter()
        f.locale = .current
        if Calendar.current.isDateInToday(conversation.lastActivity) {
            f.timeStyle = .short
        } else if Calendar.current.isDateInYesterday(conversation.lastActivity) {
            return NSLocalizedString("Yesterday", comment: "")
        } else {
            f.dateFormat = "MMM d"
        }
        return f.string(from: conversation.lastActivity)
    }
}

#Preview {
    NavigationStack { ChatsListView() }
        .environmentObject(HelenSession())
}
