#!/usr/bin/env bash
# Install screen-autorotate on Fedora / GNOME Wayland systems.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing screen-autorotate..."

install -D -m 0755 "${ROOT}/lib/autorotate.py" /usr/libexec/screen-autorotate/autorotate.py
install -D -m 0755 "${ROOT}/bin/screen-autorotatectl" /usr/local/bin/screen-autorotatectl
install -D -m 0644 "${ROOT}/config/screen-autorotate.conf" /etc/screen-autorotate.conf
install -D -m 0644 "${ROOT}/systemd/screen-autorotate.service" /etc/systemd/system/screen-autorotate.service
install -D -m 0644 "${ROOT}/udev/61-proart-px13-accel.rules" /etc/udev/rules.d/61-proart-px13-accel.rules
install -D -m 0644 "${ROOT}/systemd/iio-sensor-proxy.service.d/override.conf" \
    /etc/systemd/system/iio-sensor-proxy.service.d/override.conf
install -D -m 0644 "${ROOT}/README.md" /usr/share/doc/screen-autorotate/README.md

# Dependencies on Fedora
if command -v dnf >/dev/null; then
    dnf install -y iio-sensor-proxy python3-gobject python3-dbus || true
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=iio || true

systemctl daemon-reload
# iio-sensor-proxy is D-Bus activated on Fedora (Type=dbus); restart, do not enable.
systemctl restart iio-sensor-proxy.service || systemctl start iio-sensor-proxy.service
systemctl enable screen-autorotate.service
systemctl restart screen-autorotate.service

# Allow iio-sensor-proxy a moment to read the accelerometer before status.
sleep 1

echo
echo "Installed. Check status with: screen-autorotatectl status"
screen-autorotatectl status
