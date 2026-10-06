import SwiftUI
import SaintCore

/// The Talk tab: the conversation with SAINT, a mini player, and the message box with SAINT's button —
/// tap it to talk, tap again to stop. Drag down anywhere (or tap the conversation) to put the keyboard away.
struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var voice: VoiceEngine
    @EnvironmentObject var speaker: Speaker
    @EnvironmentObject var spotify: SpotifyService

    @State private var draft = ""
    @FocusState private var typing: Bool

    private var stateColor: Color {
        if model.thinking { return Theme.thinking }
        if speaker.isSpeaking { return Theme.speaking }
        switch voice.phase {
        case .off: return voice.problem == nil ? Theme.idle : Theme.error
        case .listening: return Theme.ready
        case .capturing: return Theme.listening
        case .thinking: return Theme.thinking
        case .speaking: return Theme.speaking
        }
    }

    private var statusText: String {
        if model.thinking { return "Thinking…" }
        if speaker.isSpeaking { return "Speaking" }
        if voice.problem != nil && voice.phase == .off { return "Can't listen — see below" }
        switch voice.phase {
        case .off: return model.permissionsOK ? "Not listening · tap the SAINT button" : "Needs microphone access"
        case .listening: return "Ready · say “SAINT”"
        case .capturing: return "Listening…"
        case .thinking: return "Thinking…"
        case .speaking: return "Speaking"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            conversation
            dock
        }
        .saintBackground()
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 10) {
            SaintLogo(size: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text("SAINT").font(.system(size: 19, weight: .bold)).foregroundStyle(Theme.text)
                HStack(spacing: 6) {
                    Circle().fill(stateColor).frame(width: 7, height: 7)
                    Text(statusText).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.muted)
                        .contentTransition(.opacity)
                        .animation(.easeInOut(duration: 0.2), value: statusText)
                }
            }
            Spacer()
            Button { model.showDevices = true } label: { pcChip }.buttonStyle(.plain)
        }
        .padding(.horizontal, 16)
        .padding(.top, 6)
        .padding(.bottom, 8)
    }

    private var pcChip: some View {
        let pc = model.brain.ownPC()
        return HStack(spacing: 6) {
            Circle().fill(pc?.online == true ? Theme.success : Theme.faint).frame(width: 7, height: 7)
            Text(pc == nil ? "No PC" : (pc!.online ? "Desktop" : "Desktop offline"))
                .font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.text)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .glassCard(radius: 99, fill: Theme.surface2)
        .accessibilityLabel(pc?.online == true ? "Desktop connected" : "Desktop not connected")
    }

    // MARK: conversation

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 12) {
                    if model.messages.isEmpty { hints }
                    ForEach(model.messages) { message in
                        MessageRow(message: message)
                            .id(message.id)
                            .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
                    }
                    if model.thinking {
                        TypingRow().id("typing")
                    }
                    Color.clear.frame(height: 4).id("bottom")
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .animation(.spring(response: 0.35, dampingFraction: 0.85), value: model.messages.count)
            }
            .scrollDismissesKeyboard(.interactively)
            .dismissKeyboardOnDragDown()
            .onTapGesture { typing = false }
            .onChange(of: model.messages.count) { _, _ in
                withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onChange(of: model.thinking) { _, _ in
                withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onChange(of: typing) { _, focused in
                if focused { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
            }
        }
    }

    private var hints: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Try saying")
            ForEach(["“SAINT, play Blinding Lights by The Weeknd”", "“SAINT, take a picture”",
                     "“SAINT, text Mom I'm on my way”", "“SAINT, remind me in 20 minutes”",
                     "“SAINT, lock my PC”"], id: \.self) { example in
                Text(example)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .glassCard(radius: 14)
            }
        }
        .padding(.top, 8)
    }

    // MARK: dock: mini player + input

    private var dock: some View {
        VStack(spacing: 10) {
            if let problem = voice.problem {
                Label(problem, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if !model.permissionsOK {
                Button { Task { await model.requestAllPermissions() } } label: {
                    Text("Allow microphone & speech").font(.system(size: 14, weight: .semibold)).frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Theme.accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .foregroundStyle(Theme.onAccent)
                }
            }
            if spotify.connected, let now = spotify.nowPlaying {
                MiniPlayer(now: now)
            }
            inputBar
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 8)
        .background(Theme.bg)
    }

    private var inputBar: some View {
        HStack(spacing: 10) {
            HStack {
                TextField("", text: $draft, prompt: Text("Message SAINT…").foregroundStyle(Theme.faint), axis: .vertical)
                    .lineLimit(1...4)
                    .focused($typing)
                    .submitLabel(.send)
                    .onSubmit(send)
                    // A vertical TextField puts a newline in the text instead of calling onSubmit, so the
                    // keyboard's Send key never sent anything: treat the newline as Send.
                    .onChange(of: draft) { _, new in
                        if new.contains("\n") {
                            submitText(new.replacingOccurrences(of: "\n", with: " "))
                        }
                    }
                    .foregroundStyle(Theme.text)
                if !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Button(action: send) {
                        Image(systemName: "arrow.up.circle.fill").font(.system(size: 26)).foregroundStyle(Theme.accent)
                    }
                    .accessibilityLabel("Send")
                    .transition(.scale.combined(with: .opacity))
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .glassCard(radius: 22, fill: Theme.surface2)
            .animation(.easeOut(duration: 0.15), value: draft.isEmpty)

            Button {
                typing = false
                model.listenOnce()
            } label: {
                OrbView(phase: model.thinking ? .thinking : voice.phase, level: voice.level, speaking: speaker.isSpeaking,
                        error: voice.problem != nil, compact: true)
                    .frame(width: 52, height: 52)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(voice.phase == .capturing ? "Stop listening" : "Talk to SAINT")
            .accessibilityHint("Tap to talk, tap again to stop")
            .contextMenu {
                Button { model.setAlwaysListening(!settings.alwaysListening) } label: {
                    Label(settings.alwaysListening ? "Stop listening for “SAINT”" : "Listen for “SAINT”", systemImage: "ear")
                }
                Button { speaker.stop() } label: { Label("Stop talking", systemImage: "speaker.slash") }
                Button { model.messages.removeAll() } label: { Label("Clear the conversation", systemImage: "trash") }
            }
        }
    }

    private func send() {
        submitText(draft)
    }

    private func submitText(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        draft = ""
        guard !text.isEmpty else { return }
        typing = false
        Task { await model.submit(text) }
    }
}

// MARK: messages

struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        if message.role == .you {
            HStack {
                Spacer(minLength: 56)
                Text(message.text)
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: 18, bottomTrailingRadius: 6,
                                                       topTrailingRadius: 18, style: .continuous).fill(Theme.accentSoft))
                    .overlay(UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: 18, bottomTrailingRadius: 6,
                                                    topTrailingRadius: 18, style: .continuous).strokeBorder(Theme.accentLine))
                    .textSelection(.enabled)
            }
        } else {
            HStack(alignment: .top, spacing: 8) {
                SaintLogo(size: 24).padding(.top, 4)
                VStack(alignment: .leading, spacing: 8) {
                    Text(message.text)
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.text)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 11)
                        .background(UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 18, bottomTrailingRadius: 18,
                                                           topTrailingRadius: 18, style: .continuous)
                            .fill(message.ok ? Theme.surface : Theme.dangerSoft))
                        .overlay(UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 18, bottomTrailingRadius: 18,
                                                        topTrailingRadius: 18, style: .continuous).strokeBorder(Theme.border))
                        .textSelection(.enabled)
                    if let chip = chip { chip }
                }
                Spacer(minLength: 40)
            }
        }
    }

    /// What SAINT did, under its answer: "Spotify · done", "Desktop", "iPhone · failed".
    private var chip: ActionChip? {
        guard message.role == .saint, !message.kind.isEmpty, message.kind != "chat" else {
            if message.source == "pc" { return ActionChip(icon: "laptopcomputer", label: "Answered by your PC", tint: Theme.info, fill: Theme.infoSoft) }
            if message.source == "model" { return ActionChip(icon: "sparkles", label: "AI model", tint: Theme.muted, fill: Theme.surface2) }
            return nil
        }
        let names = ["music": ("music.note", "Spotify"), "phone": ("iphone", "iPhone"), "reminder": ("bell", "Reminders"),
                     "memory": ("brain", "Memory"), "pc": ("laptopcomputer", "Desktop"), "skill": ("bolt", "Routine")]
        let (icon, name) = names[message.kind] ?? ("checkmark", message.kind.capitalized)
        if !message.ok { return ActionChip(icon: "xmark", label: "\(name) · didn't work", tint: Theme.danger, fill: Theme.dangerSoft) }
        if message.kind == "pc" { return ActionChip(icon: icon, label: "Done on your PC", tint: Theme.info, fill: Theme.infoSoft) }
        return ActionChip(icon: "checkmark", label: name, tint: Theme.success, fill: Theme.successSoft)
    }
}

struct ActionChip: View {
    let icon: String
    let label: String
    let tint: Color
    let fill: Color
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: icon).font(.system(size: 11, weight: .semibold))
            Text(label).font(.system(size: 12, weight: .medium))
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(fill, in: Capsule())
    }
}

/// "…" while SAINT works on an answer.
struct TypingRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            SaintLogo(size: 24).padding(.top, 4)
            TimelineView(.animation(minimumInterval: 1.0 / 20.0, paused: reduceMotion)) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                HStack(spacing: 5) {
                    ForEach(0..<3, id: \.self) { i in
                        Circle().fill(Theme.thinking)
                            .frame(width: 7, height: 7)
                            .opacity(reduceMotion ? 0.8 : 0.35 + 0.65 * max(0, sin(t * 5 - Double(i) * 0.7)))
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .glassCard(radius: 18)
            }
            Spacer()
        }
        .accessibilityLabel("SAINT is thinking")
    }
}

/// The mini player in the Talk tab: tap for Music, swipe for next / previous.
struct MiniPlayer: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var settings: AppSettings
    let now: SpotifyService.NowPlayingInfo
    @State private var dragX: CGFloat = 0

    var body: some View {
        HStack(spacing: 10) {
            AsyncImage(url: now.artworkURL) { image in image.resizable().scaledToFill() } placeholder: {
                LinearGradient(colors: [Theme.speaking, Theme.accentSoft], startPoint: .topLeading, endPoint: .bottomTrailing)
            }
            .frame(width: 40, height: 40)
            .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(now.title).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                Text(now.artist + (now.device.isEmpty ? "" : " · \(now.device)")).font(.system(size: 12)).foregroundStyle(Theme.muted).lineLimit(1)
            }
            Spacer(minLength: 6)
            Button { Task { _ = await spotify.perform(now.isPlaying ? .pause : .resume); await spotify.refreshNow() } } label: {
                Image(systemName: now.isPlaying ? "pause.fill" : "play.fill").font(.system(size: 18)).foregroundStyle(Theme.text)
                    .frame(width: 34, height: 34)
            }
            .accessibilityLabel(now.isPlaying ? "Pause" : "Play")
            Button { skip(.skip) } label: {
                Image(systemName: "forward.end.fill").font(.system(size: 16)).foregroundStyle(Theme.text).frame(width: 30, height: 34)
            }
            .accessibilityLabel("Next song")
        }
        .padding(8)
        .glassCard(radius: 16)
        .offset(x: dragX)
        .contentShape(Rectangle())
        .onTapGesture { model.tab = .music }
        .gesture(DragGesture(minimumDistance: 16)
            .onChanged { dragX = $0.translation.width * 0.4 }
            .onEnded { value in
                withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) { dragX = 0 }
                if abs(value.translation.width) > 70 { skip(value.translation.width < 0 ? .skip : .previous) }
            })
    }

    private func skip(_ intent: MusicIntent) {
        if settings.haptics { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
        Task {
            _ = await spotify.perform(intent)
            try? await Task.sleep(nanoseconds: 500_000_000)
            await spotify.refreshNow()
        }
    }
}
