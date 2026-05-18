import SwiftUI

@main
struct HelenApp: App {
    @StateObject private var session = HelenSession()

    var body: some Scene {
        WindowGroup {
            RootGate()
                .environmentObject(session)
                .environment(\.theme, HelenTheme())
                .task { await session.restoreFromDisk() }
        }
    }
}

private struct RootGate: View {
    @EnvironmentObject private var session: HelenSession

    var body: some View {
        Group {
            if session.serverURL == nil {
                ServerSelectView()
                    .transition(.opacity)
            } else if !session.isAuthenticated {
                SignInView()
                    .transition(.opacity)
            } else {
                RootTabView()
                    .transition(.opacity)
            }
        }
        .animation(HelenMotion.gentle, value: session.serverURL)
        .animation(HelenMotion.gentle, value: session.isAuthenticated)
    }
}

// MARK: - Multi-device preview helper
//
// Drop this anywhere in the app to preview a screen across the supported
// device range and both color schemes / locales at once.
struct HelenShowcase<Content: View>: View {
    let label: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        Group {
            content()
                .previewDisplayName("\(label) · iPhone SE · Light")
                .previewDevice("iPhone SE (3rd generation)")
                .preferredColorScheme(.light)
            content()
                .previewDisplayName("\(label) · 15 Pro Max · Dark")
                .previewDevice("iPhone 15 Pro Max")
                .preferredColorScheme(.dark)
            content()
                .previewDisplayName("\(label) · العربية · RTL")
                .previewDevice("iPhone 15 Pro")
                .environment(\.locale, .init(identifier: "ar"))
                .environment(\.layoutDirection, .rightToLeft)
        }
    }
}

#Preview("Helen — Sign In showcase") {
    HelenShowcase(label: "Sign In") {
        SignInView().environmentObject(HelenSession())
    }
}

#Preview("Helen — Chats showcase") {
    HelenShowcase(label: "Chats") {
        NavigationStack { ChatsListView() }
            .environmentObject(HelenSession())
    }
}

#Preview("Helen — Contacts showcase") {
    HelenShowcase(label: "Contacts") {
        NavigationStack { ContactsView() }
            .environmentObject(HelenSession())
    }
}
