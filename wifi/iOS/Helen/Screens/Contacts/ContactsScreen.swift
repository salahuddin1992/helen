import SwiftUI

struct ContactsScreen: View {
    @State private var query = ""
    private let people = MockData.people

    var body: some View {
        NavigationStack {
            List {
                ForEach(sections, id: \.letter) { section in
                    Section(section.letter) {
                        ForEach(section.people) { person in
                            NavigationLink(value: person) {
                                PersonRow(person: person)
                            }
                            .contextMenu {
                                Button { } label: { Label("Call", systemImage: "phone") }
                                Button { } label: { Label("Message", systemImage: "message") }
                                Divider()
                                Button { } label: {
                                    Label(person.isFavorite ? "Unfavorite" : "Favorite",
                                          systemImage: person.isFavorite ? "star.slash" : "star")
                                }
                            }
                        }
                    }
                }
            }
            .listStyle(.plain)
            .navigationTitle("Contacts")
            .searchable(text: $query,
                        placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "Search")
            .navigationDestination(for: Person.self) { DetailScreen(person: $0) }
            .overlay {
                if sections.isEmpty {
                    EmptyState(symbol: "magnifyingglass",
                               title: "No Results",
                               message: "Check the spelling or try a different name.")
                }
            }
        }
    }

    // MARK: – Grouping

    private struct AlphaSection { let letter: String; let people: [Person] }

    private var sections: [AlphaSection] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let filtered = q.isEmpty
            ? people
            : people.filter {
                $0.name.lowercased().contains(q)
                || $0.phone.replacingOccurrences(of: " ", with: "").contains(q)
            }
        let sorted = filtered.sorted { $0.name < $1.name }
        return Dictionary(grouping: sorted) { String($0.name.prefix(1)).uppercased() }
            .sorted { $0.key < $1.key }
            .map { AlphaSection(letter: $0.key, people: $0.value) }
    }
}
