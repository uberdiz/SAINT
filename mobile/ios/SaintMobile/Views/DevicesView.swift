import SwiftUI
import UniformTypeIdentifiers
import SaintCore

struct DevicesView: View {
    @EnvironmentObject var model: AppModel
    @State private var pairing = false

    private var own: [PeerInfo] { model.peers.filter { $0.isOwn } }
    private var friends: [PeerInfo] { model.peers.filter { !$0.isOwn } }

    var body: some View {
        NavigationStack {
            List {
                if model.peers.isEmpty { emptyState }
                if !own.isEmpty {
                    Section("My devices") {
                        ForEach(own) { peer in
                            NavigationLink { DeviceDetailView(peer: peer) } label: { DeviceRow(peer: peer) }
                        }
                    }
                    .listRowBackground(Theme.surface)
                    remoteSection
                }
                if !friends.isEmpty {
                    Section("Friends' SAINTs") {
                        ForEach(friends) { peer in
                            NavigationLink { DeviceDetailView(peer: peer) } label: { DeviceRow(peer: peer) }
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
                let unpaired = model.nearby.filter { found in !model.peers.contains(where: { $0.id == found.id }) }
                if !unpaired.isEmpty {
                    Section("Nearby on this Wi-Fi") {
                        ForEach(unpaired, id: \.id) { found in
                            Button {
                                pairing = true
                            } label: {
                                HStack {
                                    Image(systemName: "dot.radiowaves.left.and.right").foregroundStyle(Theme.accent)
                                    VStack(alignment: .leading) {
                                        Text(found.name.isEmpty ? "SAINT" : found.name).foregroundStyle(Theme.text)
                                        Text("\(found.host):\(found.port) — tap to pair").font(.caption).foregroundStyle(Theme.muted)
                                    }
                                }
                            }
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
                if !model.inbox.isEmpty {
                    Section("Received files") {
                        ForEach(model.inbox.prefix(8)) { file in
                            ShareLink(item: file.url) {
                                HStack {
                                    Image(systemName: "doc").foregroundStyle(.secondary)
                                    VStack(alignment: .leading) {
                                        Text(file.name).lineLimit(1)
                                        Text("from \(file.from) · \(ByteCountFormatter.string(fromByteCount: file.size, countStyle: .file))")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                            .swipeActions {
                                Button(role: .destructive) { model.deleteReceived(file) } label: { Label("Delete", systemImage: "trash") }
                            }
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
            }
            .scrollContentBackground(.hidden)
            .saintBackground()
            .tint(Theme.accent)
            .navigationTitle("Devices")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { pairing = true } label: { Image(systemName: "plus") }.accessibilityLabel("Add a device")
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button { Task { await model.syncNow() } } label: { Image(systemName: "arrow.triangle.2.circlepath") }
                        .accessibilityLabel("Sync now")
                }
            }
            .sheet(isPresented: $pairing) { PairSheet() }
            .sheet(item: $model.pendingPair) { link in PairSheet(link: link) }
            .refreshable { await model.syncNow() }
            .onAppear { model.refresh() }
        }
    }

    private var emptyState: some View {
        Section {
            VStack(spacing: 10) {
                Image(systemName: "laptopcomputer.and.iphone").font(.system(size: 44)).foregroundStyle(Theme.accent)
                    .frame(width: 88, height: 88)
                    .background(Theme.accentSoft, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                Text("Link your PC").font(.system(size: 20, weight: .bold)).foregroundStyle(Theme.text)
                Text("On your PC open SAINT → Devices → “Pair a phone / device”, then scan its code here. Your PC and phone share what they learn, the phone's activity shows up in your PC's History, and you can control your PC by voice.")
                    .font(.subheadline).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
                Button { pairing = true } label: {
                    Text("Add a device").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.onAccent)
                        .padding(.horizontal, 22).padding(.vertical, 11)
                        .background(Theme.accent, in: Capsule())
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 18)
        }
        .listRowBackground(Color.clear)
    }

    /// Reaching your PC away from home: through Tailscale (the PC puts its Tailscale address in the pairing code).
    private var remoteSection: some View {
        let remote = own.compactMap { peer -> (PeerInfo, String)? in
            guard let host = model.remoteHost(of: peer.id) else { return nil }
            return (peer, host)
        }.first
        return Section {
            if let r = remote {
                SettingRow(icon: "globe", title: "Reachable from anywhere",
                           subtitle: "\(r.0.name) over Tailscale (\(r.1)). Keep Tailscale on, on this iPhone too.",
                           iconTint: Theme.success, tile: Theme.successSoft)
                    .listRowInsets(EdgeInsets())
            } else {
                SettingRow(icon: "wifi", title: "Only on your Wi-Fi",
                           subtitle: "Install Tailscale on your PC and this iPhone (same account), then pair again — or add the PC's Tailscale address in its page.",
                           iconTint: Theme.accent, tile: Theme.accentSoft) {
                    Link(destination: URL(string: "https://tailscale.com/download/ios")!) {
                        Text("Get").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.onAccent)
                            .padding(.horizontal, 12).padding(.vertical, 6).background(Theme.accent, in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
                .listRowInsets(EdgeInsets())
            }
        } header: {
            Text("Away from home")
        }
        .listRowBackground(Theme.surface)
    }
}

extension Pairing.PairLink: Identifiable {
    public var id: String { "\(host):\(port)" }
}

struct DeviceRow: View {
    let peer: PeerInfo

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.system(size: 17, weight: .medium))
                .foregroundStyle(peer.online ? Theme.accent : Theme.muted)
                .frame(width: 36, height: 36)
                .background(peer.online ? Theme.accentSoft : Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(peer.name).font(.system(size: 15.5, weight: .medium)).foregroundStyle(Theme.text)
                Text(peer.online ? "Connected" : "Offline").font(.caption).foregroundStyle(peer.online ? Theme.success : Theme.muted)
            }
        }
    }

    private var icon: String {
        switch peer.platform {
        case "windows", "linux": return "desktopcomputer"
        case "mac": return "laptopcomputer"
        case "ios": return "iphone"
        default: return peer.isOwn ? "desktopcomputer" : "person.crop.circle"
        }
    }
}

// MARK: pairing

struct PairSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let link: Pairing.PairLink?

    init(link: Pairing.PairLink? = nil) { self.link = link }

    @State private var address = ""
    @State private var code = ""
    @State private var friend = false
    @State private var scanning = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                if let link = link {
                    Section {
                        Text("Pair with \(link.name.isEmpty ? link.host : link.name)?").font(.headline)
                        Text(link.role == "own" ? "It will become one of your own devices: you can control it and everything SAINT learns is shared."
                                                : "It will be added as a friend's SAINT: you can ask it to do things you've been allowed, and share files.")
                            .font(.subheadline).foregroundStyle(.secondary)
                        Button { pairNow(link) } label: { Text(busy ? "Pairing…" : "Pair").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent).disabled(busy)
                    }
                } else {
                    Section("Scan") {
                        Button { scanning = true } label: { Label("Scan the code on my PC", systemImage: "qrcode.viewfinder") }
                    }
                    Section("Or type it") {
                        TextField(model.nearby.isEmpty ? "Address, e.g. 192.168.1.20:8765" : "Address (optional — \(model.nearby.count) SAINT nearby)", text: $address)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.numbersAndPunctuation)
                        TextField("Pairing code (ABCD-EFGH)", text: $code)
                            .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        Picker("This is", selection: $friend) {
                            Text("My own device").tag(false)
                            Text("A friend's SAINT").tag(true)
                        }
                        Button { pairTyped() } label: { Text(busy ? "Pairing…" : "Pair").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent)
                            .disabled(busy || code.isEmpty || (address.isEmpty && model.nearby.isEmpty))
                    }
                    Section {
                        Text("On the PC: SAINT → Devices → “Pair a phone / device” shows a QR code and a code that works for five minutes. "
                             + "On the same Wi-Fi the code alone is enough. Otherwise type the PC's address too (its Tailscale 100.x address works from anywhere). "
                             + "The code decides own device vs. friend; if you pick differently, both sides use “friend”.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let error = error {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                }
            }
            .scrollContentBackground(.hidden)
            .saintBackground()
            .tint(Theme.accent)
            .navigationTitle("Add a device")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
            .fullScreenCover(isPresented: $scanning) {
                QRScannerView { text in
                    scanning = false
                    if let scanned = try? Pairing.parse(link: text) { pairNow(scanned) }
                    else { error = "That QR code isn't from SAINT." }
                } onCancel: { scanning = false }
            }
        }
    }

    private func pairNow(_ link: Pairing.PairLink) {
        busy = true
        error = nil
        Task {
            let problem = await model.pair(link: link)
            busy = false
            if let problem = problem { error = problem } else { dismiss() }
        }
    }

    private func pairTyped() {
        busy = true
        error = nil
        Task {
            let problem = await model.pair(address: address, code: code, role: friend ? "collaborator" : "own")
            busy = false
            if let problem = problem { error = problem } else { dismiss() }
        }
    }
}

// MARK: one device

struct DeviceDetailView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let peer: PeerInfo

    init(peer: PeerInfo) { self.peer = peer }

    @State private var command = ""
    @State private var answer = ""
    @State private var working = false
    @State private var importing = false
    @State private var promptTarget = "claude"
    @State private var promptText = ""
    @State private var messageText = ""
    @State private var confirmUnpair = false
    @State private var renaming = false
    @State private var newName = ""
    @State private var editingRemote = false
    @State private var remoteHost = ""

    private var live: PeerInfo { model.peers.first(where: { $0.id == peer.id }) ?? peer }

    private let quick: [(String, String, String)] = [
        ("Lock PC", "lock", "lock my pc"),
        ("Screenshot", "camera.viewfinder", "take a screenshot"),
        ("Mute mic", "mic.slash", "mute my mic"),
        ("Unmute mic", "mic", "unmute my mic"),
        ("Pause music", "pause", "pause"),
        ("Next song", "forward", "skip"),
        ("Volume up", "speaker.plus", "turn it up"),
        ("Volume down", "speaker.minus", "turn it down"),
    ]

    var body: some View {
        List {
            Section {
                HStack {
                    DeviceRow(peer: live)
                    Spacer()
                    if live.online {
                        Pill(text: "Online", tint: Theme.success, fill: Theme.successSoft)
                    } else {
                        Button("Connect") { Task { _ = try? await model.link.connect(peerID: live.id); model.refresh() } }
                            .buttonStyle(.bordered)
                    }
                }
            }
            if live.isOwn { ownSection } else { friendSection }
            Section("Files") {
                Button { importing = true } label: { Label("Send a file", systemImage: "paperplane") }
                    .disabled(!live.online || working)
                if let t = model.transfer {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(t.name).font(.caption)
                        ProgressView(value: t.fraction)
                    }
                }
            }
            if live.isOwn {
                Section {
                    Button { Task { await model.syncNow() } } label: { Label("Sync what we've learned", systemImage: "arrow.triangle.2.circlepath") }
                }
            }
            Section {
                Button { newName = live.name; renaming = true } label: { Label("Rename…", systemImage: "pencil") }
                Button { remoteHost = model.remoteHost(of: live.id) ?? ""; editingRemote = true } label: {
                    Label(model.remoteHost(of: live.id).map { "Away-from-home address: \($0)" } ?? "Add an away-from-home address…",
                          systemImage: "globe")
                }
            } footer: {
                Text("The away-from-home address is the PC's Tailscale address (100.x.y.z, shown on the PC's Devices page). "
                     + "SAINT tries it whenever your Wi-Fi address doesn't answer.")
            }
            Section {
                Button("Unpair this device", role: .destructive) { confirmUnpair = true }
            }
        }
        .listRowBackground(Theme.surface)
        .scrollContentBackground(.hidden)
        .saintBackground()
        .tint(Theme.accent)
        .navigationTitle(live.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { newName = live.name; renaming = true } label: { Image(systemName: "pencil") }
                    .accessibilityLabel("Rename")
            }
        }
        .alert("Rename device", isPresented: $renaming) {
            TextField("Name", text: $newName)
            Button("Save") { model.renamePeer(live.id, to: newName) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Only on this phone. To rename this iPhone everywhere, use Settings → This phone.")
        }
        .alert("Away-from-home address", isPresented: $editingRemote) {
            TextField("100.x.y.z or name.ts.net", text: $remoteHost)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            Button("Save") { model.setRemoteHost(live.id, host: remoteHost) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Your PC's Tailscale address. Leave empty to remove it.")
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item]) { result in
            if case .success(let url) = result { Task { await model.send(file: url, to: live.id) } }
        }
        .confirmationDialog("Unpair \(live.name)?", isPresented: $confirmUnpair, titleVisibility: .visible) {
            Button("Unpair", role: .destructive) { model.unpair(live.id); dismiss() }
        } message: {
            Text("It will have to be paired again with a new code.")
        }
    }

    // MARK: control my own PC

    private var ownSection: some View {
        Group {
            Section("Control this device") {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 8)], spacing: 8) {
                    ForEach(quick.indices, id: \.self) { index in
                        let item = quick[index]
                        Button { send(item.2) } label: {
                            VStack(spacing: 4) {
                                Image(systemName: item.1).font(.title3)
                                Text(item.0).font(.caption2).lineLimit(1)
                            }
                            .frame(maxWidth: .infinity, minHeight: 54)
                        }
                        .buttonStyle(.bordered)
                        .disabled(!live.online || working)
                    }
                }
                HStack {
                    TextField("Tell SAINT on your PC…", text: $command).submitLabel(.send).onSubmit { send(command) }
                    Button { send(command) } label: { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                        .disabled(command.isEmpty || !live.online || working)
                }
                if working { ProgressView() }
                if !answer.isEmpty { Text(answer).font(.subheadline).textSelection(.enabled) }
            }
        }
    }

    private func send(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return }
        command = ""
        working = true
        Task {
            answer = await model.askPC(t, peerID: live.id)
            working = false
        }
    }

    // MARK: a friend's SAINT

    private let targets = ["claude", "chatgpt", "gemini", "copilot", "perplexity", "grok"]

    private var friendSection: some View {
        Group {
            Section("Ask \(live.name)'s SAINT to…") {
                Picker("App", selection: $promptTarget) {
                    ForEach(targets, id: \.self) { Text($0.capitalized).tag($0) }
                }
                TextField("Prompt to type into it", text: $promptText, axis: .vertical).lineLimit(1...4)
                Button { automate("send_prompt", ["target": promptTarget, "prompt": promptText]); promptText = "" } label: {
                    Label("Send prompt", systemImage: "text.bubble")
                }
                .disabled(promptText.isEmpty || !live.online || working)
            }
            Section("Message") {
                TextField("Say something on their PC", text: $messageText)
                Button { automate("message", ["text": messageText]); messageText = "" } label: { Label("Send message", systemImage: "message") }
                    .disabled(messageText.isEmpty || !live.online || working)
            }
            Section {
                if working { ProgressView() }
                if !answer.isEmpty { Text(answer).font(.subheadline) }
                Text("What you can do here is up to \(live.name): each action either runs, asks them first, or is refused.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private func automate(_ name: String, _ args: JSONObject) {
        working = true
        Task {
            do {
                let result = try await model.link.runAutomation(peerID: live.id, name: name, args: args)
                answer = result.isEmpty ? "Done." : result
            } catch {
                answer = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            }
            working = false
        }
    }
}
