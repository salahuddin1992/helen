import Foundation

// MARK: - Person

struct Person: Identifiable, Hashable {
    let id = UUID()
    var name: String
    var phone: String
    var email: String? = nil
    /// One-liner like "Engineering at Helen". Render as-is, no parsing.
    var role: String? = nil
    var isFavorite: Bool = false
}

// MARK: - Call

struct Call: Identifiable, Hashable {
    let id = UUID()
    var name: String
    var phone: String
    var kind: Kind
    var date: Date
    var video: Bool = false

    enum Kind { case incoming, outgoing, missed }
}

// MARK: - Mock data

enum MockData {

    static let people: [Person] = [
        .init(name: "Ahmed Al-Hadi", phone: "+964 770 100 1004"),
        .init(name: "Helen Khalil",  phone: "+964 770 100 1002",
              email: "helen@helen.app", role: "Engineering at Helen", isFavorite: true),
        .init(name: "Layla Karim",   phone: "+964 770 100 1005",
              email: "layla@karim.io", role: "Director at Karim", isFavorite: true),
        .init(name: "Maya Saleh",    phone: "+964 770 100 1003",
              email: "maya@design.co", role: "Design at Design Co"),
        .init(name: "Noor Fadel",    phone: "+964 770 100 1007"),
        .init(name: "Omar Tariq",    phone: "+964 770 100 1006",
              role: "Iraq Ventures"),
        .init(name: "Sara Hadi",     phone: "+964 770 100 1008",
              email: "sara@hadi.dev", role: "Engineering at Hadi"),
        .init(name: "Yousef Salah",  phone: "+964 770 100 1001",
              email: "yousef@helen.app", role: "Founder at Helen", isFavorite: true),
    ]

    static let calls: [Call] = {
        let now = Date()
        func t(_ s: TimeInterval) -> Date { now.addingTimeInterval(-s) }
        return [
            .init(name: "Helen Khalil", phone: "+964 770 100 1002", kind: .incoming, date: t(2_400),  video: true),
            .init(name: "Yousef Salah", phone: "+964 770 100 1001", kind: .outgoing, date: t(720)),
            .init(name: "Maya Saleh",   phone: "+964 770 100 1003", kind: .missed,   date: t(5_400)),
            .init(name: "Layla Karim",  phone: "+964 770 100 1005", kind: .incoming, date: t(93_000)),
            .init(name: "Unknown",      phone: "+1 415 555 2002",   kind: .missed,   date: t(96_000)),
            .init(name: "Omar Tariq",   phone: "+964 770 100 1006", kind: .outgoing, date: t(259_000), video: true),
            .init(name: "Noor Fadel",   phone: "+964 770 100 1007", kind: .missed,   date: t(432_000)),
        ]
    }()
}
