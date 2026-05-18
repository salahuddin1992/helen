import SwiftUI

struct PersonRow: View {
    let person: Person

    var body: some View {
        HStack(spacing: Theme.Space.md) {
            Avatar(name: person.name, diameter: 36)

            VStack(alignment: .leading, spacing: 1) {
                Text(person.name).font(.body)
                if let role = person.role {
                    Text(role)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 2)
    }
}
