import SwiftUI
import SaintCore

struct MusicView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var settings: AppSettings
    @State private var query = ""
    @State private var status = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if spotify.connected { player } else { connectCard }
                    if let error = spotify.lastError {
                        Label(error, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
                    }
                }
                .padding(16)
            }
            .saintBackground()
            .navigationTitle("Music")
        }
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
