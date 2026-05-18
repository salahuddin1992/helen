import SwiftUI

/// Recents — the call log.
///
/// Uses native `List` so we get free swipe actions, edit-mode plumbing,
/// and that "feels right" iOS scroll physics. Headers + sections are
/// grouped by relative date, matching Apple's Phone app.
struct RecentsView: View {

    @State private var search: String = ""
    @State private var filter: Filter = .all
    @State private var records: [CallRecord] = CallRecord.samples
    @State private var editMode: EditMode = .inactive

    enum Filter: Hashable, CaseIterable {
        case all, missed
        var label: LocalizedStringKey {
            switch self {
            case .all:    return "All"
            case .missed: return "Missed"
            }
        }
    }

    @Environment(\.theme) private var theme

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()

            VStack(spacing: 0) {
                header
                filterStrip
                list
            }
        }
        .environment(\.editMode, $editMode)
    }

    // MARK: – Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text("Recents")
                .font(HelenFont.display)
                .foregroundStyle(theme.colors.textPrimary)
            Spacer()
            Button {
                withAnimation(HelenMotion.standard) {
                    editMode = (editMode == .active) ? .inactive : .active
                }
            } label: {
                Text(editMode == .active ? "Done" : "Edit")
                    .font(HelenFont.bodyMed)
                    .foregroundStyle(theme.colors.accent)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.top,        HelenSpace.lg)
        .padding(.bottom,     HelenSpace.sm)
    }

    private var filterStrip: some View {
        VStack(spacing: HelenSpace.sm) {
            HelenSearchBar(text: $search, placeholder: "Search recents")
            HelenSegmented(
                options: Filter.allCases.map { ($0, $0.label) },
                selection: $filter
            )
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.bottom,     HelenSpace.sm)
    }

    // MARK: – List

    private var list: some View {
        Group {
            if grouped.isEmpty {
                HelenEmptyState(
                    symbol: "phone.connection",
                    title: filter == .missed ? "No missed calls" : "No recent calls",
                    message: "When you make or receive a call, it'll show up here."
                )
                .padding(.top, HelenSpace.xl)
                Spacer()
            } else {
                List {
                    ForEach(grouped, id: \.title) { section in
                        Section {
                            ForEach(section.records) { record in
                                Button { callBack(record) } label: {
                                    CallRecordRow(record: record)
                                }
                                .buttonStyle(.plain)
                                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                    Button(role: .destructive) {
                                        delete(record)
                                    } label: {
                                        Label("Delete", systemImage: "trash")
                                    }
                                }
                                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                    Button {
                                        callBack(record)
                                    } label: {
                                        Label("Call", systemImage: "phone.fill")
                                    }
                                    .tint(theme.colors.success)
                                }
                                .listRowSeparatorTint(theme.colors.divider)
                                .listRowBackground(theme.colors.surface)
                            }
                            .onDelete { offsets in deleteIn(section: section, offsets: offsets) }
                        } header: {
                            Text(section.title)
                                .font(HelenFont.caption.weight(.semibold))
                                .foregroundStyle(theme.colors.textSecondary)
                                .textCase(nil)
                        }
                    }
                }
                .listStyle(.insetGrouped)
                .scrollContentBackground(.hidden)
                .background(theme.colors.background)
            }
        }
    }

    // MARK: – Grouping

    private struct DaySection: Hashable {
        let title: String
        let records: [CallRecord]
    }

    private var filtered: [CallRecord] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return records.filter { r in
            let matchesQ = q.isEmpty
                || r.contactName.lowercased().contains(q)
                || r.phone.replacingOccurrences(of: " ", with: "").contains(q)
            let matchesFilter = (filter == .all) || r.isMissed
            return matchesQ && matchesFilter
        }
    }

    private var grouped: [DaySection] {
        let cal = Calendar.current
        let buckets = Dictionary(grouping: filtered) { rec -> Date in
            cal.startOfDay(for: rec.date)
        }
        return buckets
            .sorted { $0.key > $1.key }
            .map { day, recs in
                DaySection(
                    title: title(for: day),
                    records: recs.sorted { $0.date > $1.date }
                )
            }
    }

    private func title(for day: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(day)     { return NSLocalizedString("Today",     comment: "") }
        if cal.isDateInYesterday(day) { return NSLocalizedString("Yesterday", comment: "") }
        let f = DateFormatter()
        f.locale = .current
        f.dateFormat = cal.isDate(day, equalTo: .now, toGranularity: .year)
                       ? "EEEE, MMM d"
                       : "MMM d, yyyy"
        return f.string(from: day)
    }

    // MARK: – Actions

    private func callBack(_ record: CallRecord) {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        // Hook into the call service later.
    }

    private func delete(_ record: CallRecord) {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        withAnimation(HelenMotion.standard) {
            records.removeAll { $0.id == record.id }
        }
    }

    private func deleteIn(section: DaySection, offsets: IndexSet) {
        let ids = offsets.map { section.records[$0].id }
        withAnimation(HelenMotion.standard) {
            records.removeAll { ids.contains($0.id) }
        }
    }
}

// MARK: – previews

#Preview("Recents · Light") {
    RecentsView()
        .preferredColorScheme(.light)
}

#Preview("Recents · Dark · iPhone SE") {
    RecentsView()
        .preferredColorScheme(.dark)
        .previewDevice("iPhone SE (3rd generation)")
}

#Preview("Recents · العربية · RTL") {
    RecentsView()
        .environment(\.locale, .init(identifier: "ar"))
        .environment(\.layoutDirection, .rightToLeft)
}
