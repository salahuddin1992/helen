import SwiftUI

struct SettingsScreen: View {
    @State private var notifications = true
    @State private var readReceipts  = true
    @State private var appearance: Appearance = .system

    private enum Appearance: String, CaseIterable, Identifiable {
        case system, light, dark
        var id: Self { self }
        var label: LocalizedStringKey {
            switch self {
            case .system: return "System"
            case .light:  return "Light"
            case .dark:   return "Dark"
            }
        }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NavigationLink {
                        Text("Account").navigationTitle("Account")
                    } label: {
                        accountRow
                    }
                }

                Section {
                    Toggle("Notifications",  isOn: $notifications)
                    Toggle("Read Receipts",  isOn: $readReceipts)
                }

                Section("Appearance") {
                    Picker("Theme", selection: $appearance) {
                        ForEach(Appearance.allCases) { a in
                            Text(a.label).tag(a)
                        }
                    }
                    .pickerStyle(.menu)
                }

                Section {
                    NavigationLink("Privacy & Security") { Text("Privacy") }
                    NavigationLink("Storage")            { Text("Storage") }
                    NavigationLink("Help")               { Text("Help") }
                }

                Section {
                    LabeledContent("Version") {
                        Text("1.0.0").monospacedDigit()
                    }
                    .foregroundStyle(.secondary)
                }

                Section {
                    Button("Sign Out", role: .destructive) {}
                }
            }
            .navigationTitle("Settings")
        }
        .preferredColorScheme(scheme)
    }

    private var accountRow: some View {
        HStack(spacing: Theme.Space.md) {
            Avatar(name: "Yousef Salah", diameter: 56)
            VStack(alignment: .leading, spacing: 2) {
                Text("Yousef Salah").font(.body)
                Text("yousef@helen.app")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private var scheme: ColorScheme? {
        switch appearance {
        case .system: return nil
        case .light:  return .light
        case .dark:   return .dark
        }
    }
}
