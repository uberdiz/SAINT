import SwiftUI
import SaintCore

/// The History tab, like the desktop's History page: everything SAINT did on this phone (and on your PC), newest
/// first, whether it worked, a short summary and a search. The log syncs to your PC (it appears in History there,
/// under "iPhone") whenever they're connected. Pull down to sync now.
struct ActivityView: View {
    @EnvironmentObject var model: AppModel
    @State private var filter = 0
    @State private var query = ""
    private let filters = ["All", "Phone", "Desktop", "Failed"]

    private var all: [ActionEntry] {
        _ = model.dataVersion                       // refresh when the log changes
        return model.brain.actions.all()
    }

    private var entries: [ActionEntry] {
        var list = all
        switch filter {
        case 1: list = list.filter { $0.source != "pc" && $0.kind != "pc" }
        case 2: list = list.filter { $0.source == "pc" || $0.kind == "pc" }
        case 3: list = list.filter { $0.status == "failed" }
        default: break
        }
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return list }
        return list.filter {
            $0.request.lowercased().contains(q) || $0.action.lowercased().contains(q)
                || ($0.detail ?? "").lowercased().contains(q) || $0.kind.lowercased().contains(q)
        }
    }

    /// Today, the last 7 days, and how often it worked (of the things that finished).
    private var summary: (today: Int, week: Int, success: Int?) {
        let cal = Calendar.current
        let weekAgo = Date().addingTimeInterval(-7 * 86400)
        let week = all.filter { $0.ts >= weekAgo }
        let finished = week.filter { $0.status == "done" || $0.status == "failed" }
        let ok = finished.filter { $0.status == "done" }.count
        return (all.filter { cal.isDateInToday($0.ts) }.count, week.count,
                finished.isEmpty ? nil : Int((Double(ok) / Double(finished.count) * 100).rounded()))
    }

    private var groups: [(String, [ActionEntry])] {
        let cal = Calendar.current
        var order: [String] = []
        var byDay: [String: [ActionEntry]] = [:]
        let f = DateFormatter()
        f.dateFormat = "EEEE, MMM d"
        for e in entries {
            let key = cal.isDateInToday(e.ts) ? "Today" : cal.isDateInYesterday(e.ts) ? "Yesterday" : f.string(from: e.ts)
            if byDay[key] == nil { order.append(key) }
            byDay[key, default: []].append(e)
        }
        return order.map { ($0, byDay[$0] ?? []) }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    syncChip
                    Text("Everything SAINT does on this iPhone. It's copied to your PC's History whenever they're connected.")
                        .font(.system(size: 13)).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    stats
                    segmented
                    if entries.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "waveform.path.ecg").font(.system(size: 34)).foregroundStyle(Theme.accent)
                            Text(query.isEmpty ? "Nothing yet" : "No matches").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                            Text(query.isEmpty ? "Ask SAINT something and it shows up here." : "Nothing in your history matches “\(query)”.")
                                .font(.system(size: 13)).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 40)
                    }
                    ForEach(groups, id: \.0) { group in
                        SectionTitle(group.0)
                        CardList {
                            ForEach(Array(group.1.enumerated()), id: \.element.id) { index, entry in
                                ActivityRow(entry: entry)
                                if index < group.1.count - 1 { RowDivider() }
                            }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
            }
            .refreshable { await model.syncNow() }
            .searchable(text: $query, prompt: "Search history")
            .saintBackground()
            .navigationTitle("History")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button { Task { await model.syncNow() } } label: { Label("Sync with my PC now", systemImage: "arrow.triangle.2.circlepath") }
                            .disabled(model.syncing)
                        ShareLink(item: model.activityExport()) { Label("Export the log", systemImage: "square.and.arrow.up") }
                        Button(role: .destructive) { model.brain.actions.clear(); model.dataVersion += 1 } label: {
                            Label("Clear this phone's log", systemImage: "trash")
                        }
                    } label: { Image(systemName: "ellipsis.circle").foregroundStyle(Theme.text) }
                }
            }
        }
    }

    private var syncChip: some View {
        let synced = model.lastSync
        let pc = model.brain.ownPC()
        let state: (text: String, tint: Color, fill: Color) = {
            if pc == nil { return ("No PC paired · log stays on this phone", Theme.muted, Theme.surface2) }
            if let synced = synced {
                return ("Synced to Desktop · \(synced.formatted(.relative(presentation: .named)))", Theme.success, Theme.successSoft)
            }
            if pc?.online == true { return ("Connected · syncs in a moment", Theme.success, Theme.successSoft) }
            return ("Desktop offline · will sync when it's back", Theme.accent, Theme.accentSoft)
        }()
        return Pill(text: state.text, icon: "arrow.down.circle", tint: state.tint, fill: state.fill)
    }

    private var stats: some View {
        let s = summary
        return HStack(spacing: 10) {
            statTile("\(s.today)", "today")
            statTile("\(s.week)", "last 7 days")
            statTile(s.success.map { "\($0)%" } ?? "—", "worked")
        }
    }

    private func statTile(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.system(size: 20, weight: .bold).monospacedDigit()).foregroundStyle(Theme.text)
                .lineLimit(1).minimumScaleFactor(0.7)
            Text(label).font(.system(size: 12)).foregroundStyle(Theme.muted).lineLimit(1).minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .glassCard(radius: 14)
    }

    private var segmented: some View {
        HStack(spacing: 2) {
            ForEach(filters.indices, id: \.self) { i in
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { filter = i }
                } label: {
                    Text(filters[i])
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(filter == i ? Theme.text : Theme.muted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 7)
                        .background(filter == i ? Theme.raised : Color.clear, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .glassCard(radius: 11, fill: Theme.surface2)
    }
}

struct ActivityRow: View {
    let entry: ActionEntry

    private var icon: (String, Color, Color) {
        if entry.status == "failed" { return (symbol, Theme.danger, Theme.dangerSoft) }
        if entry.kind == "pc" || entry.source == "pc" { return (symbol, Theme.info, Theme.infoSoft) }
        return (symbol, Theme.accent, Theme.accentSoft)
    }

    private var symbol: String {
        switch entry.kind {
        case "music": return "music.note"
        case "phone": return "iphone"
        case "reminder": return "clock"
        case "memory": return "brain"
        case "pc": return "laptopcomputer"
        case "skill": return "bolt"
        case "voice": return "waveform"
        default: return "sparkles"
        }
    }

    var body: some View {
        let (name, tint, fill) = icon
        HStack(spacing: 12) {
            Image(systemName: name).font(.system(size: 15, weight: .medium)).foregroundStyle(tint)
                .frame(width: 34, height: 34).background(fill, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.action.isEmpty ? entry.request : entry.action)
                    .font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(2)
                Text("“\(entry.request)” · \(entry.ts.formatted(date: .omitted, time: .shortened))\(entry.source == "pc" ? " · on PC" : "")")
                    .font(.system(size: 12)).foregroundStyle(Theme.muted).lineLimit(2)
                if let detail = entry.detail, !detail.isEmpty {
                    // Where it ran and why: "Answered on this phone — Home PC wasn't reachable".
                    Text(detail).font(.system(size: 11)).foregroundStyle(Theme.muted).lineLimit(2)
                }
            }
            Spacer(minLength: 6)
            StatusChip(status: entry.status)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .accessibilityElement(children: .combine)
    }
}
