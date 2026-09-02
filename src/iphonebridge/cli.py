"""Typer CLI entrypoints."""
from __future__ import annotations

import logging
import os
import sys

import typer

from iphonebridge import bluez_setup, config

app = typer.Typer(
    add_completion=False,
    help="iPhone ↔ Linux Bluetooth bridge for Pop!_OS.",
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


@app.command()
def run(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Start the iphonebridge daemon (runs until Ctrl+C / SIGTERM)."""
    _setup_logging(verbose)
    # Import inside command to avoid loading dbus stack just to print --help
    from iphonebridge.daemon import Daemon
    Daemon().run()


@app.command()
def doctor(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Check that all prerequisites are in place."""
    _setup_logging(verbose)
    log = logging.getLogger("doctor")

    ok = True

    # IPHONEBRIDGE_MAC configured?
    if config.IPHONE_MAC.upper() in ("AA:BB:CC:DD:EE:FF", ""):
        log.error("IPHONEBRIDGE_MAC not configured (still the placeholder).")
        log.error("    Set your iPhone's Bluetooth MAC via env var, e.g.:")
        log.error("    export IPHONEBRIDGE_MAC=AA:BB:CC:DD:EE:FF")
        log.error("    Or persist it in ~/.config/iphonebridge/local.env")
        log.error("    (see README — 'Setup'). The systemd unit picks it up.")
        ok = False
    else:
        log.info("Target MAC configured: %s", config.IPHONE_MAC)

    # bluez-obexd present? (Debian/Ubuntu/Fedora use libexec, Arch/openSUSE lib)
    obexd_paths = ("/usr/libexec/bluetooth/obexd", "/usr/lib/bluetooth/obexd")
    obexd_path = next((p for p in obexd_paths if os.path.exists(p)), None)
    if obexd_path is None:
        log.error("bluez-obexd binary not found in any of: %s",
                  ", ".join(obexd_paths))
        log.error("    → Debian/Ubuntu: sudo apt install bluez-obexd")
        log.error("    → Arch: sudo pacman -S bluez-obex")
        ok = False
    else:
        log.info("bluez-obexd installed (%s)", obexd_path)

    # Adapter CoD
    cod = bluez_setup.current_cod()
    if cod is None:
        log.error("Adapter %s not reachable via DBus", config.ADAPTER)
        ok = False
    else:
        match = bluez_setup.desired_cod_matches(cod)
        if match:
            log.info("Adapter CoD = 0x%06x (A/V Hands-Free)  OK", cod)
        else:
            log.warning("Adapter CoD = 0x%06x — not A/V Hands-Free. "
                        "Run `iphonebridge run` (needs sudo) or set manually:",
                        cod)
            log.warning("    sudo btmgmt class %d %d",
                        config.COD_MAJOR, config.COD_MINOR)
            ok = False

    # State dir writable
    try:
        config.ensure_dirs()
        log.info("State dir writable: %s", config.STATE_DIR)
    except OSError as e:
        log.error("State dir not writable: %s", e)
        ok = False

    if ok:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))
    else:
        typer.echo(typer.style("One or more checks FAILED.",
                               fg=typer.colors.RED))
        raise typer.Exit(code=1)


@app.command()
def contacts_sync(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Force a fresh PBAP pull from the iPhone (rebuilds the contacts cache)."""
    _setup_logging(verbose)
    # Heavyweight — needs sessions
    from iphonebridge.contacts import pull_phonebook
    from iphonebridge.obex.sessions import SessionManager
    sm = SessionManager()
    sm.open_all()
    try:
        n = pull_phonebook(sm)
        typer.echo(f"Pulled {n} contacts into {config.CONTACTS_DB}")
    finally:
        sm.close_all()


@app.command("sms-list")
def sms_list(
    n: int = typer.Option(20, "-n", "--limit", help="Max messages to show (most recent first)"),
    source: str = typer.Option("iphone", "--source",
                               help="iphone (live MAP query) | local (JSONL log)"),
    folder: str = typer.Option("telecom/msg/INBOX", "--folder",
                               help="MAP folder when --source=iphone "
                                    "(e.g. telecom/msg/INBOX or telecom/msg/sent)"),
    from_contact: str = typer.Option(None, "--from",
                                     help="Only show messages from this contact name or phone"),
):
    """Show recent SMS / iMessage history.

    --source iphone (default): live MAP query via the running daemon. Shows
        the iPhone's actual recent inbox (or other folder via --folder).
    --source local: read ~/.local/state/iphonebridge/events.jsonl. Only
        shows events the daemon has caught since startup, but works even
        if the daemon isn't running.
    """
    import json
    from datetime import datetime

    from iphonebridge.events import normalize_phone

    # ---- build a from-filter predicate from --from ----------------------
    # Two layers:
    #   • filter_phone_norms — for events that have a phone digit string
    #   • from_text_lower    — for events where MAP returned only the
    #                          contact's FN as sender (no phone)
    filter_phone_norms: set[str] = set()
    from_text_lower: str | None = None

    if from_contact:
        if _RECIPIENT_LOOKS_LIKE_PHONE.match(from_contact):
            norm = normalize_phone(from_contact) or ""
            filter_phone_norms = {norm, norm[-10:]} if len(norm) >= 10 else {norm}
        else:
            from iphonebridge.contacts import ContactsResolver
            matches = ContactsResolver().find_by_name(from_contact)
            if not matches:
                typer.echo(typer.style(
                    f"No contact matched {from_contact!r}.",
                    fg=typer.colors.YELLOW))
                raise typer.Exit(code=1)
            for _, phone in matches:
                filter_phone_norms.add(phone)
                if len(phone) >= 10:
                    filter_phone_norms.add(phone[-10:])
            from_text_lower = from_contact.lower()

    def passes_from_filter(e: dict) -> bool:
        if not filter_phone_norms and not from_text_lower:
            return True
        # Phone match (for entries with real phone digits)
        sp = (e.get("sender_phone_norm") or "")
        if sp:
            sp_tail = sp[-10:] if len(sp) >= 10 else sp
            if sp in filter_phone_norms or sp_tail in filter_phone_norms:
                return True
        # Substring match against raw sender (for FN-only entries)
        if from_text_lower:
            raw = (e.get("sender") or e.get("sender_phone") or "").lower()
            if from_text_lower in raw:
                return True
        return False

    # ---- when filtering, pull a wider net from MAP ----------------------
    fetch_n = max(n * 10, 100) if (filter_phone_norms or from_text_lower) else n

    # ---- helper to render a record ---------------------------------------
    def render(sender: str, body: str, ts_str: str, *, read: bool = True) -> None:
        if len(body) > 120:
            body = body[:119] + "…"
        body = body.replace("\n", " ⏎ ")
        sender_styled = typer.style(f"{sender:>20s}",
                                    fg=typer.colors.CYAN, bold=True)
        ts_styled = typer.style(ts_str, dim=True)
        unread = typer.style("•", fg=typer.colors.YELLOW) if not read else " "
        typer.echo(f"{ts_styled}  {unread} {sender_styled}  {body}")

    # ---- live MAP source ------------------------------------------------
    if source == "iphone":
        import dbus
        import dbus.mainloop.glib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        try:
            proxy = bus.get_object("com.gabriel.iphonebridge",
                                   "/com/gabriel/iphonebridge")
            iface = dbus.Interface(proxy, "com.gabriel.iphonebridge.Messages1")
        except dbus.exceptions.DBusException as e:
            typer.echo(typer.style(
                f"Daemon not reachable on DBus: {e.get_dbus_message()}\n"
                "Falling back to local JSONL (--source local).",
                fg=typer.colors.YELLOW,
            ))
            source = "local"
        else:
            try:
                raw = str(iface.ListRecent(folder, dbus.UInt32(fetch_n), timeout=30))
            except dbus.exceptions.DBusException as e:
                typer.echo(typer.style(
                    f"Live query failed: {e.get_dbus_message() or e.get_dbus_name()}\n"
                    "Falling back to local JSONL.",
                    fg=typer.colors.YELLOW,
                ))
                source = "local"
            else:
                msgs = json.loads(raw)
                if filter_phone_norms or from_text_lower:
                    msgs = [m for m in msgs if passes_from_filter(m)]
                msgs = msgs[:n]
                if not msgs:
                    if filter_phone_norms or from_text_lower:
                        typer.echo(typer.style(
                            "(no recent messages from that contact in the "
                            "iPhone's MAP inbox window)", fg=typer.colors.YELLOW))
                        typer.echo("iOS only exposes a small slice of recent "
                                   "messages via MAP. For older history, try:")
                        typer.echo(typer.style(
                            f"  iphonebridge sms-list --from {from_contact!r} "
                            f"--source local -n {n}",
                            fg=typer.colors.WHITE))
                    else:
                        typer.echo("(no messages)")
                    return
                # Resolve contact names via local cache
                from iphonebridge.contacts import ContactsResolver
                resolver = ContactsResolver()
                for m in msgs:
                    contact = resolver.resolve(m.get("sender") or
                                               m.get("sender_phone_norm"))
                    sender = contact or m.get("sender") or "?"
                    ts_raw = m.get("timestamp", "")
                    try:
                        dt = datetime.fromisoformat(ts_raw)
                        ts = dt.astimezone().strftime("%m-%d %H:%M")
                    except (ValueError, AttributeError):
                        ts = ts_raw[:16] if ts_raw else "??-?? ??:??"
                    render(sender, m.get("body", ""), ts,
                           read=m.get("read", True))
                return

    # ---- local JSONL source --------------------------------------------
    if not config.EVENTS_JSONL.exists():
        typer.echo(typer.style(
            f"No local event log yet at {config.EVENTS_JSONL}",
            fg=typer.colors.YELLOW,
        ))
        typer.echo("Is the daemon running? "
                   "Try: systemctl --user status iphonebridge")
        raise typer.Exit(code=1)

    raw_lines = config.EVENTS_JSONL.read_text(errors="replace").strip().splitlines()
    events: list[dict] = []
    for line in raw_lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if filter_phone_norms or from_text_lower:
        events = [e for e in events if passes_from_filter(e)]

    events = events[-n:][::-1]
    if not events:
        typer.echo("(no events)")
        return

    for e in events:
        sender = e.get("contact_name") or e.get("sender_phone") or "?"
        body = e.get("body") or ""
        ts_raw = e.get("seen_at", "")
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts = dt.astimezone().strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            ts = ts_raw[:16]
        render(sender, body, ts, read=e.get("is_read", True))


@app.command("ancs-enable")
def ancs_enable(
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Enable ANCS (per-app notifications) for the paired iPhone.

    Requires the sudoers helper installed via
        sudo bash systemd/install-ancs-sudoers.sh

    What this does:
      1. Looks up the local adapter MAC.
      2. Calls the sudoers-gated helper, which writes LastUsedBearer=le
         into BlueZ's pairing record for the iPhone.
      3. Disconnects + reconnects the iPhone so BlueZ uses BLE this
         time. The running iphonebridge daemon's AncsClient will pick
         up the ANCS characteristics as they appear.
    """
    _setup_logging(verbose)
    import subprocess
    import time

    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    sysbus = dbus.SystemBus()

    # Adapter MAC via BlueZ DBus
    try:
        adapter_mac = str(
            dbus.Interface(
                sysbus.get_object("org.bluez", f"/org/bluez/{config.ADAPTER}"),
                "org.freedesktop.DBus.Properties",
            ).Get("org.bluez.Adapter1", "Address")
        )
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Couldn't read adapter MAC: {e.get_dbus_message()}",
            fg=typer.colors.RED))
        raise typer.Exit(code=2) from None

    device_mac = config.IPHONE_MAC
    typer.echo(f"adapter: {adapter_mac}  device: {device_mac}")

    # Run the sudoers-gated helper
    r = subprocess.run(
        ["sudo", "-n", "/usr/local/bin/iphonebridge-set-le-bearer",
         adapter_mac, device_mac],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        msg = r.stderr.strip() or r.stdout.strip() or "(no output)"
        typer.echo(typer.style(f"helper failed: {msg}", fg=typer.colors.RED))
        if "password is required" in msg or "may not run" in msg.lower():
            typer.echo("Install the sudoers helper first:")
            typer.echo("  sudo bash systemd/install-ancs-sudoers.sh")
        raise typer.Exit(code=3)
    typer.echo(typer.style(r.stdout.strip(), fg=typer.colors.GREEN))

    # Cycle the BT connection so BlueZ honors the new bearer pref
    typer.echo("cycling Bluetooth connection ...")
    subprocess.run(["bluetoothctl", "disconnect", device_mac],
                   capture_output=True)
    time.sleep(2)
    r = subprocess.run(["bluetoothctl", "connect", device_mac],
                       capture_output=True, text=True)
    typer.echo(r.stdout.strip().splitlines()[-1] if r.stdout else "(reconnected)")

    typer.echo("\nWatch the daemon log for ANCS chars appearing:")
    typer.echo("  journalctl --user -u iphonebridge -f | grep -i ancs")


@app.command("pair-setup")
def pair_setup(
    no_restart: bool = typer.Option(False, "--no-restart",
                                     help="Don't restart the daemon at the end"),
):
    """First-run wizard: pick a paired iPhone, write the local config,
    walk through the iPhone-side toggle steps."""
    from iphonebridge.pair_setup import run_wizard
    raise typer.Exit(code=run_wizard(restart_after=not no_restart))


_RECIPIENT_LOOKS_LIKE_PHONE = __import__("re").compile(r"^\+?[\d\s()\-.]{7,}$")


def _resolve_recipient(raw: str) -> str:
    """Turn a recipient argument into a phone number.

    - If it parses as a phone number, return it normalized with leading +.
    - Otherwise, treat as a contact name substring, look it up in the
      contacts cache, prompt to disambiguate if multiple matches.

    Aborts the command (Exit) if no match is found.
    """
    raw = raw.strip()
    if _RECIPIENT_LOOKS_LIKE_PHONE.match(raw):
        # Keep it as the user typed it; daemon-side bMessage builder is
        # liberal about format.
        return raw

    # Treat as a name. Pull candidates from contact cache.
    from iphonebridge.contacts import ContactsResolver
    resolver = ContactsResolver()
    matches = resolver.find_by_name(raw)
    if not matches:
        typer.echo(typer.style(
            f"No contact matched {raw!r}. Try a phone number with +, or run "
            "`iphonebridge contacts-sync` to refresh the cache.",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=2)

    # Unique-by-name first; if exactly one unique name (across possibly
    # multiple numbers), pick a sensible default.
    by_name: dict[str, list[str]] = {}
    for name, phone in matches:
        by_name.setdefault(name, []).append(phone)

    if len(by_name) == 1:
        name = next(iter(by_name))
        phones = by_name[name]
        if len(phones) == 1:
            chosen = phones[0]
            typer.echo(typer.style(
                f"→ {name}  +{chosen}", fg=typer.colors.CYAN))
            return f"+{chosen}"
        # Multiple phones for one contact — list and prompt
        typer.echo(f"{name} has multiple numbers:")
        for i, p in enumerate(phones, 1):
            typer.echo(f"  [{i}] +{p}")
        idx = typer.prompt("Pick", type=int, default=1)
        return f"+{phones[idx - 1]}"

    # Multiple distinct contacts — show + prompt
    typer.echo(f"Multiple contacts match {raw!r}:")
    flat: list[tuple[str, str]] = []
    for name, phones in sorted(by_name.items()):
        for p in phones:
            flat.append((name, p))
    for i, (name, p) in enumerate(flat, 1):
        typer.echo(f"  [{i}] {name}  +{p}")
    idx = typer.prompt("Pick", type=int, default=1)
    try:
        chosen_name, chosen_phone = flat[idx - 1]
    except IndexError:
        typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
        raise typer.Exit(code=2) from None
    typer.echo(typer.style(
        f"→ {chosen_name}  +{chosen_phone}", fg=typer.colors.CYAN))
    return f"+{chosen_phone}"


@app.command("sms-send")
def sms_send(
    recipient: str = typer.Argument(...,
        help="Recipient: phone (e.g. +15551234567) OR contact name substring "
             "(e.g. 'Maddie')"),
    body: str = typer.Argument(..., help="Message body"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Send an SMS or iMessage via the running daemon's MAP session.

    The iPhone automatically routes to iMessage when the recipient is
    iMessage-capable (blue bubble). Otherwise falls back to SMS.

    Requires the daemon to be running (systemctl --user start iphonebridge).
    """
    _setup_logging(verbose)

    # If the recipient doesn't look like a phone, resolve via contacts.
    resolved = _resolve_recipient(recipient)

    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    try:
        proxy = bus.get_object("com.gabriel.iphonebridge",
                               "/com/gabriel/iphonebridge")
        iface = dbus.Interface(proxy, "com.gabriel.iphonebridge.Messages1")
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Couldn't reach iphonebridge daemon on DBus: {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        typer.echo("Start it with: systemctl --user start iphonebridge")
        raise typer.Exit(code=2) from None

    try:
        transfer = str(iface.Send(resolved, body, timeout=45))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Send failed: {e.get_dbus_name()}\n  {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None

    typer.echo(typer.style(
        f"Sent. Transfer: {transfer}",
        fg=typer.colors.GREEN,
    ))


def _daemon_iface(iface_name: str):
    """Build an Interface onto the running daemon's session-bus object.

    The proxy is lazy — connection errors surface when a method is called,
    so callers should wrap the actual call in a try/except.
    """
    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    proxy = bus.get_object("com.gabriel.iphonebridge",
                           "/com/gabriel/iphonebridge")
    return dbus.Interface(proxy, iface_name)


@app.command()
def call(
    recipient: str = typer.Argument(...,
        help="Phone number (e.g. +15551234567) OR contact name (e.g. 'Maddie')"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Place a phone call through the iPhone (HFP Hands-Free).

    Call audio routes through the laptop's mic + speakers. Requires the
    daemon running and HFP set up — see `iphonebridge hfp-enable`.
    """
    _setup_logging(verbose)
    import dbus

    resolved = _resolve_recipient(recipient)
    iface = _daemon_iface("com.gabriel.iphonebridge.Calls1")
    try:
        call_path = str(iface.Dial(resolved, timeout=45))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Call failed: {e.get_dbus_name()}\n  {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        typer.echo("Is the daemon running and the iPhone connected? "
                   "Try `iphonebridge hfp-enable`.")
        raise typer.Exit(code=3) from None
    typer.echo(typer.style(f"Calling {resolved} …  ({call_path})",
                           fg=typer.colors.GREEN))


@app.command()
def hangup(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Hang up all active phone calls."""
    _setup_logging(verbose)
    import dbus

    iface = _daemon_iface("com.gabriel.iphonebridge.Calls1")
    try:
        iface.HangupAll(timeout=30)
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Hangup failed: {e.get_dbus_message() or e.get_dbus_name()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None
    typer.echo("Hung up.")


@app.command()
def calls(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """List active phone calls."""
    _setup_logging(verbose)
    import json

    import dbus

    iface = _daemon_iface("com.gabriel.iphonebridge.Calls1")
    try:
        raw = str(iface.ListCalls(timeout=20))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Query failed: {e.get_dbus_message() or e.get_dbus_name()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None
    data = json.loads(raw)
    if not data:
        typer.echo("(no active calls)")
        return
    for c in data:
        peer = c.get("contact_name") or c.get("peer_phone") or "(unknown)"
        arrow = "←" if c.get("direction") == "incoming" else "→"
        typer.echo(f"  {arrow} {peer:<24s}  {c.get('state', '?')}")


@app.command("hfp-enable")
def hfp_enable(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Set up HFP call support.

    Writes the WirePlumber config that routes HFP/HSP through oFono (so
    call control is available on D-Bus), restarts WirePlumber, and prints
    the remaining root-only steps. HFP lets you take and place iPhone
    calls on the laptop — caller ID, answer/decline, dialing.
    """
    _setup_logging(verbose)
    import shutil
    import subprocess
    from pathlib import Path

    from iphonebridge.hfp.ofono_client import write_wireplumber_config

    path, backup = write_wireplumber_config()
    if backup:
        typer.echo(f"Wrote {path}  (previous backed up → {backup})")
    else:
        typer.echo(f"Wrote {path}")

    typer.echo("Restarting WirePlumber / PipeWire …")
    subprocess.run(
        ["systemctl", "--user", "restart",
         "wireplumber", "pipewire", "pipewire-pulse"],
        check=False,
    )

    ofono_installed = bool(shutil.which("ofonod")) \
        or Path("/usr/sbin/ofonod").exists()
    typer.echo("")
    if ofono_installed:
        typer.echo(typer.style("oFono is installed.", fg=typer.colors.GREEN))
        typer.echo("Finish setup — this needs root, run it yourself:")
        typer.echo(typer.style(
            "  sudo systemctl restart ofono", fg=typer.colors.WHITE))
        typer.echo("    (restart oFono AFTER WirePlumber so it can claim the "
                   "HFP profile)")
    else:
        typer.echo(typer.style("oFono is NOT installed.",
                               fg=typer.colors.YELLOW))
        typer.echo("Install it and enable the service — needs root:")
        typer.echo(typer.style(
            "  sudo apt install ofono", fg=typer.colors.WHITE))
        typer.echo(typer.style(
            "  sudo systemctl enable --now ofono", fg=typer.colors.WHITE))

    typer.echo(f"  bluetoothctl disconnect {config.IPHONE_MAC}")
    typer.echo(f"  bluetoothctl connect {config.IPHONE_MAC}")
    typer.echo("")
    typer.echo("Then restart the daemon:  "
               "systemctl --user restart iphonebridge")


@app.command()
def version():
    """Print version and exit."""
    from iphonebridge import __version__
    typer.echo(f"iphonebridge {__version__}")


if __name__ == "__main__":
    app()
