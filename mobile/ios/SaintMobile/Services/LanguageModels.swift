import Foundation
import SaintCore
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Where SAINT's free-form answers come from when neither a command nor your PC covers them:
///   1. your PC's SAINT (handled by the brain: it forwards unknown things there first, when connected)
///   2. Apple's on-device model (iOS 26 with Apple Intelligence), private and offline
///   3. Claude over the internet, if you turn it on and add an API key (Settings)
enum OnDeviceModel {
    static var isAvailable: Bool {
        #if canImport(FoundationModels)
        if #available(iOS 26.0, *) {
            if case .available = SystemLanguageModel.default.availability { return true }
        }
        #endif
        return false
    }
}

final class AppleOnDeviceModel: LanguageModel {
    func respond(system: String, prompt: String) async throws -> String {
        #if canImport(FoundationModels)
        if #available(iOS 26.0, *) {
            let session = LanguageModelSession(instructions: system)
            let response = try await session.respond(to: prompt)
            return response.content
        }
        #endif
        throw LinkError.unreachable("The on-device model isn't available on this phone.")
    }
}

final class ClaudeModel: LanguageModel {
    private let apiKey: () -> String
    private let model: () -> String

    init(apiKey: @escaping () -> String, model: @escaping () -> String) {
        self.apiKey = apiKey
        self.model = model
    }

    func respond(system: String, prompt: String) async throws -> String {
        let key = apiKey()
        guard !key.isEmpty else { throw LinkError.unreachable("No Claude API key.") }
        var request = URLRequest(url: URL(string: "https://api.anthropic.com/v1/messages")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.setValue(key, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        let body: [String: Any] = [
            "model": model(),
            "max_tokens": 800,
            "system": system,
            "messages": [["role": "user", "content": prompt]],
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200, let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw LinkError.remote(code: "claude", message: status == 401 ? "Claude didn't accept that API key." : "Claude couldn't answer (\(status)).")
        }
        let blocks = (json["content"] as? [[String: Any]]) ?? []
        let text = blocks.compactMap { $0["text"] as? String }.joined(separator: " ")
        if text.isEmpty { throw LinkError.remote(code: "claude", message: "Claude sent back nothing.") }
        return text
    }
}

/// Tries each available model in turn.
final class ModelChain: LanguageModel {
    private let providers: () -> [LanguageModel]

    init(providers: @escaping () -> [LanguageModel]) {
        self.providers = providers
    }

    func respond(system: String, prompt: String) async throws -> String {
        var lastError: Error = LinkError.unreachable("No language model is available.")
        for provider in providers() {
            do { return try await provider.respond(system: system, prompt: prompt) } catch { lastError = error }
        }
        throw lastError
    }
}
