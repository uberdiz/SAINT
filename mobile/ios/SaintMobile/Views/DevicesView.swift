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
                }
                if !friends.isEmpty {
                    Section("Friends' SAINTs") {
                        ForEach(friends) { peer in
                            NavigationLink { DeviceDetailView(peer: peer) } label: { DeviceRow(peer: peer) }
                        }
                    }
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
                                        Text(found.name.isEmpty ? "SAINT" : found.name)
                                        Text("\(found.host):\(found.port) — tap to pair").font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }
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
                }
            }
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
                Image(systemName: "desktopcomputer.and.iphone").font(.system(size: 44)).foregroundStyle(Theme.accent)
                Text("Link your PC").font(.system(.title3, design: .rounded, weight: .bold))
                Text("On your PC open SAINT → Devices → turn on Link, then scan its code here. Your PC and phone will share what they learn, and you can control your PC by voice.")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("Add a device") { pairing = true }.buttonStyle(.borderedProminent)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 18)
        }
        .listRowBackground(Color.clear)
    }
}

extension Pairing.PairLink: Identifiable {
    public var id: String { "\(host):\(port)" }
}

struct DeviceRow: View {
    let peer: PeerInfo

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.title3).foregroundStyle(peer.online ? Theme.accent : .secondary).frame(width: 30)
            VStack(alignment: .leading, spacing: 2) {
                Text(peer.name).font(.system(.body, design: .rounded, weight: .medium))
                Text(peer.online ? "Connected" : "Offline").font(.caption).foregroundStyle(peer.online ? .green : .secondary)
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
                        TextField("Address, e.g. 192.168.1.20:8765", text: $address)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.numbersAndPunctuation)
                        TextField("Pairing code (ABCD-EFGH-…)", text: $code)
                            .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        Picker("This is", selection: $friend) {
                            Text("My own device").tag(false)
                            Text("A friend's SAINT").tag(true)
                        }
                        Button { pairTyped() } label: { Text(busy ? "Pairing…" : "Pair").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent)
                            .disabled(busy || address.isEmpty || code.isEmpty)
                    }
                    Section {
                        Text("On the PC: SAINT → Devices → “Pair a phone / device” shows a QR code and a code that works for five minutes. "
                             + "Both devices must be on the same network (or reachable by IP, e.g. over Tailscale).")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let error = error {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                }
            }
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
                        Pill(text: "Online", tint: .green)
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
                Button("Unpair this device", role: .destructive) { confirmUnpair = true }
            }
        }
        .navigationTitle(live.name)
        .navigationBarTitleDisplayMode(.inline)
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
