#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Screen autorotation daemon for ASUS ProArt PX13 (GNOME / Mutter / Wayland)."""

from __future__ import annotations

import configparser
import glob
import logging
import os
import signal
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


def find_current_mode(monitors: list, connector: str) -> str | None:
    for monitor in monitors:
        info, modes, _props = monitor
        if info[0] != connector:
            continue
        for mode in modes:
            props = mode[6] if len(mode) > 6 else {}
            if props.get("is-current", False):
                return mode[0]
        if modes:
            return modes[0][0]
    return None


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
            if mode_id is None:
                mode_id = conn[1] if len(conn) > 1 else ""
            connectors.append((name, mode_id, {}))
        payload.append(
            (
                lm["x"],
                lm["y"],
                lm["scale"],
                lm["transform"],
                lm["primary"],
                connectors,
                lm["props"],
            )
        )
    return payload


class DisplayRotator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_transform: dict[str, int] = {}

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

    def apply(self, orientation: str) -> bool:
        mapping = self.settings.orientation_map or DEFAULT_ORIENTATION_MAP
        transform = mapping.get(orientation)
        if transform is None:
            LOG.warning("Unknown orientation %r, ignoring", orientation)
            return False

        connector = self.settings.internal_connector
        method = APPLY_PERSISTENT if self.settings.apply_persistent else APPLY_TEMPORARY
        changed_any = False

        for uid, conn in self._session_connections():
            cache_key = f"{uid}:{connector}"
            if self._last_transform.get(cache_key) == transform:
                continue

            try:
                if self._apply_on_connection(conn, connector, transform, method):
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

    def _apply_on_connection(
        self,
        conn: Gio.DBusConnection,
        connector: str,
        transform: int,
        method: int,
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
            LOG.debug("Connector %s not active in this session", connector)
            return False

        if target["transform"] == transform:
            return False

        target["transform"] = transform
        payload = build_logical_variant(logical, monitors)
        params = GLib.Variant("(ua(iiduba(ssa{sv}))a{sv})", (serial, method, payload, {}))
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
        self._last_applied: str | None = None
        self._rescan_interval_sec = 3

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

    def _periodic_rescan(self) -> bool:
        """Apply the last known orientation to sessions that appear later (e.g. GDM)."""
        if self._last_applied:
            self.rotator.apply(self._last_applied)
        return True

    def _wait_for_sensor_proxy(self) -> None:
        Gio.bus_watch_name(
            SENSOR_BUS,
            SENSOR_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_sensor_appeared,
            self._on_sensor_vanished,
        )
        GLib.timeout_add_seconds(self._rescan_interval_sec, self._periodic_rescan)
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

        self._sub_id = self._proxy.connect("g-signal", self._on_sensor_signal)
        orientation = self._read_orientation()
        if orientation:
            LOG.info("Initial orientation: %s", orientation)
            self._schedule_apply(orientation)

    def _on_sensor_vanished(self, _conn, _name, *_args) -> None:
        LOG.warning("iio-sensor-proxy vanished")
        self._proxy = None

    def _on_sensor_signal(
        self,
        _proxy,
        _sender: str,
        signal_name: str,
        params: GLib.Variant,
    ) -> None:
        if signal_name != "PropertiesChanged":
            return

        changed, _invalidated = params.unpack()
        if "AccelerometerOrientation" not in changed:
            return

        orientation = changed["AccelerometerOrientation"]
        LOG.debug("Orientation changed: %s", orientation)
        self._schedule_apply(orientation)

    def _read_orientation(self) -> str | None:
        if self._proxy is None:
            return None
        try:
            variant = self._proxy.get_cached_property("AccelerometerOrientation")
            return variant.get_string() if variant else None
        except GLib.Error:
            return None

    def _schedule_apply(self, orientation: str) -> None:
        self._pending_orientation = orientation
        if self._debounce_source is not None:
            return
        delay = max(0, self.settings.debounce_ms)
        self._debounce_source = GLib.timeout_add(delay, self._debounced_apply)

    def _debounced_apply(self) -> bool:
        self._debounce_source = None
        orientation = self._pending_orientation
        self._pending_orientation = None
        if not orientation or orientation == self._last_applied:
            return False
        if self.rotator.apply(orientation):
            self._last_applied = orientation
        elif self._last_applied is None:
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
    AutorotateDaemon(settings).start()


if __name__ == "__main__":
    main()
