import SwiftUI

struct RecentsScreen: View {
    @State private var calls = MockData.calls
    @State private var scope: Scope = .all
    private enum Scope: Hashable { case all, missed }

    var body: some View {
        NavigationStack {
            List {
                ForEach(grouped, id: \.title) { day in
                    Section(day.title) {
                        ForEach(day.calls) { call in
                            CallRow(call: call)
                                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                    Button(role: .destructive) {
                                        delete(call)
                                    } label: {
                                        Label("Delete", systemImage: "trash")
                                    }
                                }
                                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                    Button { /* call back */ } label: {
                                        Label("Call", systemImage: "phone.fill")
                                    }
                                    .tint(.green)
                                }
                        }
                    }
                }
            }
            .listStyle(.plain)
            .navigationTitle("Recents")
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Picker("", selection: $scope) {
                        Text("All").tag(Scope.all)
                        Text("Missed").tag(Scope.missed)
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 200)
                    .onChange(of: scope) { _, _ in Haptic.selection() }
                }
            }
            .refreshable { await refresh() }
            .overlay {
                if grouped.isEmpty {
                    EmptyState(
                        symbol: scope == .missed ? "phone.down" : "phone",
                        title: scope == .missed ? "No Missed Calls" : "No Recent Calls",
                        message: "Calls you make or receive will appear here."
                    )
                }
            }
        }
    }

    // MARK: – Actions

    private func delete(_ call: Call) {
        withAnimation(Theme.Motion.snappy) {
            calls.removeAll { $0.id == call.id }
        }
    }

    private func refresh() async {
        try? await Task.sleep(for: .milliseconds(700))
    }

    // MARK: – Grouping

    private struct DaySection { let title: String; let calls: [Call] }

    private var grouped: [DaySection] {
        let filtered = scope == .all ? calls : calls.filter { $0.kind == .missed }
        let cal = Calendar.current
        return Dictionary(grouping: filtered) { cal.startOfDay(for: $0.date) }
            .sorted { $0.key > $1.key }
            .map {
                DaySection(title: title(for: $0.key),
                           calls: $0.value.sorted { $0.date > $1.date })
            }
    }

    private func title(for day: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(day)     { return NSLocalizedString("Today",     comment: "") }
        if cal.isDateInYesterday(day) { return NSLocalizedString("Yesterday", comment: "") }
        let f = DateFormatter(); f.locale = .current
        f.dateFormat = cal.isDate(day, equalTo: .now, toGranularity: .year)
            ? "EEEE, MMM d"
            : "MMM d, yyyy"
        return f.string(from: day)
    }
}
