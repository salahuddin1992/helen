import SwiftUI

struct FavoritesScreen: View {
    @State private var favorites = MockData.people.filter(\.isFavorite)
    @State private var calling: Person?

    var body: some View {
        NavigationStack {
            Group {
                if favorites.isEmpty {
                    EmptyState(symbol: "star",
                               title: "No Favorites",
                               message: "Add favorites for one-tap dialing.")
                } else {
                    list
                }
            }
            .navigationTitle("Favorites")
            .toolbar { if !favorites.isEmpty { EditButton() } }
            .fullScreenCover(item: $calling) { CallScreen(person: $0) }
        }
    }

    private var list: some View {
        List {
            ForEach(favorites) { person in
                Button { calling = person } label: {
                    HStack(spacing: Theme.Space.md) {
                        Avatar(name: person.name, diameter: 36)
                        Text(person.name).font(.body).foregroundStyle(.primary)
                        Spacer()
                        Text("mobile")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Image(systemName: "info.circle")
                            .foregroundStyle(.tint)
                            .padding(.leading, 4)
                            .frame(minWidth: 28, minHeight: 28)
                            .contentShape(Rectangle())
                            .onTapGesture {}
                            .accessibilityLabel("Contact info")
                    }
                    .padding(.vertical, 2)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Call \(person.name)")
            }
            .onDelete { idx in
                withAnimation(Theme.Motion.snappy) { favorites.remove(atOffsets: idx) }
            }
            .onMove { from, to in
                favorites.move(fromOffsets: from, toOffset: to)
            }
        }
        .listStyle(.plain)
    }
}
