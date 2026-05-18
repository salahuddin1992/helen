import SwiftUI

/// A custom large-title bar — gives us full control over typography and
/// trailing action layout without the quirks of `.navigationTitle`.
struct HelenNavBar<Trailing: View>: View {
    let title: LocalizedStringKey
    var subtitle: LocalizedStringKey? = nil
    @ViewBuilder var trailing: () -> Trailing

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(HelenFont.display)
                    .foregroundStyle(theme.colors.textPrimary)
                if let subtitle {
                    Text(subtitle)
                        .font(HelenFont.subhead)
                        .foregroundStyle(theme.colors.textSecondary)
                }
            }
            Spacer()
            trailing()
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.top, HelenSpace.lg)
        .padding(.bottom, HelenSpace.md)
    }
}

extension HelenNavBar where Trailing == EmptyView {
    init(title: LocalizedStringKey, subtitle: LocalizedStringKey? = nil) {
        self.title = title
        self.subtitle = subtitle
        self.trailing = { EmptyView() }
    }
}
