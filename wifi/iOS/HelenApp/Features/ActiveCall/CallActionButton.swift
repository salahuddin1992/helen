import SwiftUI

/// A reusable circular call-screen button.
///
/// Three behavior shapes:
///   • `.toggle`   — flips an `@Binding<Bool>` on tap (Mute, Speaker, Video)
///   • `.action`   — fires a one-shot closure (Add Call, Keypad)
///   • `.destructive` — same as action but rendered in red (End Call)
struct CallActionButton: View {

    enum Style { case toggle, action, destructive }

    let icon: String
    let label: LocalizedStringKey
    var iconActive: String? = nil
    var style: Style = .action
    var size: CGFloat = 72
    var isOn: Binding<Bool> = .constant(false)
    var action: () -> Void = {}

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: style == .destructive ? .heavy : .medium)
                .impactOccurred()
            if style == .toggle {
                withAnimation(HelenMotion.standard) { isOn.wrappedValue.toggle() }
            }
            action()
        } label: {
            VStack(spacing: HelenSpace.sm) {
                ZStack {
                    Circle().fill(background)
                    Circle()
                        .strokeBorder(borderColor, lineWidth: 1)
                        .opacity(style == .destructive ? 0 : 1)
                    Image(systemName: currentIcon)
                        .font(.system(size: size * 0.34, weight: .semibold))
                        .foregroundStyle(foreground)
                        .symbolRenderingMode(.hierarchical)
                        .contentTransition(.symbolEffect(.replace))
                }
                .frame(width: size, height: size)
                .helenShadow(style == .destructive ? .lg : .none)

                Text(label)
                    .font(HelenFont.caption.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.92))
                    .lineLimit(1)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableScaleStyle(scale: 0.92))
        .accessibilityLabel(label)
        .accessibilityValue(style == .toggle ? Text(isOn.wrappedValue ? "On" : "Off") : Text(""))
        .accessibilityAddTraits(.isButton)
    }

    private var currentIcon: String {
        guard style == .toggle, isOn.wrappedValue, let on = iconActive else { return icon }
        return on
    }

    // Glassy off state, white "on" state, solid red destructive.
    private var background: Color {
        switch style {
        case .destructive:
            return Color(red: 0.93, green: 0.20, blue: 0.20)
        case .toggle:
            return isOn.wrappedValue ? .white : Color.white.opacity(0.18)
        case .action:
            return Color.white.opacity(0.18)
        }
    }
    private var foreground: Color {
        switch style {
        case .destructive:
            return .white
        case .toggle:
            return isOn.wrappedValue ? Color(red: 0.10, green: 0.10, blue: 0.13) : .white
        case .action:
            return .white
        }
    }
    private var borderColor: Color {
        Color.white.opacity(0.10)
    }
}

#Preview {
    @Previewable @State var muted     = false
    @Previewable @State var speaker   = true
    @Previewable @State var video     = false
    return ZStack {
        CallBackdrop()
        VStack(spacing: HelenSpace.xxl) {
            HStack(spacing: HelenSpace.xl) {
                CallActionButton(icon: "mic.slash.fill", label: "Mute",
                                 iconActive: "mic.slash.fill",
                                 style: .toggle, isOn: $muted)
                CallActionButton(icon: "speaker.wave.2.fill", label: "Speaker",
                                 iconActive: "speaker.wave.3.fill",
                                 style: .toggle, isOn: $speaker)
                CallActionButton(icon: "video.fill", label: "Video",
                                 iconActive: "video.fill",
                                 style: .toggle, isOn: $video)
            }
            HStack(spacing: HelenSpace.xl) {
                CallActionButton(icon: "person.crop.circle.badge.plus", label: "Add", style: .action)
                CallActionButton(icon: "circle.grid.3x3.fill",          label: "Keypad", style: .action)
                CallActionButton(icon: "phone.down.fill",               label: "End",
                                 style: .destructive)
            }
        }
        .padding()
    }
}
