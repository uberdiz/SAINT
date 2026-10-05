import SwiftUI
import SaintCore

/// Now Playing (artwork, progress, controls — swipe the artwork for next / previous, hold it for more), what's
/// up next, and mixes made from your listening. Everything here is also a voice command.
struct MusicView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var settings: AppSettings
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var query = ""
    @State private var status = ""
    @State private var dragX: CGFloat = 0
    @State private var showDevices = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if spotify.connected {
                        nowPlaying
                        search
                        if !spotify.upNext.isEmpty { upNext }
                        forYou
                        if !status.isEmpty {
                            Text(status).font(.system(size: 13)).foregroundStyle(Theme.muted).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } else {
                        connectCard
                    }
                    if let error = spotify.lastError {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 12.5)).foregroundStyle(Theme.accent)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
            .dismissKeyboardOnDragDown()
            .refreshable { await spotify.refreshNow(); await spotify.refreshUpNext() }
            .saintBackground()
            .navigationTitle("Music")
            .toolbar {
                if spotify.connected {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { showDevices = true } label: {
                            HStack(spacing: 5) {
                                Image(systemName: "hifispeaker").foregroundStyle(Theme.accent)
                                Text(spotify.nowPlaying?.device.isEmpty == false ? spotify.nowPlaying!.device : "Device")
                                    .lineLimit(1)
                                Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold)).foregroundStyle(Theme.muted)
                            }
                            .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Theme.text)
                        }
                        .accessibilityLabel("Play on another device")
                    }
                }
            }
            .task(id: spotify.connected) {
                guard spotify.connected else { return }
                await spotify.refreshUpNext()
                while !Task.isCancelled && spotify.connected {
                    await spotify.refreshNow()
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                }
            }
            .sheet(isPresented: $showDevices) { DevicePicker() }
        }
    }

    // MARK: now playing

    private var nowPlaying: some View {
        let now = spotify.nowPlaying
        return VStack(spacing: 14) {
            AsyncImage(url: now?.artworkURL) { image in image.resizable().scaledToFill() } placeholder: {
                ZStack {
                    LinearGradient(colors: [Theme.speaking, Theme.hex(0x5a1440)], startPoint: .topLeading, endPoint: .bottomTrailing)
                    Image(systemName: "music.note").font(.system(size: 54, weight: .semibold)).foregroundStyle(.white.opacity(0.8))
                }
            }
            .frame(width: 270, height: 270)
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            .shadow(color: Theme.speaking.opacity(0.35), radius: 30, y: 16)
            .scaleEffect(now?.isPlaying == false ? 0.92 : 1)
            .animation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.75), value: now?.isPlaying)
            .offset(x: dragX)
            .rotationEffect(.degrees(reduceMotion ? 0 : Double(dragX) / 40))
            .gesture(DragGesture(minimumDistance: 20)
                .onChanged { dragX = $0.translation.width * 0.6 }
                .onEnded { value in
                    withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.8)) { dragX = 0 }
                    if abs(value.translation.width) > 80 { run(value.translation.width < 0 ? .skip : .previous, haptic: true) }
                })
            .contextMenu {
                Button { run(.like) } label: { Label("Save to Liked Songs", systemImage: "heart") }
                Button { run(.addToPlaylist(lastPlaylist)) } label: { Label("Add to “\(lastPlaylist)”", systemImage: "text.badge.plus") }
                Button { run(.playLike("this")) } label: { Label("Play more like this", systemImage: "sparkles") }
                Button { run(.shuffle(true)) } label: { Label("Shuffle on", systemImage: "shuffle") }
                Button { run(.restart) } label: { Label("Restart song", systemImage: "backward.end") }
            }
            .accessibilityLabel(now.map { "\($0.title) by \($0.artist)" } ?? "Nothing playing")
            .accessibilityHint("Swipe left for the next song, right for the previous one")
            .padding(.top, 4)

            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(now?.title ?? "Nothing playing").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text).lineLimit(1)
                    Text(now.map { [$0.artist, $0.album].filter { !$0.isEmpty }.joined(separator: " · ") } ?? "Say “SAINT, play something”")
                        .font(.system(size: 15)).foregroundStyle(Theme.muted).lineLimit(1)
                }
                .contentTransition(.opacity)
                Spacer()
                Button { run(.like, haptic: true) } label: {
                    Image(systemName: "heart").font(.system(size: 23, weight: .medium)).foregroundStyle(Theme.accent)
                }
                .accessibilityLabel("Save to Liked Songs")
            }

            if let now = now, now.durationMs > 0 {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    VStack(spacing: 6) {
                        GeometryReader { geo in
                            let fraction = now.position(at: context.date) / Double(max(1, now.durationMs))
                            ZStack(alignment: .leading) {
                                Capsule().fill(Theme.raised).frame(height: 4)
                                Capsule().fill(Theme.accent).frame(width: geo.size.width * fraction, height: 4)
                                Circle().fill(Theme.text).frame(width: 12, height: 12)
                                    .offset(x: max(0, geo.size.width * fraction - 6))
                            }
                            .frame(maxHeight: .infinity)
                            .contentShape(Rectangle())
                            .gesture(DragGesture(minimumDistance: 0).onEnded { value in
                                let f = min(1, max(0, value.location.x / geo.size.width))
                                Task { await spotify.seek(toMs: Int(f * Double(now.durationMs))); await spotify.refreshNow() }
                            })
                        }
                        .frame(height: 16)
                        HStack {
                            Text(clock(now.position(at: context.date)))
                            Spacer()
                            Text("-" + clock(Double(now.durationMs) - now.position(at: context.date)))
                        }
                        .font(.system(size: 11.5, weight: .medium).monospacedDigit())
                        .foregroundStyle(Theme.muted)
                    }
                }
            }

            HStack {
                control("shuffle", tint: Theme.accent, size: 20) { run(.shuffle(true)) }
                Spacer()
                control("backward.end.fill", size: 26) { run(.previous, haptic: true) }
                Spacer()
                Button { run(now?.isPlaying == true ? .pause : .resume, haptic: true) } label: {
                    Image(systemName: now?.isPlaying == true ? "pause.fill" : "play.fill")
                        .font(.system(size: 28, weight: .bold))
                        .foregroundStyle(Theme.onAccent)
                        .frame(width: 66, height: 66)
                        .background(Theme.accent, in: Circle())
                        .shadow(color: Theme.accent.opacity(0.4), radius: 14, y: 6)
                }
                .accessibilityLabel(now?.isPlaying == true ? "Pause" : "Play")
                Spacer()
                control("forward.end.fill", size: 26) { run(.skip, haptic: true) }
                Spacer()
                control("text.line.first.and.arrowtriangle.forward", tint: Theme.muted, size: 20) {
                    Task { await spotify.refreshUpNext() }
                }
            }
            .padding(.horizontal, 6)

            Text("Swipe the artwork for next / previous · hold for more")
                .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.faint)
        }
    }

    private var lastPlaylist: String { settings.lastPlaylist.isEmpty ? "Workout" : settings.lastPlaylist }

    private func control(_ symbol: String, tint: Color = Theme.text, size: CGFloat, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: size)).foregroundStyle(tint).frame(width: 44, height: 44)
        }
    }

    // MARK: search

    private var search: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").foregroundStyle(Theme.muted)
            TextField("", text: $query, prompt: Text("Play a song, artist, album or playlist").foregroundStyle(Theme.faint))
                .foregroundStyle(Theme.text)
                .submitLabel(.go)
                .onSubmit(play)
            if !query.isEmpty {
                Button("Play", action: play).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.accent)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .glassCard(radius: 14, fill: Theme.surface2)
    }

    // MARK: up next

    private var upNext: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Up next")
            ForEach(spotify.upNext.prefix(5)) { track in
                HStack(spacing: 12) {
                    AsyncImage(url: track.artworkURL) { image in image.resizable().scaledToFill() } placeholder: { Theme.raised }
                        .frame(width: 44, height: 44)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(track.title).font(.system(size: 14.5, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                        Text(track.artist).font(.system(size: 12.5)).foregroundStyle(Theme.muted).lineLimit(1)
                    }
                    Spacer()
                }
            }
        }
    }

    // MARK: for you

    private var forYou: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("For you")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    mix("More like this", "From what's playing", Theme.speaking, Theme.hex(0x3a1030)) { run(.playLike("this")) }
                    mix("Songs you love", "Your liked songs", Theme.accent, Theme.hex(0x5a2a08)) { run(.playLiked) }
                    mix("Today's listening", "What you played today", Theme.listening, Theme.hex(0x0d3a3a)) { run(.playRecent) }
                    mix("Rediscover", "Favourites you haven't played lately", Theme.thinking, Theme.hex(0x1d1f4a)) { run(.recommend) }
                }
            }
        }
    }

    private func mix(_ title: String, _ subtitle: String, _ a: Color, _ b: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                ZStack(alignment: .bottomLeading) {
                    LinearGradient(colors: [a, b], startPoint: .topLeading, endPoint: .bottomTrailing)
                    Image(systemName: "play.circle.fill").font(.system(size: 26)).foregroundStyle(.white.opacity(0.9)).padding(10)
                }
                .frame(width: 128, height: 128)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                Text(subtitle).font(.system(size: 11.5)).foregroundStyle(Theme.muted).lineLimit(2)
            }
            .frame(width: 128, alignment: .leading)
        }
        .buttonStyle(.plain)
    }

    // MARK: connect

    private var connectCard: some View {
        VStack(spacing: 14) {
            Image(systemName: "music.note.house.fill").font(.system(size: 44)).foregroundStyle(Theme.accent)
            Text("Connect Spotify").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
            Text("Then just say “SAINT, play Blinding Lights”, “SAINT, play my liked songs” or “SAINT, skip”. Playback control needs Spotify Premium.")
                .font(.system(size: 14)).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
            VStack(alignment: .leading, spacing: 6) {
                Text("1. Open your app at developer.spotify.com/dashboard (the same one your PC uses is fine)")
                Text("2. Settings → Redirect URIs → add  \(SpotifyService.redirectURI)  and Save")
                Text("3. Paste its Client ID here:")
            }
            .font(.system(size: 12.5)).foregroundStyle(Theme.text)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
            TextField("", text: $settings.spotifyClientID, prompt: Text("Client ID").foregroundStyle(Theme.faint))
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .foregroundStyle(Theme.text)
                .padding(12).glassCard(radius: 12, fill: Theme.surface2)
            Button { Task { await spotify.connect() } } label: {
                Text("Sign in with Spotify").font(.system(size: 15, weight: .semibold)).frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(Theme.accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .foregroundStyle(Theme.onAccent)
            }
            .disabled(settings.spotifyClientID.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .padding(20)
        .glassCard(radius: 24)
    }

    // MARK: actions

    private func play() {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        query = ""
        hideKeyboard()
        if q.isEmpty { return }
        run(.play(q))
    }

    private func run(_ intent: MusicIntent, haptic: Bool = false) {
        if haptic && settings.haptics { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
        Task {
            let english = await spotify.perform(intent)
            status = english
            model.logMusic(intent: intent, result: english)
            try? await Task.sleep(nanoseconds: 600_000_000)
            await spotify.refreshNow()
            await spotify.refreshUpNext()
        }
    }

    private func clock(_ ms: Double) -> String {
        let s = max(0, Int(ms / 1000))
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

/// Spotify Connect: play on this iPhone, your PC, a speaker…
struct DevicePicker: View {
    @EnvironmentObject var spotify: SpotifyService
    @Environment(\.dismiss) private var dismiss
    @State private var devices: [SpotifyService.SpotifyDevice] = []
    @State private var loading = true

    var body: some View {
        NavigationStack {
            List {
                if loading { ProgressView().listRowBackground(Theme.surface) }
                if !loading && devices.isEmpty {
                    Text("No Spotify devices are open. Open Spotify on a phone, PC or speaker first.")
                        .foregroundStyle(Theme.muted).listRowBackground(Theme.surface)
                }
                ForEach(devices) { d in
                    Button {
                        Task { _ = await spotify.perform(.transfer(d.name)); await spotify.refreshNow(); dismiss() }
                    } label: {
                        HStack {
                            Image(systemName: d.symbol).foregroundStyle(Theme.accent).frame(width: 28)
                            Text(d.name).foregroundStyle(Theme.text)
                            Spacer()
                            if d.active { Image(systemName: "checkmark").foregroundStyle(Theme.accent) }
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.bg)
            .navigationTitle("Play on")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { devices = await spotify.devices(); loading = false }
        }
        .presentationDetents([.medium])
    }
}
