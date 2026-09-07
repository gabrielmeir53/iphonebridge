<div align="center">

# 📱 iphonebridge

**Your iPhone's messages, calls, notifications, and contacts — on your Linux desktop, over Bluetooth.**

[![CI](https://github.com/gabrielmeir53/iphonebridge/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielmeir53/iphonebridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/gabrielmeir53/iphonebridge?color=brightgreen)](https://github.com/gabrielmeir53/iphonebridge/releases)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux%20%2F%20GNOME-lightgrey.svg)](#requirements)

*No Mac relay. No cloud service. No subscription. Just Bluetooth.*

</div>

---

Microsoft's **Phone Link** gives Windows users their iPhone's texts and notifications on the desktop. There has never been a Linux equivalent — KDE Connect needs the Android/iOS *app* and only does Wi-Fi, `ancs4linux` does notifications only, Mac-relay bridges (BlueBubbles, AirMessage) need an actual Mac, and Beeper costs money.

**iphonebridge is that missing piece.** It's a small Python daemon that talks to a paired iPhone over standard Bluetooth profiles (MAP, PBAP, ANCS, HFP) and surfaces everything as native desktop notifications, a CLI, and a GTK4 desktop app.

## ✨ What it does

| Feature | How | Status |
|---|---|---|
| 📨 **Incoming SMS + iMessage** as desktop notifications | MAP MNS push | ✅ |
| 📤 **Send SMS + iMessage** from the CLI or app | MAP `PushMessage` | ✅ |
| 📋 **Verification codes auto-copied** to the clipboard | OTP detection | ✅ |
| 👤 **Contact-name resolution** (1000s of contacts) | PBAP → SQLite cache | ✅ |
| 🔔 **Every app's notifications** — Slack, WhatsApp, Mail… | ANCS over BLE | ✅ |
| 📞 **Take & place phone calls** — caller ID, answer/decline, dial | HFP via oFono | ✅ |
| 🔁 **Read-state sync** — read on either device, syncs to both | MAP read-state writes | ✅ |
| 📜 **Message history** — incoming + your desktop replies | `sms-list` / the app | ✅ |
| 🖥️ **Desktop app** — conversations, notification feed, call UI | GTK4 / libadwaita | ✅ |
| ⚙️ Runs unattended as a **systemd user service** | — | ✅ |

### 🤯 The iMessage surprise

Every prior writeup of Bluetooth on iOS says **iMessage is invisible** to a paired computer — that you *must* use a Mac relay to bridge blue-bubble messages.

**That is not true on iOS 26.5.** iphonebridge receives *and sends* iMessage through the standard MAP Bluetooth profile, with no Mac, no Apple ID login, nothing. iOS labels iMessage and SMS identically (`Type: sms-gsm`) and exposes both. Outgoing messages route as iMessage automatically when the recipient is iMessage-capable.

As far as we know, **iphonebridge is the first free, open-source, Mac-free iMessage bridge for Linux.** The empirical proof is in [`spike/RESULTS.md`](spike/RESULTS.md) §6.

## 📋 Requirements

| | Minimum | Tested with |
|---|---|---|
| **OS** | Linux + GNOME, BlueZ 5.72+ | Pop!_OS 24.04 |
| **Bluetooth adapter** | Intel chipset (for ANCS) | Intel AX-series |
| **Python** | 3.10+ | 3.12 |
| **iPhone** | iOS 16.5+ | iPhone 16 Pro Max, iOS 26.5 |
| **System packages** | `bluez`, `bluez-obexd`, `ofono`, `python3-dbus`, `python3-gi` (+ `wl-clipboard` for code auto-copy) | — |

> ⚠️ **Adapter chipset matters for ANCS.** Per-app notifications need a real BLE bond with the iPhone. Intel adapters do this reliably. **Realtek adapters and every USB Bluetooth dongle tested so far do *not*** — their firmware negotiates legacy keys that block the cross-transport key derivation iOS needs. SMS/iMessage/contacts (MAP/PBAP) work on any adapter; only ANCS is picky. See [bmh129/ancs4linux's hardware notes](https://github.com/bmh129/ancs4linux).

## 🚀 Installation

### 1 · System packages

```bash
sudo apt install bluez bluez-obexd ofono python3-dbus python3-gi python3-venv
# For the desktop app (iphonebridge-ui):
sudo apt install gir1.2-gtk-4.0 gir1.2-adw-1
# For auto-copying verification codes (Wayland):
sudo apt install wl-clipboard
```

### 2 · Clone & install

```bash
git clone https://github.com/gabrielmeir53/iphonebridge.git
cd iphonebridge

# A venv that inherits the system PyGObject + dbus-python.
# (Never install those two from PyPI — the builds are notoriously fragile.)
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .

# Put `iphonebridge` (CLI + daemon) and `iphonebridge-ui` (app) on your PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/.venv/bin/iphonebridge" ~/.local/bin/iphonebridge
ln -sf "$(pwd)/.venv/bin/iphonebridge-ui" ~/.local/bin/iphonebridge-ui
```

### 3 · Prepare and start the daemon

```bash
mkdir -p ~/.config/systemd/user
cp systemd/iphonebridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iphonebridge
```

The daemon must be running before pairing so the adapter presents the
A/V Hands-Free class and advertises the services that make the iPhone's
message and contact toggles appear.

### 4 · Pair your iPhone

Pair normally — GNOME **Settings → Bluetooth**, or `bluetoothctl`. Then run the wizard:

```bash
iphonebridge pair-setup
```

It finds your iPhone among paired devices, writes
`~/.config/iphonebridge/local.env`, and offers to restart the daemon with the
new device configuration.

### 5 · iPhone-side toggles

On the iPhone: **Settings → Bluetooth → tap the ⓘ next to your computer →** enable

- **Show Message Notifications** — gates SMS/iMessage (MAP)
- **Sync Contacts** — gates contacts (PBAP)
- **Show System Notifications** — gates per-app notifications (ANCS)

> These toggles only appear once the daemon has run at least once (it sets the adapter's Bluetooth class + advertises correctly). If you don't see them, re-run `iphonebridge pair-setup` and restart the daemon.

<details>
<summary><b>6 · (Optional) Enable per-app notifications — ANCS</b></summary>

ANCS needs a true BLE bond, which only forms during a fresh pairing while the adapter is correctly configured. One-time setup:

```bash
# Install the privileged helper (writes one specific BlueZ setting)
sudo bash systemd/install-ancs-sudoers.sh

# Apply it and re-pair
iphonebridge ancs-enable
```

Then **forget + re-pair** the iPhone one more time (the wizard walks you through it). After the fresh pair, iOS performs cross-transport key derivation and the BLE bond sticks — ANCS notifications start flowing automatically. You only do this once.

</details>

<details>
<summary><b>7 · (Optional) Enable phone calls — HFP</b></summary>

To take and place calls on the laptop, iphonebridge uses **oFono** for HFP
call control and PipeWire's oFono backend for the call audio.

```bash
# Enable the oFono service installed with the system packages above.
sudo systemctl enable --now ofono

# Write the WirePlumber config + print the remaining steps
iphonebridge hfp-enable
```

`hfp-enable` writes `~/.config/wireplumber/wireplumber.conf.d/51-bluez-hfp-hf.conf`
(routing HFP through oFono) and restarts WirePlumber. Follow its printed
steps — restart oFono **after** WirePlumber so it can claim the HFP profile,
reconnect the iPhone, restart the daemon — and incoming calls will pop up
with **Answer / Decline** buttons. Place calls with `iphonebridge call`.

</details>

<details>
<summary><b>(Optional) Persist the Bluetooth class across reboots</b></summary>

```bash
sudo bash systemd/install-cod-sudoers.sh
```

Lets the daemon set the adapter's Class-of-Device on every start without a password prompt. Without it you'd occasionally need to re-run setup after a reboot.

</details>

## 🖥️ Desktop app

`iphonebridge-ui` is a GTK4 / libadwaita app — a separate process from the daemon, talking to it over D-Bus, so you can open and close it freely while the daemon keeps running in the background. Four tabs:

- **Messages** — SMS & iMessage conversations grouped by contact. Read history and reply from a compose box; the replies you send are saved into the thread.
- **Notifications** — a live feed of every app's notifications (Slack, Mail, WhatsApp…), mirrored from the iPhone over ANCS.
- **Calls** — a dialer to place calls, plus Answer / Hang-up controls for active ones; an incoming call raises this tab automatically.
- **Setup** — daemon health, contact and message counts, and the iPhone-toggle checklist.

```bash
iphonebridge-ui
```

## 💻 CLI

The `iphonebridge` command does everything the app does, plus setup and diagnostics:

| Command | What it does |
|---|---|
| `iphonebridge run` | Run the daemon in the foreground (the systemd service uses this) |
| `iphonebridge doctor` | Check prerequisites — config, adapter class, obexd, state dir |
| `iphonebridge pair-setup` | First-run wizard — find the paired iPhone, write config |
| `iphonebridge sms-list` | Recent messages — `-n N`, `--from <contact>`, `--source iphone\|local` |
| `iphonebridge sms-send <to> <body>` | Send an SMS / iMessage (`<to>` = number or contact name) |
| `iphonebridge call <to>` | Place a phone call over HFP |
| `iphonebridge calls` | List active calls |
| `iphonebridge hangup` | Hang up the active call(s) |
| `iphonebridge contacts-sync` | Force a contacts refresh (otherwise automatic every 24 h) |
| `iphonebridge ancs-enable` | One-time setup for per-app notifications (ANCS) |
| `iphonebridge hfp-enable` | One-time setup for phone calls (HFP) |
| `iphonebridge version` | Print the version |

```bash
# Recent messages — live from the iPhone, or from the daemon's own log
iphonebridge sms-list -n 20
iphonebridge sms-list --from Maddie
iphonebridge sms-list --source local

# Send — recipient can be a phone number OR a contact name
iphonebridge sms-send "+15551234567" "on my way"
iphonebridge sms-send Maddie "running late"

# Calls — needs HFP set up (install step 7)
iphonebridge call Maddie
iphonebridge calls
iphonebridge hangup

# Watch the daemon live · control the service
journalctl --user -u iphonebridge -f
systemctl --user {start,stop,restart} iphonebridge
```

## 🔔 How it behaves

- **Incoming messages** appear as **persistent notifications** — they stay until you dismiss them on the desktop *or* read the message on your iPhone. **Read-state syncs both ways.**
- **Verification codes** — when a text carries a one-time / 2FA code, iphonebridge detects it and copies it to your clipboard automatically; press <kbd>Ctrl</kbd>+<kbd>V</kbd> to paste. Detection needs both a verification keyword and a 4–8 digit number, so ordinary texts don't trigger it.
- **Incoming calls** raise a notification with **Answer / Decline** buttons that act on the call directly.
- **Sent messages** — replies you send from the desktop are recorded into conversation history, so a thread shows both sides.

## 🏗️ How it works

```
              iPhone  (paired: BR/EDR + BLE)
   ┌──────────┬──────────┬───────────┬──────────┐
   │ MAP      │ PBAP     │ ANCS      │ HFP      │
   │ (OBEX)   │ (OBEX)   │ (BLE GATT)│ (oFono)  │
   ▼          ▼          ▼           ▼
 messages   contacts   app notifs   calls
   └──────────┴──────────┴───────────┴──────────┘
                     │
           iphonebridge daemon
         (Python · GLib · D-Bus)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  notifications   JSONL log   D-Bus service
  + clipboard     (history)   (CLI · GTK app)
```

- **MAP** (Message Access Profile) — read SMS/iMessage, get real-time push of new ones, and send.
- **PBAP** (Phone Book Access Profile) — pull the iPhone's contacts so messages show names, not numbers.
- **ANCS** (Apple Notification Center Service) — every app's notifications, over a BLE GATT link.
- **HFP** (Hands-Free Profile) — take and place calls; oFono speaks the HFP protocol, PipeWire's oFono backend carries the call audio to the laptop's mic/speakers.
- One daemon, pluggable **sinks** (desktop popups, verification-code clipboard copy, append-only JSONL log), and a **D-Bus service** so the CLI and the GTK app can send messages, control calls, and subscribe to a live event feed.

Design rationale and the empirical Bluetooth findings that shaped it are in [`spike/RESULTS.md`](spike/RESULTS.md).

## 🩺 Troubleshooting

<details>
<summary><b>Messages stopped arriving</b></summary>

The iPhone times out OBEX sessions. Restart the daemon:
```bash
systemctl --user restart iphonebridge
```
If the iOS toggles vanished from Bluetooth settings, forget + re-pair the iPhone.
</details>

<details>
<summary><b><code>Forbidden</code> errors in the log</b></summary>

An iPhone toggle is off. Check **Settings → Bluetooth → ⓘ → Show Message Notifications / Sync Contacts / Show System Notifications**.
</details>

<details>
<summary><b>ANCS notifications never arrive</b></summary>

ANCS needs a BLE bond, which needs a fresh pair done with the adapter correctly set up. Run `iphonebridge ancs-enable`, then forget + re-pair the iPhone. Also confirm your adapter is Intel — Realtek and USB dongles can't do it.
</details>

<details>
<summary><b>Calls don't connect, or there's no call audio</b></summary>

HFP needs oFono, and oFono must start *after* WirePlumber so it can claim the HFP profile. Run `iphonebridge hfp-enable`, then `sudo systemctl restart ofono`, reconnect the iPhone, and restart the daemon. If `journalctl -u ofono` shows `RegisterProfile … UUID already registered`, the start order is wrong — restart oFono again after WirePlumber is up.
</details>

<details>
<summary><b>Verification codes aren't being copied</b></summary>

Install a clipboard tool: `sudo apt install wl-clipboard` (Wayland) or `xclip` (X11). The daemon log shows `no clipboard tool worked` when none is present.
</details>

<details>
<summary><b><code>iphonebridge: command not found</code></b></summary>

The CLI lives in the venv. Either `source .venv/bin/activate`, or create the `~/.local/bin` symlink from install step 2.
</details>

## 🚧 Limitations

These are Apple's Bluetooth-stack limits, not bugs:

- No iMessage **attachments, reactions, read receipts, or typing indicators** (MAP doesn't carry them).
- No **group iMessage / MMS / RCS** — MAP is 1-to-1 only.
- **Messages composed on the iPhone itself don't sync** — iOS exposes only your *inbox* over MAP, never the sent folder. Replies you send *from* iphonebridge are recorded into conversation history; texts you type on the phone aren't visible to any Bluetooth bridge.
- HFP calls are **1-to-1 voice only** — no conference calls, no FaceTime (HFP carries neither).
- Notification *bodies* are subject to the iPhone's "Show Previews" setting.

## 🗺️ Roadmap

- **Flatpak** for the UI — a draft manifest lives in [`packaging/flatpak/`](packaging/flatpak/); it still needs a build pass.

See [`BACKLOG.md`](BACKLOG.md).

## 🙏 Credits

iphonebridge stands on the shoulders of two prior projects, both GPL-2.0:

- **[bmh129/ancs4linux](https://github.com/bmh129/ancs4linux)** — an actively-maintained 2026 fork whose empirical work on BR/EDR-vs-BLE coexistence, the `LastUsedBearer=le` unlock, and adapter compatibility made iphonebridge's ANCS support possible. The ANCS wire-format code in [`src/iphonebridge/ancs/`](src/iphonebridge/ancs/) is derived from their `observer/ancs/` modules.
- **[pzmarzly/ancs4linux](https://github.com/pzmarzly/ancs4linux)** — the original 2022 reference implementation of ANCS on Linux.

## 📄 License

[GPL-2.0-or-later](LICENSE) · © 2026 Gabe Shatunovsky
