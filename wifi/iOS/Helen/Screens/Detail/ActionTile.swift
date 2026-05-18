import SwiftUI

/// One of four quick actions on the contact detail header (Call, Message,
/// Video, Mail). Material-backed so it sits cleanly on the inset-grouped
/// background in both color schemes.
struct ActionTile: View {
    let symbol: String
    let title: LocalizedStringKey
    var enabled: Bool = true
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.tap()
            action()
        } label: {
            VStack(spacing: 6) {
                Image(systemName: symbol)
                    .font(.callout)
                    .accessibilityHidden(true)
                Text(title)
                    .font(.caption2)
            }
            .foregroundStyle(enabled ? Color.accentColor : .secondary)
            .frame(maxWidth: .infinity)
            .frame(height: 64)
            .background(.fill.tertiary,
                        in: RoundedRectangle(cornerRadius: Theme.Radius.md, style: .continuous))
            .opacity(enabled ? 1 : 0.45)
        }
        .buttonStyle(PressShrink(scale: 0.95))
        .disabled(!enabled)
        .accessibilityLabel(title)
    }
}
