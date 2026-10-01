import AppIntents
import SaintCore

/// Siri and Shortcuts can't be given a new wake phrase, but they can reach SAINT:
///   "Hey Siri, ask SAINT" → SAINT asks what you want, answers, and says it back.
///   Action button / Lock Screen control / Back Tap → "Start listening".
struct AskSaintIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask SAINT"
    static var description = IntentDescription("Ask SAINT anything, or tell it to do something, in any language it knows.")

    @Parameter(title: "What", requestValueDialog: "What would you like SAINT to do?")
    var request: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask SAINT \(\.$request)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let reply = await AppModel.shared.brain.handle(request)
        AppModel.shared.link.shareTurn(user: request, reply: reply.text)
        return .result(value: reply.text, dialog: IntentDialog(stringLiteral: reply.text.isEmpty ? "Done." : reply.text))
    }
}

struct StartListeningIntent: AppIntent {
    static var title: LocalizedStringResource = "Start listening"
    static var description = IntentDescription("Open SAINT and listen for one command right away.")
    static var openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        AppModel.shared.listenOnce()
        return .result()
    }
}

struct SaintShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskSaintIntent(), phrases: [
            "Ask \(.applicationName)",
            "Hey \(.applicationName)",
            "Tell \(.applicationName) something",
        ], shortTitle: "Ask SAINT", systemImageName: "waveform.circle.fill")
        AppShortcut(intent: StartListeningIntent(), phrases: [
            "Start listening with \(.applicationName)",
            "Open \(.applicationName) and listen",
        ], shortTitle: "Start listening", systemImageName: "mic.circle.fill")
    }
}
