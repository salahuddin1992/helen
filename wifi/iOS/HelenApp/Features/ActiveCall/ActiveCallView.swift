import SwiftUI

/// Full-screen call-in-progress UI: gradient, caller, action grid, end call.
///
/// All visual chrome is white-on-gradient so the screen reads instantly as
/// "in-call" regardless of system appearance. The grid uses a single
/// reusable `CallActionButton` — adding a future action means one more cell.
struct ActiveCallView: View {

    // MARK: – inputs
    let callerName: String
    let phone:      String
    var imageURL:   URL? = nil
    var startedAt:  Date = .now

    var onEnd:    () -> Void = {}
    var onAdd:    () -> Void = {}
    var onKeypad: () -> Void = {}

    // MARK: – state
    @State private var isMuted    = false
    @State private var isSpeaker  = false
    @State private var isVideo    = false
    @State private var showKeypad = false

    var body: some View {
        ZStack {
            CallBackdrop()

            VStack(spacing: 0) {
                callerSection
                Spacer(minLength: HelenSpace.lg)
                actionGrid
                Spacer(minLength: HelenSpace.xl)
                endCallButton
                    .padding(.bottom, HelenSpace.xl)
            }
            .padding(.horizontal, HelenSpace.pageH)
            .padding(.top, HelenSpace.xxl)
        }
        .preferredColorScheme(.dark)        // gradient + glass UI is always dark-styled
        .statusBarHidden(false)
        .sheet(isPresented: $showKeypad) {
            CallKeypadSheet()
                .presentationDetents([.medium])
                .presentationDragIndicator(.visible)
                .presentationBackground(.thinMaterial)
        }
    }

    // MARK: – sections

    private var callerSection: some View {
        VStack(spacing: HelenSpace.md) {
            CallTimerView(start: startedAt)
                .padding(.horizontal, HelenSpace.md)
                .padding(.vertical, 4)
                .background(.white.opacity(0.12), in: Capsule())

            CallerAvatarView(name: callerName, imageURL: imageURL, size: 168)
                .padding(.top, HelenSpace.md)

            VStack(spacing: HelenSpace.xs) {
                Text(callerName)
                    .font(HelenFont.title.weight(.semibold))
                    .foregroundStyle(.white)
                Text(phone)
                    .font(HelenFont.subhead.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.7))
            }
            .multilineTextAlignment(.center)
            .accessibilityElement(children: .combine)
        }
    }

    private var actionGrid: some View {
        // 2 rows × 3 columns.
        let columns = Array(repeating: GridItem(.flexible(), spacing: HelenSpace.lg), count: 3)
        return LazyVGrid(columns: columns, spacing: HelenSpace.xl) {
            CallActionButton(
                icon: "mic.fill",
                label: "Mute",
                iconActive: "mic.slash.fill",
                style: .toggle,
                isOn: $isMuted
            )
            CallActionButton(
                icon: "circle.grid.3x3.fill",
                label: "Keypad",
                style: .action,
                action: { showKeypad = true; onKeypad() }
            )
            CallActionButton(
                icon: "speaker.wave.2.fill",
                label: "Speaker",
                iconActive: "speaker.wave.3.fill",
                style: .toggle,
                isOn: $isSpeaker
            )

            CallActionButton(
                icon: "person.crop.circle.badge.plus",
                label: "Add Call",
                style: .action,
                action: onAdd
            )
            CallActionButton(
                icon: "video.fill",
                label: "Video",
                iconActive: "video.slash.fill",
                style: .toggle,
                isOn: $isVideo
            )
            CallActionButton(
                icon: "person.crop.rectangle.stack.fill",
                label: "Contacts",
                style: .action
            )
        }
    }

    private var endCallButton: some View {
        CallActionButton(
            icon: "phone.down.fill",
            label: "End",
            style: .destructive,
            size: 78,
            action: {
                UINotificationFeedbackGenerator().notificationOccurred(.warning)
                onEnd()
            }
        )
        .accessibilityHint("Hangs up the current call")
    }
}

// MARK: – keypad sheet (kept private, lives only with this screen)

private struct CallKeypadSheet: View {
    @State private var dialed: String = ""
    @Environment(\.dismiss) private var dismiss

    private let keys: [[String]] = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        ["*", "0", "#"],
    ]

    var body: some View {
        VStack(spacing: HelenSpace.lg) {
            Text(dialed.isEmpty ? " " : dialed)
                .font(HelenFont.title.monospacedDigit())
                .foregroundStyle(.white)
                .frame(height: 40)

            VStack(spacing: HelenSpace.md) {
                ForEach(keys, id: \.self) { row in
                    HStack(spacing: HelenSpace.xl) {
                        ForEach(row, id: \.self) { k in
                            Button {
                                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                                dialed.append(k)
                            } label: {
                                Text(k)
                                    .font(.system(size: 32, weight: .light))
                                    .foregroundStyle(.white)
                                    .frame(width: 64, height: 64)
                                    .background(.white.opacity(0.18))
                                    .clipShape(Circle())
                            }
                            .buttonStyle(PressableScaleStyle())
                            .accessibilityLabel("Key \(k)")
                        }
                    }
                }
            }

            Button { dismiss() } label: {
                Text("Hide")
                    .font(HelenFont.bodyEmph)
                    .foregroundStyle(.white)
                    .padding(.vertical, 8).padding(.horizontal, 20)
                    .background(.white.opacity(0.18), in: Capsule())
            }
            .buttonStyle(PressableScaleStyle())
        }
        .padding(.vertical, HelenSpace.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: – previews

#Preview("Call · Light") {
    ActiveCallView(
        callerName: "Yousef Salah",
        phone: "+964 770 100 1001",
        startedAt: Date().addingTimeInterval(-32)
    )
    .preferredColorScheme(.light)
}

#Preview("Call · Dark · iPhone SE") {
    ActiveCallView(
        callerName: "Helen Khalil",
        phone: "+964 770 100 1002",
        startedAt: Date().addingTimeInterval(-184)
    )
    .preferredColorScheme(.dark)
    .previewDevice("iPhone SE (3rd generation)")
}

#Preview("Call · العربية · RTL") {
    ActiveCallView(
        callerName: "ليلى كريم",
        phone: "+964 770 100 1005",
        startedAt: Date().addingTimeInterval(-7)
    )
    .environment(\.locale, .init(identifier: "ar"))
    .environment(\.layoutDirection, .rightToLeft)
}
