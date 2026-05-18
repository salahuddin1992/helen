import SwiftUI

/// Floating, capsule-shaped tab bar. Selected item shows label + filled
/// icon on an accent-tinted pill; unselected items show only the icon.
/// Uses `matchedGeometryEffect` so the pill slides between items.
struct CustomTabBar: View {

    enum Tab: Hashable, CaseIterable {
        case contacts, recents, favorites, settings

        var label: LocalizedStringKey {
            switch self {
            case .contacts:  return "Contacts"
            case .recents:   return "Recents"
            case .favorites: return "Favorites"
            case .settings:  return "Settings"
            }
        }
        var iconOff: String {
            switch self {
            case .contacts:  return "person.2"
            case .recents:   return "clock.arrow.circlepath"
            case .favorites: return "star"
            case .settings:  return "gearshape"
            }
        }
        var iconOn: String {
            switch self {
            case .contacts:  return "person.2.fill"
            case .recents:   return "clock.arrow.circlepath"
            case .favorites: return "star.fill"
            case .settings:  return "gearshape.fill"
            }
        }
    }

    @Binding var selection: Tab
    @Namespace private var ns
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.xs) {
            ForEach(Tab.allCases, id: \.self) { tab in
                TabItem(
                    tab: tab,
                    isSelected: selection == tab,
                    namespace: ns
                ) {
                    guard selection != tab else { return }
                    UISelectionFeedbackGenerator().selectionChanged()
                    withAnimation(HelenMotion.standard) { selection = tab }
                }
            }
        }
        .padding(6)
        .background(
            Capsule().fill(theme.colors.surface)
                .helenShadow(.lg)
        )
        .overlay(
            Capsule().strokeBorder(theme.colors.border, lineWidth: 0.5)
        )
        .padding(.horizontal, HelenSpace.pageH)
    }
}

private struct TabItem: View {
    let tab: CustomTabBar.Tab
    let isSelected: Bool
    let namespace: Namespace.ID
    let action: () -> Void

    @Environment(\.theme) private var theme

    var body: some View {
        Button(action: action) {
            HStack(spacing: HelenSpace.xs) {
                Image(systemName: isSelected ? tab.iconOn : tab.iconOff)
                    .font(.body.weight(.semibold))
                    .symbolEffect(.bounce, value: isSelected)
                if isSelected {
                    Text(tab.label)
                        .font(HelenFont.subhead.weight(.semibold))
                        .lineLimit(1)
                        .transition(.scale(scale: 0.8).combined(with: .opacity))
                }
            }
            .foregroundStyle(isSelected ? theme.colors.textOnAccent
                                        : theme.colors.textSecondary)
            .padding(.horizontal, isSelected ? HelenSpace.md : HelenSpace.sm)
            .padding(.vertical, HelenSpace.sm)
            .frame(minHeight: 44)
            .frame(maxWidth: isSelected ? .infinity : nil)
            .background {
                if isSelected {
                    Capsule()
                        .fill(theme.colors.accent)
                        .matchedGeometryEffect(id: "tab-pill", in: namespace)
                }
            }
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(tab.label)
        .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : .isButton)
    }
}

#Preview {
    @Previewable @State var sel: CustomTabBar.Tab = .contacts
    return VStack {
        Spacer()
        CustomTabBar(selection: $sel)
            .padding(.bottom, 24)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(HelenColor.background)
}
