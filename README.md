# Screen Autorotate — ASUS ProArt PX13

Automatic screen rotation for the **ASUS ProArt PX13 (HN7306)** on Fedora/GNOME Wayland, including the **GDM login screen**.

## How it works

| Layer | Component |
|-------|-----------|
| Hardware | AMD HID sensor hub (`hid_sensor_accel_3d`) |
| Kernel | IIO device `accel_3d` at `iio:device0` |
| Sensor daemon | `iio-sensor-proxy` (orientation via D-Bus) |
| Rotation | GNOME Mutter `DisplayConfig` on active sessions |
| Service | `screen-autorotate.service` (system-wide) |

The service listens for **`AccelerometerOrientation`** property changes from `iio-sensor-proxy` (event-driven, not polling) and rotates only the internal panel (`eDP-1`), leaving external monitors untouched.

### Orientations

| Sensor (`iio-sensor-proxy`) | Screen transform |
|-----------------------------|------------------|
| `normal` | 0° (landscape) |
| `right-up` | 90° clockwise |
| `bottom-up` | 180° |
| `left-up` | 270° clockwise |

## Requirements

- ASUS ProArt PX13 (product name contains `ProArt PX13`)
- Fedora with GNOME on Wayland + GDM
- Packages: `iio-sensor-proxy`, `python3-gobject`

## Install

```bash
git clone <this-repo>
cd screen-autorotate
sudo ./install.sh
```

## Enable / disable

```bash
# Boot-time enable/disable
sudo screen-autorotatectl enable
sudo screen-autorotatectl disable

# Runtime toggle (updates /etc/screen-autorotate.conf)
sudo screen-autorotatectl on
sudo screen-autorotatectl off

# Status (sensor, orientation, service)
screen-autorotatectl status
```

## Configuration

Edit `/etc/screen-autorotate.conf`:

```ini
[autorotate]
enabled = true
internal_connector = eDP-1
product_match = ProArt PX13
require_product = true
apply_persistent = false
debounce_ms = 400
orientation_map = normal:0,bottom-up:2,right-up:1,left-up:3
```

After changes: `sudo systemctl restart screen-autorotate`

## Pre-login (GDM)

The service runs as root and connects to every active session D-Bus (`/run/user/*/bus`) that exposes `org.gnome.Mutter.DisplayConfig`. This includes:

- **GDM greeter** (`/run/user/42/bus`) at the login screen
- **Your user session** after login

## Avoid conflicts with GNOME

GNOME Settings → Displays includes a built-in **Auto Rotate** toggle. Disable it if you use this service, especially with external monitors connected — this service only rotates `eDP-1`, while GNOME may rotate the primary display.

## Troubleshooting

**Check sensor:**
```bash
monitor-sensor
# or
busctl get-property net.hadess.SensorProxy /net/hadess/SensorProxy \
  net.hadess.SensorProxy AccelerometerOrientation
```

**Check accelerometer raw values:**
```bash
cat /sys/bus/iio/devices/iio:device0/name
cat /sys/bus/iio/devices/iio:device0/in_accel_{x,y,z}_raw
```

**Service logs:**
```bash
journalctl -u screen-autorotate -f
journalctl -u iio-sensor-proxy -f
```

**If orientation is inverted**, adjust `orientation_map` in the config.

## Uninstall

```bash
sudo systemctl disable --now screen-autorotate.service
sudo rm -f /etc/systemd/system/screen-autorotate.service \
  /etc/systemd/system/iio-sensor-proxy.service.d/override.conf \
  /etc/screen-autorotate.conf \
  /etc/udev/rules.d/61-proart-px13-accel.rules \
  /usr/local/libexec/screen-autorotate/autorotate.py \
  /usr/local/bin/screen-autorotatectl
sudo rm -rf /usr/local/libexec/screen-autorotate \
  /usr/local/share/doc/screen-autorotate
sudo systemctl daemon-reload
```
