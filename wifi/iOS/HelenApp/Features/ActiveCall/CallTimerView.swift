import SwiftUI

/// Counts up from a fixed start moment. Rebuilt every second via
/// `TimelineView` — no manual `Timer.scheduledTimer` to clean up.
struct CallTimerView: View {
    let start: Date
    var tint: Color = .white

    var body: some View {
        TimelineView(.periodic(from: start, by: 1)) { context in
            Text(format(context.date.timeIntervalSince(start)))
                .font(HelenFont.body.monospacedDigit().weight(.medium))
                .foregroundStyle(tint.opacity(0.85))
                .accessibilityLabel("Call duration \(verbose(context.date.timeIntervalSince(start)))")
        }
        .contentTransition(.numericText())
    }

    private func format(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        let h =  s / 3600
        let m = (s % 3600) / 60
        let r =  s % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, r)
            : String(format: "%02d:%02d", m, r)
    }

    private func verbose(_ seconds: TimeInterval) -> String {
        let f = DateComponentsFormatter()
        f.allowedUnits  = [.hour, .minute, .second]
        f.unitsStyle    = .full
        return f.string(from: seconds) ?? ""
    }
}
