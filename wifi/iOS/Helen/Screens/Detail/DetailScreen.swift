import SwiftUI

struct DetailScreen: View {
    let person: Person

    @State private var isFavorite: Bool
    @State private var calling = false
    @State private var heroAppeared = false

    init(person: Person) {
        self.person = person
        _isFavorite = State(initialValue: person.isFavorite)
    }

    var body: some View {
        List {
            Section {
                hero
                actions
            }
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 8, leading: 0, bottom: 12, trailing: 0))

            Section {
                DetailField(label: "mobile", value: person.phone, copyable: true)
                if let email = person.email {
                    DetailField(label: "email", value: email, copyable: true)
                }
            }

            if let role = person.role {
                Section("Notes") {
                    Text(role).font(.body)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Haptic.selection()
                    withAnimation(Theme.Motion.snappy) { isFavorite.toggle() }
                } label: {
                    Image(systemName: isFavorite ? "star.fill" : "star")
                        .contentTransition(.symbolEffect(.replace))
                        .foregroundStyle(isFavorite ? Color.yellow : Color.accentColor)
                }
                .accessibilityLabel(isFavorite ? "Remove from Favorites" : "Add to Favorites")
            }
        }
        .fullScreenCover(isPresented: $calling) { CallScreen(person: person) }
        .onAppear {
            // Hero scales in subtly the first time the screen mounts.
            withAnimation(Theme.Motion.spring.delay(0.05)) { heroAppeared = true }
        }
    }

    // MARK: – Sections

    private var hero: some View {
        VStack(spacing: Theme.Space.md) {
            Avatar(name: person.name, diameter: 96)
                .scaleEffect(heroAppeared ? 1.0 : 0.92)
                .opacity(heroAppeared ? 1 : 0)
            VStack(spacing: 2) {
                Text(person.name).font(.title2.weight(.semibold))
                if let role = person.role {
                    Text(role)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 6)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, Theme.Space.sm)
        .accessibilityElement(children: .combine)
    }

    private var actions: some View {
        HStack(spacing: Theme.Space.sm) {
            ActionTile(symbol: "phone.fill",    title: "call")     { calling = true }
            ActionTile(symbol: "message.fill",  title: "message")  {}
            ActionTile(symbol: "video.fill",    title: "video")    { calling = true }
            ActionTile(symbol: "envelope.fill", title: "mail",
                       enabled: person.email != nil)               {}
        }
        .padding(.top, Theme.Space.xs)
    }
}

#Preview {
    NavigationStack { DetailScreen(person: MockData.people[1]) }
}
