import SwiftUI

/// A label/value row inside the detail screen's grouped list. Tappable
/// values are copied to the clipboard with a haptic tick.
struct DetailField: View {
    let label: LocalizedStringKey
    let value: String
    var copyable: Bool

    var body: some View {
        Button {
            guard copyable else { return }
            UIPasteboard.general.string = value
            Haptic.selection()
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(label)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .textCase(nil)
                Text(value)
                    .font(.body)
                    .foregroundStyle(copyable ? Color.accentColor : .primary)
            }
            .padding(.vertical, 2)
        }
        .buttonStyle(.plain)
        .disabled(!copyable)
        .accessibilityHint(copyable ? Text("Double-tap to copy") : Text(""))
    }
}
