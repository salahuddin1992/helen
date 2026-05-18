import SwiftUI

struct RootTabView: View {
    @State private var tab: Tab = .chats
    enum Tab { case chats, contacts, profile, settings }

    @Environment(\.theme) private var theme

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack {
                ChatsListView()
                    .navigationDestination(for: Conversation.self) { ChatView(conversation: $0) }
            }
            .tabItem { Label("Chats",    systemImage: "bubble.left.and.bubble.right.fill") }
            .tag(Tab.chats)

            NavigationStack {
                ContactsView()
                    .navigationDestination(for: Contact.self) { ContactDetailView(contact: $0) }
            }
            .tabItem { Label("Contacts", systemImage: "person.2.fill") }
            .tag(Tab.contacts)

            NavigationStack { ProfileView() }
                .tabItem { Label("Profile",  systemImage: "person.crop.circle.fill") }
                .tag(Tab.profile)

            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape.fill") }
                .tag(Tab.settings)
        }
        .tint(theme.colors.accent)
    }
}

#Preview { RootTabView().environmentObject(HelenSession()) }
