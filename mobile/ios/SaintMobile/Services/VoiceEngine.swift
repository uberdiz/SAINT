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
            self.startEngineAndRecognition()
            self.manual = true
            self.wakeSession = nil
            self.wakeEnd = 0
            self.captureStarted = Date()
            self.lastChange = Date()
            self.setPhase(.capturing)
            self.publishTranscript("")
        }
    }

    // MARK: audio engine

    private func configureSession(speaking: Bool = false) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default,
                                options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP, .mixWithOthers])
        try session.setActive(true, options: [])
    }

    private func startEngineAndRecognition() {
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
        startRecognition()
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
        sinkLock.lock()
        let current = sinks
        sinkLock.unlock()
        for request in current { request.append(buffer) }
        let scaled = min(1, peak * 12)
        DispatchQueue.main.async { self.level = self.level * 0.6 + scaled * 0.4 }
    }

    // MARK: recognisers

    private func languageCodes() -> [String] {
        let codes = settings?.listenLanguages ?? AppSettings.defaultLanguages()
        return Array((codes.isEmpty ? ["en"] : codes).prefix(2))
    }

    private func refreshSinks() {
        let requests = sessions.compactMap { $0.request }
        sinkLock.lock()
        sinks = requests
        sinkLock.unlock()
    }

    private func startRecognition() {
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
        for s in made { begin(s) }
        refreshSinks()
    }

    private func begin(_ s: Recognition) {
        s.task?.cancel()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.addsPunctuation = false
        request.taskHint = .unspecified
        if s.recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        request.contextualStrings = ["SAINT", "Hey SAINT", "OK SAINT"] + (settings?.customWakeWords ?? "")
            .split(whereSeparator: { ",;\n".contains($0) }).map { String($0) }
        s.request = request
        s.transcript = ""
        s.confidence = 0
        s.segments = []
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
            s.transcript = result.bestTranscription.formattedString
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
            let delay = min(5.0, 0.2 * Double(s.failures * s.failures))
            queue.asyncAfter(deadline: .now() + delay) { [weak self, weak s] in
                guard let self = self, let s = s, self.phaseValue == .listening || self.phaseValue == .capturing else { return }
                self.restart(s)
            }
        } else if result != nil {
            s.failures = 0
        }
    }

    private func pauseBefore(_ s: Recognition) -> (Int) -> Bool {
        let segs = s.segments
        return { index in
            guard index > 0, index < segs.count else { return false }
            let previous = segs[index - 1]
            return segs[index].timestamp - (previous.timestamp + previous.duration) >= 0.6
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
            // recognisers only run for about a minute: refresh them while nothing is being said
            for s in sessions where now.timeIntervalSince(s.startedAt) > 50 && s.transcript.isEmpty { restart(s) }
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
