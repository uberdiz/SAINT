import Foundation
import AVFoundation
import Speech
import UIKit
import AudioToolbox
import SaintCore

/// Always listening for "SAINT", like Siri but with our own word.
///
/// One AVAudioEngine feeds the microphone to a speech recogniser per language you speak (say English and
/// Spanish), all running on the device where the phone supports it. Every partial transcript is checked for the
/// wake word; when it's heard, what follows is the command, and a short silence ends it. The engine keeps
/// running — that is what lets iOS keep the app alive in the background and with the screen locked (the
/// "audio" background mode) — and the recognisers are paused while SAINT is thinking or speaking so it
/// doesn't hear itself.
final class VoiceEngine: ObservableObject {
    enum Phase: Equatable {
        case off            // not listening
        case listening      // waiting for the wake word
        case capturing      // heard it: collecting the command
        case thinking       // command sent, recognisers paused
        case speaking
    }

    // what the UI shows
    @Published private(set) var phase: Phase = .off
    @Published private(set) var transcript = ""
    @Published private(set) var level: Float = 0
    @Published private(set) var problem: String?
    /// Where the sound goes and comes from, e.g. "AirPods Pro · mic: iPhone", for the UI.
    @Published private(set) var route = ""
    /// True when a Bluetooth headset is in its hands-free (phone-call quality) profile.
    @Published private(set) var callQuality = false
    @Published private(set) var inputName = "iPhone"
    @Published private(set) var outputName = "iPhone"
    /// Something worth writing in the Activity log ("Speech recognition failed: …"), rate-limited.
    var onDiagnostic: ((String) -> Void)?
    private var lastDiagnostic: [String: Date] = [:]

    /// Called with the command and the language it seems to be in.
    var onCommand: ((String, String) -> Void)?
    var settings: AppSettings?

    private let queue = DispatchQueue(label: "app.saint.voice")
    private let engine = AVAudioEngine()
    private var sessions: [Recognition] = []
    private var running = false
    private var wantsListening = false
    private var phaseValue: Phase = .off
    private var wakeSession: Recognition?
    private var wakeEnd = 0
    private var captureStarted = Date()
    private var lastChange = Date()
    private var manual = false                 // capturing without a wake word (the mic button, a follow-up question)
    private var timer: DispatchSourceTimer?
    private var observersAdded = false
    /// The requests the audio thread feeds. Kept apart from `sessions` (which the voice queue changes) behind a lock.
    private let sinkLock = NSLock()
    private var sinks: [SFSpeechAudioBufferRecognitionRequest] = []
    /// The last ~0.7 s of microphone audio (behind `sinkLock`). A tap on the orb starts a new recogniser; it is
    /// handed this first so the start of what you said isn't cut off.
    private var preroll: [AVAudioPCMBuffer] = []
    private var prerollFrames: AVAudioFrameCount = 0

    /// One speech recogniser, for one language, working on the shared microphone stream.
    private final class Recognition {
        let code: String
        let locale: Locale
        let recognizer: SFSpeechRecognizer
        var request: SFSpeechAudioBufferRecognitionRequest?
        var task: SFSpeechRecognitionTask?
        var transcript = ""
        var confidence: Double = 0
        var segments: [SFTranscriptionSegment] = []
        var startedAt = Date()
        var failures = 0
        /// When each word of ``transcript`` first appeared (our own clock: partial results carry no timestamps).
        var wordTimes: [Date] = []
        var lastChange = Date()
        /// On-device recognition kept failing for this language: use Apple's servers instead.
        var serverFallback = false

        init(code: String, locale: Locale, recognizer: SFSpeechRecognizer) {
            self.code = code
            self.locale = locale
            self.recognizer = recognizer
        }
    }

    // MARK: permissions

    static func requestPermissions() async -> (microphone: Bool, speech: Bool) {
        let mic: Bool = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
        }
        let speech: Bool = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0 == .authorized) }
        }
        return (mic, speech)
    }

    static var permissionsGranted: Bool {
        AVAudioApplication.shared.recordPermission == .granted && SFSpeechRecognizer.authorizationStatus() == .authorized
    }

    // MARK: start and stop

    /// Start listening for the wake word. Safe to call again.
    func start() {
        queue.async {
            self.wantsListening = true
            self.addObserversIfNeeded()
            self.startEngineAndRecognition()
        }
    }

    /// Stop listening completely (the microphone indicator goes away and iOS may suspend the app).
    func stop() {
        queue.async {
            self.wantsListening = false
            self.teardown()
            self.setPhase(.off)
        }
    }

    /// SAINT is working on a command or speaking: stop hearing things, keep the audio session alive.
    func pauseRecognition(for phase: Phase = .thinking) {
        queue.async {
            self.stopRecognition()
            self.setPhase(phase)
        }
    }

    /// Pick up listening again after SAINT has finished talking.
    func resume() {
        queue.async {
            guard self.wantsListening else { return }
            self.manual = false
            self.startEngineAndRecognition()
        }
    }

    /// Listen for a command right now without the wake word: the mic button, or an answer to a question SAINT asked.
    func listenNow() {
        queue.async {
            self.wantsListening = true
            self.addObserversIfNeeded()
            self.stopRecognition()                  // start clean: nothing said a moment ago should count
            self.startEngineAndRecognition(withPreroll: true)
            self.manual = true
            self.wakeSession = nil
            self.wakeEnd = 0
            self.captureStarted = Date()
            self.lastChange = Date()
            self.setPhase(.capturing)
            self.publishTranscript("")
        }
    }

    /// The orb was tapped while listening for a command: stop listening. What was said so far is sent; if
    /// nothing was said, SAINT goes back to waiting for its name.
    func stopCapture() {
        queue.async {
            guard self.phaseValue == .capturing else { return }
            if self.currentCommand().isEmpty { self.cancelCapture() } else { self.finishCommand() }
        }
    }

    var isCapturing: Bool { phase == .capturing }

    // MARK: audio engine

    /// AirPods and other Bluetooth headsets have two modes: high-quality stereo (A2DP) that can't carry a
    /// microphone, and the hands-free profile (HFP) used for phone calls, which can but makes *everything* —
    /// music included — sound like a call. `.allowBluetooth` lets iOS pick HFP whenever the mic is open, which
    /// is what made SAINT's listening sound like a phone call. So by default SAINT listens with the iPhone's own
    /// microphone and keeps the headset on A2DP; "Use my headset's microphone" in Settings opts into HFP.
    private func configureSession(speaking: Bool = false) throws {
        let session = AVAudioSession.sharedInstance()
        let output = settings?.audioOutput ?? "auto"
        let headsetMic = (settings?.useHeadsetMic ?? false) && output == "auto"
        var options: AVAudioSession.CategoryOptions = [.mixWithOthers]
        switch output {
        case "speaker": options.insert(.defaultToSpeaker)                      // never the headphones
        case "earpiece": break                                                // the receiver, held to your ear
        default:
            options.formUnion([.defaultToSpeaker, .allowBluetoothA2DP])       // headphones when connected
            if headsetMic { options.insert(.allowBluetooth) }                 // AirPods mic: call quality
        }
        try session.setCategory(.playAndRecord, mode: .default, options: options)
        try session.setActive(true, options: [])
        try? session.overrideOutputAudioPort(output == "speaker" ? .speaker : .none)
        if !headsetMic, let builtIn = session.availableInputs?.first(where: { $0.portType == .builtInMic }) {
            try? session.setPreferredInput(builtIn)
        }
        publishRoute()
    }

    /// Re-apply the input/output choice now (Settings changed it).
    func applyAudioRoute() {
        queue.async {
            guard self.running else { self.publishRoute(); return }
            do { try self.configureSession() } catch { self.publishProblem("Couldn't switch audio: \(error.localizedDescription)") }
        }
    }

    private func publishRoute() {
        let route = AVAudioSession.sharedInstance().currentRoute
        let output = route.outputs.first
        let input = route.inputs.first
        let hfp = route.outputs.contains { $0.portType == .bluetoothHFP } || route.inputs.contains { $0.portType == .bluetoothHFP }
        var text = output?.portName ?? "No output"
        if let input = input, input.portName != output?.portName { text += " · mic: \(input.portName)" }
        if hfp { text += " · call quality" }
        let outName: String = {
            switch output?.portType {
            case .builtInSpeaker?: return "iPhone speaker"
            case .builtInReceiver?: return "Earpiece"
            case nil: return "No output"
            default: return output?.portName ?? "Headphones"
            }
        }()
        let inName = input?.portType == .builtInMic ? "iPhone" : (input?.portName ?? "None")
        DispatchQueue.main.async {
            self.route = text
            self.callQuality = hfp
            self.outputName = outName
            self.inputName = inName
        }
    }

    private func diagnose(_ key: String, _ text: String) {
        let now = Date()
        if let last = lastDiagnostic[key], now.timeIntervalSince(last) < 600 { return }
        lastDiagnostic[key] = now
        let handler = onDiagnostic
        DispatchQueue.main.async { handler?(text) }
    }

    private func startEngineAndRecognition(withPreroll: Bool = false) {
        do {
            try configureSession()
        } catch {
            publishProblem("Couldn't use the microphone: \(error.localizedDescription)")
            return
        }
        if !running {
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            if format.sampleRate == 0 {
                publishProblem("The microphone isn't available right now.")
                return
            }
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
                self?.receive(buffer)
            }
            engine.prepare()
            do {
                try engine.start()
                running = true
            } catch {
                publishProblem("Couldn't start listening: \(error.localizedDescription)")
                return
            }
            startTimer()
        }
        publishProblem(nil)
        startRecognition(withPreroll: withPreroll)
        if phaseValue != .capturing { setPhase(.listening) }
    }

    private func teardown() {
        stopRecognition()
        timer?.cancel()
        timer = nil
        if running {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            running = false
        }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Runs on the audio thread: hand the audio to every recogniser and measure how loud it is.
    private func receive(_ buffer: AVAudioPCMBuffer) {
        var peak: Float = 0
        if let channel = buffer.floatChannelData?[0] {
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<n { sum += channel[i] * channel[i] }
            peak = n > 0 ? sqrt(sum / Float(n)) : 0
        }
        let copy = VoiceEngine.copy(buffer)
        sinkLock.lock()
        let current = sinks
        if let copy = copy {
            preroll.append(copy)
            prerollFrames += copy.frameLength
            let keep = AVAudioFrameCount(buffer.format.sampleRate * 0.7)
            while prerollFrames > keep, let first = preroll.first, preroll.count > 1 {
                prerollFrames -= first.frameLength
                preroll.removeFirst()
            }
        }
        sinkLock.unlock()
        for request in current { request.append(buffer) }
        let scaled = min(1, peak * 12)
        DispatchQueue.main.async { self.level = self.level * 0.6 + scaled * 0.4 }
    }

    private static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength),
              let src = buffer.floatChannelData, let dst = out.floatChannelData else { return nil }
        out.frameLength = buffer.frameLength
        let n = Int(buffer.frameLength)
        for ch in 0..<Int(buffer.format.channelCount) {
            dst[ch].update(from: src[ch], count: n)
        }
        return out
    }

    // MARK: recognisers

    private func languageCodes() -> [String] {
        let codes = settings?.listenLanguages ?? AppSettings.defaultLanguages()
        // One recogniser unless asked for two: several iPhones can't keep two live recognition tasks going, and
        // then neither hears the wake word.
        return Array((codes.isEmpty ? ["en"] : codes).prefix((settings?.listenBothLanguages ?? false) ? 2 : 1))
    }

    private func refreshSinks() {
        let requests = sessions.compactMap { $0.request }
        sinkLock.lock()
        sinks = requests
        sinkLock.unlock()
    }

    private func startRecognition(withPreroll: Bool = false) {
        if !sessions.isEmpty { return }
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            publishProblem("Speech recognition isn't allowed. Turn it on in Settings → SAINT.")
            return
        }
        var made: [Recognition] = []
        for code in languageCodes() {
            let locale = settings?.recognitionLocale(for: code) ?? Locale(identifier: code)
            guard let recognizer = SFSpeechRecognizer(locale: locale), recognizer.isAvailable else { continue }
            made.append(Recognition(code: code, locale: locale, recognizer: recognizer))
        }
        if made.isEmpty {
            publishProblem("No speech recogniser is available for your languages.")
            return
        }
        sessions = made
        for s in made { begin(s, withPreroll: withPreroll) }
        refreshSinks()
    }

    /// `withPreroll`: only for a tap (no wake word). Never for the wake-word recognisers, which would hear the
    /// last "SAINT" again and wake twice.
    private func begin(_ s: Recognition, withPreroll: Bool = false) {
        s.task?.cancel()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.addsPunctuation = false
        request.taskHint = .unspecified
        if s.recognizer.supportsOnDeviceRecognition && !s.serverFallback { request.requiresOnDeviceRecognition = true }
        request.contextualStrings = ["SAINT", "Hey SAINT", "OK SAINT"] + (settings?.customWakeWords ?? "")
            .split(whereSeparator: { ",;\n".contains($0) }).map { String($0) }
        if withPreroll {
            sinkLock.lock()
            let earlier = preroll
            sinkLock.unlock()
            for buffer in earlier { request.append(buffer) }
        }
        s.request = request
        s.transcript = ""
        s.confidence = 0
        s.segments = []
        s.wordTimes = []
        s.lastChange = Date()
        s.startedAt = Date()
        s.task = s.recognizer.recognitionTask(with: request) { [weak self, weak s] result, error in
            guard let self = self, let s = s else { return }
            self.queue.async { self.handle(result, error: error, in: s) }
        }
        refreshSinks()
    }

    private func stopRecognition() {
        let old = sessions
        sessions = []
        for s in old {
            s.task?.cancel()
            s.request?.endAudio()
            s.request = nil
            s.task = nil
        }
        refreshSinks()
        wakeSession = nil
        manual = false
        publishTranscript("")
    }

    private func restart(_ s: Recognition) {
        guard sessions.contains(where: { $0 === s }) else { return }
        s.request?.endAudio()
        begin(s)
    }

    // MARK: results

    private func handle(_ result: SFSpeechRecognitionResult?, error: Error?, in s: Recognition) {
        guard sessions.contains(where: { $0 === s }) else { return }
        if let result = result {
            let text = result.bestTranscription.formattedString
            if text != s.transcript { s.lastChange = Date() }
            let words = text.split(whereSeparator: { $0.isWhitespace }).count
            if words < s.wordTimes.count { s.wordTimes.removeLast(s.wordTimes.count - words) }
            while s.wordTimes.count < words { s.wordTimes.append(Date()) }
            s.transcript = text
            s.segments = result.bestTranscription.segments
            if result.isFinal, !s.segments.isEmpty {
                s.confidence = Double(s.segments.map { $0.confidence }.reduce(0, +)) / Double(s.segments.count)
            }
            onTranscript(s)
            if result.isFinal && phaseValue == .listening { restart(s) }
            if result.isFinal && phaseValue == .capturing && (manual || s === wakeSession) && !currentCommand().isEmpty {
                finishCommand()                     // the recogniser ended the utterance itself
            }
        }
        if error != nil && !(result?.isFinal ?? false) {
            // the task ended (a minute is the limit, or a hiccup): start a fresh one, backing off if it keeps failing
            s.failures += 1
            if s.failures >= 3 && !s.serverFallback && s.recognizer.supportsOnDeviceRecognition {
                // On-device recognition for this language isn't working (model not downloaded, Siri off…):
                // Apple's servers still can.
                s.serverFallback = true
                diagnose("ondevice-\(s.code)", "On-device speech recognition kept failing (\(error?.localizedDescription ?? "unknown error")); using Apple's servers instead.")
            }
            if s.failures == 6 {
                diagnose("failing-\(s.code)", "Speech recognition keeps failing: \(error?.localizedDescription ?? "unknown error"). Check Settings → Siri & Search and Settings → SAINT → Speech Recognition.")
            }
            let delay = min(5.0, 0.2 * Double(s.failures * s.failures))
            queue.asyncAfter(deadline: .now() + delay) { [weak self, weak s] in
                guard let self = self, let s = s, self.phaseValue == .listening || self.phaseValue == .capturing else { return }
                self.restart(s)
            }
        } else if result != nil {
            s.failures = 0
        }
    }

    /// Was there a pause before word ``index``? Final results carry timestamps; partial ones don't (they're all
    /// zero), so we also use when each word first appeared. Without this, "SAINT" said after any earlier words in
    /// the same recognition session never counted, which is why the wake word seemed not to work at all.
    private func pauseBefore(_ s: Recognition) -> (Int) -> Bool {
        let segs = s.segments
        let times = s.wordTimes
        let gap = 0.9 - 0.5 * min(1, max(0, settings?.wakeSensitivity ?? 0.5))      // 0.4 s (sensitive) … 0.9 s
        return { index in
            guard index > 0 else { return false }
            if index < segs.count {
                let previous = segs[index - 1]
                if segs[index].timestamp > 0 && segs[index].timestamp - (previous.timestamp + previous.duration) >= gap { return true }
            }
            if index < times.count && times[index].timeIntervalSince(times[index - 1]) >= gap { return true }
            return false
        }
    }

    private func onTranscript(_ s: Recognition) {
        switch phaseValue {
        case .listening:
            let variants = settings?.wakeVariants ?? WakeWord.base
            guard let match = WakeWord.find(in: s.transcript, variants: variants, pauseBefore: pauseBefore(s)) else {
                publishTranscript("")
                return
            }
            wakeSession = s
            wakeEnd = match.wakeEnd
            manual = false
            captureStarted = Date()
            lastChange = Date()
            setPhase(.capturing)
            announceWake()
            publishTranscript(match.command)
        case .capturing:
            let text: String
            if manual {
                // no wake word: take the session that has the most to say
                let best = sessions.max { $0.transcript.count < $1.transcript.count }
                text = best?.transcript ?? ""
            } else if s === wakeSession {
                text = WakeWord.command(in: s.transcript, after: wakeEnd)
            } else {
                return
            }
            if text != transcript { lastChange = Date() }
            publishTranscript(text)
        default:
            break
        }
    }

    // MARK: ending a command

    private func startTimer() {
        timer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 0.2, repeating: 0.2)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    private func tick() {
        let now = Date()
        if phaseValue == .capturing {
            let quiet = now.timeIntervalSince(lastChange)
            let endpoint = settings?.endpointSeconds ?? 1.2
            let spoken = currentCommand()
            if !spoken.isEmpty && quiet >= endpoint {
                finishCommand()
            } else if spoken.isEmpty && now.timeIntervalSince(captureStarted) > (manual ? 8 : 6) {
                // heard "SAINT" and then nothing: give up quietly
                cancelCapture()
            } else if now.timeIntervalSince(captureStarted) > 30 {
                finishCommand()
            }
        } else if phaseValue == .listening {
            for s in sessions {
                if now.timeIntervalSince(s.startedAt) > 50 && s.transcript.isEmpty {
                    restart(s)                    // recognisers only run for about a minute: refresh them while quiet
                } else if !s.transcript.isEmpty && now.timeIntervalSince(s.lastChange) > 2.0 {
                    restart(s)                    // someone talked (not to SAINT): start fresh, so the next "SAINT" is first
                }
            }
        }
    }

    private func currentCommand() -> String {
        if manual { return (sessions.max { $0.transcript.count < $1.transcript.count })?.transcript.trimmed ?? "" }
        guard let w = wakeSession else { return "" }
        return WakeWord.command(in: w.transcript, after: wakeEnd)
    }

    private func finishCommand() {
        var candidates: [(language: String, text: String, confidence: Double)] = []
        if manual {
            for s in sessions { candidates.append((s.code, s.transcript, s.confidence)) }
        } else {
            let variants = settings?.wakeVariants ?? WakeWord.base
            for s in sessions {
                if s === wakeSession {
                    candidates.append((s.code, WakeWord.command(in: s.transcript, after: wakeEnd), s.confidence))
                } else if let m = WakeWord.find(in: s.transcript, variants: variants, pauseBefore: pauseBefore(s)) {
                    candidates.append((s.code, m.command, s.confidence))
                }
            }
        }
        guard let best = WakeWord.best(candidates) else {
            cancelCapture()
            return
        }
        stopRecognition()
        setPhase(.thinking)
        let handler = onCommand
        DispatchQueue.main.async { handler?(best.text, best.language) }
    }

    private func cancelCapture() {
        stopRecognition()
        manual = false
        if wantsListening {
            startRecognition()
            setPhase(.listening)
        }
    }

    // MARK: feedback

    private func announceWake() {
        DispatchQueue.main.async {
            if self.settings?.chime ?? true { AudioServicesPlaySystemSound(1113) }
            if self.settings?.haptics ?? true { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
        }
    }

    private func setPhase(_ new: Phase) {
        phaseValue = new
        DispatchQueue.main.async { self.phase = new }
    }

    private func publishTranscript(_ text: String) {
        DispatchQueue.main.async { if self.transcript != text { self.transcript = text } }
    }

    private func publishProblem(_ text: String?) {
        DispatchQueue.main.async { self.problem = text }
    }

    // MARK: interruptions (calls, Siri, headphones)

    private func addObserversIfNeeded() {
        if observersAdded { return }
        observersAdded = true
        let center = NotificationCenter.default
        center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { [weak self] note in
            guard let self = self,
                  let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            self.queue.async {
                if type == .began {
                    self.running = false
                    self.stopRecognition()
                    self.setPhase(.off)
                } else if self.wantsListening {
                    // a call or Siri finished: come back after a moment
                    self.queue.asyncAfter(deadline: .now() + 0.8) { self.startEngineAndRecognition() }
                }
            }
        }
        center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: nil) { [weak self] _ in
            self?.publishRoute()                    // AirPods in / out: show where the sound goes now
        }
        center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            guard let self = self else { return }
            self.queue.async {
                guard self.wantsListening else { return }
                // headphones plugged in, Bluetooth connected…: rebuild the audio graph
                self.stopRecognition()
                if self.running {
                    self.engine.inputNode.removeTap(onBus: 0)
                    self.engine.stop()
                    self.running = false
                }
                self.startEngineAndRecognition()
            }
        }
        center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: nil) { [weak self] _ in
            self?.queue.async {
                guard let self = self, self.wantsListening else { return }
                self.running = false
                self.sessions = []
                self.startEngineAndRecognition()
            }
        }
    }
}
