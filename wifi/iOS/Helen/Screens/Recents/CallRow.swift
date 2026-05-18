import SwiftUI

struct CallRow: View {
    let call: Call

    var body: some View {
        HStack(spacing: Theme.Space.md) {
            Image(systemName: arrowSymbol)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(call.kind == .missed ? .red : .secondary)
                .frame(width: 14)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 1) {
                Text(call.name)
                    .font(.body)
                    .foregroundStyle(call.kind == .missed ? .red : .primary)
                    .lineLimit(1)
                Text(metadata)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer(minLength: Theme.Space.sm)

            Text(timestamp)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)

            Image(systemName: "info.circle")
                .foregroundStyle(.tint)
                .padding(.leading, 2)
                .frame(minWidth: 28, minHeight: 28)
                .contentShape(Rectangle())
                .onTapGesture {}
                .accessibilityLabel("Call info")
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(spokenKind), \(call.name), \(timestamp)")
    }

    // MARK: – derived

    private var arrowSymbol: String {
        switch call.kind {
        case .incoming, .missed: return "arrow.down.left"
        case .outgoing:          return "arrow.up.right"
        }
    }

    private var metadata: String {
        let kind: String
        switch call.kind {
        case .incoming: kind = NSLocalizedString("Incoming", comment: "")
        case .outgoing: kind = NSLocalizedString("Outgoing", comment: "")
        case .missed:   kind = NSLocalizedString("Missed",   comment: "")
        }
        return call.video ? "\(kind) · FaceTime" : kind
    }

    private var spokenKind: String {
        switch call.kind {
        case .incoming: return NSLocalizedString("Incoming call", comment: "")
        case .outgoing: return NSLocalizedString("Outgoing call", comment: "")
        case .missed:   return NSLocalizedString("Missed call",   comment: "")
        }
    }

    private var timestamp: String {
        let cal = Calendar.current
        let f = DateFormatter(); f.locale = .current
        if cal.isDateInToday(call.date)         { f.timeStyle = .short }
        else if cal.isDateInYesterday(call.date) {
            return NSLocalizedString("Yesterday", comment: "")
        } else {
            f.dateFormat = "MMM d"
        }
        return f.string(from: call.date)
    }
}
