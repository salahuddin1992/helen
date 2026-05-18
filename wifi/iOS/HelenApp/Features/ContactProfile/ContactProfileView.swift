import SwiftUI

/// Premium contact-detail screen.
///
/// Composed top-down: hero → quick actions → info → notes → recent
/// activity → edit. Each block is its own card so feature-level layout
/// is just a vertical stack — the cards handle their own chrome.
struct ContactProfileView: View {
    let profile: ContactProfile
    var onEdit:    () -> Void = {}
    var onCall:    () -> Void = {}
    var onMessage: () -> Void = {}
    var onVideo:   () -> Void = {}
    var onEmail:   () -> Void = {}

    @State private var notesExpanded = false
    @State private var isFavorite: Bool

    @Environment(\.theme) private var theme
    @Environment(\.dismiss) private var dismiss

    init(profile: ContactProfile,
         onEdit:    @escaping () -> Void = {},
         onCall:    @escaping () -> Void = {},
         onMessage: @escaping () -> Void = {},
         onVideo:   @escaping () -> Void = {},
         onEmail:   @escaping () -> Void = {}) {
        self.profile   = profile
        self.onEdit    = onEdit
        self.onCall    = onCall
        self.onMessage = onMessage
        self.onVideo   = onVideo
        self.onEmail   = onEmail
        _isFavorite    = State(initialValue: profile.isFavorite)
    }

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()

            ScrollView {
                VStack(spacing: HelenSpace.xl) {
                    ProfileHeroView(profile: profile)

                    VStack(spacing: HelenSpace.lg) {
                        ProfileActionRow(
                            canEmail: profile.email != nil,
                            onCall:     onCall,
                            onMessage:  onMessage,
                            onFaceTime: onVideo,
                            onEmail:    onEmail
                        )

                        infoCard
                        if profile.notes != nil { notesCard }
                        if !profile.interactions.isEmpty { interactionsCard }
                        editButton
                    }
                    .padding(.horizontal, HelenSpace.pageH)
                    .padding(.bottom, HelenSpace.huge)
                }
            }
            .scrollIndicators(.hidden)
        }
        .navigationBarBackButtonHidden(true)
        .toolbar(content: toolbarContent)
        .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
    }

    // MARK: – Toolbar

    @ToolbarContentBuilder
    private func toolbarContent() -> some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Button { dismiss() } label: {
                Image(systemName: "chevron.backward")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
            }
            .accessibilityLabel("Back")
        }
        ToolbarItem(placement: .topBarTrailing) {
            Button {
                UISelectionFeedbackGenerator().selectionChanged()
                withAnimation(HelenMotion.standard) { isFavorite.toggle() }
            } label: {
                Image(systemName: isFavorite ? "star.fill" : "star")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(isFavorite ? theme.colors.warning
                                                : theme.colors.accent)
                    .contentTransition(.symbolEffect(.replace))
            }
            .accessibilityLabel(isFavorite ? "Remove from favorites" : "Add to favorites")
        }
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button { } label: { Label("Share contact", systemImage: "square.and.arrow.up") }
                Button { } label: { Label("Mute notifications", systemImage: "bell.slash") }
                Button(role: .destructive) {} label: { Label("Block contact", systemImage: "hand.raised") }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
            }
            .accessibilityLabel("More")
        }
    }

    // MARK: – Cards

    private var infoCard: some View {
        VStack(spacing: 0) {
            InfoLine(symbol: "phone.fill",   label: "Mobile",
                     value: profile.phone,   tappable: true)
            if let email = profile.email {
                Divider().overlay(theme.colors.divider).padding(.leading, 60)
                InfoLine(symbol: "envelope.fill", label: "Email",
                         value: email, tappable: true)
            }
            if let company = profile.company {
                Divider().overlay(theme.colors.divider).padding(.leading, 60)
                InfoLine(symbol: "building.2.fill", label: "Company",
                         value: company, tappable: false)
            }
            if let title = profile.title {
                Divider().overlay(theme.colors.divider).padding(.leading, 60)
                InfoLine(symbol: "briefcase.fill", label: "Title",
                         value: title, tappable: false)
            }
        }
        .helenCardSurface()
    }

    private var notesCard: some View {
        HelenCard {
            VStack(alignment: .leading, spacing: HelenSpace.sm) {
                HStack {
                    Text("Notes")
                        .font(HelenFont.headline)
                        .foregroundStyle(theme.colors.textPrimary)
                    Spacer()
                    Image(systemName: "lock.fill")
                        .font(.caption2)
                        .foregroundStyle(theme.colors.textTertiary)
                }
                Text(profile.notes ?? "")
                    .font(HelenFont.body)
                    .foregroundStyle(theme.colors.textSecondary)
                    .lineLimit(notesExpanded ? nil : 3)
                    .animation(HelenMotion.standard, value: notesExpanded)

                if (profile.notes?.count ?? 0) > 100 {
                    Button {
                        withAnimation(HelenMotion.standard) { notesExpanded.toggle() }
                    } label: {
                        Text(notesExpanded ? "Show less" : "Read more")
                            .font(HelenFont.footnote.weight(.semibold))
                            .foregroundStyle(theme.colors.accent)
                    }
                    .buttonStyle(.plain)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var interactionsCard: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            HelenSectionHeader(
                title: "Recent activity",
                actionTitle: "See all",
                actionIcon: "chevron.forward",
                action: {}
            )

            VStack(spacing: 0) {
                ForEach(Array(profile.interactions.enumerated()), id: \.element.id) { idx, item in
                    InteractionRow(interaction: item)
                    if idx < profile.interactions.count - 1 {
                        Divider().overlay(theme.colors.divider).padding(.leading, 60)
                    }
                }
            }
            .helenCardSurface()
        }
    }

    private var editButton: some View {
        HelenButton(
            title: "Edit Contact",
            icon: "pencil",
            variant: .secondary,
            action: onEdit
        )
        .padding(.top, HelenSpace.sm)
    }
}

// MARK: – InfoLine (private to this screen — no need to share)

private struct InfoLine: View {
    let symbol: String
    let label: LocalizedStringKey
    let value: String
    let tappable: Bool

    @Environment(\.theme) private var theme

    var body: some View {
        Button {
            guard tappable else { return }
            UISelectionFeedbackGenerator().selectionChanged()
            UIPasteboard.general.string = value
        } label: {
            HStack(spacing: HelenSpace.md) {
                ZStack {
                    RoundedRectangle(cornerRadius: HelenRadius.sm, style: .continuous)
                        .fill(theme.colors.accentMuted)
                    Image(systemName: symbol)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(theme.colors.accent)
                }
                .frame(width: 36, height: 36)

                VStack(alignment: .leading, spacing: 2) {
                    Text(label)
                        .font(HelenFont.caption.weight(.medium))
                        .foregroundStyle(theme.colors.textSecondary)
                    Text(value)
                        .font(HelenFont.body)
                        .foregroundStyle(tappable ? theme.colors.accent
                                                  : theme.colors.textPrimary)
                        .lineLimit(1)
                }
                Spacer()
                if tappable {
                    Image(systemName: "doc.on.doc")
                        .font(.caption)
                        .foregroundStyle(theme.colors.textTertiary)
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, HelenSpace.lg)
            .padding(.vertical, HelenSpace.md)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!tappable)
        .accessibilityLabel("\(value)")
        .accessibilityHint(tappable ? Text("Double-tap to copy") : Text(""))
    }
}

// MARK: – previews

#Preview("Profile · Light") {
    NavigationStack {
        ContactProfileView(profile: .sample)
    }
    .preferredColorScheme(.light)
}

#Preview("Profile · Dark · iPhone SE") {
    NavigationStack {
        ContactProfileView(profile: .sample)
    }
    .preferredColorScheme(.dark)
    .previewDevice("iPhone SE (3rd generation)")
}

#Preview("Profile · العربية · RTL") {
    NavigationStack {
        ContactProfileView(profile: .sample)
    }
    .environment(\.locale, .init(identifier: "ar"))
    .environment(\.layoutDirection, .rightToLeft)
}
