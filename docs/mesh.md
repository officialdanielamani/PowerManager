# Mesh (ESP-NOW)

Meters that cannot reach Wi-Fi talk to the server through a nearby meter that can.
**One firmware** for every meter; the role is decided at run time.

```
 Leaf meter ──ESP-NOW──▶ Gateway meter ──Wi-Fi / WSS──▶ Server
 (no Wi-Fi)  ◀─────────  (online)       ◀──────────────
```

## Roles

| Setting (meter page → Settings → Mesh) | Behaviour |
|---|---|
| **Auto** (default) | Wi-Fi first. While online, the meter is also a **gateway**: it sends a beacon every 2 s and relays for up to 10 leaves. If Wi-Fi is down for **90 s** it becomes a **leaf** and looks for a gateway; every **10 min** it tries Wi-Fi again. |
| **Wi-Fi only** | Never relays, never uses the mesh. |
| **Mesh only** (setup: *Use mesh only (no Wi-Fi here)*) | Always a leaf. |

One hop only: leaf → gateway → server. A leaf never relays. Use 2–3 gateways for redundancy; a leaf takes the strongest
beacon (on core 3.x; core 2.0.x reports no signal strength, so it takes the first one heard) and searches again if its gateway
is silent for 10 s or 8 sends in a row fail.

## Security

- Every leaf message is wrapped as `{"t":"via","d":<leaf id>,"n":<counter>,"h":<HMAC>,"m":"<message>"}`.
  `h` = HMAC-SHA256 (first 128 bits) over `"<id>|<n>|<m>"` with the leaf's **link key**.
- The server checks the signature and that `n` only increases (no replays), then treats the message as if the leaf were
  connected directly. Its replies are signed the same way with a millisecond counter; the leaf checks them.
- So a gateway (or anyone listening to the radio) **cannot fake, change or replay** a leaf's data, and cannot send it
  commands. ESP-NOW frames are not encrypted, so readings can be *read* by a radio receiver nearby.
- The link key is issued at pairing. Meters paired before firmware 0.4 receive it over their own Wi-Fi connection the next
  time they connect directly (it is never sent through a gateway).
- Pairing a brand-new leaf through a gateway sends the leaf's credentials through that gateway once: only pair through
  meters you own.

## Radio details

- ESP-NOW only works on the **same channel** as the gateway's Wi-Fi router. A leaf scans channels 1–13 (≈ 4 s) for
  beacons, then moves its radio — and its own `Power_…` hotspot — to that channel. If a phone is connected to the
  hotspot during a search, the hotspot moves channel and the phone reconnects.
- Frames carry at most 250 bytes; longer messages are split and reassembled (max 4 KB). Telemetry is sent in batches of 8
  items over the mesh.
- The leaf's local page stays on its `Power_…` hotspot (it has no home-network address).

## Server

- Gateway messages `via` / `via_pair` are handled in `server/app/services/mesh.py`; each leaf gets a virtual connection, so
  commands, config, Get Data Now and Get meter info work as usual.
- A leaf goes offline after 75 s without messages, or immediately when its gateway disconnects (*Offline — gateway lost*).
- Meter Info shows **Connection: Mesh via <gateway>**; dashboard cards show *via <gateway>*.

## First test with your hardware

1. Flash firmware 0.4.0 to two meters; both connected to Wi-Fi and paired as usual (both show *Wi-Fi*).
2. On meter B's page → Settings → Wi-Fi → enter a wrong password (or switch the router off for B only). After ~90 s B
   should show *Mesh: looking for a gateway…*, then *Mesh via <A>*; on the server, Meter Info → Connection: *Mesh via A*.
3. Try Relay OFF/ON, Get Data Now and Get meter info on B from the server.
4. Power off A: B goes offline (*gateway lost*), keeps measuring and enforcing limits, buffers data, and catches up when A
   (or Wi-Fi) is back.
5. Mesh-only setup: factory-reset a meter, create the admin, press *Use mesh only*, then pair it from its own page
   (it pairs through a gateway).

The serial monitor shows `[mesh]` lines (gateway found, lost, dropped messages) that help if something does not work.
