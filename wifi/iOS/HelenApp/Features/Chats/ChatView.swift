import SwiftUI

struct ChatView: View {
    let conversation: Conversation
    @State private var draft: String = ""
    @State private var localMessages: [ChatMessage] = []

    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss

    /// Live messages from the session, mapped into ChatMessage; falls back
    /// to local-only state (or the sample set) when the session is offline.
    private var messages: [ChatMessage] {
        if session.isAuthenticated, let server = session.messagesByChannel[conversation.id] {
            let myId = session.currentUser?.id ?? ""
            let mapped = server.map { ChatMessage(from: $0, myUserId: myId) }
            return mapped + localMessages
        }
        return localMessages.isEmpty ? ChatMessage.sample : localMessages
    }

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()

            VStack(spacing: 0) {
                header
                Divider().overlay(theme.colors.divider)
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: HelenSpace.sm) {
                            ForEach(grouped, id: \.0) { (day, items) in
                                Text(day)
                                    .font(HelenFont.caption2.weight(.semibold))
                                    .foregroundStyle(theme.colors.textTertiary)
                                    .padding(.vertical, 4).padding(.horizontal, 10)
                                    .background(theme.colors.surfaceAlt)
                                    .clipShape(Capsule())
                                    .padding(.vertical, HelenSpace.sm)
                                ForEach(items) { m in
                                    Bubble(message: m).id(m.id)
                                }
                            }
                        }
                        .padding(.horizontal, HelenSpace.pageH)
                        .padding(.vertical, HelenSpace.md)
                    }
                    .onAppear { withAnimation { proxy.scrollTo(messages.last?.id, anchor: .bottom) } }
                    .onChange(of: messages.count) { _, _ in
                        withAnimation(HelenMotion.standard) {
                            proxy.scrollTo(messages.last?.id, anchor: .bottom)
                        }
                    }
                }
                composer
            }
        }
        .navigationBarBackButtonHidden(true)
        .toolbar(.hidden, for: .navigationBar)
        .task {
            guard session.isAuthenticated else { return }
            await session.loadMessages(channelId: conversation.id)
        }
    }

    // MARK: – Header
    private var header: some View {
        HStack(spacing: HelenSpace.md) {
            Button { dismiss() } label: {
                Image(systemName: "chevron.backward")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
                    .frame(width: 32, height: 32)
            }
            .accessibilityLabel("Back")

            HelenAvatar(name: conversation.title, size: .md, presence: conversation.presence)

            VStack(alignment: .leading, spacing: 2) {
                Text(conversation.title)
                    .font(HelenFont.bodyEmph)
                    .foregroundStyle(theme.colors.textPrimary)
                Text(conversation.presence == .online ? "Online" : "Last seen recently")
                    .font(HelenFont.caption)
                    .foregroundStyle(theme.colors.textSecondary)
            }
            Spacer()
            HStack(spacing: HelenSpace.sm) {
                IconCircle(symbol: "phone")
                IconCircle(symbol: "video")
                IconCircle(symbol: "ellipsis")
            }
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.vertical, HelenSpace.sm)
        .background(theme.colors.surface)
    }

    // MARK: – Composer
    private var composer: some View {
        HStack(alignment: .bottom, spacing: HelenSpace.sm) {
            Button {} label: {
                Image(systemName: "plus")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
                    .frame(width: 36, height: 36)
                    .background(theme.colors.accentMuted)
                    .clipShape(Circle())
            }
            .accessibilityLabel("Attach")

            HStack(alignment: .bottom, spacing: HelenSpace.sm) {
                TextField("Message", text: $draft, axis: .vertical)
                    .lineLimit(1...4)
                    .font(HelenFont.body)
                    .foregroundStyle(theme.colors.textPrimary)
                    .tint(theme.colors.accent)
                Button {} label: {
                    Image(systemName: "face.smiling")
                        .foregroundStyle(theme.colors.textTertiary)
                }
                .accessibilityLabel("Emoji")
            }
            .padding(.horizontal, HelenSpace.md)
            .padding(.vertical, HelenSpace.sm)
            .background(theme.colors.surfaceAlt)
            .clipShape(RoundedRectangle(cornerRadius: HelenRadius.lg, style: .continuous))

            Button { send() } label: {
                Image(systemName: draft.isEmpty ? "mic.fill" : "arrow.up")
                    .font(.body.weight(.bold))
                    .foregroundStyle(.white)
                    .frame(width: 36, height: 36)
                    .background(theme.colors.accent)
                    .clipShape(Circle())
                    .scaleEffect(draft.isEmpty ? 1.0 : 1.04)
                    .animation(HelenMotion.standard, value: draft.isEmpty)
            }
            .accessibilityLabel(draft.isEmpty ? "Record voice message" : "Send")
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.vertical, HelenSpace.sm)
        .background(theme.colors.background)
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()

        if session.isAuthenticated {
            // Real send — server will fan out and the message will appear
            // in `session.messagesByChannel[id]` via the socket.
            let pending = ChatMessage(
                id: UUID().uuidString, senderId: "me",
                content: text, timestamp: .now,
                isMine: true, status: .sending,
            )
            withAnimation(HelenMotion.standard) {
                localMessages.append(pending)
                draft = ""
            }
            Task { @MainActor in
                await session.sendMessage(channelId: conversation.id, content: text)
                // Drop the optimistic placeholder once the server echo arrives.
                if let idx = localMessages.firstIndex(where: { $0.id == pending.id }) {
                    localMessages.remove(at: idx)
                }
            }
        } else {
            // Offline / preview mode — purely local.
            let msg = ChatMessage(id: UUID().uuidString, senderId: "me",
                                  content: text, timestamp: .now,
                                  isMine: true, status: .sending)
            withAnimation(HelenMotion.standard) {
                localMessages.append(msg)
                draft = ""
            }
        }
    }

    private var grouped: [(String, [ChatMessage])] {
        let f = DateFormatter(); f.dateStyle = .medium; f.locale = .current
        let dict = Dictionary(grouping: messages) { f.string(from: $0.timestamp) }
        return dict.sorted { lhs, rhs in
            (dict[lhs.key]?.first?.timestamp ?? .distantPast) <
            (dict[rhs.key]?.first?.timestamp ?? .distantPast)
        }.map { ($0.key, $0.value.sorted { $0.timestamp < $1.timestamp }) }
    }
}

// MARK: - Bubble
private struct Bubble: View {
    let message: ChatMessage
    @Environment(\.theme) private var theme

    var body: some View {
        HStack {
            if message.isMine { Spacer(minLength: HelenSpace.huge) }
            VStack(alignment: .leading, spacing: 4) {
                Text(message.content)
                    .font(HelenFont.body)
                    .foregroundStyle(message.isMine ? .white : theme.colors.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 4) {
                    Text(time(message.timestamp))
                        .font(HelenFont.caption2.monospacedDigit())
                        .foregroundStyle(message.isMine
                                         ? Color.white.opacity(0.75)
                                         : theme.colors.textTertiary)
                    if message.isMine { statusIcon }
                }
            }
            .padding(.horizontal, HelenSpace.md)
            .padding(.vertical, HelenSpace.sm)
            .background(message.isMine ? theme.colors.accent : theme.colors.surface)
            .overlay(
                RoundedRectangle(cornerRadius: HelenRadius.lg, style: .continuous)
                    .strokeBorder(message.isMine ? .clear : theme.colors.border, lineWidth: 0.5)
            )
            .clipShape(BubbleShape(isMine: message.isMine))
            .helenShadow(message.isMine ? .none : .sm)
            if !message.isMine { Spacer(minLength: HelenSpace.huge) }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var statusIcon: some View {
        switch message.status {
        case .sending:   Image(systemName: "clock").font(.caption2)
        case .sent:      Image(systemName: "checkmark").font(.caption2)
        case .delivered: Image(systemName: "checkmark.circle").font(.caption2)
        case .read:      Image(systemName: "checkmark.circle.fill").font(.caption2)
                          .foregroundStyle(.white)
        case .failed:    Image(systemName: "exclamationmark.triangle.fill")
                            .font(.caption2).foregroundStyle(theme.colors.danger)
        }
    }

    private func time(_ d: Date) -> String {
        let f = DateFormatter(); f.timeStyle = .short; f.locale = .current
        return f.string(from: d)
    }
}

private struct BubbleShape: Shape {
    let isMine: Bool
    func path(in rect: CGRect) -> Path {
        let r: CGFloat = HelenRadius.lg
        let tr: CGFloat = isMine ? 4 : r
        let tl: CGFloat = isMine ? r : 4
        return Path { p in
            p.move(to: CGPoint(x: tl, y: 0))
            p.addLine(to: CGPoint(x: rect.maxX - tr, y: 0))
            p.addQuadCurve(to: CGPoint(x: rect.maxX, y: tr), control: CGPoint(x: rect.maxX, y: 0))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - r))
            p.addQuadCurve(to: CGPoint(x: rect.maxX - r, y: rect.maxY), control: CGPoint(x: rect.maxX, y: rect.maxY))
            p.addLine(to: CGPoint(x: r, y: rect.maxY))
            p.addQuadCurve(to: CGPoint(x: 0, y: rect.maxY - r), control: CGPoint(x: 0, y: rect.maxY))
            p.addLine(to: CGPoint(x: 0, y: tl))
            p.addQuadCurve(to: CGPoint(x: tl, y: 0), control: CGPoint(x: 0, y: 0))
        }
    }
}

private struct IconCircle: View {
    let symbol: String
    @Environment(\.theme) private var theme
    var body: some View {
        Button {} label: {
            Image(systemName: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(theme.colors.accent)
                .frame(width: 36, height: 36)
                .background(theme.colors.accentMuted)
                .clipShape(Circle())
        }
        .buttonStyle(PressableScaleStyle())
    }
}

#Preview {
    NavigationStack { ChatView(conversation: Conversation.samples[0]) }
        .environmentObject(HelenSession())
}
