import Foundation
import AVFoundation
import Contacts
import MessageUI
import UIKit
import SaintCore

/// What the phone itself can do, by voice. Everything iOS lets an app do is done directly (flashlight, brightness,
/// the camera, opening apps, Maps, battery). A call or a text is prepared and you confirm it in Apple's own
/// screen — iOS never lets an app send or dial by itself. Things only iOS controls (Low Power Mode, Wi-Fi,
/// Bluetooth, Do Not Disturb, Airplane Mode, dark mode) run a Shortcut you made once: "SAINT Low Power On" etc.
/// (Settings → Phone control lists them).
final class PhoneActions: PhoneService {
    weak var model: AppModel?

    /// Shortcut names SAINT runs for system switches: "SAINT Low Power On", "SAINT Wi-Fi Off", …
    static let systemShortcuts: [(name: String, title: String)] = [
        ("low power mode", "Low Power"), ("wi-fi", "Wi-Fi"), ("bluetooth", "Bluetooth"), ("do not disturb", "Do Not Disturb"),
        ("airplane mode", "Airplane Mode"), ("dark mode", "Dark Mode"), ("focus", "Focus"), ("hotspot", "Hotspot"),
    ]

    static func shortcutName(_ system: String, on: Bool?) -> String {
        let title = systemShortcuts.first { $0.name == system }?.title ?? system.capitalized
        guard let on = on else { return "SAINT \(title)" }
        return "SAINT \(title) \(on ? "On" : "Off")"
    }

    /// Apps SAINT can open by name (their URL schemes).
    static let apps: [String: String] = [
        "spotify": "spotify:", "instagram": "instagram://", "whatsapp": "whatsapp://", "youtube": "youtube://",
        "youtube music": "youtubemusic://", "tiktok": "snssdk1233://", "snapchat": "snapchat://", "x": "twitter://",
        "twitter": "twitter://", "facebook": "fb://", "messenger": "fb-messenger://", "discord": "discord://",
        "reddit": "reddit://", "netflix": "nflx://", "gmail": "googlegmail://", "google maps": "comgooglemaps://",
        "chrome": "googlechrome://", "google": "google://", "uber": "uber://", "zoom": "zoomus://", "teams": "msteams://",
        "slack": "slack://", "telegram": "tg://", "signal": "sgnl://", "linkedin": "linkedin://", "pinterest": "pinterest://",
        "twitch": "twitch://", "steam": "steam://", "outlook": "ms-outlook://", "amazon": "com.amazon.mobile.shopping://",
        "maps": "maps://", "apple maps": "maps://", "messages": "sms:", "mail": "message://", "phone": "tel://",
        "facetime": "facetime://", "music": "music://", "apple music": "music://", "podcasts": "podcasts://",
        "notes": "mobilenotes://", "calendar": "calshow://", "photos": "photos-redirect://", "app store": "itms-apps://",
        "shortcuts": "shortcuts://", "safari": "x-web-search://", "clock": "clock-alarm://", "files": "shareddocuments://",
        "settings": UIApplication.openSettingsURLString, "wallet": "shoebox://", "books": "ibooks://", "news": "applenews://",
        "weather": "weather://", "reminders": "x-apple-reminderkit://", "health": "x-apple-health://", "fitness": "fitnessapp://",
        "find my": "findmy://", "home": "com.apple.home://", "tv": "videos://", "camera": "",
    ]

    func perform(_ intent: PhoneIntent) async -> PhoneResult? {
        switch intent {
        case .takePhoto(let selfie):
            await present(CameraRequest(mode: .photo, front: selfie, autoShoot: true))
            return PhoneResult("Smile — taking a \(selfie ? "selfie" : "picture") in 3, 2, 1.")
        case .openCamera:
            await present(CameraRequest(mode: .photo, front: false, autoShoot: false))
            return PhoneResult("Camera's open.")
        case .recordVideo:
            await present(CameraRequest(mode: .video, front: false, autoShoot: false))
            return PhoneResult("Camera's ready — tap record.")
        case .flashlight(let on):
            return torch(on)
        case .brightness(let percent):
            await MainActor.run { Self.screen?.brightness = CGFloat(percent) / 100 }
            return PhoneResult("Brightness \(percent)%.")
        case .brightnessStep(let up):
            let now: Int = await MainActor.run {
                guard let screen = Self.screen else { return -1 }
                screen.brightness = min(1, max(0.05, screen.brightness + (up ? 0.2 : -0.2)))
                return Int((screen.brightness * 100).rounded())
            }
            return now < 0 ? PhoneResult("I can't change the brightness right now.", ok: false) : PhoneResult("Brightness \(now)%.")
        case .call(let who):
            return await call(who, facetime: false)
        case .facetime(let who):
            return await call(who, facetime: true)
        case .text(let raw):
            return await text(raw)
        case .openApp(let name):
            return await openApp(name)
        case .navigate(let place):
            let q = place.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? place
            return await open("maps://?daddr=\(q)") ? PhoneResult("Getting directions to \(place).")
                                                     : PhoneResult("I couldn't open Maps.", ok: false)
        case .battery:
            let (level, state): (Float, UIDevice.BatteryState) = await MainActor.run {
                UIDevice.current.isBatteryMonitoringEnabled = true
                return (UIDevice.current.batteryLevel, UIDevice.current.batteryState)
            }
            if level < 0 { return PhoneResult("I can't read the battery right now.", ok: false) }
            let pct = Int((level * 100).rounded())
            let charging = state == .charging ? ", charging" : (state == .full ? ", full" : "")
            let lowPower = ProcessInfo.processInfo.isLowPowerModeEnabled ? " Low Power Mode is on." : ""
            return PhoneResult("Your battery is at \(pct)%\(charging).\(lowPower)")
        case .system(let name, let on):
            return await runShortcut(PhoneActions.shortcutName(name, on: on), describe: describe(name, on))
        case .shortcut(let name):
            return await runShortcut(name, describe: "Running your “\(name)” shortcut.")
        case .openSettings:
            return await open(UIApplication.openSettingsURLString) ? PhoneResult("Opening Settings.") : nil
        case .webSearch(let q):
            let encoded = q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? q
            return await open("https://www.google.com/search?q=\(encoded)") ? PhoneResult("Searching for \(q).") : nil
        }
    }

    private func describe(_ name: String, _ on: Bool?) -> String {
        let title = PhoneActions.systemShortcuts.first { $0.name == name }?.title ?? name
        guard let on = on else { return "Switching \(title)." }
        return "Turning \(title) \(on ? "on" : "off")."
    }

    // MARK: helpers

    @MainActor private static var screen: UIScreen? {
        (UIApplication.shared.connectedScenes.first { $0.activationState == .foregroundActive } as? UIWindowScene)?.screen
            ?? (UIApplication.shared.connectedScenes.first as? UIWindowScene)?.screen
    }

    private func present(_ request: CameraRequest) async {
        await MainActor.run { model?.cameraRequest = request }
    }

    @discardableResult
    private func open(_ string: String) async -> Bool {
        guard let url = URL(string: string) else { return false }
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                UIApplication.shared.open(url, options: [:]) { ok in continuation.resume(returning: ok) }
            }
        }
    }

    private func torch(_ on: Bool?) -> PhoneResult {
        guard let device = AVCaptureDevice.default(for: .video), device.hasTorch else {
            return PhoneResult("This iPhone has no flashlight I can use.", ok: false)
        }
        do {
            try device.lockForConfiguration()
            let turnOn = on ?? (device.torchMode != .on)
            if turnOn { try device.setTorchModeOn(level: AVCaptureDevice.maxAvailableTorchLevel) } else { device.torchMode = .off }
            device.unlockForConfiguration()
            return PhoneResult(turnOn ? "Flashlight on." : "Flashlight off.")
        } catch {
            return PhoneResult("I couldn't switch the flashlight: \(error.localizedDescription)", ok: false)
        }
    }

    private func openApp(_ raw: String) async -> PhoneResult? {
        let name = raw.lowercased().replacingOccurrences(of: " app", with: "").trimmingCharacters(in: .whitespaces)
        if name == "camera" {
            await present(CameraRequest(mode: .photo, front: false, autoShoot: false))
            return PhoneResult("Camera's open.")
        }
        guard let scheme = PhoneActions.apps[name] else { return nil }          // not a phone app SAINT knows: maybe the PC
        if await open(scheme) { return PhoneResult("Opening \(raw.capitalized).") }
        return PhoneResult("\(raw.capitalized) doesn't seem to be installed.", ok: false)
    }

    private func runShortcut(_ name: String, describe: String) async -> PhoneResult {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? name
        let url = "shortcuts://x-callback-url/run-shortcut?name=\(encoded)&x-success=saint://shortcut-done&x-error=saint://shortcut-error"
        if await open(url) {
            await MainActor.run { model?.pendingShortcut = name }
            return PhoneResult(describe)
        }
        return PhoneResult("I couldn't open Shortcuts. iOS only lets apps change that through a Shortcut — "
                           + "make one called “\(name)” (Settings → Phone control explains how).", ok: false)
    }

    // MARK: contacts, calls and texts

    private func contactStore() async -> CNContactStore? {
        let store = CNContactStore()
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return store
        case .notDetermined:
            let ok = (try? await store.requestAccess(for: .contacts)) ?? false
            return ok ? store : nil
        default:
            if #available(iOS 18.0, *), CNContactStore.authorizationStatus(for: .contacts) == .limited { return store }
            return nil
        }
    }

    struct Person {
        var name: String
        var phone: String?
        var email: String?
    }

    /// Who is "mom" / "Gian" / "+1 555 0100"? (Nicknames and given names count.)
    private func person(_ spoken: String) async -> Person? {
        let digits = spoken.filter { $0.isNumber || $0 == "+" }
        if digits.count >= 5 { return Person(name: spoken, phone: digits, email: nil) }
        guard let store = await contactStore() else { return nil }
        let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactNicknameKey,
                                        CNContactPhoneNumbersKey, CNContactEmailAddressesKey] as [CNKeyDescriptor]
        let wanted = fold(spoken)
        var matches = (try? store.unifiedContacts(matching: CNContact.predicateForContacts(matchingName: spoken), keysToFetch: keys)) ?? []
        if matches.isEmpty {
            // "mom", "dad": usually a nickname or the name itself.
            var found: [CNContact] = []
            try? store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { c, stop in
                if [c.nickname, c.givenName, "\(c.givenName) \(c.familyName)"].map(fold).contains(wanted) {
                    found.append(c)
                    stop.pointee = true
                }
            }
            matches = found
        }
        guard let c = matches.first else { return nil }
        let name = [c.givenName, c.familyName].filter { !$0.isEmpty }.joined(separator: " ")
        let mobile = c.phoneNumbers.first { ($0.label ?? "").lowercased().contains("mobile") || $0.label == CNLabelPhoneNumberiPhone }
            ?? c.phoneNumbers.first
        return Person(name: name.isEmpty ? spoken : name, phone: mobile?.value.stringValue,
                      email: c.emailAddresses.first.map { String($0.value) })
    }

    private func call(_ who: String, facetime: Bool) async -> PhoneResult {
        guard let p = await person(who) else {
            return PhoneResult("I couldn't find \(who) in your contacts. Allow Contacts for SAINT, or say the number.", ok: false)
        }
        let target = (facetime ? (p.phone ?? p.email) : p.phone)?.filter { !$0.isWhitespace && $0 != "-" && $0 != "(" && $0 != ")" }
        guard let t = target, !t.isEmpty else { return PhoneResult("\(p.name) has no number I can call.", ok: false) }
        let ok = await open("\(facetime ? "facetime" : "tel")://\(t)")
        return ok ? PhoneResult("\(facetime ? "FaceTiming" : "Calling") \(p.name) — confirm on screen.")
                  : PhoneResult("This iPhone can't place that call.", ok: false)
    }

    /// "mom I'm on my way" → Mom, "I'm on my way": the longest start of the words that's a contact.
    private func text(_ raw: String) async -> PhoneResult {
        var words = raw.split(separator: " ").map(String.init)
        for sep in ["saying", "that says", "that", "to say"] where words.contains(sep) {
            if let i = words.firstIndex(of: sep), i > 0 { words.remove(at: i); break }
        }
        var person: Person?
        var body = ""
        for n in stride(from: min(3, words.count - 1), through: 1, by: -1) where person == nil {
            if let p = await self.person(words.prefix(n).joined(separator: " ")) {
                person = p
                body = words.dropFirst(n).joined(separator: " ")
            }
        }
        guard let p = person, let number = p.phone ?? p.email else {
            return PhoneResult("Who should I text? I couldn't find “\(words.first ?? raw)” in your contacts.", ok: false)
        }
        body = body.trimmingCharacters(in: CharacterSet(charactersIn: " :,"))
        if body.isEmpty { return PhoneResult("What should the message to \(p.name) say?", ok: false) }
        let draft = MessageDraft(recipient: number, name: p.name, body: Brain.sentence(body))
        let canCompose = await MainActor.run { MFMessageComposeViewController.canSendText() }
        if canCompose {
            await MainActor.run { model?.messageDraft = draft }
            return PhoneResult("Here's your text to \(p.name) — tap Send.")
        }
        let encoded = draft.body.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? draft.body
        return await open("sms:\(number)&body=\(encoded)") ? PhoneResult("Opening Messages for \(p.name) — tap Send.")
                                                         : PhoneResult("This iPhone can't send texts.", ok: false)
    }
}

// MARK: requests the screens present

struct CameraRequest: Identifiable, Equatable {
    enum Mode { case photo, video }
    let id = UUID()
    var mode: Mode
    var front: Bool
    var autoShoot: Bool
}

struct MessageDraft: Identifiable, Equatable {
    let id = UUID()
    var recipient: String
    var name: String
    var body: String
}
