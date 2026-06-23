#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Screen autorotation daemon for ASUS ProArt PX13 (GNOME / Mutter / Wayland)."""

from __future__ import annotations

import configparser
import glob
import logging
import os
import pwd
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio  # noqa: E402

LOG = logging.getLogger("screen-autorotate")

SENSOR_BUS = Gio.BusType.SYSTEM
SENSOR_NAME = "net.hadess.SensorProxy"
SENSOR_PATH = "/net/hadess/SensorProxy"
SENSOR_IFACE = "net.hadess.SensorProxy"

MUTTER_NAME = "org.gnome.Mutter.DisplayConfig"
MUTTER_PATH = "/org/gnome/Mutter/DisplayConfig"
MUTTER_IFACE = "org.gnome.Mutter.DisplayConfig"

DEFAULT_CONFIG = "/etc/screen-autorotate.conf"
DEFAULT_INTERNAL = "eDP-1"

# iio-sensor-proxy orientation -> Mutter transform (0=normal, 1=90° CW, 2=180°, 3=270°)
DEFAULT_ORIENTATION_MAP = {
    "normal": 0,
    "bottom-up": 2,
    "right-up": 1,
    "left-up": 3,
}

APPLY_TEMPORARY = 1
APPLY_PERSISTENT = 2

VALID_ORIENTATIONS = frozenset(DEFAULT_ORIENTATION_MAP.keys())
APPLY_MONITORS_CONFIG_SIG = "(uua(iiduba(ssa{sv}))a{sv})"


@dataclass
class Settings:
    enabled: bool = True
    internal_connector: str = DEFAULT_INTERNAL
    product_match: str = "ProArt PX13"
    orientation_map: dict[str, int] | None = None
    apply_persistent: bool = False
    debounce_ms: int = 400
    require_product: bool = True

    def __post_init__(self) -> None:
        if self.orientation_map is None:
            self.orientation_map = dict(DEFAULT_ORIENTATION_MAP)


def load_settings(path: str = DEFAULT_CONFIG) -> Settings:
    settings = Settings()
    if not os.path.isfile(path):
        return settings

    parser = configparser.ConfigParser()
    parser.read(path)

    section = "autorotate"
    if section not in parser:
        return settings

    cfg = parser[section]
    settings.enabled = cfg.getboolean("enabled", fallback=True)
    settings.internal_connector = cfg.get("internal_connector", fallback=DEFAULT_INTERNAL)
    settings.product_match = cfg.get("product_match", fallback="ProArt PX13")
    settings.apply_persistent = cfg.getboolean("apply_persistent", fallback=False)
    settings.debounce_ms = cfg.getint("debounce_ms", fallback=400)
    settings.require_product = cfg.getboolean("require_product", fallback=True)

    raw_map = cfg.get("orientation_map", fallback="").strip()
    if raw_map:
        mapping: dict[str, int] = {}
        for item in raw_map.split(","):
            key, _, value = item.partition(":")
            if key and value:
                mapping[key.strip()] = int(value.strip())
        if mapping:
            settings.orientation_map = mapping

    return settings


def read_product_name() -> str:
    try:
        with open("/sys/class/dmi/id/product_name", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def product_matches(settings: Settings) -> bool:
    if not settings.require_product:
        return True
    product = read_product_name()
    return settings.product_match.lower() in product.lower()


def is_valid_orientation(orientation: str | None) -> bool:
    return bool(orientation and orientation in VALID_ORIENTATIONS)


def connector_is_connected(monitors: list, connector: str) -> bool:
    return any(monitor[0][0] == connector for monitor in monitors)


def preferred_mode(monitors: list, connector: str) -> tuple[str, float] | None:
    for monitor in monitors:
        info, modes, _props = monitor
        if info[0] != connector:
            continue
        for mode in modes:
            props = mode[6] if len(mode) > 6 else {}
            if props.get("is-current", False):
                return mode[0], mode[4]
            if props.get("is-preferred", False):
                return mode[0], mode[4]
        if modes:
            return modes[0][0], modes[0][4]
    return None


def find_current_mode(monitors: list, connector: str) -> str | None:
    mode = preferred_mode(monitors, connector)
    return mode[0] if mode else None


def unpack_logical(logical_raw: list) -> list:
    unpacked = []
    for entry in logical_raw:
        x, y, scale, transform, primary, connectors, props = entry
        unpacked.append(
            {
                "x": x,
                "y": y,
                "scale": scale,
                "transform": transform,
                "primary": primary,
                "connectors": list(connectors),
                "props": dict(props) if props else {},
            }
        )
    return unpacked


def build_logical_variant(logical: list[dict], monitors: list) -> list:
    payload = []
    for lm in logical:
        connectors = []
        for conn in lm["connectors"]:
            name = conn[0]
            mode_id = find_current_mode(monitors, name)
            if mode_id is None and len(conn) > 1 and "@" in str(conn[1]):
                mode_id = conn[1]
            if mode_id is None:
                mode_id = ""
            connectors.append((name, mode_id, {}))
        payload.append(
            (
                lm["x"],
                lm["y"],
                lm["scale"],
                lm["transform"],
                lm["primary"],
                connectors,
            )
        )
    return payload


class DisplayRotator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_transform: dict[str, int] = {}
        self._saved_layout: dict[int, list] = {}
        self._warned_no_sessions = False

    def _list_session_uids(self) -> list[int]:
        uids: list[int] = []
        for bus_path in sorted(glob.glob("/run/user/*/bus")):
            try:
                uid = int(bus_path.split("/")[3])
            except (IndexError, ValueError):
                continue
            uids.append(uid)
        return uids

    def _runuser_env(self, uid: int) -> dict[str, str] | None:
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            return None
        runtime = f"/run/user/{uid}"
        bus_path = f"{runtime}/bus"
        if not os.path.exists(bus_path):
            return None
        return {
            "HOME": pw.pw_dir,
            "USER": pw.pw_name,
            "LOGNAME": pw.pw_name,
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus_path}",
        }

    def _apply_via_runuser(self, uid: int, orientation: str, *, force: bool) -> bool:
        env = self._runuser_env(uid)
        if env is None:
            return False
        cmd = ["runuser", "-u", env["USER"], "--", sys.executable, __file__, "--apply", orientation]
        if force:
            cmd.append("--force")
        try:
            proc = subprocess.run(
                cmd,
                env={**os.environ, **env},
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            LOG.warning("Timed out applying rotation for uid %s", uid)
            return False
        return proc.returncode == 0

    def _session_connection(self) -> Gio.DBusConnection | None:
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return None

        try:
            conn.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                GLib.Variant("(s)", (MUTTER_NAME,)),
                GLib.VariantType("(s)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        except GLib.Error:
            return None

        return conn

    def _session_connections(self) -> Iterable[tuple[int, Gio.DBusConnection]]:
        for bus_path in sorted(glob.glob("/run/user/*/bus")):
            try:
                uid = int(bus_path.split("/")[3])
            except (IndexError, ValueError):
                continue

            address = f"unix:path={bus_path}"
            try:
                conn = Gio.DBusConnection.new_for_address_sync(
                    address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT,
                    None,
                    None,
                )
                conn.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "Hello",
                    None,
                    GLib.VariantType("(s)"),
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
            except GLib.Error:
                continue

            try:
                conn.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "GetNameOwner",
                    GLib.Variant("(s)", (MUTTER_NAME,)),
                    GLib.VariantType("(s)"),
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
            except GLib.Error:
                continue

            yield uid, conn

    def apply_to_session_bus(self, orientation: str, *, force: bool = False) -> bool:
        if not is_valid_orientation(orientation):
            return False

        conn = self._session_connection()
        if conn is None:
            LOG.debug("No Mutter session on current D-Bus")
            return False

        mapping = self.settings.orientation_map or DEFAULT_ORIENTATION_MAP
        transform = mapping[orientation]
        connector = self.settings.internal_connector
        method = APPLY_PERSISTENT if self.settings.apply_persistent else APPLY_TEMPORARY
        uid = os.getuid()
        cache_key = f"{uid}:{connector}"

        try:
            if transform == 0 and uid in self._saved_layout:
                if self._restore_layout(conn, uid, connector, method):
                    self._last_transform.pop(cache_key, None)
                    LOG.info("Restored saved layout for uid %s", uid)
                    return True
                return False

            if not force and self._last_transform.get(cache_key) == transform:
                return False

            if self._apply_on_connection(
                conn, uid, connector, transform, method, force=force
            ):
                self._last_transform[cache_key] = transform
                LOG.info(
                    "Applied transform %s (%s) on %s for uid %s",
                    transform,
                    orientation,
                    connector,
                    uid,
                )
                return True
        except GLib.Error as exc:
            LOG.warning("Failed rotation for uid %s: %s", uid, exc.message)

        return False

    def apply(self, orientation: str, *, force: bool = False) -> bool:
        if not is_valid_orientation(orientation):
            LOG.debug("Orientation %r not ready or unsupported, skipping", orientation)
            return False

        mapping = self.settings.orientation_map or DEFAULT_ORIENTATION_MAP
        transform = mapping[orientation]
        connector = self.settings.internal_connector
        changed_any = False

        if os.geteuid() == 0:
            session_uids = self._list_session_uids()
            if not session_uids:
                if not self._warned_no_sessions:
                    LOG.warning("No user sessions found; cannot apply rotation")
                    self._warned_no_sessions = True
                else:
                    LOG.debug("No user sessions found; cannot apply rotation")
                return False

            self._warned_no_sessions = False
            for uid in session_uids:
                if self._apply_via_runuser(uid, orientation, force=force):
                    changed_any = True
            return changed_any

        sessions = list(self._session_connections())
        if not sessions:
            if not self._warned_no_sessions:
                LOG.warning("No Mutter sessions found; cannot apply rotation")
                self._warned_no_sessions = True
            else:
                LOG.debug("No Mutter sessions found; cannot apply rotation")
            return False

        self._warned_no_sessions = False
        method = APPLY_PERSISTENT if self.settings.apply_persistent else APPLY_TEMPORARY

        for uid, conn in sessions:
            cache_key = f"{uid}:{connector}"
            try:
                if transform == 0 and uid in self._saved_layout:
                    if self._restore_layout(conn, uid, connector, method):
                        self._last_transform.pop(cache_key, None)
                        changed_any = True
                        LOG.info("Restored saved layout for uid %s", uid)
                    continue

                if not force and self._last_transform.get(cache_key) == transform:
                    continue

                if self._apply_on_connection(
                    conn, uid, connector, transform, method, force=force
                ):
                    self._last_transform[cache_key] = transform
                    changed_any = True
                    LOG.info(
                        "Applied transform %s (%s) on %s for uid %s",
                        transform,
                        orientation,
                        connector,
                        uid,
                    )
            except GLib.Error as exc:
                LOG.warning("Failed rotation for uid %s: %s", uid, exc.message)

        return changed_any

    def _restore_layout(
        self,
        conn: Gio.DBusConnection,
        uid: int,
        connector: str,
        method: int,
    ) -> bool:
        saved = self._saved_layout.pop(uid, None)
        if not saved:
            return False

        _old_serial, _monitors, logical_raw, _props = saved
        result = conn.call_sync(
            MUTTER_NAME,
            MUTTER_PATH,
            MUTTER_IFACE,
            "GetCurrentState",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        serial, monitors, _logical, _props = result.unpack()
        logical = unpack_logical(logical_raw)
        for lm in logical:
            names = [c[0] for c in lm["connectors"]]
            if connector in names:
                lm["transform"] = 0

        payload = build_logical_variant(logical, monitors)
        params = GLib.Variant(APPLY_MONITORS_CONFIG_SIG, (serial, method, payload, {}))
        conn.call_sync(
            MUTTER_NAME,
            MUTTER_PATH,
            MUTTER_IFACE,
            "ApplyMonitorsConfig",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return True

    def _apply_on_connection(
        self,
        conn: Gio.DBusConnection,
        uid: int,
        connector: str,
        transform: int,
        method: int,
        *,
        force: bool = False,
    ) -> bool:
        result = conn.call_sync(
            MUTTER_NAME,
            MUTTER_PATH,
            MUTTER_IFACE,
            "GetCurrentState",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        serial, monitors, logical_raw, _props = result.unpack()
        logical = unpack_logical(logical_raw)

        target = None
        for lm in logical:
            names = [c[0] for c in lm["connectors"]]
            if connector in names:
                target = lm
                break

        if target is None:
            if not connector_is_connected(monitors, connector):
                LOG.debug("Connector %s not connected", connector)
                return False
            if transform == 0:
                LOG.debug("Connector %s connected but inactive; laptop mode", connector)
                return False
            if uid not in self._saved_layout:
                self._saved_layout[uid] = result.unpack()
            return self._enable_internal_display(
                conn, serial, monitors, connector, transform, method
            )

        if target["transform"] == transform and not force:
            LOG.debug(
                "Transform %s already set on %s for uid %s",
                transform,
                connector,
                uid,
            )
            return False

        if target["transform"] != transform:
            target["transform"] = transform
        payload = build_logical_variant(logical, monitors)
        params = GLib.Variant(APPLY_MONITORS_CONFIG_SIG, (serial, method, payload, {}))
        conn.call_sync(
            MUTTER_NAME,
            MUTTER_PATH,
            MUTTER_IFACE,
            "ApplyMonitorsConfig",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return True

    def _enable_internal_display(
        self,
        conn: Gio.DBusConnection,
        serial: int,
        monitors: list,
        connector: str,
        transform: int,
        method: int,
    ) -> bool:
        mode = preferred_mode(monitors, connector)
        if mode is None:
            LOG.warning("No usable mode for %s", connector)
            return False

        mode_id, scale = mode
        payload = [
            (0, 0, scale, transform, True, [(connector, mode_id, {})]),
        ]
        params = GLib.Variant(APPLY_MONITORS_CONFIG_SIG, (serial, method, payload, {}))
        conn.call_sync(
            MUTTER_NAME,
            MUTTER_PATH,
            MUTTER_IFACE,
            "ApplyMonitorsConfig",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        LOG.info("Enabled %s with transform %s (tablet mode)", connector, transform)
        return True


class AutorotateDaemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rotator = DisplayRotator(settings)
        self.loop = GLib.MainLoop()
        self.sensor_conn: Gio.DBusConnection | None = None
        self._proxy: Gio.DBusProxy | None = None
        self._sub_id: int | None = None
        self._debounce_source: GLib.Source | None = None
        self._pending_orientation: str | None = None
        self._pending_force = False
        self._last_applied: str | None = None
        self._known_session_uids: set[int] = set()
        self._rescan_interval_sec = 2
        self._sensor_wait_source: GLib.Source | None = None

    def start(self) -> None:
        if not product_matches(self.settings):
            product = read_product_name()
            LOG.error(
                "Product %r does not match %r; refusing to start",
                product,
                self.settings.product_match,
            )
            sys.exit(1)

        if not self.settings.enabled:
            LOG.info("Service disabled in %s", DEFAULT_CONFIG)
            sys.exit(0)

        LOG.info(
            "Starting screen autorotate for %s (internal=%s)",
            read_product_name(),
            self.settings.internal_connector,
        )

        try:
            self.sensor_conn = Gio.bus_get_sync(SENSOR_BUS, None)
        except GLib.Error as exc:
            LOG.error("Cannot connect to system D-Bus: %s", exc.message)
            sys.exit(1)

        self._wait_for_sensor_proxy()

    def _on_new_sessions(self, uids: set[int]) -> None:
        """GNOME reloads monitors.xml when a user logs in; re-apply after that settles."""
        orientation = self._read_orientation()
        if not is_valid_orientation(orientation):
            return
        for uid in sorted(uids):
            cache_key = f"{uid}:{self.settings.internal_connector}"
            self.rotator._last_transform.pop(cache_key, None)
            LOG.info("New session uid %s detected, scheduling rotation sync", uid)
        self._schedule_apply(orientation, force=True)
        for delay_sec in (3, 8):
            GLib.timeout_add_seconds(
                delay_sec,
                self._delayed_session_apply,
                orientation,
            )

    def _delayed_session_apply(self, orientation: str) -> bool:
        if is_valid_orientation(orientation):
            self._schedule_apply(orientation, force=True)
        return False

    def _periodic_rescan(self) -> bool:
        """Re-read orientation and apply to sessions that appear after boot."""
        current_uids = set(self.rotator._list_session_uids())
        new_uids = current_uids - self._known_session_uids
        if new_uids:
            self._on_new_sessions(new_uids)
        self._known_session_uids = current_uids

        orientation = self._read_orientation()
        if is_valid_orientation(orientation):
            self._schedule_apply(
                orientation,
                force=(orientation != self._last_applied or bool(new_uids)),
            )
        return True

    def _wait_for_valid_orientation(self) -> bool:
        orientation = self._read_orientation()
        if not is_valid_orientation(orientation):
            return True
        LOG.info("Sensor ready, orientation: %s", orientation)
        self._sensor_wait_source = None
        self._schedule_apply(orientation, force=True)
        return False

    def _wait_for_sensor_proxy(self) -> None:
        Gio.bus_watch_name(
            SENSOR_BUS,
            SENSOR_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_sensor_appeared,
            self._on_sensor_vanished,
        )
        GLib.timeout_add_seconds(self._rescan_interval_sec, self._periodic_rescan)
        self._sensor_wait_source = GLib.timeout_add_seconds(1, self._wait_for_valid_orientation)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self.loop.run()

    def _handle_signal(self, _signum: int, _frame) -> None:
        LOG.info("Stopping")
        self.loop.quit()

    def _on_sensor_appeared(self, _conn, _name, *_args) -> None:
        LOG.info("iio-sensor-proxy available")
        try:
            self._proxy = Gio.DBusProxy.new_sync(
                self.sensor_conn,
                Gio.DBusProxyFlags.NONE,
                None,
                SENSOR_NAME,
                SENSOR_PATH,
                SENSOR_IFACE,
                None,
            )
        except GLib.Error as exc:
            LOG.error("Failed to create sensor proxy: %s", exc.message)
            return

        try:
            self._proxy.call_sync(
                "ClaimAccelerometer",
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            LOG.warning("ClaimAccelerometer failed: %s", exc.message)

        if self._sub_id is not None:
            self._proxy.disconnect_signal(self._sub_id)

        self._sub_id = self._proxy.connect("g-properties-changed", self._on_properties_changed)
        self._proxy.connect("notify::accelerometer-orientation", self._on_orientation_notify)
        orientation = self._read_orientation()
        if is_valid_orientation(orientation):
            LOG.info("Initial orientation: %s", orientation)
            self._schedule_apply(orientation, force=True)
        else:
            LOG.info("Waiting for accelerometer (currently %r)", orientation)

    def _on_properties_changed(
        self,
        _proxy,
        changed: GLib.Variant,
        _invalidated: GLib.Variant,
    ) -> None:
        changed_dict = changed.unpack()
        orientation = changed_dict.get("AccelerometerOrientation")
        if is_valid_orientation(orientation):
            LOG.debug("Orientation changed: %s", orientation)
            self._schedule_apply(orientation, force=True)

    def _on_orientation_notify(self, _proxy, _pspec) -> None:
        orientation = self._read_orientation()
        if is_valid_orientation(orientation):
            self._schedule_apply(orientation, force=True)

    def _on_sensor_vanished(self, _conn, _name, *_args) -> None:
        LOG.warning("iio-sensor-proxy vanished")
        self._proxy = None
        self._last_applied = None

    def _read_orientation(self) -> str | None:
        if self._proxy is None:
            return None
        try:
            variant = self._proxy.get_cached_property("AccelerometerOrientation")
            return variant.get_string() if variant else None
        except GLib.Error:
            return None

    def _schedule_apply(self, orientation: str, *, force: bool = False) -> None:
        if not is_valid_orientation(orientation):
            return
        if not force and orientation == self._last_applied:
            return
        self._pending_orientation = orientation
        self._pending_force = self._pending_force or force
        if self._debounce_source is not None:
            return
        delay = max(0, self.settings.debounce_ms)
        self._debounce_source = GLib.timeout_add(delay, self._debounced_apply)

    def _debounced_apply(self) -> bool:
        self._debounce_source = None
        orientation = self._pending_orientation
        force = self._pending_force
        self._pending_orientation = None
        self._pending_force = False
        if not is_valid_orientation(orientation):
            return False
        if self.rotator.apply(orientation, force=force):
            self._last_applied = orientation
        return False


def configure_logging() -> None:
    level = os.environ.get("SCREEN_AUTOROTATE_LOG", "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(name)s[%(levelname)s]: %(message)s",
    )


def main() -> None:
    configure_logging()
    settings = load_settings()

    if len(sys.argv) >= 3 and sys.argv[1] == "--apply":
        orientation = sys.argv[2]
        force = "--force" in sys.argv[3:]
        ok = DisplayRotator(settings).apply_to_session_bus(orientation, force=force)
        sys.exit(0 if ok else 1)

    AutorotateDaemon(settings).start()


if __name__ == "__main__":
    main()
