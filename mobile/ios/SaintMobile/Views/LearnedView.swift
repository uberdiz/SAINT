import SwiftUI
import SaintCore

/// The Memory tab, like the desktop's Memory page: what SAINT knows about you — and what it has learned from your
/// other devices — with a search. Swipe to forget; + to teach it something.
struct LearnedView: View {
    @EnvironmentObject var model: AppModel
    @State private var adding = false
    @State private var newFact = ""
    @State private var query = ""

    private func matches(_ texts: String...) -> Bool {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return q.isEmpty || texts.contains { $0.lowercased().contains(q) }
    }

    private var memories: [MemoryEntry] {
        _ = model.dataVersion
        return model.brain.memory.all().filter { matches($0.content, $0.category) }
    }
    private var skills: [Skill] {
        _ = model.dataVersion
        return model.brain.skills.all().filter { matches($0.phrase, $0.steps.joined(separator: " ")) }
    }
    private var aliases: [(key: String, value: String)] {
        _ = model.dataVersion
        return model.brain.aliases.all().sorted { $0.key < $1.key }.map { (key: $0.key, value: $0.value) }
            .filter { matches($0.key, $0.value) }
    }
    private var routines: [Routine] {
        _ = model.dataVersion
        return model.brain.scenes.all().filter { matches($0.name, $0.phrase, $0.steps.joined(separator: " ")) }
    }

    private var syncLine: String {
        let pcs = model.peers.filter { $0.isOwn && $0.online }
        if let pc = pcs.first { return "Shared live with \(pc.name)" }
        return model.peers.contains(where: { $0.isOwn }) ? "Your PC is offline — changes sync when it's back" : "Pair your PC to share what SAINT learns"
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label(syncLine, systemImage: "arrow.triangle.2.circlepath")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section("About you") {
                    if memories.isEmpty {
                        Text(query.isEmpty
                             ? "Nothing yet. Say “SAINT, my favorite color is green” or “SAINT, remember that my sister is called Ana”."
                             : "Nothing about you matches “\(query)”.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    ForEach(memories) { m in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(Brain.sentence(Brain.flip(m.content))).font(.system(.body, design: .rounded))
                            HStack(spacing: 6) {
                                Text(m.category).font(.caption).foregroundStyle(.secondary)
                                if m.how == "learned" { Pill(text: "learned", icon: "sparkles", tint: .purple) }
                            }
                        }
                        .swipeActions {
                            Button(role: .destructive) { model.brain.memory.forget(id: m.id) } label: { Label("Forget", systemImage: "trash") }
                        }
                    }
                }
                if !skills.isEmpty {
                    Section("Things I can do when you say…") {
                        ForEach(skills) { s in
                            VStack(alignment: .leading, spacing: 2) {
                                Text("“\(s.phrase)”").font(.system(.body, design: .rounded, weight: .medium))
                                Text(s.steps.joined(separator: " → ")).font(.caption).foregroundStyle(.secondary)
                            }
                            .swipeActions {
                                Button(role: .destructive) { model.brain.skills.forget(id: s.id) } label: { Label("Forget", systemImage: "trash") }
                            }
                        }
                    }
                }
                if !routines.isEmpty {
                    Section("Routines") {
                        ForEach(routines) { r in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(r.name).font(.system(.body, design: .rounded, weight: .medium))
                                Text(r.steps.joined(separator: " → ")).font(.caption).foregroundStyle(.secondary)
                                if !r.phrase.isEmpty { Text("say “\(r.phrase)”").font(.caption).foregroundStyle(Theme.accent) }
                            }
                        }
                    }
                }
                if !aliases.isEmpty {
                    Section("Nicknames") {
                        ForEach(aliases, id: \.key) { a in
                            HStack { Text(a.key); Spacer(); Text(a.value).foregroundStyle(.secondary).lineLimit(1) }
                                .swipeActions {
                                    Button(role: .destructive) { model.brain.aliases.forget(a.key) } label: { Label("Forget", systemImage: "trash") }
                                }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .searchable(text: $query, prompt: "Search memory")
            .refreshable { await model.syncNow() }
            .saintBackground()
            .tint(Theme.accent)
            .navigationTitle("Memory")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { adding = true } label: { Image(systemName: "plus") }.accessibilityLabel("Teach SAINT something")
                }
                ToolbarItem(placement: .topBarLeading) {
                    SyncNowButton()
                }
            }
            .alert("Teach SAINT", isPresented: $adding) {
                TextField("e.g. my dog is called Rex", text: $newFact)
                Button("Remember") {
                    let text = newFact.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !text.isEmpty { Task { await model.submit("remember that " + text) } }
                    newFact = ""
                }
                Button("Cancel", role: .cancel) { newFact = "" }
            } message: {
                Text("Tell it something about you. It will be shared with your other devices.")
            }
        }
    }
}
