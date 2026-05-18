import SwiftUI

struct ContactsView: View {
    @State private var search: String = ""
    @State private var filter: Filter = .all
    @State private var isRefreshing = false

    enum Filter: Hashable { case all, online, recent }

    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    private var contacts: [Contact] {
        if session.isAuthenticated {
            let myId = session.currentUser?.id
            return session.users
                .filter { $0.id != myId }       // hide self
                .map(Contact.init(from:))
        }
        return Contact.samples
    }

    var body: some View {
        ZStack(alignment: .top) {
            theme.colors.background.ignoresSafeArea()

            ScrollView {
                LazyVStack(spacing: HelenSpace.lg, pinnedViews: []) {
                    HelenNavBar(title: "Contacts", subtitle: "\(contacts.count) people on your network") {
                        Button {} label: {
                            ZStack {
                                Circle().fill(theme.colors.accentMuted).frame(width: 36, height: 36)
                                Image(systemName: "person.badge.plus")
                                    .foregroundStyle(theme.colors.accent)
                            }
                        }
                        .accessibilityLabel("Add contact")
                    }

                    VStack(spacing: HelenSpace.md) {
                        HelenSearchBar(text: $search, placeholder: "Search people")
                        HelenSegmented(
                            options: [(.all, "All"), (.online, "Online"), (.recent, "Recent")],
                            selection: $filter
                        )
                    }
                    .padding(.horizontal, HelenSpace.pageH)

                    if filtered.isEmpty {
                        HelenEmptyState(
                            symbol: "person.crop.circle.badge.questionmark",
                            title: "No matches",
                            message: "Try a different name or check the spelling."
                        )
                    } else {
                        contactsCard
                    }

                    Spacer(minLength: HelenSpace.huge)
                }
                .padding(.top, HelenSpace.xs)
            }
            .refreshable { await reload() }
        }
        .task { await reload() }
        .onChange(of: search) { _, q in
            // Server-side search when the query is non-trivial.
            guard session.isAuthenticated, q.count >= 2 else { return }
            Task { @MainActor in await session.reloadUsers(search: q) }
        }
    }

    @MainActor
    private func reload() async {
        guard session.isAuthenticated, !isRefreshing else { return }
        isRefreshing = true
        await session.reloadUsers()
        isRefreshing = false
    }

    private var contactsCard: some View {
        VStack(spacing: 0) {
            ForEach(Array(filtered.enumerated()), id: \.element.id) { idx, c in
                NavigationLink(value: c) {
                    HelenListRow(
                        title: c.displayName,
                        subtitle: c.status,
                        leading: { HelenAvatar(name: c.displayName, size: .md, presence: c.presence) },
                        trailing: { Image(systemName: "chevron.forward")
                                .font(.footnote.weight(.semibold))
                                .foregroundStyle(theme.colors.textTertiary) }
                    )
                }
                .buttonStyle(.plain)
                if idx < filtered.count - 1 {
                    Divider()
                        .overlay(theme.colors.divider)
                        .padding(.leading, HelenSpace.lg + HelenSize.avatarMd + HelenSpace.md)
                }
            }
        }
        .helenCardSurface()
        .padding(.horizontal, HelenSpace.pageH)
    }

    private var filtered: [Contact] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return contacts.filter { c in
            let matchesQ = q.isEmpty ||
                c.displayName.lowercased().contains(q) ||
                c.username.lowercased().contains(q)
            let matchesFilter: Bool = {
                switch filter {
                case .all:    return true
                case .online: return c.presence == .online
                case .recent: return c.lastSeen != nil
                }
            }()
            return matchesQ && matchesFilter
        }
    }
}

#Preview {
    NavigationStack { ContactsView() }
        .environmentObject(HelenSession())
}
