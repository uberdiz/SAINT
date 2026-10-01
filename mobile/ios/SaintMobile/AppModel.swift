import SwiftUI
import Combine
import Network
import UserNotifications
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

    let keychain: Keychain
    let settings: AppSettings
    let brain: Brain
    let link: LinkManager
    let voice = VoiceEngine()
    let speaker = Speaker()
    let spotify: SpotifyService
    let reminderCenter = ReminderCenter()

    @Published var messages: [ChatMessage] = []
    @Published var thinking = false
    @Published var peers: [PeerInfo] = []
    @Published var nearby: [BonjourBrowser.Found] = []
    @Published var inbox: [ReceivedFile] = []
    @Published var transfer: TransferState?
    @Published var banner: String?
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
        settings.$deviceName.sink { [weak self] name in self?.link.rename(to: name) }.store(in: &cancellables)
        settings.$keepScreenOn.sink { on in UIApplication.shared.isIdleTimerDisabled = on }.store(in: &cancellables)

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
            messages.append(ChatMessage(role: .saint, text: reply.text, language: reply.language, source: reply.source, ok: reply.ok))
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

    /// The mic button: listen for one command right now.
    func listenOnce() {
        guard permissionsOK else {
            Task { await requestAllPermissions() }
            return
        }
        speaker.stop()
        voice.listenNow()
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
        if url.host == "pair", let link = try? Pairing.parse(link: url.absoluteString) { pendingPair = link }
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
        let n = await link.syncNow()
        banner = n > 0 ? "Synced." : "No device to sync with right now."
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
