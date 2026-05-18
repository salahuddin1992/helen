import SwiftUI

/// A custom segmented control with smooth selection animation.
/// Built on top of `Picker` so accessibility comes for free.
struct HelenSegmented<Value: Hashable>: View {
    let options: [(value: Value, label: LocalizedStringKey)]
    @Binding var selection: Value
    @Namespace private var ns
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: 0) {
            ForEach(options, id: \.value) { opt in
                let isSelected = opt.value == selection
                Button {
                    withAnimation(HelenMotion.standard) { selection = opt.value }
                    UISelectionFeedbackGenerator().selectionChanged()
                } label: {
                    Text(opt.label)
                        .font(HelenFont.subhead.weight(.semibold))
                        .foregroundStyle(isSelected
                                         ? theme.colors.textPrimary
                                         : theme.colors.textSecondary)
                        .padding(.vertical, 8)
                        .frame(maxWidth: .infinity)
                        .background {
                            if isSelected {
                                RoundedRectangle(cornerRadius: HelenRadius.sm,
                                                 style: .continuous)
                                    .fill(theme.colors.surface)
                                    .helenShadow(.sm)
                                    .matchedGeometryEffect(id: "seg", in: ns)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(theme.colors.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous))
    }
}
