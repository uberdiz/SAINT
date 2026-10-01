# SAINT Mobile

The iPhone app for [SAINT](https://github.com/uberdiz/SAINT): say **"SAINT"** and it listens — like "Hey Siri", but it's yours
and it knows your PC.

- **Always listening for "SAINT"** (on-device speech recognition, background audio so it keeps listening with the screen
  locked), with a push-to-talk orb and a text box.
- **Any language, even mixed.** Spanish, French, Portuguese, German and Italian as well as English — "pon música de Bad Bunny",
  "recuérdame to call mom at 5", "pon some jazz". It answers in the language you spoke, in the same mix if you mixed, and speaks each
  language with its own voice.
- **Everything the desktop assistant does for you on a phone:** reminders and timers (real notifications), Spotify, memory
  ("my favorite color is green"), skills you teach it, scenes, small talk, and open questions via your PC's AI, Apple's on-device
  model or Claude.
- **Linked to your PC.** What one learns, the other knows (both ways); "what did I just ask?" works on either; and you can run your PC
  from the phone: "SAINT, lock my PC", "SAINT, take a screenshot", "SAINT, play Rammstein on my PC".
- **Collaborators.** Pair with a friend's SAINT and say "send this prompt to Gian's PC on Claude: …", send them files, share a
  routine — within whatever *they* allow (each action runs, asks them first, or is refused).

```
mobile/
  ios/
    Package.swift           SaintCore: everything that isn't user interface (pure Swift, `swift test`)
      Sources/SaintCore/
        Link/               Noise IK/XXpsk3 handshakes, framing, pairing, LinkConnection, LinkManager (sync, files, asks)
        Sync/               last-writer-wins sync engine with tombstones and a hybrid logical clock
        Lang/               language detection, command translation, reply phrasebook, mixed-language handling
        Brain/              the on-device intent router (music, reminders, memory, PC and friends, model fallback)
        Reminders/          natural-language time parsing, schedules
        Store/              memory, skills, aliases, scenes, reminders, settings (each is a sync adapter)
        Voice/              wake-word matching
        Resources/Lexicon/  the language packs (shared with the desktop app)
      Tests/SaintCoreTests/ unit tests + the desktop's shared vectors and cases
    SaintMobile/            the SwiftUI app: voice engine, Spotify, notifications, views, App Intents
    project.yml             XcodeGen spec → SaintMobile.xcodeproj
    scripts/sync_shared.py  copy the language packs and test data from the desktop repo
  docs/
    IPHONE_SETUP.md         getting it onto your phone (Windows without a Mac, AltStore, Xcode)
    LINK.md                 the protocol and the security model
(.github/workflows/mobile-ios.yml, at the repo root, builds, tests, and attaches an unsigned .ipa)
```

## Get it on your phone

See **[docs/IPHONE_SETUP.md](docs/IPHONE_SETUP.md)**. Short version for a Windows PC: GitHub builds it for you (Actions → **iOS app**),
you download the `.ipa`, and install it with **Sideloadly** using your Apple ID.

## How it fits with the desktop app

The desktop side — the encrypted Link (`modules/link`), the sync adapters, the language layer (`modules/lang`), the Devices
page — is in the rest of this repository. The phone and the PC share their language packs and test data: run
`python mobile/ios/scripts/sync_shared.py` after changing either.

## Honest status

It builds, and its tests pass on a macOS runner (Xcode 16, run 36861798812 on the `ios-app` branch): the whole of
SaintCore compiles, its test suite is green, and the SwiftUI app compiles into an unsigned `.ipa`. It has **not been run on a phone
yet** — that is the next step, and the first launch will likely show things only a device reveals (permissions, audio routing,
layout).

- *Tested:* the desktop (Python) side, about 220 tests (the Noise handshakes are checked byte for byte against an independent
  implementation). The shared vectors and cases from that side run in SaintCore's tests too: crypto and Noise known answers, the
  language and time cases, the router, stores, sync between two simulated devices, and the link against a stand-in PC in memory.
- *Not covered by any automated test:* the SwiftUI screens, the microphone / speech recogniser / wake-word engine, text-to-speech,
  Spotify, notifications, Bonjour discovery, QR scanning — all of which need a real device.
- *Known limits of iOS, not bugs:* Siri's own wake phrase can't be changed (SAINT's word works inside SAINT); background listening is
  best-effort; the phone dials out and never listens; Spotify playback control needs Premium and a developer client ID.

## Developing

```bash
cd ios
swift test                       # SaintCore tests (macOS or Linux with a Swift toolchain)
brew install xcodegen && xcodegen generate && open SaintMobile.xcodeproj    # the app
```

Requirements: iOS 17+, Xcode 16+ (Liquid Glass and the on-device model light up automatically with Xcode 26 / iOS 26).
