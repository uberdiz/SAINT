import SwiftUI
import SaintCore

struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var voice: VoiceEngine
    @EnvironmentObject var speaker: Speaker

    @State private var draft = ""
    @State private var showSettings = false
    @FocusState private var typing: Bool

    private var statusText: String {
        if model.thinking { return "Thinking…" }
        if speaker.isSpeaking { return "Speaking" }
        switch voice.phase {
        case .off: return model.permissionsOK ? "Not listening — tap the orb" : "Needs microphone access"
        case .listening: return "Listening for “SAINT”"
        case .capturing: return "Go ahead…"
        case .thinking: return "Thinking…"
        case .speaking: return "Speaking"
        }
    }

    private var ownPC: PeerInfo? { model.brain.ownPC() }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header
                conversation
                inputBar
            }
            .saintBackground()
            .navigationTitle("SAINT")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarLeading) { connectionPill }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .overlay(alignment: .top) { bannerView }
        }
    }

    // MARK: pieces

    private var connectionPill: some View {
        Group {
            if let pc = ownPC {
                Pill(text: pc.name, icon: pc.online ? "desktopcomputer" : "desktopcomputer.trianglebadge.exclamationmark",
                     tint: pc.online ? .green : .secondary)
            } else {
                Pill(text: "No PC linked", icon: "link", tint: .secondary)
            }
        }
    }

    private var header: some View {
        VStack(spacing: 6) {
            Button { model.listenOnce() } label: {
                OrbView(phase: model.thinking ? .thinking : voice.phase, level: voice.level, speaking: speaker.isSpeaking)
                    .frame(height: 220)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Talk to SAINT")
            Text(statusText)
                .font(.system(.headline, design: .rounded))
                .foregroundStyle(.secondary)
                .animation(.default, value: statusText)
            Text(voice.transcript.isEmpty ? " " : voice.transcript)
                .font(.system(.title3, design: .rounded, weight: .medium))
                .multilineTextAlignment(.center)
                .lineLimit(3)
                .padding(.horizontal, 24)
                .frame(minHeight: 56)
            if let problem = voice.problem {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 24)
            }
            if !model.permissionsOK {
                Button("Allow microphone & speech") { Task { await model.requestAllPermissions() } }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(.top, 4)
    }

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 10) {
                    if model.messages.isEmpty { hints }
                    ForEach(model.messages) { message in
                        MessageRow(message: message).id(message.id)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: model.messages.count) { _, _ in
                if let last = model.messages.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    private var hints: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Try saying")
            ForEach(["“SAINT, remind me at 5 to call mom”", "“SAINT, pon música de Bad Bunny”",
                     "“SAINT, lock my PC”", "“SAINT, send this prompt to Gian's PC on Claude…”"], id: \.self) { example in
                Text(example)
                    .font(.system(.subheadline, design: .rounded))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .glassCard(radius: 16)
            }
        }
        .padding(.top, 8)
    }

    private var inputBar: some View {
        HStack(spacing: 10) {
            TextField("Type to SAINT…", text: $draft, axis: .vertical)
                .lineLimit(1...3)
                .focused($typing)
                .submitLabel(.send)
                .onSubmit(send)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .glassCard(radius: 22)
            Button(action: send) {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 32))
            }
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel("Send")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var bannerView: some View {
        if let text = model.banner {
            Text(text)
                .font(.system(.subheadline, design: .rounded, weight: .medium))
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .glassCard(radius: 20)
                .padding(.top, 6)
                .transition(.move(edge: .top).combined(with: .opacity))
                .task(id: text) {
                    try? await Task.sleep(nanoseconds: 3_500_000_000)
                    withAnimation { model.banner = nil }
                }
        }
    }

    private func send() {
        let text = draft
        draft = ""
        Task { await model.submit(text) }
    }
}

struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .you { Spacer(minLength: 48) }
            VStack(alignment: message.role == .you ? .trailing : .leading, spacing: 4) {
                Text(message.text)
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(message.role == .you ? Color.white : Color.primary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(background)
                    .textSelection(.enabled)
                if message.role == .saint, message.source != "phone" {
                    Pill(text: message.source == "pc" ? "Answered by your PC" : "AI model",
                         icon: message.source == "pc" ? "desktopcomputer" : "sparkles", tint: .secondary)
                }
            }
            if message.role != .you { Spacer(minLength: 48) }
        }
        .frame(maxWidth: .infinity, alignment: message.role == .you ? .trailing : .leading)
    }

    @ViewBuilder
    private var background: some View {
        if message.role == .you {
            RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.accent.gradient)
        } else {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(message.ok ? Color(.secondarySystemBackground) : Color.orange.opacity(0.18))
        }
    }
}
