import Foundation

/// A contact specifically modeled for the call screen — same person as
/// `Contact` but with the phone field surfaced as required (not optional)
/// because every card has to render a number.
struct CallContact: Identifiable, Hashable {
    let id: String
    var name: String
    var phone: String
    var presence: HelenAvatar.Presence
    var avatarURL: URL? = nil
    var isFavorite: Bool = false

    static let samples: [CallContact] = [
        .init(id: "u1", name: "Yousef Salah",  phone: "+964 770 100 1001", presence: .online,  isFavorite: true),
        .init(id: "u2", name: "Helen Khalil",  phone: "+964 770 100 1002", presence: .online),
        .init(id: "u3", name: "Maya Saleh",    phone: "+964 770 100 1003", presence: .away,    isFavorite: true),
        .init(id: "u4", name: "Ahmed Al-Hadi", phone: "+964 770 100 1004", presence: .offline),
        .init(id: "u5", name: "Layla Karim",   phone: "+964 770 100 1005", presence: .online),
        .init(id: "u6", name: "Omar Tariq",    phone: "+964 770 100 1006", presence: .away),
        .init(id: "u7", name: "Noor Fadel",    phone: "+964 770 100 1007", presence: .offline),
        .init(id: "u8", name: "Sara Hadi",     phone: "+964 770 100 1008", presence: .online),
    ]
}
