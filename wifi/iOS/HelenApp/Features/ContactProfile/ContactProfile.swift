import Foundation

/// A richer contact model than the basic `Contact` — surfaces the fields
/// the profile screen needs as separate properties so the UI doesn't have
/// to parse strings.
struct ContactProfile: Identifiable, Hashable {
    let id: String
    var displayName: String
    var title: String?              // job title
    var company: String?
    var phone: String
    var email: String?
    var photoURL: URL? = nil
    var presence: HelenAvatar.Presence
    var notes: String?
    var interactions: [Interaction] = []
    var isFavorite: Bool = false
}

struct Interaction: Identifiable, Hashable {
    enum Kind: Hashable {
        case callIncoming
        case callOutgoing
        case callMissed
        case message
        case email
    }

    let id: String
    var kind: Kind
    var summary: String
    var date: Date
}

extension ContactProfile {
    static let sample = ContactProfile(
        id: "u2",
        displayName: "Helen Khalil",
        title: "Lead Product Engineer",
        company: "Helen Networks",
        phone: "+964 770 100 1002",
        email: "helen@helennet.io",
        presence: .online,
        notes:
            "Met at the LAN-comms summit in Erbil. Always reachable on Wi-Fi 6E. " +
            "Prefers async messages outside of working hours.",
        interactions: [
            .init(id: "i1", kind: .callOutgoing, summary: "Outgoing voice · 5m 12s",
                  date: Date().addingTimeInterval(-3 * 3600)),
            .init(id: "i2", kind: .message,      summary: "“Shipping the redesign tonight.”",
                  date: Date().addingTimeInterval(-86_400)),
            .init(id: "i3", kind: .callMissed,   summary: "Missed video call",
                  date: Date().addingTimeInterval(-2 * 86_400)),
            .init(id: "i4", kind: .email,        summary: "Re: Q2 OKR review",
                  date: Date().addingTimeInterval(-5 * 86_400)),
        ],
        isFavorite: true
    )
}
