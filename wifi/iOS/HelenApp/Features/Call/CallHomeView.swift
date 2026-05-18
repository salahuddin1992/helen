import SwiftUI

/// Call home screen — the main shell that pulls the header, search, list,
/// FAB, and custom tab bar together.
struct CallHomeView: View {

    // MARK: – state
    @State private var search: String = ""
    @State private var selectedTab: CustomTabBar.Tab = .contacts
    @State private var contacts: [CallContact] = CallContact.samples
    @State private var showAddSheet = false

    // MARK: – inputs
    var userName: String = "Yousef Salah"
    var isConnected: Bool = true

    @Environment(\.theme) private var theme

    var body: some View {
        ZStack(alignment: .bottom) {
            theme.colors.background.ignoresSafeArea()

            mainContent

            // Floating layer — FAB sits above the tab bar.
            VStack(spacing: HelenSpace.md) {
                HStack {
                    Spacer()
                    AddContactFAB { showAddSheet = true }
                        .padding(.trailing, HelenSpace.pageH)
                }
                CustomTabBar(selection: $selectedTab)
                    .padding(.bottom, HelenSpace.sm)
            }
        }
        .sheet(isPresented: $showAddSheet) {
            AddContactPlaceholder()
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }

    // MARK: – content per tab

    @ViewBuilder
    private var mainContent: some View {
        switch selectedTab {
        case .contacts:  contactsList(contacts: filtered, emptyMessage: "No contacts match your search.")
        case .recents:   contactsList(contacts: filtered, emptyMessage: "Your recent calls will appear here.")
        case .favorites: contactsList(contacts: filtered.filter(\.isFavorite),
                                      emptyMessage: "Star a contact to find them faster.")
        case .settings:  settingsPlaceholder
        }
    }

    private func contactsList(contacts: [CallContact],
                              emptyMessage: LocalizedStringKey) -> some View {
        ScrollView {
            VStack(spacing: HelenSpace.lg) {
                CallHeaderView(userName: userName, isConnected: isConnected)

                HelenSearchBar(text: $search, placeholder: "Search contacts")
                    .padding(.horizontal, HelenSpace.pageH)

                if contacts.isEmpty {
                    HelenEmptyState(
                        symbol: "person.crop.circle.badge.questionmark",
                        title: "Nothing here yet",
                        message: emptyMessage
                    )
                    .padding(.top, HelenSpace.xl)
                } else {
                    LazyVStack(spacing: HelenSpace.md) {
                        ForEach(contacts) { c in
                            ContactCardView(
                                contact: c,
                                onOpen: {},
                                onCall: { startCall(with: c) }
                            )
                        }
                    }
                    .padding(.horizontal, HelenSpace.pageH)
                }

                // Bottom space so the FAB + tab bar never overlap content
                Color.clear.frame(height: 140)
            }
            .padding(.top, HelenSpace.xs)
        }
        .scrollIndicators(.hidden)
    }

    private var settingsPlaceholder: some View {
        VStack {
            CallHeaderView(userName: userName, isConnected: isConnected)
            HelenEmptyState(
                symbol: "gearshape",
                title: "Settings",
                message: "Configure your call experience here."
            )
            Spacer()
        }
    }

    // MARK: – derived

    private var filtered: [CallContact] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return contacts }
        return contacts.filter {
            $0.name.lowercased().contains(q) ||
            $0.phone.replacingOccurrences(of: " ", with: "").contains(q)
        }
    }

    private func startCall(with contact: CallContact) {
        // Hook up to the actual call service later. For now: haptic only.
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }
}

// MARK: – Add-contact placeholder sheet

private struct AddContactPlaceholder: View {
    @State private var name  = ""
    @State private var phone = ""
    @Environment(\.dismiss) private var dismiss
    @Environment(\.theme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: HelenSpace.lg) {
            HStack {
                Text("New contact")
                    .font(HelenFont.title2)
                    .foregroundStyle(theme.colors.textPrimary)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title3)
                        .foregroundStyle(theme.colors.textTertiary)
                }
                .accessibilityLabel("Close")
            }
            HelenTextField(label: "Name",  text: $name,
                           placeholder: "Full name", icon: "person")
            HelenTextField(label: "Phone", text: $phone,
                           placeholder: "+964 …",     icon: "phone",
                           keyboard: .phonePad)
            Spacer()
            HelenButton(title: "Save contact",
                        icon: "checkmark",
                        variant: .primary,
                        isDisabled: name.isEmpty || phone.isEmpty) {
                dismiss()
            }
        }
        .padding(HelenSpace.pageH)
        .padding(.top, HelenSpace.sm)
        .background(theme.colors.background)
    }
}

// MARK: – previews

#Preview("Call · Light · iPhone SE") {
    CallHomeView()
        .preferredColorScheme(.light)
        .previewDevice("iPhone SE (3rd generation)")
}

#Preview("Call · Dark · 15 Pro Max") {
    CallHomeView()
        .preferredColorScheme(.dark)
        .previewDevice("iPhone 15 Pro Max")
}

#Preview("Call · العربية · RTL") {
    CallHomeView()
        .environment(\.locale, .init(identifier: "ar"))
        .environment(\.layoutDirection, .rightToLeft)
}
