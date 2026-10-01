# SAINT Link — how the devices talk

This is the design of the connection between SAINT on a PC (`modules/link` in the desktop repo) and SAINT on an iPhone
(`ios/Sources/SaintCore/Link`, `Sync`), and between a PC and a friend's PC. Two implementations, one protocol; the
known-answer vectors in `link_vectors.json` and the sync/time/language cases are checked by both test suites.

## Two modes

| | **Own devices** | **Collaborators** |
|---|---|---|
| Who | your PC, your other PCs, your phone | a friend's SAINT |
| Learned things | synced both ways (memories, skills, aliases, scenes, reminders, language settings) | never synced; one item can be *shared*, and the receiver chooses to keep it |
| Context ("what I just asked") | shared, memory only, 30 min | not shared |
| Control | `chat.ask` (full SAINT), files, status | a closed list of automations, each allow / ask / deny |
| Files | yes | yes (a Windows executable is saved as `.unsafe`) |

The mode is chosen at pairing, by which button made the code (*Pair a phone / my device* or *Add a friend*), and it is
checked inside the handshake — a device can't turn a friend pairing into an own-device one.

## Transport

TCP, default port 8765. Everything after a 4-byte preamble is length-framed:

```
connection start (clear):  'S' 'L' <version = 1> <mode>       mode 1 = reconnect (Noise IK), 2 = pairing (Noise XXpsk3)
frame:                     <uint16 big-endian length> <bytes>
handshake:                 2 (IK) or 3 (XXpsk3) Noise messages, one frame each; the payloads are JSON hellos
afterwards:                each frame is one ChaCha20-Poly1305 message; plaintext = <type byte> <body>
    0x01  a whole JSON message
    0x02  a JSON fragment: <flags: 1 = last> <bytes>        (messages over 60 000 bytes; up to 32 MiB in all)
    0x03  a file chunk:    <transfer id: 16> <offset: 8, big-endian> <bytes>        (32 KiB)
```

The Noise prologue is `"SAINT-LINK/1/" + <mode byte>`, so a connection can't be replayed in the other mode.

### Handshakes

- **Pairing — `Noise_XXpsk3_25519_ChaChaPoly_SHA256`.** The PC shows a one-time 128-bit code (26 base32 characters; the
  QR code holds `saint://pair?h=<ip>&p=<port>&t=<code>&r=<own|collaborator>&n=<name>`). The pre-shared key is
  `HKDF-SHA256(code, salt "SAINT-LINK-PAIRING", info "psk")`, mixed in at the third message. A wrong code fails the
  handshake without revealing anything usable. The window lasts five minutes, works once, and closes after ten failures. Each side
  learns the other's static public key and records it. The server then sends `pair.ok`.
- **Reconnect — `Noise_IK_25519_ChaChaPoly_SHA256`.** The initiator already knows the responder's static key (from pairing), so
  it can send its identity with the first message; the responder only answers if that key is in its list *and* the
  device id in the hello matches the key. Mutual authentication, forward secrecy.
- A device id is `SHA-256(public key)[:16 hex]`. An id can't be claimed without the key.
- Both phone and PC dial out; the iPhone app never listens. If both ends dial at once, the connection started by the
  lower device id is kept.
- Implementation notes: Noise revision 34. The desktop implements X25519, HKDF and ChaCha20-Poly1305 in the Python standard library
  plus numpy (and uses `cryptography` when installed), because Windows Application Control can block compiled packages; the
  iPhone uses CryptoKit. `tests/data/link_vectors.json` holds the shared known answers: RFC primitives, a full IK and a full
  XXpsk3 transcript (every message byte, and the first transport message each way), and the pairing-code → PSK derivation.

## Messages

```
request       {"t": "chat.ask", "id": 7, "d": {…}}
answer        {"re": 7, "ok": true,  "d": {…}}
              {"re": 7, "ok": false, "e": {"code": "denied", "msg": "…"}}
notification  {"t": "context.feed", "d": {…}}         (no id: no answer)
keepalive     {"t": "ping"} every 15 s; a connection silent for 50 s is closed
```

| type | from → to | what |
|---|---|---|
| `status.get` | any own device | device info, what's playing, language list |
| `chat.ask` `{text, lang}` → `{text, expects_reply, lang}` | own: phone → PC | runs the text through that SAINT's agent as if typed there; the answer comes back already in the user's language |
| `sync.manifest` `{manifest}` → `{want, items}` | own, either way | exchange manifests; the answer lists what the other side wants and the items it has that are newer |
| `sync.push` `{items}` → `{applied}` | own, either way | send items (batches of 150) |
| `context.feed` `{source, user, reply, ts}` | own, notification | what was just said on another device |
| `automation.list` / `automation.run` `{name, args}` | to a PC | `send_prompt`, `message`, `open_url`, `run_scene`, `play_music`, `ask`; validated, then permission-checked |
| `share.push` `{items}` | to a PC | one fact / skill / alias / scene offered to a collaborator (queued until they accept) |
| `file.offer` `{id, name, size, sha256}` → `{accept}` · chunks · `file.done` `{id}` · `file.cancel` | files | SHA-256 and size verified at the end; the name is flattened to its last path component; size and free-space limits |

### Sync

Every synced item has a stable uid. Each device keeps a *mirror* (uid → timestamp, origin device, content hash, deleted?). A
changed hash is a local edit; a missing item is a deletion, kept as a tombstone for 180 days; so no store has to announce
changes. Timestamps are a **hybrid logical clock**: they never run backwards and follow the largest timestamp seen, so a
phone with a wrong clock can't overwrite everything. Merging is **last-writer-wins per item**, ties broken on the device id.
Applying a remote item records its hash *as stored locally*, so it isn't mistaken for a local edit and echoed back.
Reminders sync as data (title, message, schedule, status); scheduled *commands* and the PC's own settings do not, because they
act on one machine.

| kind | uid | data |
|---|---|---|
| `memory` | 16-hex | content, key, value, category, how (told/learned), tags, confidence *(confidence is ignored when detecting edits)* |
| `skill` | 8-hex | phrase, steps[], how, said |
| `alias` | the phrase | target |
| `scene` | 8-hex | name, steps[], phrase *(the schedule stays on its PC)* |
| `reminder` | 8-hex | title, message, schedule, status, last_run |
| `settings` | `language` | preferred[], mixed_mode, reply_in_user_language |

## Permissions

Per paired device and per feature: **allow**, **ask** (the owner approves by voice — "yes" / "no" — or by button; it times out
after 60 s as a no), or **deny**. Features: `sync`, `chat`, `status`, `files`, `notify`, `context`, `share`, `auto.send_prompt`,
`auto.message`, `auto.open_url`, `auto.run_scene`, `auto.play_music`, `auto.ask`. Defaults: own devices allow everything;
collaborators may message and send files and *share*, are asked about prompts, links, music, questions and status, and are
denied sync, chat, context and scenes.

**The phone is stricter than the PC.** Because it only ever dials out, a collaborator that the phone connected to could
otherwise send requests back over that same connection. The iPhone app answers `sync.*`, `chat.*`, `status.*`, `file.*` and
`context.feed` only from *own* devices, and refuses everything else with `denied`.

## What the link doesn't do (yet)

- No relay: both devices must be able to reach each other's address. Tailscale or another VPN makes that work across networks.
- Collaborator automations run on the PC side; the phone can *send* them but doesn't accept them.
- The phone can't be reached by the PC unless the phone has dialled in — so a PC can push changes only while the app is open or
  listening. (iOS doesn't allow an always-on listening socket for a normal app; background audio keeps the outgoing connection alive.)
