import Foundation

/// One entry in the call log.
///
/// `repeatCount` mirrors Apple's Phone app: when a contact rings you twice
/// in a row, the list shows a single row with "(2)" appended — store that
/// count here instead of duplicating rows in the data layer.
struct CallRecord: Identifiable, Hashable {
    enum Kind: Hashable {
        case incoming
        case outgoing
        case missed
    }

    enum Channel: Hashable {
        case voice
        case video
    }

    let id: String
    var contactName: String
    var phone: String
    var kind: Kind
    var channel: Channel
    var date: Date
    var duration: TimeInterval?     // nil for missed
    var repeatCount: Int = 1
    var isUnknown: Bool = false     // true when no matching contact

    var isMissed: Bool { kind == .missed }
}

extension CallRecord {
    static let samples: [CallRecord] = {
        let now = Date()
        let cal = Calendar.current
        func ago(_ s: Int) -> Date { now.addingTimeInterval(TimeInterval(-s)) }
        func days(_ d: Int, hour: Int, minute: Int) -> Date {
            cal.date(bySettingHour: hour, minute: minute, second: 0,
                     of: cal.date(byAdding: .day, value: -d, to: now) ?? now) ?? now
        }
        return [
            // Today
            .init(id: "r1", contactName: "Yousef Salah",  phone: "+964 770 100 1001",
                  kind: .outgoing, channel: .voice, date: ago(60 * 12),       duration: 312),
            .init(id: "r2", contactName: "Helen Khalil",  phone: "+964 770 100 1002",
                  kind: .incoming, channel: .video, date: ago(60 * 47),       duration: 1206),
            .init(id: "r3", contactName: "Maya Saleh",    phone: "+964 770 100 1003",
                  kind: .missed,   channel: .voice, date: ago(60 * 95),       duration: nil, repeatCount: 3),
            .init(id: "r4", contactName: "Ahmed Al-Hadi", phone: "+964 770 100 1004",
                  kind: .outgoing, channel: .voice, date: ago(60 * 180),      duration: 84),
            // Yesterday
            .init(id: "r5", contactName: "Layla Karim",   phone: "+964 770 100 1005",
                  kind: .incoming, channel: .voice, date: days(1, hour: 19, minute: 22), duration: 540),
            .init(id: "r6", contactName: "Unknown",       phone: "+1 415 555 2002",
                  kind: .missed,   channel: .voice, date: days(1, hour: 14, minute: 11), duration: nil, isUnknown: true),
            // Earlier
            .init(id: "r7", contactName: "Omar Tariq",    phone: "+964 770 100 1006",
                  kind: .outgoing, channel: .video, date: days(3, hour: 11, minute: 8),  duration: 2340),
            .init(id: "r8", contactName: "Noor Fadel",    phone: "+964 770 100 1007",
                  kind: .missed,   channel: .voice, date: days(5, hour: 8,  minute: 41), duration: nil),
            .init(id: "r9", contactName: "Sara Hadi",     phone: "+964 770 100 1008",
                  kind: .incoming, channel: .voice, date: days(7, hour: 17, minute: 30), duration: 96),
        ]
    }()
}
