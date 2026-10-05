import Foundation
import AuthenticationServices
import CryptoKit
import UIKit
import SaintCore

/// Spotify through its Web API: sign-in with PKCE (no client secret on the phone), then search, play, pause,
/// skip, volume, shuffle, like. Playback is controlled on whichever of your Spotify devices is active, which is
/// usually this phone's Spotify app. Replies are SAINT's English phrases so they translate like any other.
///
/// You need a free Spotify developer app (client id) with the redirect URI `saint://spotify-callback`, and
/// Spotify Premium for the playback-control endpoints (a Spotify rule, not ours).
final class SpotifyService: NSObject, ObservableObject, MusicService, ASWebAuthenticationPresentationContextProviding {
    @Published private(set) var connected = false
    @Published private(set) var accountName = ""
    @Published private(set) var lastError: String?
    /// What's playing (the Music tab and the mini player), refreshed while those are on screen.
    @Published private(set) var nowPlaying: NowPlayingInfo?
    @Published private(set) var upNext: [QueueTrack] = []

    static let redirectURI = "saint://spotify-callback"
    /// Spotify's "redirect_uri: Not matching configuration" page means this exact URI isn't in the app's Redirect
    /// URIs on developer.spotify.com — a dashboard setting, so all SAINT can do is say exactly what to add.
    static let redirectHelp = "If Spotify said “redirect_uri: Not matching configuration”, open your app on "
        + "developer.spotify.com/dashboard → Settings → Redirect URIs, add saint://spotify-callback exactly, and Save. "
        + "One Spotify app can serve your PC and your iPhone: keep the PC's URI there too and use the same Client ID."
    private static let scopes = ["user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
                                 "user-library-modify", "user-library-read", "playlist-read-private", "user-top-read",
                                 "user-read-private", "user-read-recently-played", "playlist-modify-private",
                                 "playlist-modify-public"]
    private static let grantedKey = "spotify.grantedScopes"

    private let keychain: Keychain
    private let settings: AppSettings
    private var accessToken: String?
    private var expiry = Date.distantPast
    private var authSession: ASWebAuthenticationSession?
    private let refreshKey = "spotify.refresh"

    init(keychain: Keychain, settings: AppSettings) {
        self.keychain = keychain
        self.settings = settings
        super.init()
        connected = keychain.string(refreshKey) != nil
    }

    // MARK: signing in

    private static func randomString(_ length: Int) -> String {
        let alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return String((0..<length).map { _ in alphabet[Int.random(in: 0..<alphabet.count)] })
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    @MainActor
    func connect() async {
        let clientID = settings.spotifyClientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clientID.isEmpty else {
            lastError = "Add your Spotify client ID in Settings first."
            return
        }
        let verifier = SpotifyService.randomString(80)
        let challenge = SpotifyService.base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
        let state = SpotifyService.randomString(16)
        var components = URLComponents(string: "https://accounts.spotify.com/authorize")!
        components.queryItems = [
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "redirect_uri", value: SpotifyService.redirectURI),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
            URLQueryItem(name: "code_challenge", value: challenge),
            URLQueryItem(name: "scope", value: SpotifyService.scopes.joined(separator: " ")),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "show_dialog", value: "true"),     // always Spotify's page, so a wrong try can be redone
        ]
        // A sign-in still open from a failed try (wrong redirect, error page) blocked the next one.
        authSession?.cancel()
        authSession = nil
        let retrying = lastError != nil
        do {
            let callback: URL = try await withCheckedThrowingContinuation { continuation in
                let session = ASWebAuthenticationSession(url: components.url!, callbackURLScheme: "saint") { url, error in
                    if let url = url { continuation.resume(returning: url) }
                    else { continuation.resume(throwing: error ?? LinkError.closed) }
                }
                session.presentationContextProvider = self
                // After a failed try, start clean: no remembered Spotify page sending you back to the same error.
                session.prefersEphemeralWebBrowserSession = retrying
                self.authSession = session
                session.start()
            }
            let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if let denied = items.first(where: { $0.name == "error" })?.value {
                lastError = denied == "access_denied" ? "You didn't allow SAINT to use Spotify." : "Spotify said: \(denied)."
                return
            }
            guard items.first(where: { $0.name == "state" })?.value == state,
                  let code = items.first(where: { $0.name == "code" })?.value else {
                lastError = "Spotify didn't confirm the sign-in. Try again."
                return
            }
            try await exchange(["grant_type": "authorization_code", "code": code, "redirect_uri": SpotifyService.redirectURI,
                                "client_id": clientID, "code_verifier": verifier])
            authSession = nil
            lastError = nil
            await loadProfile()
        } catch {
            if (error as? ASWebAuthenticationSessionError)?.code == .canceledLogin {
                // Closed before Spotify sent you back — usually because Spotify showed an error page.
                lastError = "The Spotify sign-in was closed. " + SpotifyService.redirectHelp
                return
            }
            lastError = "Couldn't sign in to Spotify: \(error.localizedDescription)"
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        return scene?.windows.first(where: { $0.isKeyWindow }) ?? ASPresentationAnchor()
    }

    func disconnect() {
        keychain.delete(account: refreshKey)
        accessToken = nil
        DispatchQueue.main.async {
            self.connected = false
            self.accountName = ""
        }
    }

    private func exchange(_ form: [String: String]) async throws {
        var request = URLRequest(url: URL(string: "https://accounts.spotify.com/api/token")!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = form.map { "\($0.key)=\(SpotifyService.formEncode($0.value))" }.joined(separator: "&").data(using: .utf8)
        let (data, response) = try await URLSession.shared.data(for: request)
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (response as? HTTPURLResponse)?.statusCode == 200, let token = json?["access_token"] as? String else {
            let reason = (json?["error_description"] as? String) ?? (json?["error"] as? String) ?? "no reason given"
            throw LinkError.remote(code: "spotify", message: "Spotify refused the sign-in (\(reason)).")
        }
        accessToken = token
        if let granted = json?["scope"] as? String { UserDefaults.standard.set(granted, forKey: SpotifyService.grantedKey) }
        expiry = Date().addingTimeInterval(((json?["expires_in"] as? Double) ?? 3600) - 60)
        if let refresh = json?["refresh_token"] as? String { keychain.setString(refresh, account: refreshKey) }
        DispatchQueue.main.async { self.connected = true }
    }

    private static func formEncode(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? s
    }

    private func token() async throws -> String {
        if let t = accessToken, expiry > Date() { return t }
        guard let refresh = keychain.string(refreshKey) else { throw SpotifyError.notConnected }
        try await exchange(["grant_type": "refresh_token", "refresh_token": refresh,
                            "client_id": settings.spotifyClientID.trimmingCharacters(in: .whitespacesAndNewlines)])
        guard let t = accessToken else { throw SpotifyError.notConnected }
        return t
    }

    private func loadProfile() async {
        if let (_, json) = try? await api("GET", "/v1/me"), let obj = json as? [String: Any] {
            let name = (obj["display_name"] as? String) ?? (obj["id"] as? String) ?? ""
            DispatchQueue.main.async { self.accountName = name }
        }
    }

    // MARK: web API

    enum SpotifyError: Error {
        case notConnected
        case premium
        case noDevice
        case busy
        case failed(Int)
        case network
    }

    @discardableResult
    private func api(_ method: String, _ path: String, query: [String: String] = [:], body: [String: Any]? = nil,
                     retried: Bool = false) async throws -> (Int, Any?) {
        var components = URLComponents(string: "https://api.spotify.com" + path)!
        if !query.isEmpty { components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("Bearer \(try await token())", forHTTPHeaderField: "Authorization")
        if let body = body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        } else if method != "GET" {
            request.setValue("0", forHTTPHeaderField: "Content-Length")
        }
        let data: Data, response: URLResponse
        do { (data, response) = try await URLSession.shared.data(for: request) } catch { throw SpotifyError.network }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 && !retried {
            accessToken = nil
            return try await api(method, path, query: query, body: body, retried: true)
        }
        if status == 429 { throw SpotifyError.busy }
        var json: Any?
        if !data.isEmpty { json = try? JSONSerialization.jsonObject(with: data) }
        if status == 403 { throw SpotifyError.premium }
        if status == 404, let reason = ((json as? [String: Any])?["error"] as? [String: Any])?["reason"] as? String,
           reason == "NO_ACTIVE_DEVICE" { throw SpotifyError.noDevice }
        if !(200..<300).contains(status) { throw SpotifyError.failed(status) }
        return (status, json)
    }

    /// Run a playback command, waking a device if none is active.
    private func control(_ method: String, _ path: String, query: [String: String] = [:], body: [String: Any]? = nil) async throws {
        do {
            try await api(method, path, query: query, body: body)
        } catch SpotifyError.noDevice {
            try await wakeDevice()
            try await api(method, path, query: query, body: body)
        }
    }

    private func wakeDevice() async throws {
        let (_, json) = try await api("GET", "/v1/me/player/devices")
        let devices = ((json as? [String: Any])?["devices"] as? [[String: Any]]) ?? []
        let phoneName = UIDevice.current.name.lowercased()
        let pick = devices.first { ($0["is_active"] as? Bool) == true }
            ?? devices.first { (($0["name"] as? String) ?? "").lowercased() == phoneName }
            ?? devices.first { ($0["type"] as? String) == "Smartphone" }
            ?? devices.first
        guard let id = pick?["id"] as? String else { throw SpotifyError.noDevice }
        try await api("PUT", "/v1/me/player", body: ["device_ids": [id], "play": false])
        try? await Task.sleep(nanoseconds: 700_000_000)
    }

    // MARK: what's playing

    private struct Playing {
        var name: String
        var artist: String
        var id: String
        var isPlaying: Bool
    }

    private func current() async throws -> Playing? {
        let (status, json) = try await api("GET", "/v1/me/player/currently-playing")
        guard status == 200, let obj = json as? [String: Any], let item = obj["item"] as? [String: Any] else { return nil }
        let artists = ((item["artists"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }
        return Playing(name: (item["name"] as? String) ?? "", artist: artists.joined(separator: ", "),
                       id: (item["id"] as? String) ?? "", isPlaying: (obj["is_playing"] as? Bool) ?? false)
    }

    /// What the Now Playing card shows.
    struct NowPlayingInfo: Equatable {
        var title: String
        var artist: String
        var album: String
        var artworkURL: URL?
        var progressMs: Int
        var durationMs: Int
        var isPlaying: Bool
        var device: String
        var fetchedAt = Date()

        /// Where the song is now, counting the time since it was fetched.
        func position(at date: Date = Date()) -> Double {
            let elapsed = isPlaying ? date.timeIntervalSince(fetchedAt) * 1000 : 0
            return min(Double(durationMs), Double(progressMs) + elapsed)
        }
    }

    struct QueueTrack: Identifiable, Equatable {
        var id: String
        var title: String
        var artist: String
        var artworkURL: URL?
    }

    struct SpotifyDevice: Identifiable, Equatable {
        var id: String
        var name: String
        var type: String
        var active: Bool
        var symbol: String {
            switch type.lowercased() {
            case "smartphone": return "iphone"
            case "computer": return "laptopcomputer"
            case "speaker": return "hifispeaker"
            case "tv": return "tv"
            default: return "music.note"
            }
        }
    }

    /// Scopes added after the first release (recently played, editing playlists) need a fresh sign-in.
    private func missingScope(_ scope: String) -> Bool {
        guard let granted = UserDefaults.standard.string(forKey: SpotifyService.grantedKey) else { return false }
        return !granted.split(separator: " ").contains(Substring(scope))
    }

    @MainActor
    func refreshNow() async {
        let info = await fetchNowPlaying()
        if info != nowPlaying { nowPlaying = info }
    }

    @MainActor
    func refreshUpNext() async {
        guard connected, let (status, json) = try? await api("GET", "/v1/me/player/queue"), status == 200,
              let obj = json as? [String: Any] else { return }
        let items = (obj["queue"] as? [[String: Any]]) ?? []
        upNext = items.prefix(10).enumerated().map { i, t in
            QueueTrack(id: "\(i)-\((t["id"] as? String) ?? "")", title: (t["name"] as? String) ?? "",
                       artist: SpotifyService.artists(t), artworkURL: SpotifyService.art(t))
        }
    }

    func devices() async -> [SpotifyDevice] {
        guard let (_, json) = try? await api("GET", "/v1/me/player/devices") else { return [] }
        let list = ((json as? [String: Any])?["devices"] as? [[String: Any]]) ?? []
        return list.compactMap { d in
            guard let id = d["id"] as? String else { return nil }
            return SpotifyDevice(id: id, name: (d["name"] as? String) ?? "Device", type: (d["type"] as? String) ?? "",
                                 active: (d["is_active"] as? Bool) ?? false)
        }
    }

    static func artists(_ track: [String: Any]) -> String {
        ((track["artists"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }.joined(separator: ", ")
    }

    static func art(_ track: [String: Any]) -> URL? {
        let images = ((track["album"] as? [String: Any])?["images"] as? [[String: Any]]) ?? []
        return (images.last(where: { (($0["width"] as? Int) ?? 0) >= 64 })?["url"] as? String ?? images.first?["url"] as? String)
            .flatMap { URL(string: $0) }
    }

    /// The full player state, or nil when nothing is playing (or Spotify isn't reachable).
    func fetchNowPlaying() async -> NowPlayingInfo? {
        guard connected, let (status, json) = try? await api("GET", "/v1/me/player"), status == 200,
              let obj = json as? [String: Any], let item = obj["item"] as? [String: Any] else { return nil }
        let artists = ((item["artists"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }
        let album = item["album"] as? [String: Any]
        let images = (album?["images"] as? [[String: Any]]) ?? []
        let art = (images.first?["url"] as? String).flatMap { URL(string: $0) }
        return NowPlayingInfo(title: (item["name"] as? String) ?? "", artist: artists.joined(separator: ", "),
                              album: (album?["name"] as? String) ?? "", artworkURL: art,
                              progressMs: (obj["progress_ms"] as? Int) ?? 0, durationMs: (item["duration_ms"] as? Int) ?? 0,
                              isPlaying: (obj["is_playing"] as? Bool) ?? false,
                              device: ((obj["device"] as? [String: Any])?["name"] as? String) ?? "")
    }

    func seek(toMs ms: Int) async {
        _ = try? await control("PUT", "/v1/me/player/seek", query: ["position_ms": String(max(0, ms))])
    }

    // MARK: searching

    private struct Hit {
        var kind: String          // track, artist, album, playlist
        var name: String
        var by: String
        var uri: String
        var artistID = ""         // tracks: the first artist, for "keep playing more of them"
    }

    private func search(_ q: String, types: [String], limit: Int = 5) async throws -> [Hit] {
        let (_, json) = try await api("GET", "/v1/search", query: ["q": q, "type": types.joined(separator: ","), "limit": String(limit)])
        guard let obj = json as? [String: Any] else { return [] }
        var hits: [Hit] = []
        func items(_ key: String) -> [[String: Any]] {
            ((obj[key] as? [String: Any])?["items"] as? [Any])?.compactMap { $0 as? [String: Any] } ?? []
        }
        for t in items("tracks") {
            let by = ((t["artists"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }.joined(separator: ", ")
            let artistID = ((t["artists"] as? [[String: Any]])?.first?["id"] as? String) ?? ""
            hits.append(Hit(kind: "track", name: (t["name"] as? String) ?? "", by: by, uri: (t["uri"] as? String) ?? "",
                            artistID: artistID))
        }
        for a in items("artists") { hits.append(Hit(kind: "artist", name: (a["name"] as? String) ?? "", by: "", uri: (a["uri"] as? String) ?? "")) }
        for a in items("albums") {
            let by = ((a["artists"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }.joined(separator: ", ")
            hits.append(Hit(kind: "album", name: (a["name"] as? String) ?? "", by: by, uri: (a["uri"] as? String) ?? ""))
        }
        for p in items("playlists") { hits.append(Hit(kind: "playlist", name: (p["name"] as? String) ?? "", by: "", uri: (p["uri"] as? String) ?? "")) }
        return hits.filter { !$0.uri.isEmpty }
    }

    // MARK: MusicService

    func perform(_ intent: MusicIntent) async -> String {
        guard connected else { return "Spotify isn't connected." }
        do {
            return try await run(intent)
        } catch SpotifyError.notConnected {
            disconnect()
            return "Spotify isn't connected."
        } catch SpotifyError.premium {
            return "Spotify said no. Controlling playback needs Spotify Premium — if you have it, sign in to Spotify again in Settings."
        } catch SpotifyError.noDevice {
            return "Open Spotify on a device first, then try again."
        } catch SpotifyError.busy {
            return "Spotify is busy right now. Try again in a moment."
        } catch SpotifyError.network {
            return "I couldn't reach Spotify."
        } catch {
            return "Spotify said no to that."
        }
    }

    private func run(_ intent: MusicIntent) async throws -> String {
        switch intent {
        case .pause:
            try await control("PUT", "/v1/me/player/pause")
            return "Paused."
        case .resume:
            try await control("PUT", "/v1/me/player/play")
            return "Resuming."
        case .skip:
            try await control("POST", "/v1/me/player/next")
            try? await Task.sleep(nanoseconds: 600_000_000)
            if let now = try? await current() { return "Skipped. Now playing \(now.name)." }
            return "Skipped."
        case .previous:
            try await control("POST", "/v1/me/player/previous")
            return "Going back."
        case .restart:
            try await control("PUT", "/v1/me/player/seek", query: ["position_ms": "0"])
            return "Okay."
        case .volumeUp, .volumeDown:
            let (_, json) = try await api("GET", "/v1/me/player")
            let level = (((json as? [String: Any])?["device"] as? [String: Any])?["volume_percent"] as? Int) ?? 50
            let target = max(0, min(100, level + (intent == .volumeUp ? 15 : -15)))
            try await control("PUT", "/v1/me/player/volume", query: ["volume_percent": String(target)])
            return "Volume \(target)%."
        case .volume(let n):
            try await control("PUT", "/v1/me/player/volume", query: ["volume_percent": String(n)])
            return "Volume \(n)%."
        case .shuffle(let on):
            try await control("PUT", "/v1/me/player/shuffle", query: ["state": on ? "true" : "false"])
            return on ? "Shuffle on." : "Shuffle off."
        case .nowPlaying:
            guard let now = try await current() else { return "Spotify isn't playing anything right now." }
            return now.isPlaying ? "Playing \(now.name) by \(now.artist)." : "Paused on \(now.name) by \(now.artist)."
        case .like:
            guard let now = try await current() else { return "Spotify isn't playing anything right now." }
            try await api("PUT", "/v1/me/tracks", query: ["ids": now.id])
            return "Saved \(now.name) to your liked songs."
        case .play(let query):
            return try await playQuery(query)
        case .playAlbum(let query):
            guard let hit = try await search(query, types: ["album"], limit: 3).first else { return "I didn't find anything for that on Spotify." }
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": hit.uri])
            return "Playing the album \(hit.name) by \(hit.by)."
        case .playPlaylist(let name):
            return try await playPlaylist(name)
        case .playLike(let seed):
            return try await playLike(seed)
        case .playGenre(let genre):
            let hits = try await search("genre:\"\(genre)\"", types: ["track"], limit: 20)
            let tracks = hits.filter { $0.kind == "track" }
            if tracks.isEmpty { return try await playQuery(genre) }
            try await control("PUT", "/v1/me/player/play", body: ["uris": tracks.shuffled().map { $0.uri }])
            return "Playing \(genre)."
        case .playLiked:
            let uris = try await likedURIs(limit: 50)
            guard !uris.isEmpty else { return "You haven't liked any songs on Spotify yet." }
            try await control("PUT", "/v1/me/player/play", body: ["uris": uris.shuffled()])
            return "Playing your liked songs."
        case .playRecent, .recentSummary:
            if missingScope("user-read-recently-played") { return "Sign in to Spotify again in Settings so I can see what you've played." }
            let (_, json) = try await api("GET", "/v1/me/player/recently-played", query: ["limit": "50"])
            let items = ((json as? [String: Any])?["items"] as? [[String: Any]]) ?? []
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let cal = Calendar.current
            let today = items.filter { item in
                let s = (item["played_at"] as? String) ?? ""
                let date = iso.date(from: s) ?? ISO8601DateFormatter().date(from: s)
                return date.map { cal.isDateInToday($0) } ?? false
            }
            let pool = today.isEmpty ? items : today
            let tracks = pool.compactMap { $0["track"] as? [String: Any] }
            if tracks.isEmpty { return "You haven't played anything on Spotify lately." }
            if intent == .recentSummary {
                var counts: [String: Int] = [:]
                for t in tracks { counts[SpotifyService.artists(t).components(separatedBy: ", ").first ?? "", default: 0] += 1 }
                let top = counts.sorted { $0.value > $1.value }.prefix(2).map { $0.key }.filter { !$0.isEmpty }
                let when = today.isEmpty ? "Lately" : "Today"
                let last = tracks.first.map { " The last one was \(($0["name"] as? String) ?? "") by \(SpotifyService.artists($0))." } ?? ""
                return "\(when) you played \(tracks.count) song\(tracks.count == 1 ? "" : "s"), mostly \(top.joined(separator: " and "))." + last
            }
            var seen = Set<String>()
            let uris = tracks.compactMap { $0["uri"] as? String }.filter { seen.insert($0).inserted }
            try await control("PUT", "/v1/me/player/play", body: ["uris": uris])
            return today.isEmpty ? "Playing what you've listened to lately." : "Playing what you've listened to today."
        case .topTrack:
            let (_, json) = try await api("GET", "/v1/me/top/tracks", query: ["limit": "1", "time_range": "short_term"])
            guard let t = ((json as? [String: Any])?["items"] as? [[String: Any]])?.first else {
                return "I don't know your favourites yet. Listen to a few things first."
            }
            return "Your most played song lately is \((t["name"] as? String) ?? "") by \(SpotifyService.artists(t))."
        case .queue(let query):
            guard let hit = try await search(query, types: ["track"], limit: 3).first(where: { $0.kind == "track" }) else {
                return "I didn't find anything for that on Spotify."
            }
            try await control("POST", "/v1/me/player/queue", query: ["uri": hit.uri])
            return "Added \(hit.name) by \(hit.by) to the queue."
        case .addToPlaylist(let name):
            if missingScope("playlist-modify-private") { return "Sign in to Spotify again in Settings so I can edit your playlists." }
            guard let now = try await current() else { return "Spotify isn't playing anything right now." }
            guard let list = try await myPlaylist(named: name) else { return "I couldn't find a playlist called \(name)." }
            try await api("POST", "/v1/playlists/\(list.id)/tracks", body: ["uris": ["spotify:track:\(now.id)"]])
            DispatchQueue.main.async { self.settings.lastPlaylist = list.name }
            return "Added \(now.name) to \(list.name)."
        case .recommend:
            // Spotify closed its recommendations to new apps: mix your favourites you haven't played lately.
            let (_, top) = try await api("GET", "/v1/me/top/tracks", query: ["limit": "50", "time_range": "long_term"])
            let favourites = ((top as? [String: Any])?["items"] as? [[String: Any]]) ?? []
            var recent = Set<String>()
            if !missingScope("user-read-recently-played"), let (_, rj) = try? await api("GET", "/v1/me/player/recently-played", query: ["limit": "50"]) {
                for item in ((rj as? [String: Any])?["items"] as? [[String: Any]]) ?? [] {
                    if let uri = (item["track"] as? [String: Any])?["uri"] as? String { recent.insert(uri) }
                }
            }
            let picks = favourites.filter { !recent.contains(($0["uri"] as? String) ?? "") }.shuffled().prefix(25)
            guard let first = picks.first else { return "Listen to a few more things and I'll have ideas for you." }
            try await control("PUT", "/v1/me/player/play", body: ["uris": picks.compactMap { $0["uri"] as? String }])
            return "How about \((first["name"] as? String) ?? "this") by \(SpotifyService.artists(first))? Playing favourites you haven't heard in a while."
        case .transfer(let target):
            let list = await devices()
            let t = fold(target)
            let pick = list.first { fold($0.name).contains(t) || t.contains(fold($0.name)) }
                ?? list.first { d in
                    (["phone", "iphone", "my phone", "this phone"].contains(t) && d.type == "Smartphone")
                        || (["pc", "computer", "desktop", "laptop", "my pc"].contains(t) && d.type == "Computer")
                        || (t.contains("speaker") && d.type == "Speaker")
                }
            guard let device = pick else { return "I can't find a Spotify device called \(target). Open Spotify on it first." }
            try await api("PUT", "/v1/me/player", body: ["device_ids": [device.id], "play": true])
            return "Playing on \(device.name)."
        case .seek(let seconds):
            let (_, json) = try await api("GET", "/v1/me/player")
            let progress = ((json as? [String: Any])?["progress_ms"] as? Int) ?? 0
            try await control("PUT", "/v1/me/player/seek", query: ["position_ms": String(max(0, progress + seconds * 1000))])
            return seconds >= 0 ? "Skipped ahead \(seconds) seconds." : "Went back \(-seconds) seconds."
        case .playSomethingILike:
            let (_, json) = try await api("GET", "/v1/me/top/tracks", query: ["limit": "30", "time_range": "medium_term"])
            let items = ((json as? [String: Any])?["items"] as? [[String: Any]]) ?? []
            let picks = items.shuffled().prefix(20)
            guard let first = picks.first else { return "I don't know your taste yet. Listen to a few things first." }
            try await control("PUT", "/v1/me/player/play", body: ["uris": picks.compactMap { $0["uri"] as? String }])
            let artist = ((first["artists"] as? [[String: Any]])?.first?["name"] as? String) ?? ""
            return "Playing \((first["name"] as? String) ?? "something") by \(artist)."
        }
    }

    /// A track, then more from that artist, so the music keeps going (the desktop queues the same way).
    private func playTrackAndMore(_ track: Hit) async throws {
        var uris = [track.uri]
        if !track.artistID.isEmpty, let (_, json) = try? await api("GET", "/v1/artists/\(track.artistID)/top-tracks",
                                                                   query: ["market": "from_token"]) {
            let more = (((json as? [String: Any])?["tracks"] as? [[String: Any]]) ?? []).compactMap { $0["uri"] as? String }
            uris += more.filter { $0 != track.uri }.shuffled()
        }
        try await control("PUT", "/v1/me/player/play", body: ["uris": Array(uris.prefix(30))])
    }

    private func likedURIs(limit: Int) async throws -> [String] {
        let (_, json) = try await api("GET", "/v1/me/tracks", query: ["limit": String(limit)])
        let items = ((json as? [String: Any])?["items"] as? [[String: Any]]) ?? []
        return items.compactMap { ($0["track"] as? [String: Any])?["uri"] as? String }
    }

    private func myPlaylist(named name: String) async throws -> (id: String, name: String, uri: String)? {
        let (_, json) = try await api("GET", "/v1/me/playlists", query: ["limit": "50"])
        let mine = ((json as? [String: Any])?["items"] as? [[String: Any]]) ?? []
        let wanted = fold(name.replacingOccurrences(of: " playlist", with: ""))
        let words = wanted.split(separator: " ").map(String.init).filter { !["my", "the", "music", "songs", "mix"].contains($0) }
        let scored = mine.compactMap { p -> (Int, [String: Any])? in
            let n = fold((p["name"] as? String) ?? "")
            if n == wanted { return (100, p) }
            if n.contains(wanted) || wanted.contains(n) { return (50, p) }
            let hits = words.filter { n.contains($0) }.count
            return hits > 0 ? (hits * 10, p) : nil
        }.sorted { $0.0 > $1.0 }
        guard let best = scored.first?.1, let id = best["id"] as? String, let uri = best["uri"] as? String else { return nil }
        return (id, (best["name"] as? String) ?? name, uri)
    }

    private func playQuery(_ raw: String) async throws -> String {
        var q = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // "my gym music", "my coding playlist": one of your playlists first.
        if q.lowercased().hasPrefix("my ") {
            if ["my liked songs", "my likes", "my favorites", "my favourites"].contains(q.lowercased()) {
                return try await run(.playLiked)
            }
            if let list = try await myPlaylist(named: String(q.dropFirst(3))) {
                try await control("PUT", "/v1/me/player/play", body: ["context_uri": list.uri])
                return "Playing your \(list.name) playlist."
            }
        }
        var wantedArtist: String?
        if let range = q.range(of: " by ", options: .caseInsensitive) {
            let title = String(q[q.startIndex..<range.lowerBound])
            let artist = String(q[range.upperBound...])
            wantedArtist = artist
            q = "track:\(title) artist:\(artist)"
        }
        var hits = try await search(q, types: ["track", "artist", "playlist"], limit: 5)
        if hits.isEmpty, wantedArtist != nil { hits = try await search(raw.replacingOccurrences(of: " by ", with: " "), types: ["track"], limit: 5) }
        let folded = fold(raw)
        if wantedArtist == nil, let artist = hits.first(where: { $0.kind == "artist" && fold($0.name) == folded }) {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": artist.uri])
            return "Playing \(artist.name)."
        }
        if let track = hits.first(where: { $0.kind == "track" }) {
            try await playTrackAndMore(track)
            return "Playing \(track.name) by \(track.by), then more from \(track.by.components(separatedBy: ", ").first ?? track.by)."
        }
        if let artist = hits.first(where: { $0.kind == "artist" }) {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": artist.uri])
            return "Playing \(artist.name)."
        }
        if let playlist = hits.first(where: { $0.kind == "playlist" }) {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": playlist.uri])
            return "Playing the \(playlist.name) playlist (a public playlist)."
        }
        return "I didn't find anything for that on Spotify."
    }

    private func playPlaylist(_ name: String) async throws -> String {
        let (_, json) = try await api("GET", "/v1/me/playlists", query: ["limit": "50"])
        let mine = ((json as? [String: Any])?["items"] as? [[String: Any]]) ?? []
        let wanted = fold(name)
        if let match = mine.first(where: { fold(($0["name"] as? String) ?? "").contains(wanted) }),
           let uri = match["uri"] as? String {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": uri])
            return "Playing the \((match["name"] as? String) ?? name) playlist."
        }
        if let hit = try await search(name, types: ["playlist"], limit: 3).first {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": hit.uri])
            return "Playing the \(hit.name) playlist (a public playlist)."
        }
        return "I didn't find anything for that on Spotify."
    }

    /// Spotify closed its recommendations endpoint to new apps, so "something like X" plays X's radio playlist,
    /// or X's own music when there isn't one.
    private func playLike(_ raw: String) async throws -> String {
        var seed = raw
        if ["this", "this song", "that", "that song", "it", "what's playing"].contains(raw.lowercased()) {
            guard let now = try await current() else { return "Spotify isn't playing anything right now." }
            seed = now.artist.components(separatedBy: ", ").first ?? now.artist
        }
        if let radio = try await search("\(seed) radio", types: ["playlist"], limit: 3).first(where: { fold($0.name).contains(fold(seed)) }) {
            try await control("PUT", "/v1/me/player/play", body: ["context_uri": radio.uri])
            return "Playing the \(radio.name) playlist."
        }
        let hits = try await search(seed, types: ["track", "artist"], limit: 5)
        if let track = hits.first(where: { $0.kind == "track" }) {
            try await playTrackAndMore(track)
            return "Playing \(track.name) by \(track.by), with more like it after."
        }
        return "I didn't find anything for that on Spotify."
    }
}
