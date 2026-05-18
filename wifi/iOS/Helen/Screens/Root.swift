import SwiftUI

struct Root: View {
    @State private var tab: Tab = .contacts
    private enum Tab { case contacts, recents, favorites, settings }

    var body: some View {
        TabView(selection: $tab) {
            ContactsScreen()
                .tag(Tab.contacts)
                .tabItem { Label("Contacts", systemImage: "person.2") }

            RecentsScreen()
                .tag(Tab.recents)
                .tabItem { Label("Recents", systemImage: "clock") }

            FavoritesScreen()
                .tag(Tab.favorites)
                .tabItem { Label("Favorites", systemImage: "star") }

            SettingsScreen()
                .tag(Tab.settings)
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}

#Preview("Light")  { Root() }
#Preview("Dark")   { Root().preferredColorScheme(.dark) }
#Preview("Arabic") {
    Root()
        .environment(\.locale, .init(identifier: "ar"))
        .environment(\.layoutDirection, .rightToLeft)
}
