import SwiftUI

/// One row in the Recents list. Renders compactly so a full screen of
/// rows looks calm — most weight goes to the name, everything else is
/// secondary chrome.
struct CallRecordRow: View {
    let record: CallRecord

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.md) {
            HelenAvatar(name: record.contactName, size: .md)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(record.contactName)
                        .font(HelenFont.bodyMed)
                        .foregroundStyle(record.isMissed
                                         ? theme.colors.danger
                                         : theme.colors.textPrimary)
                        .lineLimit(1)
                    if record.repeatCount > 1 {
                        Text("(\(record.repeatCount))")
                            .font(HelenFont.subhead.monospacedDigit())
                            .foregroundStyle(record.isMissed
                                             ? theme.colors.danger
                                             : theme.colors.textSecondary)
                    }
                }

                HStack(spacing: 4) {
                    Image(systemName: kindSymbol)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(kindColor)
                    Text(subtitleText)
                        .font(HelenFont.footnote)
                        .foregroundStyle(theme.colors.textSecondary)
                        .lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .trailing, spacing: HelenSpace.sm) {
                Text(timeText)
                    .font(HelenFont.footnote.monospacedDigit())
                    .foregroundStyle(theme.colors.textSecondary)
            }

            // Apple-standard "ⓘ" affordance — opens detail without
            // disturbing the row tap target (which calls the contact).
            Button {
                UISelectionFeedbackGenerator().selectionChanged()
            } label: {
                Image(systemName: "info.circle")
                    .font(.body)
                    .foregroundStyle(theme.colors.accent)
                    .frame(width: 30, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Call info")
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    // MARK: – derived bits

    private var kindSymbol: String {
        switch (record.kind, record.channel) {
        case (.incoming, .video):  return "video.fill"
        case (.outgoing, .video):  return "video.fill"
        case (.incoming, .voice):  return "arrow.down.left"
        case (.outgoing, .voice):  return "arrow.up.right"
        case (.missed,   .video):  return "video.slash.fill"
        case (.missed,   .voice):  return "arrow.down.left"
        }
    }
    private var kindColor: Color {
        record.isMissed ? theme.colors.danger : theme.colors.textSecondary
    }
    private var subtitleText: String {
        let label: String = {
            switch record.kind {
            case .incoming: return record.channel == .video ? "Incoming video" : "Incoming"
            case .outgoing: return record.channel == .video ? "Outgoing video" : "Outgoing"
            case .missed:   return record.channel == .video ? "Missed video"   : "Missed"
            }
        }()
        if let dur = record.duration { return "\(label) · \(format(duration: dur))" }
        return label
    }
    private var timeText: String {
        let f = DateFormatter()
        f.locale = .current
        if Calendar.current.isDateInToday(record.date) {
            f.timeStyle = .short
        } else if Calendar.current.isDateInYesterday(record.date) {
            return NSLocalizedString("Yesterday", comment: "")
        } else if Calendar.current.isDate(record.date, equalTo: .now, toGranularity: .weekOfYear) {
            f.dateFormat = "EEE"
        } else {
            f.dateFormat = "MMM d"
        }
        return f.string(from: record.date)
    }
    private var accessibilityText: String {
        let kind: String = {
            switch record.kind {
            case .incoming: return NSLocalizedString("Incoming", comment: "")
            case .outgoing: return NSLocalizedString("Outgoing", comment: "")
            case .missed:   return NSLocalizedString("Missed",   comment: "")
            }
        }()
        return "\(kind), \(record.contactName), \(timeText)"
    }
    private func format(duration: TimeInterval) -> String {
        let s = max(0, Int(duration))
        if s >= 3600 { return String(format: "%dh %dm", s / 3600, (s % 3600) / 60) }
        if s >= 60   { return String(format: "%dm %ds", s / 60, s % 60) }
        return       String(format: "%ds", s)
    }
}

#Preview {
    List {
        CallRecordRow(record: CallRecord.samples[0])
        CallRecordRow(record: CallRecord.samples[1])
        CallRecordRow(record: CallRecord.samples[2])
        CallRecordRow(record: CallRecord.samples[5])
    }
    .listStyle(.insetGrouped)
}
