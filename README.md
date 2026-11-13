# Meshcore-Bitchat-Bridge (MBB)

A bidirectional bridge between [Bitchat](https://bitchat.io) (Bluetooth LE mesh chat) and [MeshCore](https://meshcore.io) (LoRa mesh).

This project idea emerged during **french protests** during which cellular network was unavaible due to high number of people present in the street.
The aims of this bridge is to lets Bitchat users on their phones reach far beyond Bluetooth range by relaying chat over a LoRa mesh, and lets MeshCore users be answered from a phone with no LoRa radio at all.
Instead of relying on a patched firmware that needs to be updated frequently (cf. [jooray's work](https://github.com/jooray/MeshCore-BitChat)), I went with the same approach as [GigaProjects/meshtastic-bitchat-bridge](https://github.com/GigaProjects/meshtastic-bitchat-bridge) but for MeshCore instead of Meshtastic.

```text
┌──────────────┐  USB serial (companion protocol) ┌─────────────────────────┐  BLE / BlueZ    ┌──────────────┐
│  Heltec V3   │ ◄──────────────────────────────► │  Raspberry Pi           │ ◄─────────────► │ Bitchat apps │
│  MeshCore    │    /dev/ttyUSB0 or /dev/ttyACM0  │  meshcore-bitchat-      │  Pi = central   │  (phones)    │
│  companion   │                                  │  bridge                 │ phones = periph │              │
└──────────────┘                                  └─────────────────────────┘                 └──────────────┘
```

<div align="center">

[🌐 Website](https://pastabox.dev) • [✅ Requirements](#requirements) • [🚀 Install](#install) • [⚙️ Usage](#usage) • [🔒 Security Notes](#security-notes) • [❓ Troubleshooting](#troubleshooting)

[![stars](https://img.shields.io/github/stars/WildPasta/meshcore-bitchat-bridge?style=flat&logo=github)](https://github.com/WildPasta/meshcore-bitchat-bridge/stargazers)
[![open issues](https://img.shields.io/github/issues-raw/WildPasta/meshcore-bitchat-bridge?style=flat&logo=github)](https://github.com/WildPasta/meshcore-bitchat-bridge/issues)
[![license](https://img.shields.io/github/license/WildPasta/meshcore-bitchat-bridge?style=flat&logo=github)](https://github.com/WildPasta/meshcore-bitchat-bridge/blob/main/LICENSE)
[![code size](https://img.shields.io/github/languages/code-size/WildPasta/meshcore-bitchat-bridge?style=flat&logo=github)](https://github.com/WildPasta/meshcore-bitchat-bridge)

</div>

## How it works

Two directions, one bridge:

* **Bitchat → MeshCore:**
  The bridge keeps scanning BLE for devices advertising the Bitchat service, connects to them as a BLE central and performs the signed ANNOUNCE handshake.
  Broadcast chat messages from phones are relayed to the configured MeshCore hashtag channel as `bitchat/{nick}: {text}`.
* **MeshCore → Bitchat:**
  The bridge subscribes to channel messages on the LoRa board via the MeshCore companion protocol over USB serial and broadcasts them to connected phones as `[Mesh: {sender}] {text}`.
* **Loop prevention:**
  Without this, the bridge's own relays would come back around and bounce forever.
  Messages already carrying the `bitchat/` origin marker are never re-relayed, and duplicates (same channel + timestamp + text) are dropped within a 30-second receive-time window.

## Requirements

Nothing exotic, you can get started with:

* Raspberry Pi (tested target: RPi 3B+) or any Linux box with Bluetooth, running Python 3.11+
* A MeshCore **companion firmware** node connected by USB and [flashed](https://meshcore.io/flasher) **with radio_usb firmware**
* Bluetooth on the Pi must be enabled (`sudo systemctl status bluetooth`)

## Install

Two commands, then coffee:

```bash
git clone <this repo> && cd meshcore-bitchat-bridge
./install.sh
```

If you'd rather do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bridge.py --debug
```

### Bluetooth tweaks (important for iOS)

Here's the thing about iPhones: BlueZ, out of the box, makes iOS pop up **"Enter PIN to pair"** dialogs for a bridge that doesn't want any pairing at all.

1. Edit `/etc/bluetooth/main.conf`, `[General]` section:

   ```ini
   JustWorksRepairing = never
   ControllerMode = le
   Privacy = device
   ```

2. Disable the BlueZ battery plugin (it triggers pairing requests when it pokes phones for their battery level):

   ```bash
   sudo systemctl edit bluetooth.service
   ```

   add:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/libexec/bluetooth/bluetoothd -P battery
   ```

   (`/usr/lib/bluetooth/bluetoothd` on some distros), then:

   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart bluetooth
   ```

## Usage

For a first run, keep it in a terminal and watch it work:

```bash
.venv/bin/python bridge.py --debug
```

Options (CLI, or `~/.config/meshcore-bitchat-bridge/config.toml` — see `config.toml.example`):

| Option            | Default                             | Description                                             |
|-------------------|-------------------------------------|---------------------------------------------------------|
| `--port`          | auto (`ttyUSB0`, `ttyACM0`)         | serial port of the companion node                       |
| `--hashtag`       | `bitchat`                           | MeshCore hashtag channel to bridge (no `#`)             |
| `--nickname`      | `MeshCoreBridge`                    | Bitchat nickname of the bridge                          |
| `--state-dir`     | `~/.config/meshcore-bitchat-bridge` | persistent identity location                            |
| `--log-file`      | `<state-dir>/logs/bridge.log`       | where the persistent log lives                          |
| `--log-keep-days` | `14`                                | days of daily-rotated logs to keep (min 1)              |
| `--log-level`     | `info`                              | `debug`, `info`, `warning`, `error`, `critical`         |
| `--debug`         | off                                 | verbose logging (alias for `--log-level debug`)         |

### Logs

Everything printed to the console is mirrored to a log file (default `~/.config/meshcore-bitchat-bridge/logs/bridge.log`) — history survives reboots and power cuts even on hosts without a systemd journal.
The file rotates daily at midnight and rotated files are deleted after `--log-keep-days` (default 14), so it won't slowly eat your SD card.
Startup prints the resolved path — `Logging to … (keeping N days)`.
And if the log path happens to be unwritable, the bridge falls back to console-only with a warning instead of crashing.
Rotated files are gzipped in place, so two weeks of history costs a fraction of the disk.

### What startup looks like

On startup the bridge:

1. connects to the node and reads its capabilities,
2. looks for a channel slot whose secret matches `SHA256("#<hashtag>")[:16]` (the same derivation the MeshCore app uses), creating it in a free slot if needed,
3. starts scanning for Bitchat peers.

Then open the Bitchat app near the Pi — the bridge (`MeshCoreBridge`) appears as a peer.
Everyone in the same `#hashtag` channel on the LoRa mesh sees the relay; join that channel on your other MeshCore nodes to participate.

### Run at boot (systemd)

Once you trust it, let the Pi babysit it:

```bash
PORT=/dev/ttyUSB0 ./install.sh --systemd
sudo systemctl start meshcore-bitchat-bridge
journalctl -u meshcore-bitchat-bridge -f
```

The service runs as the installing user; make sure that user is in the `bluetooth` group (`sudo usermod -aG bluetooth $USER`, then re-login) — BLE access is group-gated, and the group change only applies to fresh logins.

### Uninstall

```bash
./uninstall.sh             # remove the systemd unit + virtualenv
./uninstall.sh --purge     # also delete ~/.config/meshcore-bitchat-bridge
                           # (bridge identity keys + config + logs — phones
                           #  will see a new peer if you reinstall later)
```

The uninstaller does not touch the manual Bluetooth tweaks above (group membership, `/etc/bluetooth/main.conf`, the `bluetoothd -P battery` override) nor this checkout itself — remove those by hand if desired.

## Message format

| Direction            | Format                                                    |
|----------------------|-----------------------------------------------------------|
| Bitchat → MeshCore   | `bitchat/{nick}: {text}`  (chunked `(1/N)` past 160 bytes)|
| MeshCore → Bitchat   | `[Mesh: {sender}] {text}` (chunked `(1/N)` past 99 bytes) |

Why the odd byte counts? Two different radios, two different limits.
MeshCore group messages carry the sender's display name inside the text
(unverified, by protocol design) — the bridge passes it through as-is.
Long messages are split to respect MeshCore's 160-byte text limit, and
again on the Bitchat side to stay under the official apps' 100-byte
compression threshold: the apps re-compress payloads before verifying
signatures, so a slightly-too-large uncompressed message would fail
verification on the phone — and nobody enjoys debugging that.

## Security notes

LoRa meshes are public airspace by nature, so it's worth knowing exactly what this bridge does and doesn't protect:

* The MeshCore `#hashtag` channel is encrypted on air, but its key is derived from the channel name — anyone can derive it too, so treat it as public.
  Use a private channel (configure the slot manually) if you need privacy.
  All relayed traffic lands on this one channel.
* Bitchat identity: the bridge keeps a persistent Ed25519 signing key and X25519 key in `~/.config/meshcore-bitchat-bridge/identity.json` (chmod 600) so phones recognise it across restarts.
  Its peer id is `SHA256(X25519 public key)[:8]` — derived from the Noise key exactly like the official apps (which enforce the binding on every ANNOUNCE); the Ed25519 key is used only to sign packets.
* **Broadcast-only relaying (enforced):** the bridge relays Bitchat broadcast (public) chat only.
  Private DMs (packets carrying a specific recipient id) are dropped with a debug log — they never reach LoRa.
  Your private conversations stay on the phone-to-phone Noise channels where they belong.
* **Signature verification (trust-on-first-ANNOUNCE):** every peer's Ed25519 signing key is learned from its signed ANNOUNCE packet (the key must hash to the peer's id, and the ANNOUNCE's own signature must verify).
  Afterwards, every MESSAGE from that peer is verified against the stored key; packets that fail verification are dropped and logged as possible spoofing, and messages from senders with no verified ANNOUNCE are dropped too.
  What this guarantees: a known sender id cannot be impersonated.
  What it does not: an attacker can still mint a *fresh* identity (new key whose hash matches its own id).

## Troubleshooting

Some failures I've hit so you don't have to:

* **No MeshCore response / "No companion response"** — wrong firmware (needs the USB companion build) or wrong port; check with `ls /dev/ttyUSB* /dev/ttyACM*`, and try `--port` explicitly.
* **Bluetooth stuck** (no discovery): `sudo systemctl restart bluetooth`; as a last resort `sudo hciconfig hci0 down && sudo hciconfig hci0 up`.
* **iOS PIN popups**: apply the BlueZ tweaks above.
* **Messages one way only**: check with `--debug` that the channel slot matches on all nodes (same `#hashtag`, same key derivation) and that the bridge dropped it for a reason in the logs (`bitchat/` marker, dedup).

## Credits

* [GigaProjects/meshtastic-bitchat-bridge](https://github.com/GigaProjects/meshtastic-bitchat-bridge) — the original Meshtastic↔Bitchat bridge this project is modeled on; the Bitchat wire-protocol implementation here is a port of theirs.
* [MeshCore-BitChat](https://github.com/jooray/MeshCore-BitChat) — firmware patch for Meshcore↔Bitchat bridge
* [kaganisildak/bitchat-python](https://github.com/kaganisildak/bitchat-python) — Bitchat protocol reference implementation in Python.
* [meshcore-dev/meshcore_py](https://github.com/meshcore-dev/meshcore_py) — official MeshCore Python library (companion protocol).
* [MeshCore](https://github.com/ripplebiz/MeshCore) and [Bitchat](https://github.com/bitchat/nostr-chat) by their respective authors.

## License

MIT
