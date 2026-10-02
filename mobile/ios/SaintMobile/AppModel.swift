import SwiftUI
import Combine
import Network
import UserNotifications
import MessageUI
import SaintCore

struct ChatMessage: Identifiable, Equatable {
    enum Role { case you, saint, note }
    let id = UUID()
    var role: Role
    var text: String
    var language = ""
    var source = "phone"          // "phone", "pc" or "model"
    var ok = true
    var date = Date()
    var kind = ""                 // what SAINT did: music, phone, reminder, memory, pc, skill, chat
    var status = ""               // done, failed, sent (ran on the PC)
}

struct ReceivedFile: Identifiable, Equatable {
    var id: String { url.path }
    var name: String
    var url: URL
    var from: String
    var size: Int64
    var date: Date
}

struct TransferState: Equatable {
    var name: String
    var done: Int64
    var total: Int64
    var failed: String?
    var fraction: Double { total > 0 ? Double(done) / Double(total) : 0 }
}

/// Everything the screens share: the brain, the link to your PC, the voice, Spotify, reminders.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    enum Tab: Hashable { case talk, music, activity, devices, settings }

    let keychain: Keychain
    let settings: AppSettings
    let brain: Brain
    let link: LinkManager
    let voice = VoiceEngine()
    let speaker = Speaker()
    let spotify: SpotifyService
    let reminderCenter = ReminderCenter()
    let phoneActions = PhoneActions()

    @Published var messages: [ChatMessage] = []
    @Published var thinking = false
    @Published var peers: [PeerInfo] = []
    @Published var nearby: [BonjourBrowser.Found] = []
    @Published var inbox: [ReceivedFile] = []
    @Published var transfer: TransferState?
    @Published var banner: String? { didSet { if banner != nil { bannerID = UUID() } } }
    /// Changes with every banner, so a new one stays up for its full time.
    @Published private(set) var bannerID = UUID()
    @Published var tab: Tab = .talk
    @Published var showListening = false
    @Published var cameraRequest: CameraRequest?
    @Published var messageDraft: MessageDraft?
    /// The Shortcut SAINT just started ("SAINT Low Power On"); saint://shortcut-done says how it went.
    @Published var pendingShortcut: String?
    @Published var lastSync: Date?
    @Published var dataVersion = 0
    @Published var pendingPair: Pairing.PairLink?
    @Published var permissionsOK = VoiceEngine.permissionsGranted

    private let bonjour = BonjourBrowser()
    private let monitor = NWPathMonitor()
    private var cancellables = Set<AnyCancellable>()
    private var tickTimer: Timer?
    private var started = false
    private let dataDirectory: URL
    private let inboxDirectory: URL

    init() {
        let keychain = Keychain()
        self.keychain = keychain
        let settings = AppSettings(keychain: keychain)
        self.settings = settings

        let support = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil,
                                                    create: true)) ?? FileManager.default.temporaryDirectory
        let data = support.appendingPathComponent("SAINT", isDirectory: true)
        try? FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
        dataDirectory = data
        let documents = (try? FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil,
                                                      create: true)) ?? data
        let inbox = documents.appendingPathComponent("From my devices", isDirectory: true)       // shows up in the Files app
        inboxDirectory = inbox

        let brain = Brain(directory: data)
        self.brain = brain
        let identity = LinkIdentity.loadOrCreate(secrets: keychain, name: settings.deviceName)
        let engine = SyncEngine(deviceID: identity.deviceID, adapters: brain.adapters,
                                storage: data.appendingPathComponent("sync-mirror.json"))
        link = LinkManager(identity: identity, peerStore: PeerStore(directory: data), engine: engine, feed: brain.feed,
                           inboxDirectory: inbox) { host, port in NWTransport(host: host, port: port) }
        spotify = SpotifyService(keychain: keychain, settings: settings)

        brain.music = spotify
        brain.pc = link
        brain.phone = phoneActions
        brain.deviceName = settings.deviceName
        brain.preferPC = settings.preferPC
        brain.model = ModelChain { [settings] in
            var providers: [LanguageModel] = []
            let claude = ClaudeModel(apiKey: { settings.claudeKey }, model: { settings.claudeModel })
            if settings.useClaude && !settings.claudeKey.isEmpty { providers.append(claude) }
            if OnDeviceModel.isAvailable { providers.append(AppleOnDeviceModel()) }
            return providers
        }
        link.shareContext = settings.shareContext
        link.languages = { [brain] in brain.settings.preferred }
        voice.settings = settings
        speaker.rate = settings.speechRate
        applyLanguageSettings()
        wire()
    }

    // MARK: wiring

    private func wire() {
        phoneActions.model = self
        voice.onDiagnostic = { [weak self] text in
            self?.log(request: "(listening)", action: text, kind: "voice", ok: false)
        }
        link.onEvent = { [weak self] event in Task { @MainActor in self?.handle(event) } }
        voice.onCommand = { [weak self] text, language in
            Task { @MainActor in await self?.heard(text, language: language) }
        }
        bonjour.onFound = { [weak self] found in
            Task { @MainActor in self?.found(found) }
        }
        reminderCenter.onForegroundNotification = { _, _ in }      // the one-second tick speaks reminders while SAINT is open

        settings.$preferPC.sink { [weak self] on in self?.brain.preferPC = on }.store(in: &cancellables)
        settings.$shareContext.sink { [weak self] on in self?.link.shareContext = on }.store(in: &cancellables)
        settings.$speechRate.sink { [weak self] rate in self?.speaker.rate = rate }.store(in: &cancellables)
        settings.$deviceName.sink { [weak self] name in
            self?.link.rename(to: name)
            self?.brain.deviceName = name
        }.store(in: &cancellables)
        settings.$audioOutput.dropFirst().removeDuplicates().sink { [weak self] _ in
            // next runloop: the new value is stored by then, and the engine reads it from settings
            DispatchQueue.main.async { self?.voice.applyAudioRoute() }
        }.store(in: &cancellables)
        settings.$keepScreenOn.sink { on in UIApplication.shared.isIdleTimerDisabled = on }.store(in: &cancellables)
        settings.$useHeadsetMic.dropFirst().sink { [weak self] _ in
            // re-open the audio session with the new Bluetooth profile
            guard let self = self, self.voice.phase != .off else { return }
            self.voice.start()
        }.store(in: &cancellables)
        reminderCenter.snoozeMinutes = { [settings] in settings.snoozeMinutes }

        NotificationCenter.default.publisher(for: .saintDataChanged)
            .debounce(for: .milliseconds(400), scheduler: DispatchQueue.main)
            .sink { [weak self] _ in self?.dataChanged() }
            .store(in: &cancellables)

        voice.$phase.removeDuplicates().sink { [weak self] phase in
            if phase == .capturing { self?.speaker.stop() }
        }.store(in: &cancellables)

        monitor.pathUpdateHandler = { [weak self] path in
            if path.status == .satisfied { Task { @MainActor in self?.link.reconnectNow() } }
        }
    }

    private func dataChanged() {
        dataVersion += 1
        applyLanguageSettings()
        Task { await reminderCenter.reschedule(brain.reminders.notificationPlans()) }
    }

    /// The language list that syncs between devices is also what the microphone listens in.
    private func applyLanguageSettings() {
        brain.reloadSettings()
        let preferred = brain.settings.preferred
        if !preferred.isEmpty && Array(preferred.prefix(2)) != settings.listenLanguages {
            settings.listenLanguages = Array(preferred.prefix(2))
        }
    }

    func setLanguages(_ codes: [String]) {
        settings.listenLanguages = Array(codes.prefix(2))
        brain.settings.update(preferred: codes)
        brain.reloadSettings()
        if voice.phase != .off { voice.start() }
    }

    // MARK: lifecycle

    func start() async {
        if started { return }
        started = true
        link.start()
        bonjour.start()
        monitor.start(queue: DispatchQueue(label: "app.saint.path"))
        refresh()
        if await reminderCenter.isAuthorized() { await reminderCenter.reschedule(brain.reminders.notificationPlans()) }
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        permissionsOK = VoiceEngine.permissionsGranted
        if settings.onboarded && settings.alwaysListening && permissionsOK { voice.start() }
    }

    func appBecameActive() {
        link.reconnectNow()
        bonjour.start()
        refresh()
        UIApplication.shared.isIdleTimerDisabled = settings.keepScreenOn
        permissionsOK = VoiceEngine.permissionsGranted
    }

    func refresh() {
        peers = link.peers
        refreshInbox()
    }

    func requestAllPermissions() async {
        let result = await VoiceEngine.requestPermissions()
        _ = await reminderCenter.requestAuthorization()
        permissionsOK = result.microphone && result.speech
        if permissionsOK && settings.alwaysListening { voice.start() }
        await reminderCenter.reschedule(brain.reminders.notificationPlans())
    }

    func setAlwaysListening(_ on: Bool) {
        settings.alwaysListening = on
        if on && permissionsOK { voice.start() } else { voice.stop() }
    }

    // MARK: talking

    /// A command from the microphone.
    func heard(_ text: String, language: String) async {
        await run(text, language: language, spoken: true)
    }

    /// A command typed in the box.
    func submit(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return }
        await run(trimmed, language: "", spoken: false)
    }

    private func run(_ text: String, language: String, spoken: Bool) async {
        speaker.stop()
        messages.append(ChatMessage(role: .you, text: text, language: language))
        thinking = true
        voice.pauseRecognition(for: .thinking)
        let reply = await brain.handle(text, hint: language)
        thinking = false
        if !reply.text.isEmpty {
            var message = ChatMessage(role: .saint, text: reply.text, language: reply.language, source: reply.source, ok: reply.ok)
            if let entry = brain.actions.all().first, entry.request == text {
                message.kind = entry.kind
                message.status = entry.status
            }
            messages.append(message)
        }
        trimTranscript()
        link.shareTurn(user: text, reply: reply.text)
        dataChanged()
        if reply.stopSpeaking || reply.text.isEmpty || !settings.speakReplies {
            finishedTalking(expectsReply: reply.expectsReply && !reply.text.isEmpty)
            return
        }
        voice.pauseRecognition(for: .speaking)
        speaker.speak(reply.text, language: reply.language) { [weak self] in
            Task { @MainActor in self?.finishedTalking(expectsReply: reply.expectsReply) }
        }
    }

    private func finishedTalking(expectsReply: Bool) {
        if expectsReply && settings.alwaysListening && permissionsOK {
            voice.listenNow()
        } else if settings.alwaysListening && permissionsOK {
            voice.resume()
        } else {
            voice.stop()
        }
    }

    /// The orb: tap to listen, tap again to stop. Tapping while SAINT talks cuts it off and listens.
    func listenOnce() {
        guard permissionsOK else {
            Task { await requestAllPermissions() }
            return
        }
        if voice.isCapturing {
            voice.stopCapture()
            if settings.haptics { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
            return
        }
        speaker.stop()
        voice.listenNow()
        showListening = true
    }

    private func trimTranscript() {
        if messages.count > 60 { messages.removeFirst(messages.count - 60) }
    }

    private func tick() {
        for reminder in brain.reminders.tick() {
            let text = reminder.isTimer ? "Time's up." : "Reminder: \(reminder.message)."
            messages.append(ChatMessage(role: .saint, text: text))
            let language = brain.lang.replyLanguage
            var turn = LangTurn(text: text)
            turn.language = language
            let local = brain.lang.localize(text, for: turn)
            if voice.phase == .listening || voice.phase == .off {
                voice.pauseRecognition(for: .speaking)
                speaker.speak(local.text, language: language) { [weak self] in
                    Task { @MainActor in self?.finishedTalking(expectsReply: false) }
                }
            }
            dataVersion += 1
        }
    }

    // MARK: devices

    private func found(_ f: BonjourBrowser.Found) {
        link.setAddressHint(peerID: f.id, host: f.host, port: f.port)
        if let i = nearby.firstIndex(where: { $0.id == f.id }) { nearby[i] = f } else { nearby.append(f) }
    }

    private func handle(_ event: LinkEvent) {
        peers = link.peers
        switch event {
        case .paired(let peer):
            banner = "Paired with \(peer.name)."
            pendingPair = nil
        case .synced(let peer, let received, let sent):
            lastSync = Date()
            if received > 0 { banner = "Learned \(received) thing\(received == 1 ? "" : "s") from \(peer)." }
            _ = sent
            dataVersion += 1
        case .fileReceived(let peer, let name, _):
            banner = "Got “\(name)” from \(peer)."
            refreshInbox()
        case .fileProgress(_, let name, let done, let total):
            transfer = TransferState(name: name, done: done, total: total)
            if done >= total { transfer = nil }
        case .problem(let message):
            banner = message
        case .connected, .disconnected:
            break
        }
    }

    func handleOpen(_ url: URL) {
        guard url.scheme == "saint" else { return }
        switch url.host {
        case "pair":
            if let link = try? Pairing.parse(link: url.absoluteString) { pendingPair = link }
        case "shortcut-done", "shortcut-error":
            // Back from the Shortcuts app (x-callback-url).
            let name = pendingShortcut ?? "The shortcut"
            pendingShortcut = nil
            let ok = url.host == "shortcut-done"
            let error = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
                .first { $0.name == "errorMessage" }?.value
            let text = ok ? "\u{201C}\(name)\u{201D} ran."
                          : "\u{201C}\(name)\u{201D} didn't run\(error.map { ": \($0)" } ?? ""). Make a Shortcut with exactly that name (Settings \u{2192} Phone control)."
            banner = text
            log(request: "Shortcut", action: text, kind: "phone", ok: ok)
        default:
            break
        }
    }

    // MARK: things the phone did outside a conversation turn

    private func log(request: String, action: String, kind: String, ok: Bool) {
        brain.actions.add(ActionEntry(request: request, action: action, kind: kind, status: ok ? "done" : "failed",
                                      device: settings.deviceName))
        dataVersion += 1
    }

    /// Apple's message sheet closed.
    func messageFinished(_ draft: MessageDraft, result: MessageComposeResult) {
        messageDraft = nil
        let request = "Text \(draft.name)"
        switch result {
        case .sent:
            banner = "Text sent to \(draft.name)."
            log(request: request, action: "Sent: \(draft.body)", kind: "phone", ok: true)
        case .failed:
            banner = "The text to \(draft.name) didn't send."
            log(request: request, action: "The message failed to send", kind: "phone", ok: false)
        default:
            log(request: request, action: "You cancelled the message", kind: "phone", ok: true)
        }
    }

    /// SAINT's camera saved a photo or video.
    func cameraSaved(_ what: String) {
        banner = what == "video" ? "Video saved to Photos." : "Photo saved to Photos."
        log(request: what == "video" ? "Record a video" : "Take a picture", action: "Saved the \(what) to Photos",
            kind: "phone", ok: true)
    }

    /// A button on the Music tab did something.
    func logMusic(intent: MusicIntent, result: String) {
        let lower = result.lowercased()
        let failed = ["isn't", "couldn't", "can't", "didn't", "not found", "no active"].contains { lower.contains($0) }
        log(request: "Music tab", action: result, kind: "music", ok: !failed)
    }

    /// The Activity log as tab-separated text, for sharing.
    func activityExport() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let lines = brain.actions.all().map { e in
            [f.string(from: e.ts), e.kind, e.status, e.source, e.request, e.action, e.detail ?? ""].joined(separator: "\t")
        }
        return (["time\tkind\tstatus\twhere\tyou asked\tSAINT did\tdetail"] + lines).joined(separator: "\n")
    }

    func pair(address: String, code: String, role: String) async -> String? {
        let target = Pairing.parse(address: address)
        do {
            try await link.pair(host: target.host, port: target.port, code: code, role: role)
            peers = link.peers
            return nil
        } catch {
            return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    func pair(link pairLink: Pairing.PairLink) async -> String? {
        do {
            try await link.pair(link: pairLink)
            peers = link.peers
            return nil
        } catch {
            return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    func unpair(_ id: String) {
        link.unpair(id)
        peers = link.peers
    }

    /// Your own name for a paired device ("Gaming PC").
    func renamePeer(_ id: String, to name: String) {
        link.renamePeer(id, to: name)
        peers = link.peers
        dataVersion += 1
    }

    /// The address SAINT tries when the Wi-Fi one doesn't answer (a Tailscale 100.x address or MagicDNS name).
    func remoteHost(of id: String) -> String? {
        _ = dataVersion
        guard let peer = link.peerStore.get(id) else { return nil }
        let hosts = [peer.host] + (peer.altHosts ?? [])
        return hosts.first { Self.isTailscale($0) } ?? peer.altHosts?.first
    }

    func setRemoteHost(_ id: String, host: String) {
        let clean = host.trimmingCharacters(in: .whitespacesAndNewlines)
        link.peerStore.update(id) { peer in
            var alts = (peer.altHosts ?? []).filter { !Self.isTailscale($0) && $0 != clean }
            if !clean.isEmpty { alts.insert(clean, at: 0) }
            peer.altHosts = alts.isEmpty ? nil : alts
        }
        dataVersion += 1
        link.reconnectNow()
    }

    /// 100.64.0.0/10 (Tailscale's addresses) or a MagicDNS name.
    static func isTailscale(_ host: String) -> Bool {
        if host.hasSuffix(".ts.net") { return true }
        let parts = host.split(separator: ".").compactMap { Int($0) }
        return parts.count == 4 && parts[0] == 100 && (64...127).contains(parts[1])
    }

    /// Say something to your PC's SAINT directly (the Control tab).
    func askPC(_ text: String, peerID: String) async -> String {
        do {
            let answer = try await link.ask(peerID: peerID, text: text, language: brain.lang.replyLanguage)
            link.shareTurn(user: text, reply: answer.text)
            return answer.text
        } catch {
            return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    func syncNow() async {
        let results = await link.syncAll()
        if results.isEmpty {
            banner = "Pair your PC first (Devices), then sync."
        } else {
            banner = results.map { r -> String in
                if let why = r.error { return "Couldn't reach \(r.peer): \(why)" }
                if r.received == 0 && r.sent == 0 { return "Already in sync with \(r.peer) — nothing new on either side." }
                return "Synced with \(r.peer): got \(r.received), sent \(r.sent)."
            }.joined(separator: " ")
            if results.contains(where: { $0.error == nil }) { lastSync = Date() }
        }
        refresh()
    }

    func send(file url: URL, to peerID: String) async {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        let temp = FileManager.default.temporaryDirectory.appendingPathComponent(url.lastPathComponent)
        try? FileManager.default.removeItem(at: temp)
        do {
            try FileManager.default.copyItem(at: url, to: temp)
        } catch {
            banner = "Couldn't read that file."
            return
        }
        defer { try? FileManager.default.removeItem(at: temp) }
        let name = temp.lastPathComponent
        transfer = TransferState(name: name, done: 0, total: 1)
        do {
            _ = try await link.sendFile(peerID: peerID, url: temp) { [weak self] done, total in
                Task { @MainActor in self?.transfer = TransferState(name: name, done: done, total: max(total, 1)) }
            }
            transfer = nil
            banner = "Sent “\(name)”."
        } catch {
            transfer = nil
            banner = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    func refreshInbox() {
        var found: [ReceivedFile] = []
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]
        if let walker = FileManager.default.enumerator(at: inboxDirectory, includingPropertiesForKeys: keys,
                                                       options: [.skipsHiddenFiles]) {
            for case let url as URL in walker {
                guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else { continue }
                found.append(ReceivedFile(name: url.lastPathComponent, url: url, from: url.deletingLastPathComponent().lastPathComponent,
                                          size: Int64(values.fileSize ?? 0), date: values.contentModificationDate ?? Date()))
            }
        }
        inbox = found.sorted { $0.date > $1.date }
    }

    func deleteReceived(_ file: ReceivedFile) {
        try? FileManager.default.removeItem(at: file.url)
        refreshInbox()
    }
}
