# Getting SAINT (or any custom app) onto your iPhone

iPhones only run apps that are **signed**. For an app that isn't on the App Store that means signing it with
an Apple ID — yours — and that's what every route below does. Your phone already has **Developer Mode** on, which
is the one switch iOS requires before it will run a sideloaded app (Settings → Privacy & Security → Developer Mode).

Pick the route that matches what you have:

| You have… | Use | Cost | App lasts |
|---|---|---|---|
| A Windows PC, no Mac | **A. GitHub builds the app, Sideloadly installs it** | free | 7 days, then re-sign (automatic while your PC is on and reachable) |
| A Windows PC, want auto-refresh from the phone | **B. AltStore** | free | 7 days, refreshed by AltStore |
| A Mac | **C. Xcode** | free, or $99/year | 7 days free · 1 year paid |
| Nothing but a browser | **D. A cloud Mac** | a few dollars | same as C |

The same routes work for *any* iOS app you build later: you need its `.ipa` (routes A, B) or its Xcode project (C).

> Why not "just copy the app over"? iOS refuses unsigned code, and a build for iPhone can only be produced by Apple's
> compiler, which only runs on macOS. Route A gets a Mac for you for free, for a few minutes, from GitHub.

---

## A. Windows only: GitHub Actions + Sideloadly  (recommended to start)

### 1. Get the `.ipa`

The workflow in `.github/workflows/mobile-ios.yml` builds SAINT on a macOS machine GitHub lends you, runs the tests, and
attaches **`SaintMobile-unsigned.ipa`**. It runs on every push that touches `mobile/ios`.

1. Open the repository on GitHub → **Actions** → **iOS app** → pick the latest green run (or **Run workflow**).
   (Public repositories get free macOS minutes; on private ones GitHub's free allowance covers a few builds a month.)
2. When it finishes (about 10 minutes), scroll to **Artifacts** and download **SaintMobile-unsigned-ipa**. Unzip it.

If the build fails, the compiler's errors are shown as an annotation on the run (and in the `build-log` artifact).

### 2. Install the tools (once)

- **iTunes and iCloud for Windows — from Apple's website, not the Microsoft Store.** Sideloadly and AltStore
  talk to the phone through Apple's USB drivers, and the Store versions don't include what they need. If you already
  have the Store versions, uninstall them first. (On Apple's site, search for "download iTunes for Windows" and
  "download iCloud for Windows"; get the installers that aren't "Get from Microsoft Store".)
- **Sideloadly:** <https://sideloadly.io> → Windows download → install.

### 3. Install SAINT on the phone

1. Plug the iPhone in with a cable. Unlock it. Tap **Trust** on the phone, and enter your passcode. Open iTunes once so
   it sees the phone, then close it.
2. Open **Sideloadly**. Your iPhone appears at the top.
3. Drag `SaintMobile-unsigned.ipa` onto the Sideloadly window.
4. Type your **Apple ID** (a free one is enough; many people make a spare one for this). Press **Start**, enter the
   password, and the two-factor code if asked. Sideloadly sends your login to Apple only, to get a free signing
   certificate.
5. Wait for "Done". SAINT is now on your home screen.
6. On the phone: **Settings → General → VPN & Device Management → (your Apple ID) → Trust "…" → Trust.**
   Until you do this the app shows "Untrusted Developer".
7. Open SAINT. Allow the **microphone**, **speech recognition** and **notifications**; allow **Local Network** the first
   time it talks to your PC.

### 4. Keep it alive (the 7-day rule)

A free Apple ID's signature expires after **7 days**; the app then refuses to open until it's signed again. You can have
at most about **3 sideloaded apps** at a time (and 10 new App IDs per week).

- In Sideloadly, turn on its **automatic refresh** option (under Advanced options) and leave Sideloadly running in
  the tray. Make sure iTunes → your phone → **"Sync with this iPhone over Wi-Fi"** is ticked.
  Then, whenever the PC is on and on the same Wi-Fi as the phone, it re-signs the app before it expires.
- If it lapses, just plug in and press **Start** again. Your data (memories, reminders, pairings) stays — it's the same app.

---

## B. AltStore (Windows)

AltStore does the same signing, but the *phone* app tells your PC when to refresh, so it's more hands-off.

1. Install iTunes and iCloud from Apple's website (as above), then **AltServer for Windows**: <https://altstore.io>.
2. Run AltServer (it lives in the tray). Plug the phone in. Tray icon → **Install AltStore → your iPhone**, enter your Apple ID.
3. On the phone: Trust the profile (step 6 above). Open **AltStore**.
4. Put `SaintMobile-unsigned.ipa` somewhere the phone can open it (AirDrop isn't available from Windows; use iCloud
   Drive, OneDrive, or email it to yourself). In AltStore → **My Apps → +** → choose the file.
5. AltStore renews your apps in the background while AltServer is running and reachable on the same Wi-Fi.
   AltStore itself counts as one of your 3 apps.

---

## C. Mac + Xcode

```bash
brew install xcodegen
cd ios
xcodegen generate            # makes SaintMobile.xcodeproj
open SaintMobile.xcodeproj
```

In Xcode: select the **SaintMobile** target → **Signing & Capabilities** → **Team** → add your Apple ID (Xcode → Settings →
Accounts) and pick it. Plug in the iPhone, choose it as the run destination, press **Run**. First run: Trust the profile on
the phone (step 6 above). With a **free** account the app expires after 7 days (press Run again to renew); with a **paid**
Apple Developer Program account ($99/year) it lasts a year, you can use **TestFlight** to install builds without a cable, and
there's no 3-app limit.

To run the tests without a phone: `cd ios && swift test`.

---

## D. A cloud Mac

If you want Xcode but have no Mac, rent one by the hour (MacinCloud, AWS EC2 Mac, or Codemagic/Bitrise for builds) and follow
route C there, or use GitHub's macOS runners as in route A. A rented Mac can't plug into your phone, so you'd download the
`.ipa` and finish with Sideloadly.

---

## First-run checklist

1. **Settings → "Allow microphone, speech & notifications"** if you skipped it.
2. Say **"SAINT"**, then a command: "what time is it", "remind me in ten minutes to stretch", "pon música de Bad Bunny".
3. **Spotify:** make a free app at <https://developer.spotify.com/dashboard>, add the redirect URI `saint://spotify-callback`,
   paste its *Client ID* in **Settings → Spotify**, and sign in. Controlling playback needs Spotify Premium (Spotify's rule).
4. **Your PC:** on the PC open SAINT → **Devices** → turn on **Link** → **Pair a phone**. In the app: **Devices → +** and scan
   the QR code, or type the address and code. Both devices need to reach each other (same Wi-Fi, or a VPN such as Tailscale).

## What "always listening" really means on iOS

- **"SAINT" is SAINT's own wake word.** iOS doesn't let apps change *Siri's* wake phrase, so "Hey Siri" stays Siri's.
  SAINT listens for its name itself, using Apple's on-device speech recogniser, only while the app is running.
- **Keeping it running.** With *Always listening* on, SAINT holds an active microphone session; iOS treats that like a
  recorder and keeps the app alive in the background and with the screen locked (you'll see the orange microphone dot).
  That is the supported way to do it, but iOS can still stop an app (a phone call, Siri, low memory, a very long time, or
  swiping SAINT away). It resumes when you open it. It's best-effort, not a guarantee — for something you need every time,
  keep SAINT in the foreground (Settings → "Keep the screen on").
- **Battery.** Continuous listening costs battery — expect roughly the drain of playing audio. Turn *Always listening* off
  when you don't need it, or when charging only.
- **Siri and Shortcuts:** "Hey Siri, ask SAINT …" works, and **Start listening** can go on the Action button, Lock Screen or Back Tap.
- **Privacy.** Recognition runs on the phone where iOS supports it for your languages. Nothing is sent anywhere unless you
  ask a question SAINT forwards to *your* PC (encrypted, to your own paired device) or you turn on Claude in Settings.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Untrusted Developer" when opening | Settings → General → VPN & Device Management → Trust (step 6) |
| "Developer Mode required" | Settings → Privacy & Security → Developer Mode → On, restart |
| Sideloadly: "no device" | Unlock the phone, tap Trust, use a data cable, install iTunes from Apple's site (not the Store) |
| Sideloadly: "maximum number of apps / App IDs" | Free accounts allow ~3 apps and 10 new App IDs a week: delete one app, or wait, or use a second Apple ID |
| The app stops opening after a week | The 7-day signature ran out: re-run Sideloadly (or let auto-refresh do it) |
| SAINT never hears its name | Check the mic and speech permissions; try the extra wake words in Settings (a recogniser may write "SAINT" as "sent" or "seint"); say it at the start of a sentence |
| Can't find the PC | Settings → Local Network must be on for SAINT; same Wi-Fi; PC firewall allows Python on private networks; or type `IP:8765` |
| Wrong language answers | Settings → Languages: set your main and second language |
