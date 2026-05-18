import Foundation

struct Conversation: Identifiable, Hashable {
    let id: String
    var title: String
    var subtitle: String        // last message preview
    var lastActivity: Date
    var unreadCount: Int
    var pinned: Bool
    var muted: Bool
    var isGroup: Bool
    var presence: HelenAvatar.Presence?

    /// Adapter from a server channel (no message preview yet —
    /// `ChatsListView` will fill `subtitle` once messages are loaded).
    init(from channel: HelenChannel, lastMessage: HelenMessage? = nil) {
        self.id = channel.id
        self.title = channel.name
        self.subtitle = lastMessage?.content ?? ""
        self.lastActivity = Conversation.parseISO(channel.createdAt)
            ?? Conversation.parseISO(lastMessage?.createdAt)
            ?? Date()
        self.unreadCount = 0
        self.pinned = false
        self.muted = false
        self.isGroup = (channel.type != "dm" && channel.type != "direct")
        self.presence = nil
    }

    init(id: String, title: String, subtitle: String, lastActivity: Date,
         unreadCount: Int, pinned: Bool, muted: Bool, isGroup: Bool,
         presence: HelenAvatar.Presence?) {
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.lastActivity = lastActivity
        self.unreadCount = unreadCount
        self.pinned = pinned
        self.muted = muted
        self.isGroup = isGroup
        self.presence = presence
    }

    fileprivate static func parseISO(_ s: String?) -> Date? {
        guard let s = s else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    static let samples: [Conversation] = [
        .init(id: "c1", title: "Yousef Salah",   subtitle: "أنا أراك من iPhone",
              lastActivity: Date().addingTimeInterval(-60),    unreadCount: 2, pinned: true,  muted: false, isGroup: false, presence: .online),
        .init(id: "c2", title: "Design Team",     subtitle: "Maya: shipped the new tokens",
              lastActivity: Date().addingTimeInterval(-900),   unreadCount: 12, pinned: true, muted: false, isGroup: true,  presence: nil),
        .init(id: "c3", title: "Helen Khalil",    subtitle: "Voice message · 0:42",
              lastActivity: Date().addingTimeInterval(-3600),  unreadCount: 0, pinned: false, muted: false, isGroup: false, presence: .away),
        .init(id: "c4", title: "Family",          subtitle: "Ahmed: تذكروا الموعد بكره",
              lastActivity: Date().addingTimeInterval(-7200),  unreadCount: 5, pinned: false, muted: true,  isGroup: true,  presence: nil),
        .init(id: "c5", title: "Maya Saleh",      subtitle: "📷 Photo",
              lastActivity: Date().addingTimeInterval(-86400), unreadCount: 0, pinned: false, muted: false, isGroup: false, presence: .offline),
        .init(id: "c6", title: "Ops · On-call",   subtitle: "All systems green",
              lastActivity: Date().addingTimeInterval(-172800), unreadCount: 0, pinned: false, muted: true, isGroup: true,  presence: nil),
    ]
}

struct ChatMessage: Identifiable, Hashable {
    let id: String
    let senderId: String
    let content: String
    let timestamp: Date
    let isMine: Bool
    var status: Status = .delivered

    enum Status { case sending, sent, delivered, read, failed }

    /// Adapter from a server message. `myUserId` decides bubble alignment.
    init(from server: HelenMessage, myUserId: String) {
        self.id = server.id
        self.senderId = server.senderId
        self.content = server.content
        self.timestamp = Conversation.parseISO(server.createdAt) ?? Date()
        self.isMine = server.senderId == myUserId
        self.status = .delivered
    }

    init(id: String, senderId: String, content: String, timestamp: Date,
         isMine: Bool, status: Status = .delivered) {
        self.id = id
        self.senderId = senderId
        self.content = content
        self.timestamp = timestamp
        self.isMine = isMine
        self.status = status
    }

    static let sample: [ChatMessage] = [
        .init(id: "m1", senderId: "u1", content: "مرحبا، انا على Helen Desktop",
              timestamp: Date().addingTimeInterval(-600), isMine: false, status: .read),
        .init(id: "m2", senderId: "me", content: "أهلين! أنا أراك من iPhone",
              timestamp: Date().addingTimeInterval(-540), isMine: true,  status: .read),
        .init(id: "m3", senderId: "u1", content: "الرسالة وصلت بدون internet — كل شيء على نفس الـWiFi",
              timestamp: Date().addingTimeInterval(-420), isMine: false, status: .read),
        .init(id: "m4", senderId: "me", content: "تمام — السيرفر :3088 وسط بيننا",
              timestamp: Date().addingTimeInterval(-360), isMine: true,  status: .read),
        .init(id: "m5", senderId: "u1", content: "Looks great. Let's ship the contacts redesign tonight.",
              timestamp: Date().addingTimeInterval(-120), isMine: false, status: .delivered),
        .init(id: "m6", senderId: "me", content: "On it.", timestamp: Date().addingTimeInterval(-60), isMine: true, status: .sent),
    ]
}
