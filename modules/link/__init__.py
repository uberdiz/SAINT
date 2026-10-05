"""
SAINT Link: connect your devices (and other people's SAINTs) over IP:port.

    identity.py     this device's key, the paired devices and what each may do, pairing codes
    crypto.py       X25519 / HKDF / ChaCha20-Poly1305 (pure Python; uses `cryptography` if present)
    noise.py        the Noise handshakes: IK (reconnect) and XXpsk3 (pairing)
    wire.py         framing, the encrypted channel, handshake drivers
    node.py         server, dialling, sessions, request/response, permissions
    files.py        chunked, hash-checked file transfer into an inbox
    sync.py         last-writer-wins sync of memories, skills, aliases, scenes, reminders
    automations.py  the closed list of things a collaborator can ask this PC to do
    approvals.py    "Gian wants to ... Allow?" by voice or on screen
    context_feed.py what you just did on your other devices
    discovery.py    mDNS + UDP beacon
    service.py      the one object the rest of SAINT uses

See docs/LINK.md.
"""
