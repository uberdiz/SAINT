import SwiftUI

/// Voice mode: full screen while SAINT listens for a command. The orb follows your voice, the words appear as
/// you say them, and the audio route (iPhone mic, AirPods…) is shown and can be changed. Tap the orb (or ✕) to
/// stop. It closes by itself once the command is sent, so it can never leave you stuck.
struct ListeningView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var voice: VoiceEngine
    @EnvironmentObject var speaker: Speaker
    @EnvironmentObject var settings: AppSettings
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showRoutes = false

    private var title: String {
        if model.thinking { return "Thinking" }
        switch voice.phase {
        case .capturing: return "Listening"
        case .thinking: return "Thinking"
        case .speaking: return "Speaking"
        case .listening: return "Say “SAINT”"
        case .off: return voice.problem == nil ? "Not listening" : "Can't listen"
        }
    }

    var body: some View {
        ZStack {
            RadialGradient(colors: [Theme.listening.opacity(0.14), Theme.bg], center: .center, startRadius: 10, endRadius: 420)
                .ignoresSafeArea()
            VStack(spacing: 18) {
                HStack {
                    Button { close() } label: {
                        Image(systemName: "xmark").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                            .frame(width: 36, height: 36).background(Theme.surface2, in: Circle())
                    }
                    .accessibilityLabel("Close")
                    Spacer()
                    Button { showRoutes = true } label: {
                        HStack(spacing: 6) {
                            Image(systemName: voice.callQuality ? "phone.fill" : "headphones").foregroundStyle(Theme.accent)
                            Text(voice.outputName).lineLimit(1)
                            Image(systemName: "chevron.down").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.muted)
                        }
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(Theme.text)
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .glassCard(radius: 99, fill: Theme.surface2)
                    }
                    .accessibilityLabel("Audio output: \(voice.outputName). Change.")
                }
                .padding(.horizontal, 16)

                Button { model.listenOnce() } label: {
                    OrbView(phase: model.thinking ? .thinking : voice.phase, level: voice.level, speaking: speaker.isSpeaking,
                            error: voice.problem != nil)
                        .frame(width: 270, height: 270)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(voice.phase == .capturing ? "Stop listening" : "Start listening")

                Text(title)
                    .font(.system(size: 28, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    .contentTransition(.opacity)
                    .animation(.easeInOut(duration: 0.2), value: title)
                Text(voice.phase == .capturing ? "Tap the icon to stop · pause to send" : "Tap the icon to talk")
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.muted)

                Text(voice.transcript.isEmpty ? "…" : "“\(voice.transcript)”")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundStyle(voice.transcript.isEmpty ? Theme.faint : Theme.text)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
                    .padding(16)
                    .glassCard(radius: 20)
                    .padding(.horizontal, 16)
                    .animation(.easeOut(duration: 0.15), value: voice.transcript)

                LevelBars(level: voice.level, active: voice.phase == .capturing && !reduceMotion)
                    .frame(height: 40)

                HStack(spacing: 8) {
                    Pill(text: "Mic: \(voice.inputName)", icon: "mic")
                    Pill(text: voice.callQuality ? "Call quality" : "Full quality", icon: voice.callQuality ? "phone" : "checkmark",
                         tint: voice.callQuality ? Theme.accent : Theme.success)
                }

                if let problem = voice.problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.danger)
                        .padding(.horizontal, 24).multilineTextAlignment(.center)
                }
                Spacer()
                StateLegend()
                    .padding(.bottom, 8)
            }
            .padding(.top, 8)
        }
        .confirmationDialog("Sound output", isPresented: $showRoutes, titleVisibility: .visible) {
            Button("Headphones (when connected)") { settings.audioOutput = "auto" }
            Button("iPhone speaker") { settings.audioOutput = "speaker" }
            Button("Earpiece (private)") { settings.audioOutput = "earpiece" }
        }
        .onChange(of: voice.phase) { old, new in
            // The command went to SAINT, or listening ended: back to the conversation.
            if new == .thinking || (old == .capturing && (new == .listening || new == .off)) { close() }
        }
        .onChange(of: model.thinking) { _, thinking in if thinking { close() } }
    }

    private func close() {
        if voice.phase == .capturing { voice.stopCapture() }
        model.showListening = false
    }
}

/// The sound level as moving bars (teal, like the desktop's listening colour).
struct LevelBars: View {
    let level: Float
    let active: Bool
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24.0, paused: !active)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 4) {
                ForEach(0..<24, id: \.self) { i in
                    let wave = active ? (0.35 + 0.65 * abs(sin(t * 6 + Double(i) * 0.55))) : 0.2
                    let h = max(4, 40 * CGFloat(wave) * CGFloat(0.25 + Double(level) * 1.6).clamped(0.15, 1))
                    RoundedRectangle(cornerRadius: 2).fill(Theme.listening.opacity(0.4 + 0.6 * wave)).frame(width: 4, height: h)
                }
            }
        }
        .accessibilityHidden(true)
    }
}

struct StateLegend: View {
    var body: some View {
        HStack(spacing: 12) {
            ForEach([("Ready", Theme.ready), ("Listening", Theme.listening), ("Thinking", Theme.thinking),
                     ("Speaking", Theme.speaking), ("Error", Theme.error)], id: \.0) { item in
                HStack(spacing: 5) {
                    Circle().fill(item.1).frame(width: 7, height: 7)
                    Text(item.0).font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.muted)
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .glassCard(radius: 14)
        .accessibilityElement(children: .combine)
    }
}

extension CGFloat {
    func clamped(_ lo: CGFloat, _ hi: CGFloat) -> CGFloat { Swift.min(hi, Swift.max(lo, self)) }
}
