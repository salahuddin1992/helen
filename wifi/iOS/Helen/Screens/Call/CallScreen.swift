import SwiftUI

struct CallScreen: View {
    let person: Person
    @Environment(\.dismiss) private var dismiss

    @State private var muted   = false
    @State private var speaker = false
    @State private var video   = false
    @State private var startedAt = Date()
    @State private var hasAppeared = false

    var body: some View {
        ZStack {
            CallBackdrop()

            VStack(spacing: 0) {
                Spacer().frame(height: 56)

                Text(video ? "FaceTime" : "Phone")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.white.opacity(0.55))
                    .textCase(.uppercase)
                    .tracking(1.2)

                Text(person.name)
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(.white)
                    .padding(.top, Theme.Space.xs)

                TimelineView(.periodic(from: startedAt, by: 1)) { ctx in
                    Text(elapsed(ctx.date))
                        .font(.title3.monospacedDigit())
                        .foregroundStyle(.white.opacity(0.65))
                }
                .padding(.top, 2)

                Spacer()

                Avatar(name: person.name, diameter: 168, palette: .onDark)
                    .shadow(color: .black.opacity(0.35), radius: 24, y: 14)
                    .scaleEffect(hasAppeared ? 1.0 : 0.94)
                    .opacity(hasAppeared ? 1 : 0)

                Spacer()

                grid.padding(.horizontal, 32)

                endCall
                    .padding(.top, 32)
                    .padding(.bottom, 28)
            }
            .opacity(hasAppeared ? 1 : 0.0)
        }
        .preferredColorScheme(.dark)
        .onAppear {
            withAnimation(Theme.Motion.spring) { hasAppeared = true }
        }
    }

    // MARK: – Grid

    private var grid: some View {
        let cols = Array(repeating: GridItem(.flexible(), spacing: Theme.Space.lg), count: 3)
        return LazyVGrid(columns: cols, spacing: Theme.Space.xxl) {
            CallToggle(off: "mic.fill",            on: "mic.slash.fill",
                       title: "mute",              isOn: $muted)
            CallTap(symbol: "circle.grid.3x3.fill", title: "keypad") {}
            CallToggle(off: "speaker.wave.2.fill", on: "speaker.wave.3.fill",
                       title: "speaker",          isOn: $speaker)
            CallTap(symbol: "person.badge.plus",   title: "add")     {}
            CallToggle(off: "video.fill",          on: "video.slash.fill",
                       title: "video",            isOn: $video)
            CallTap(symbol: "person.crop.rectangle.stack.fill",
                    title: "contacts")            {}
        }
    }

    // MARK: – End

    private var endCall: some View {
        Button {
            Haptic.notice(.warning)
            dismiss()
        } label: {
            ZStack {
                Circle().fill(Color.red)
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 26, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 70, height: 70)
            .shadow(color: .black.opacity(0.4), radius: 16, y: 8)
        }
        .buttonStyle(PressShrink(scale: 0.92))
        .accessibilityLabel("End call")
    }

    // MARK: – Helpers

    private func elapsed(_ now: Date) -> String {
        let s = max(0, Int(now.timeIntervalSince(startedAt)))
        return s >= 3600
            ? String(format: "%d:%02d:%02d", s/3600, (s%3600)/60, s%60)
            : String(format: "%d:%02d", s/60, s%60)
    }
}

#Preview { CallScreen(person: MockData.people[1]) }
