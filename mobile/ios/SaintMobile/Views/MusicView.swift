import SwiftUI
import SaintCore

struct MusicView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var settings: AppSettings
    @State private var query = ""
    @State private var status = ""
    @State private var busy = false
    @State private var now: SpotifyService.NowPlayingInfo?
    @State private var dragX: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if spotify.connected { nowPlayingCard; player } else { connectCard }
                    if let error = spotify.lastError {
                        Label(error, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
                    }
                }
                .padding(16)
            }
            .refreshable { await refreshNow() }                 // pull down: what's playing now
            .saintBackground()
            .navigationTitle("Music")
            .task(id: spotify.connected) {
                // Keep the card current while this screen is open (Spotify has no push for the Web API).
                while !Task.isCancelled && spotify.connected {
                    await refreshNow()
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                }
            }
        }
    }

    private func refreshNow() async {
        let info = await spotify.fetchNowPlaying()
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.3)) { now = info }
    }

    // MARK: now playing

    private var nowPlayingCard: some View {
        VStack(spacing: 14) {
            artwork
                .offset(x: dragX)
                .rotationEffect(.degrees(reduceMotion ? 0 : Double(dragX) / 40))
                .gesture(DragGesture(minimumDistance: 20)
                    .onChanged { dragX = $0.translation.width * 0.6 }
                    .onEnded { value in
                        let dx = value.translation.width
                        withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.8)) { dragX = 0 }
                        if abs(dx) > 80 { Task { await skip(dx < 0 ? .skip : .previous) } }   // swipe: next / previous
                    })
                .contextMenu {                                                       // long press
                    Button { Task { await run(.like) } } label: { Label("Save to Liked Songs", systemImage: "heart") }
                    Button { Task { await run(.shuffle(true)) } } label: { Label("Shuffle on", systemImage: "shuffle") }
                    Button { Task { await run(.restart) } } label: { Label("Restart song", systemImage: "backward.end") }
                    Button { Task { await run(.playSomethingILike) } } label: { Label("Something I'd like", systemImage: "sparkles") }
                }
                .accessibilityLabel(now.map { "\($0.title) by \($0.artist)" } ?? "Nothing playing")
                .accessibilityHint("Swipe left for the next song, right for the previous one")
            VStack(spacing: 4) {
                Text(now?.title ?? "Nothing playing")
                    .font(.system(.title2, design: .rounded, weight: .bold)).lineLimit(1)
                Text(now.map { [$0.artist, $0.album].filter { !$0.isEmpty }.joined(separator: " · ") } ?? "Say “SAINT, play something”")
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            .contentTransition(.opacity)
            if let now = now, now.durationMs > 0 {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    VStack(spacing: 4) {
                        ProgressView(value: now.position(at: context.date), total: Double(now.durationMs))
                            .tint(Theme.accent)
                        HStack {
                            Text(clock(now.position(at: context.date)))
                            Spacer()
                            if !now.device.isEmpty { Label(now.device, systemImage: "hifispeaker").lineLimit(1) }
                            Spacer()
                            Text(clock(Double(now.durationMs)))
                        }
                        .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .glassCard(radius: 28)
    }

    private var artwork: some View {
        AsyncImage(url: now?.artworkURL) { image in
            image.resizable().scaledToFill()
        } placeholder: {
            ZStack {
                LinearGradient(colors: [Theme.accent.opacity(0.6), Theme.thinking.opacity(0.5)], startPoint: .topLeading,
                               endPoint: .bottomTrailing)
                Image(systemName: "music.note").font(.system(size: 48, weight: .semibold)).foregroundStyle(.white.opacity(0.8))
            }
        }
        .frame(width: 240, height: 240)
        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        .shadow(color: .black.opacity(0.25), radius: 18, y: 10)
        .scaleEffect(now?.isPlaying == false ? 0.92 : 1)
        .animation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.75), value: now?.isPlaying)
    }

    private func clock(_ ms: Double) -> String {
        let s = Int(ms / 1000)
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    private func skip(_ intent: MusicIntent) async {
        if settings.haptics { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
        await run(intent)
    }

    private var connectCard: some View {
        VStack(spacing: 14) {
            Image(systemName: "music.note.house.fill").font(.system(size: 44)).foregroundStyle(.green)
            Text("Connect Spotify").font(.system(.title2, design: .rounded, weight: .bold))
            Text("Then just say “SAINT, play Bad Bunny”, “SAINT, pausa” or “SAINT, skip”. Playback control needs Spotify Premium.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            if settings.spotifyClientID.trimmingCharacters(in: .whitespaces).isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("1. Make a free app at developer.spotify.com/dashboard").font(.footnote)
                    Text("2. Add this redirect URI to it: \(SpotifyService.redirectURI)").font(.footnote).textSelection(.enabled)
                    Text("3. Paste its Client ID here (or in Settings)").font(.footnote)
                    TextField("Client ID", text: $settings.spotifyClientID)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textFieldStyle(.roundedBorder)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Button {
                Task { await spotify.connect() }
            } label: {
                Text("Sign in with Spotify").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.green)
            .disabled(settings.spotifyClientID.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .padding(20)
        .glassCard(radius: 28)
    }

    private var player: some View {
        VStack(spacing: 16) {
            VStack(spacing: 12) {
                Text(spotify.accountName.isEmpty ? "Spotify" : spotify.accountName)
                    .font(.system(.headline, design: .rounded)).foregroundStyle(.secondary)
                Text(status.isEmpty ? "Ready" : status)
                    .font(.system(.title3, design: .rounded, weight: .semibold))
                    .multilineTextAlignment(.center)
                    .frame(minHeight: 56)
                HStack(spacing: 30) {
                    control("backward.fill", .previous)
                    control("playpause.fill", .resume, alternate: .pause)
                    control("forward.fill", .skip)
                }
                .font(.system(size: 28))
                HStack(spacing: 12) {
                    Button { Task { await run(.volumeDown) } } label: { Image(systemName: "speaker.minus") }
                    Button { Task { await run(.volumeUp) } } label: { Image(systemName: "speaker.plus") }
                    Button { Task { await run(.shuffle(true)) } } label: { Image(systemName: "shuffle") }
                    Button { Task { await run(.like) } } label: { Image(systemName: "heart") }
                    Button { Task { await run(.nowPlaying) } } label: { Image(systemName: "info.circle") }
                }
                .buttonStyle(.bordered)
            }
            .padding(20)
            .frame(maxWidth: .infinity)
            .glassCard(radius: 28)

            HStack {
                TextField("Play…", text: $query).submitLabel(.go).onSubmit(play)
                Button("Play", action: play).buttonStyle(.borderedProminent).disabled(query.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(12)
            .glassCard(radius: 18)

            VStack(alignment: .leading, spacing: 10) {
                SectionTitle("Quick picks")
                FlowButtons(items: [("Something I'd like", MusicIntent.playSomethingILike), ("Shuffle on", .shuffle(true)),
                                    ("Shuffle off", .shuffle(false)), ("Restart song", .restart)]) { intent in
                    Task { await run(intent) }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button("Sign out of Spotify", role: .destructive) { spotify.disconnect() }
                .font(.footnote)
        }
    }

    private func control(_ symbol: String, _ intent: MusicIntent, alternate: MusicIntent? = nil) -> some View {
        Button {
            Task { await run(intent) }
        } label: {
            Image(systemName: symbol)
        }
        .contextMenu {
            if let alternate = alternate { Button("Pause") { Task { await run(alternate) } } }
        }
        .disabled(busy)
    }

    private func play() {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        query = ""
        if q.isEmpty { return }
        Task { await run(.play(q)) }
    }

    private func run(_ intent: MusicIntent) async {
        busy = true
        let english = await spotify.perform(intent)
        status = english
        busy = false
        try? await Task.sleep(nanoseconds: 500_000_000)       // let Spotify catch up, then show the new state
        await refreshNow()
    }
}

/// A wrapping row of small buttons.
struct FlowButtons: View {
    let items: [(String, MusicIntent)]
    let action: (MusicIntent) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 8)], alignment: .leading, spacing: 8) {
            ForEach(items.indices, id: \.self) { i in
                Button(items[i].0) { action(items[i].1) }
                    .buttonStyle(.bordered)
                    .frame(maxWidth: .infinity)
            }
        }
    }
}
