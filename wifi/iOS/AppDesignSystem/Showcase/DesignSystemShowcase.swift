import SwiftUI

/// Live gallery of every token + component in the design system.
/// Open this in Xcode previews to see the system at a glance, or push it
/// into a debug menu to inspect on-device.
struct DesignSystemShowcase: View {
    @State private var search = ""
    @State private var name = ""
    @State private var password = ""

    private let contacts: [ContactCard.Model] = [
        .init(id: "u1", name: "Yousef Salah",  phone: "+964 770 100 1001", presence: .online,  isFavorite: true),
        .init(id: "u2", name: "Helen Khalil",  phone: "+964 770 100 1002", presence: .away),
        .init(id: "u3", name: "Maya Saleh",    phone: "+964 770 100 1003", presence: .offline),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: AppSpacing.xxl) {
                hero

                section("Buttons") {
                    VStack(spacing: AppSpacing.md) {
                        PrimaryButton(title: "Continue") {}
                        PrimaryButton(title: "Add contact",
                                      icon: "person.badge.plus",
                                      variant: .secondary) {}
                        PrimaryButton(title: "Cancel", variant: .ghost) {}
                        PrimaryButton(title: "Delete account",
                                      icon: "trash",
                                      variant: .destructive) {}
                        HStack(spacing: AppSpacing.md) {
                            PrimaryButton(title: "Loading", isLoading: true) {}
                            PrimaryButton(title: "Disabled", isDisabled: true) {}
                        }
                    }
                }

                section("Inputs") {
                    VStack(spacing: AppSpacing.md) {
                        InputField(label: "Name", text: $name,
                                   placeholder: "Full name", icon: "person",
                                   helper: "Letters only")
                        InputField(label: "Password", text: $password,
                                   placeholder: "••••••••", icon: "lock",
                                   isSecure: true,
                                   error: password.count > 0 && password.count < 6
                                          ? "At least 6 characters"
                                          : nil)
                    }
                }

                section("Search") {
                    SearchBarView(text: $search, placeholder: "Search contacts")
                }

                section("Status badges") {
                    HStack(spacing: AppSpacing.sm) {
                        StatusBadge(text: "Online",   icon: "circle.fill", tone: .success)
                        StatusBadge(text: "Away",     icon: "moon.fill",   tone: .warning)
                        StatusBadge(text: "Offline",  icon: "circle",      tone: .neutral)
                        StatusBadge(text: "Beta",     tone: .accent)
                        StatusBadge(text: "Failed",   icon: "xmark", tone: .danger)
                    }
                }

                section("Avatars") {
                    HStack(spacing: AppSpacing.lg) {
                        Avatar(name: "Yousef Salah", size: .sm, presence: .online)
                        Avatar(name: "Helen Khalil", size: .md, presence: .away)
                        Avatar(name: "Maya Saleh",   size: .lg, presence: .offline)
                        Avatar(name: "Ahmed",        size: .xl)
                    }
                }

                section("Contact cards") {
                    VStack(spacing: AppSpacing.md) {
                        ForEach(contacts) { c in
                            ContactCard(contact: c)
                        }
                    }
                }

                section("Card") {
                    AppCard(elevation: .sm) {
                        VStack(alignment: .leading, spacing: AppSpacing.xs) {
                            Text("Subscription")
                                .font(AppTypography.headline)
                                .foregroundStyle(AppColors.textPrimary)
                            Text("Renews on Apr 24, 2027")
                                .font(AppTypography.subhead)
                                .foregroundStyle(AppColors.textSecondary)
                        }
                    }
                }

                section("Empty state") {
                    EmptyStateView(
                        symbol: "person.crop.circle.badge.questionmark",
                        title: "No contacts yet",
                        message: "Add a contact to get started.",
                        actionTitle: "Add contact",
                        action: {}
                    )
                    .appCardSurface()
                }

                colorScale
                spacingScale
                radiusScale
            }
            .padding(.horizontal, AppSpacing.pageH)
            .padding(.vertical, AppSpacing.xl)
        }
        .background(AppColors.background.ignoresSafeArea())
    }

    // MARK: – Bits

    private var hero: some View {
        VStack(alignment: .leading, spacing: AppSpacing.xs) {
            Text("Design System")
                .font(AppTypography.display)
                .foregroundStyle(AppColors.textPrimary)
            Text("Tokens, components, and patterns for the calling app.")
                .font(AppTypography.subhead)
                .foregroundStyle(AppColors.textSecondary)
        }
    }

    @ViewBuilder
    private func section<Content: View>(
        _ title: LocalizedStringKey,
        @ViewBuilder _ content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: AppSpacing.md) {
            Text(title)
                .font(AppTypography.title3)
                .foregroundStyle(AppColors.textPrimary)
            content()
        }
    }

    private var colorScale: some View {
        section("Colors") {
            let pairs: [(String, Color)] = [
                ("primary",         AppColors.primary),
                ("primaryMuted",    AppColors.primaryMuted),
                ("background",      AppColors.background),
                ("surface",         AppColors.surface),
                ("surfaceAlt",      AppColors.surfaceAlt),
                ("textPrimary",     AppColors.textPrimary),
                ("textSecondary",   AppColors.textSecondary),
                ("border",          AppColors.border),
                ("success",         AppColors.success),
                ("warning",         AppColors.warning),
                ("danger",          AppColors.danger),
            ]
            VStack(spacing: 0) {
                ForEach(pairs, id: \.0) { (name, color) in
                    HStack {
                        RoundedRectangle(cornerRadius: AppRadius.xs, style: .continuous)
                            .fill(color)
                            .overlay(
                                RoundedRectangle(cornerRadius: AppRadius.xs)
                                    .strokeBorder(AppColors.border, lineWidth: 0.5)
                            )
                            .frame(width: 32, height: 32)
                        Text(name)
                            .font(AppTypography.callout.monospaced())
                            .foregroundStyle(AppColors.textPrimary)
                        Spacer()
                    }
                    .padding(.horizontal, AppSpacing.lg)
                    .padding(.vertical, AppSpacing.sm)
                }
            }
            .appCardSurface()
        }
    }

    private var spacingScale: some View {
        section("Spacing") {
            let scale: [(String, CGFloat)] = [
                ("xxs",  AppSpacing.xxs),
                ("xs",   AppSpacing.xs),
                ("sm",   AppSpacing.sm),
                ("md",   AppSpacing.md),
                ("lg",   AppSpacing.lg),
                ("xl",   AppSpacing.xl),
                ("xxl",  AppSpacing.xxl),
                ("xxxl", AppSpacing.xxxl),
            ]
            VStack(spacing: AppSpacing.xs) {
                ForEach(scale, id: \.0) { (name, value) in
                    HStack(spacing: AppSpacing.md) {
                        Text(name)
                            .font(AppTypography.caption.monospaced())
                            .foregroundStyle(AppColors.textSecondary)
                            .frame(width: 36, alignment: .leading)
                        RoundedRectangle(cornerRadius: 2)
                            .fill(AppColors.primary)
                            .frame(width: value, height: 8)
                        Text("\(Int(value)) pt")
                            .font(AppTypography.caption.monospaced())
                            .foregroundStyle(AppColors.textTertiary)
                    }
                }
            }
            .padding(AppSpacing.md)
            .appCardSurface()
        }
    }

    private var radiusScale: some View {
        section("Radii") {
            HStack(spacing: AppSpacing.md) {
                ForEach([("xs", AppRadius.xs), ("sm", AppRadius.sm),
                         ("md", AppRadius.md), ("lg", AppRadius.lg),
                         ("xl", AppRadius.xl)], id: \.0) { (name, value) in
                    VStack(spacing: 6) {
                        RoundedRectangle(cornerRadius: value, style: .continuous)
                            .fill(AppColors.primary.opacity(0.18))
                            .overlay(
                                RoundedRectangle(cornerRadius: value)
                                    .strokeBorder(AppColors.primary, lineWidth: 1)
                            )
                            .frame(height: 56)
                        Text(name)
                            .font(AppTypography.caption.monospaced())
                            .foregroundStyle(AppColors.textSecondary)
                    }
                }
            }
        }
    }
}

// MARK: – previews

#Preview("Showcase · Light") {
    DesignSystemShowcase()
        .preferredColorScheme(.light)
}

#Preview("Showcase · Dark · iPhone SE") {
    DesignSystemShowcase()
        .preferredColorScheme(.dark)
        .previewDevice("iPhone SE (3rd generation)")
}

#Preview("Showcase · العربية · RTL") {
    DesignSystemShowcase()
        .environment(\.locale, .init(identifier: "ar"))
        .environment(\.layoutDirection, .rightToLeft)
}
