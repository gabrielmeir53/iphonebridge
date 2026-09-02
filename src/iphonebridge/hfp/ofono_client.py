"""oFono HFP client — observes the iPhone's Hands-Free modem and exposes
call control (answer / hang up / dial) plus a CallEvent stream.

oFono does the HFP protocol itself (AT commands over RFCOMM, the service-
level connection, codec negotiation). We just watch org.ofono on the
system bus:

  org.ofono.Manager           ModemAdded / ModemRemoved, GetModems
  org.ofono.Modem             one per device; Type="hfp" is the iPhone
  org.ofono.VoiceCallManager  CallAdded / CallRemoved, Dial, HangupAll
  org.ofono.VoiceCall         per-call State + LineIdentification,
                              Answer / Hangup

Call audio (SCO) is carried by PipeWire's oFono HFP backend — nothing for
us to route. `iphonebridge hfp-enable` writes the one WirePlumber config
that selects that backend.

Confirmed end-to-end against iPhone 16 Pro Max / iOS 26.5 — see
spike/05b_hfp_ofono.py and spike/RESULTS.md (HFP addendum).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import dbus
import dbus.exceptions

from iphonebridge.bus import system_bus
from iphonebridge.hfp.events import CallEvent, call_event_from_ofono

log = logging.getLogger(__name__)

OFONO = "org.ofono"
_MGR_IFACE = f"{OFONO}.Manager"
_MODEM_IFACE = f"{OFONO}.Modem"
_VCM_IFACE = f"{OFONO}.VoiceCallManager"
_VC_IFACE = f"{OFONO}.VoiceCall"


# ---- WirePlumber config -------------------------------------------------

WIREPLUMBER_HFP_CONF = (
    Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d"
    / "51-bluez-hfp-hf.conf"
)

_WP_CONF_BODY = """# Installed by `iphonebridge hfp-enable`.
# Enable the HFP Hands-Free role for bluez5 devices and hand HFP/HSP off to
# oFono, so call control (answer/hangup/dial, caller ID) is available on
# D-Bus. Reversible: restore the .bak alongside this file, restart wireplumber.
monitor.bluez.properties = {
  "bluez5.roles"            = [ "hsp_hs", "hsp_ag", "hfp_hf", "hfp_ag", "a2dp_sink", "a2dp_source" ]
  "bluez5.codecs"           = [ "sbc", "sbc_xq", "msbc" ]
  "bluez5.enable-msbc"      = true
  "bluez5.enable-hw-volume" = true
  "bluez5.hfphsp-backend"   = "ofono"
}
"""


def write_wireplumber_config() -> tuple[Path, Path | None]:
    """Write the HFP/oFono WirePlumber config.

    Returns (config_path, backup_path_or_None). An existing file with a
    different body is backed up first.
    """
    path = WIREPLUMBER_HFP_CONF
    backup: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_text() == _WP_CONF_BODY:
            return path, None
        backup = path.with_suffix(f".conf.bak.{int(time.time())}")
        backup.write_text(path.read_text())
    path.write_text(_WP_CONF_BODY)
    return path, backup


# ---- HFP manager --------------------------------------------------------

class HfpError(RuntimeError):
    """Raised by call-control methods when no HFP modem is available."""


class HfpManager:
    """Tracks the iPhone's oFono HFP modem and its calls.

    Independent of the MAP/PBAP OBEX sessions — wired into the daemon like
    AncsClient. Idempotent: start()/stop() are safe to call repeatedly.
    """

    def __init__(
        self,
        on_event: Callable[[CallEvent], None],
        resolve_contact: Callable[[str | None], str | None] | None = None,
    ) -> None:
        self.on_event = on_event
        self.resolve_contact = resolve_contact or (lambda _raw: None)

        self._modem_path: str | None = None
        self._vcm_hooked = False

        self._mgr_matches: list = []          # ModemAdded / ModemRemoved
        self._modem_sub = None                # modem PropertyChanged
        self._vcm_matches: list = []          # CallAdded / CallRemoved
        self._call_subs: dict[str, object] = {}   # call_path -> SignalMatch
        # call_path -> {direction, state, peer_phone, peer_name, contact_name}
        self._calls: dict[str, dict] = {}

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        # get_object() triggers activation, so it raises ServiceUnknown here
        # when oFono isn't installed at all — keep it inside the try.
        try:
            mgr = dbus.Interface(system_bus.get_object(OFONO, "/"), _MGR_IFACE)
            modems = mgr.GetModems()
        except dbus.exceptions.DBusException as e:
            log.warning(
                "oFono not available (%s) — HFP calls disabled. "
                "Run `iphonebridge hfp-enable`, then restart the daemon.",
                e.get_dbus_name(),
            )
            return
        self._mgr_matches.append(
            mgr.connect_to_signal("ModemAdded", self._on_modem_added))
        self._mgr_matches.append(
            mgr.connect_to_signal("ModemRemoved", self._on_modem_removed))
        for path, props in modems:
            self._on_modem_added(path, props)
        log.info("HFP manager started (oFono); modem=%s", self._modem_path)

    def stop(self) -> None:
        log.info("HFP manager stopping")
        for m in self._mgr_matches:
            _safe_remove(m)
        self._mgr_matches = []
        self._teardown_modem()

    def _teardown_modem(self) -> None:
        _safe_remove(self._modem_sub)
        self._modem_sub = None
        for m in self._vcm_matches:
            _safe_remove(m)
        self._vcm_matches = []
        for sub in self._call_subs.values():
            _safe_remove(sub)
        self._call_subs.clear()
        self._calls.clear()
        self._modem_path = None
        self._vcm_hooked = False

    # ---- modem tracking -------------------------------------------------

    def _on_modem_added(self, path, props) -> None:
        props = dict(props)
        if str(props.get("Type", "")) != "hfp":
            return
        if self._modem_path is not None:
            return  # we track a single iPhone modem
        self._modem_path = str(path)
        log.info("HFP modem appeared: %s (%s)",
                 self._modem_path, props.get("Name"))
        modem = dbus.Interface(
            system_bus.get_object(OFONO, self._modem_path), _MODEM_IFACE)
        self._modem_sub = modem.connect_to_signal(
            "PropertyChanged", self._on_modem_prop)
        self._ensure_powered(modem, props)
        self._maybe_hook_vcm(props)

    def _on_modem_removed(self, path) -> None:
        if str(path) != self._modem_path:
            return
        log.info("HFP modem removed: %s", path)
        # Surface a clean ending for any calls we were tracking.
        for cpath, info in list(self._calls.items()):
            self._emit(cpath, info, ended=True)
        self._teardown_modem()

    def _on_modem_prop(self, name, _value) -> None:
        if name not in ("Interfaces", "Powered") or self._modem_path is None:
            return
        try:
            modem = dbus.Interface(
                system_bus.get_object(OFONO, self._modem_path), _MODEM_IFACE)
            self._maybe_hook_vcm(dict(modem.GetProperties()))
        except dbus.exceptions.DBusException:
            pass

    def _ensure_powered(self, modem: dbus.Interface, props: dict) -> None:
        if props.get("Powered"):
            return
        # An HFP modem normally auto-powers once oFono's service-level
        # connection is up. If it didn't, the usual cause is a startup-order
        # race (oFono lost the HFP-profile registration to PipeWire's native
        # backend) — `iphonebridge hfp-enable` fixes the config + ordering.
        try:
            modem.SetProperty("Powered", dbus.Boolean(True))
            log.info("HFP modem powered on")
        except dbus.exceptions.DBusException as e:
            log.warning(
                "could not power the HFP modem (%s) — call control may be "
                "unavailable. Run `iphonebridge hfp-enable` and make sure "
                "oFono is restarted after WirePlumber.",
                e.get_dbus_message() or e.get_dbus_name(),
            )

    def _maybe_hook_vcm(self, props: dict) -> None:
        if self._vcm_hooked or self._modem_path is None:
            return
        ifaces = [str(i) for i in props.get("Interfaces", [])]
        if _VCM_IFACE not in ifaces:
            return
        vcm = dbus.Interface(
            system_bus.get_object(OFONO, self._modem_path), _VCM_IFACE)
        self._vcm_matches.append(
            vcm.connect_to_signal("CallAdded", self._on_call_added))
        self._vcm_matches.append(
            vcm.connect_to_signal("CallRemoved", self._on_call_removed))
        self._vcm_hooked = True
        log.info("HFP call control ready")
        try:
            for cpath, cprops in vcm.GetCalls():
                self._on_call_added(cpath, cprops)
        except dbus.exceptions.DBusException:
            pass

    # ---- call tracking --------------------------------------------------

    def _on_call_added(self, path, props) -> None:
        path = str(path)
        if path in self._calls:
            return
        props = dict(props)
        state = str(props.get("State", "") or "")
        direction = "incoming" if state in ("incoming", "waiting") else "outgoing"
        peer = props.get("LineIdentification")
        peer = str(peer) if peer not in (None, "") else None
        name = props.get("Name")
        name = str(name) if name not in (None, "") else None
        info = {
            "direction": direction,
            "state": state,
            "peer_phone": peer,
            "peer_name": name,
            "contact_name": self.resolve_contact(peer) if peer else None,
        }
        self._calls[path] = info
        vc = dbus.Interface(system_bus.get_object(OFONO, path), _VC_IFACE)
        self._call_subs[path] = vc.connect_to_signal(
            "PropertyChanged",
            lambda n, v, p=path: self._on_call_prop(p, n, v),
        )
        log.info("call %s: %s %s peer=%r", path.rsplit("/", 1)[-1],
                 direction, state, info["contact_name"] or peer)
        self._emit(path, info)

    def _on_call_prop(self, path: str, name, value) -> None:
        info = self._calls.get(path)
        if info is None:
            return
        if name == "State":
            info["state"] = str(value)
            log.info("call %s → %s", path.rsplit("/", 1)[-1], info["state"])
            self._emit(path, info)
        elif name == "LineIdentification":
            raw = str(value) if value else None
            info["peer_phone"] = raw
            info["contact_name"] = self.resolve_contact(raw) if raw else None

    def _on_call_removed(self, path) -> None:
        path = str(path)
        _safe_remove(self._call_subs.pop(path, None))
        info = self._calls.pop(path, None)
        if info is None:
            return
        log.info("call %s ended", path.rsplit("/", 1)[-1])
        self._emit(path, info, ended=True)

    def _emit(self, path: str, info: dict, *, ended: bool = False) -> None:
        event = call_event_from_ofono(
            path,
            {"State": info["state"],
             "LineIdentification": info["peer_phone"],
             "Name": info["peer_name"]},
            direction=info["direction"],
            contact_name=info["contact_name"],
            ended=ended,
        )
        try:
            self.on_event(event)
        except Exception:
            log.exception("HFP on_event callback raised")

    # ---- call control (called from the D-Bus service) ------------------

    def list_calls(self) -> list[dict]:
        """Snapshot of currently-tracked calls as JSON-friendly dicts."""
        from iphonebridge.events import normalize_phone
        out = []
        for path, info in self._calls.items():
            out.append({
                "call_path": path,
                "direction": info["direction"],
                "state": info["state"],
                "peer_phone": info["peer_phone"],
                "peer_phone_norm": normalize_phone(info["peer_phone"]),
                "contact_name": info["contact_name"],
                "peer_name": info["peer_name"],
            })
        return out

    def answer(self, call_path: str) -> None:
        self._require_modem()
        self._voicecall(call_path).Answer()

    def hangup(self, call_path: str) -> None:
        self._require_modem()
        self._voicecall(call_path).Hangup()

    def hangup_all(self) -> None:
        self._require_modem()
        dbus.Interface(
            system_bus.get_object(OFONO, self._modem_path), _VCM_IFACE
        ).HangupAll()

    def dial(self, number: str) -> str:
        """Place a call. Returns the new oFono VoiceCall object path."""
        self._require_modem()
        vcm = dbus.Interface(
            system_bus.get_object(OFONO, self._modem_path), _VCM_IFACE)
        return str(vcm.Dial(number, ""))

    def _voicecall(self, call_path: str) -> dbus.Interface:
        return dbus.Interface(
            system_bus.get_object(OFONO, call_path), _VC_IFACE)

    def _require_modem(self) -> None:
        if not self._modem_path or not self._vcm_hooked:
            raise HfpError(
                "no HFP modem ready — is the iPhone connected? "
                "Run `iphonebridge hfp-enable` if you haven't."
            )


def _safe_remove(match) -> None:
    if match is None:
        return
    try:
        match.remove()
    except Exception:
        pass
