import SwiftUI
import SaintCore

/// Everything SAINT did on this phone, newest first, and whether it worked. The log syncs to your PC (it appears
/// in History there, under "iPhone") whenever they're connected. Pull down to sync now.
struct ActivityView: View {
    @EnvironmentObject var model: AppModel
    @State private var filter = 0
    private let filters = ["All", "Phone", "Desktop", "Failed"]

    private var entries: [ActionEntry] {
        _ = model.dataVersion                       // refresh when the log changes
        let all = model.brain.actions.all()
        switch filter {
        case 1: return all.filter { $0.source != "pc" && $0.kind != "pc" }
        case 2: return all.filter { $0.source == "pc" || $0.kind == "pc" }
        case 3: return all.filter { $0.status == "failed" }
        default: return all
        }
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
                    segmented
                    if entries.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "waveform.path.ecg").font(.system(size: 34)).foregroundStyle(Theme.accent)
                            Text("Nothing yet").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                            Text("Ask SAINT something and it shows up here.").font(.system(size: 13)).foregroundStyle(Theme.muted)
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
            .saintBackground()
            .navigationTitle("Activity")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button { Task { await model.syncNow() } } label: { Label("Sync with my PC now", systemImage: "arrow.triangle.2.circlepath") }
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
            }
            Spacer(minLength: 6)
            StatusChip(status: entry.status)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .accessibilityElement(children: .combine)
    }
}
