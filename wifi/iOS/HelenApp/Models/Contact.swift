import Foundation

struct Contact: Identifiable, Hashable {
    let id: String
    var displayName: String
    var username: String
    var status: String?
    var avatarURL: URL?
    var presence: HelenAvatar.Presence
    var phone: String?
    var lastSeen: Date?

    init(from user: HelenUser) {
        self.id = user.id
        self.displayName = user.displayName ?? user.username
        self.username = user.username.hasPrefix("@") ? user.username : "@\(user.username)"
        self.status = user.status
        self.avatarURL = user.avatarUrl.flatMap(URL.init(string:))
        self.presence = Contact.presenceFromStatus(user.status)
        self.phone = nil
        self.lastSeen = nil
    }

    init(id: String, displayName: String, username: String, status: String?,
         avatarURL: URL?, presence: HelenAvatar.Presence,
         phone: String?, lastSeen: Date?) {
        self.id = id
        self.displayName = displayName
        self.username = username
        self.status = status
        self.avatarURL = avatarURL
        self.presence = presence
        self.phone = phone
        self.lastSeen = lastSeen
    }

    private static func presenceFromStatus(_ status: String?) -> HelenAvatar.Presence {
        switch status?.lowercased() {
        case "online": return .online
        case "away":   return .away
        default:       return .offline
        }
    }

    static let samples: [Contact] = [
        .init(id: "u1", displayName: "Yousef Salah",  username: "@yousf1",  status: "Online",       avatarURL: nil, presence: .online,  phone: "+964 770 100 1001", lastSeen: nil),
        .init(id: "u2", displayName: "Helen Khalil",  username: "@helenk",  status: "On a call",    avatarURL: nil, presence: .away,    phone: "+964 770 100 1002", lastSeen: Date().addingTimeInterval(-300)),
        .init(id: "u3", displayName: "Maya Saleh",    username: "@maya",    status: "Be right back", avatarURL: nil, presence: .away,   phone: "+964 770 100 1003", lastSeen: Date().addingTimeInterval(-1800)),
        .init(id: "u4", displayName: "Ahmed Al-Hadi", username: "@ahmed",   status: "Last seen 2h",  avatarURL: nil, presence: .offline, phone: "+964 770 100 1004", lastSeen: Date().addingTimeInterval(-7200)),
        .init(id: "u5", displayName: "Layla Karim",   username: "@layla",   status: "Online",       avatarURL: nil, presence: .online,  phone: "+964 770 100 1005", lastSeen: nil),
        .init(id: "u6", displayName: "Omar Tariq",    username: "@omar",    status: "Working",      avatarURL: nil, presence: .online,  phone: "+964 770 100 1006", lastSeen: nil),
        .init(id: "u7", displayName: "Noor Fadel",    username: "@noor",    status: "Last seen yesterday", avatarURL: nil, presence: .offline, phone: "+964 770 100 1007", lastSeen: Date().addingTimeInterval(-86400)),
    ]
}
