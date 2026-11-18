# Development guide

Run the commands from the repository root on Linux. The local demos and tests use synthetic data. They need no cloud account or connected radio.

## Host and local demos

The host tests target Python 3.13. On Ubuntu, install the media tools and native compiler, then run the suite.

```sh
sudo apt-get update
sudo apt-get install -y ffmpeg mpv g++ python3-venv
python3 -m unittest discover -s tests -v
```

To try the Wi-Fi setup flow, start the demo and visit the address printed by the server. Choose a synthetic network. The demo simulates joining and leaves the computer's network settings alone.

```sh
python3 host/wifi_portal.py --port 8080
```

For the dashboard demo, follow the [quick start](../README.md#try-the-dashboard). To build a local music manifest, set the source directory to a folder containing MP3 or WAV files you have permission to use.

```sh
music_source="$HOME/Music"
library_dir=$(mktemp -d)
python3 host/library.py "$music_source" --destination "$library_dir"
```

## Browser and networking checks

Use Node.js 22 for browser checks. Playwright stores its generated screenshots under the chosen output directory.

```sh
browser_dir=$(mktemp -d)
npm install --prefix "$browser_dir" playwright@1.63.0
"$browser_dir/node_modules/.bin/playwright" install --with-deps chromium
export PLAYWRIGHT_MODULE="$browser_dir/node_modules/playwright"
node tools/check_dashboard.cjs --output "$browser_dir/dashboard"
node tools/check_wifi_portal.cjs --output "$browser_dir/portal"
node tools/check_wifi_portal.cjs --radio-fixture --output "$browser_dir/radio"
```

Run the service configuration and packet-filter checks on a disposable Linux machine with systemd. These checks require root privileges.

```sh
sudo apt-get install -y network-manager dnsmasq-base nftables
sudo python3 tools/check_wifi_artifacts.py
```

## Dial firmware

Install Arduino CLI 1.5.1. Install the ESP32 board support package and the exact display library versions below. Compile the sketch without connecting the Dial.

```sh
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.11
arduino-cli lib update-index
arduino-cli lib install --no-deps 'M5GFX@0.2.29' 'M5Unified@0.2.22' 'M5Dial@1.0.3'
build_dir=$(mktemp -d)
arduino-cli compile --fqbn esp32:esp32:m5stack_dial --build-path "$build_dir" firmware/family_dial
```

The native layout check exercises the firmware display code. A separate decoder reads the QR output and checks its payload.

```sh
layout_dir=$(mktemp -d)
python3 -m venv "$layout_dir/venv"
"$layout_dir/venv/bin/pip" install --only-binary=:all: zxing-cpp==3.1.1 Pillow==13.0.0
"$layout_dir/venv/bin/python" tools/check_dial_layout.py --output "$layout_dir/screens"
"$layout_dir/venv/bin/python" tools/check_wifi_qr.py --manifest "$layout_dir/screens/wifi-qr.json" --self-test
```

## Enclosures

The Blender source and STL meshes are under `cad/`. Start with mesh checks. They run with the Python standard library.

```sh
python3 cad/test_checks.py
python3 cad/check_stl.py cad/stl/*.stl
```

With Blender 5.2.2 on your path, regenerate the meshes in a clean checkout and check clearance around the V1 connectors.

```sh
blender -b --python-exit-code 1 --python cad/build.py
blender -b --python-exit-code 1 --python cad/test_v1_keepouts.py
git diff --exit-code -- cad/stl
```

To preview the shapes and color options, generate the images locally. Open the resulting images under `cad/renders/`.

```sh
blender -b --python-exit-code 1 --python cad/build.py -- --render
blender -b --python-exit-code 1 --python cad/colorways.py
python3 cad/check_renders.py
python3 cad/check_renders.py --colorways
```

For the slicing check, set `ORCA_BIN` to the OrcaSlicer 2.4.2 executable and `ORCA_PROFILES` to its `resources/profiles/BBL` directory.

```sh
python3 cad/slice_check.py --orca "$ORCA_BIN" --profiles "$ORCA_PROFILES" cad/stl/*.stl
```

Mesh and slicer checks cannot determine whether an individual speaker, knob or connector fits. Print the matching coupon and check it against the actual part before printing a full enclosure.

## Install the local player on a dedicated Pi

Use these steps on a fresh Debian-based target with systemd, a working default ALSA output, and a Dial running the matching firmware. Start in the repository root on the target. The commands install the local library player and the optional dashboard. For phone-based onboarding, follow the [optional Wi-Fi setup](#optional-wi-fi-setup-on-a-pi) after installing the player.

1. Install the media tools and create a service account with a persistent home. On an existing installation, check the account and home before reusing them.

   ```sh
   sudo apt-get update
   sudo apt-get install -y python3 ffmpeg mpv
   sudo useradd --system --user-group --create-home --home-dir /var/lib/family-radio --shell /usr/sbin/nologin family-radio
   sudo usermod --append --groups audio,dialout family-radio
   ```

2. Install the host files and service units.

   ```sh
   sudo install -d /usr/local/lib/family-radio/host
   sudo cp -R host/. /usr/local/lib/family-radio/host/
   sudo install -m 0644 deploy/systemd/*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

3. Import at least one MP3 or WAV file. Set `music_source` to a directory the service account can read and traverse.

   ```sh
   music_source=/srv/music
   sudo -u family-radio -H python3 /usr/local/lib/family-radio/host/library.py "$music_source"
   ```

4. Connect the Dial over USB. The player selects a device when its default pattern matches exactly one entry under `/dev/serial/by-id/`. If several devices match, set `FAMILY_RADIO_DEVICE` to the Dial's full stable path in `/var/lib/family-radio/.local/share/family-radio/service.env`. Keep that file on the device.

5. Start the player and inspect its status.

   ```sh
   sudo systemctl enable --now family-radio.service
   systemctl status family-radio.service
   ```

6. To use the optional dashboard on the same machine, start its service and get a single-use login link. Open the link in a local browser. The service binds to loopback.

   ```sh
   sudo systemctl enable --now family-radio-dashboard.service
   sudo -u family-radio -H python3 /usr/local/lib/family-radio/host/dashboard.py login-url
   ```

To stop the services and disable automatic startup, use the following command. The imported library remains in the service account's home.

```sh
sudo systemctl disable --now family-radio-dashboard.service family-radio.service
```

## Run the privacy checks before pushing

Install Gitleaks 8.30.1 before enabling the pre-push hook in your checkout. On Linux x86-64, download the official archive and verify its SHA-256 checksum before installing the executable.

```sh
(
  set -eu
  download_dir=$(mktemp -d)
  cd "$download_dir"
  archive=gitleaks_8.30.1_linux_x64.tar.gz
  curl --fail --silent --show-error --location --output "$archive" \
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/$archive"
  echo '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  gitleaks_8.30.1_linux_x64.tar.gz' | sha256sum --check
  tar --extract --gzip --file "$archive" gitleaks
  install -Dm0755 gitleaks "$HOME/.local/bin/gitleaks"
)
export PATH="$HOME/.local/bin:$PATH"
git config core.hooksPath .githooks
```

The hook checks the working tree for private filenames and personal details before Git sends a push. It also scans the working tree and local Git history for secrets. Run it from a clean checkout so it checks the files you plan to share. GitHub Actions repeats the checks after a push or pull request.

Review the commits and files yourself before publishing. The checks can miss personal details in history, images or other artifacts. Start any public release from a reviewed snapshot and fresh history. Avoid mirroring private repositories.

## Optional Wi-Fi setup on a Pi

The phone portal demo above runs without changing the network. On a radio, the setup service temporarily switches `wlan0` from its current network to a setup access point. Configure this feature manually with a local console available. These installation steps have not been verified on a physical target for this edition.

1. Install the local player first. Install `network-manager`, `dnsmasq-base` and `nftables`. Confirm that NetworkManager manages `wlan0`, the adapter supports access point mode, and the system provides the `dnsmasq` user and `nogroup` group. Check that both NetworkManager connection directories exist under `/run/NetworkManager/` and `/etc/NetworkManager/` as `system-connections/`. Leave their existing ownership and permissions in place.

2. On a fresh installation, create the unprivileged portal account and the separate radio socket access group.

   ```sh
   sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin family-wifi
   sudo groupadd --system family-wifi-ctl
   ```

3. From the repository root, install the configurations as root. The radio service drop-in grants socket access only to the player service. The broker checks the connecting process's user ID against the `family-radio` account. The portal runs as `family-wifi` and uses a separate control socket.

   ```sh
   sudo install -d /usr/local/lib/family-radio/deploy/wifi /etc/systemd/system/family-radio.service.d
   sudo install -m 0644 deploy/wifi/dnsmasq.conf deploy/wifi/isolation.nft /usr/local/lib/family-radio/deploy/wifi/
   sudo install -m 0644 deploy/wifi/*.service deploy/wifi/*.socket /etc/systemd/system/
   sudo install -m 0644 deploy/wifi/radio-wifi-control.conf /etc/systemd/system/family-radio.service.d/
   sudo install -m 0644 deploy/wifi/family-radio-wifi-tmpfiles.conf /etc/tmpfiles.d/
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/family-radio-wifi-tmpfiles.conf
   sudo python3 /usr/local/lib/family-radio/host/wifi_setup.py init
   sudo systemctl daemon-reload
   ```

   Initialization stores the generated setup network name under `/var/lib/family-radio-wifi`. The temporary-files rule creates `/run/family-radio-wifi-status`. Both directories belong to root and use mode 0700. The socket unit gives the control group access to its root-owned socket with mode 0660.

4. With NetworkManager active, enable recovery and the broker socket. Restart the player when playback can be interrupted so systemd applies its new group. The socket starts the fixed-operation broker on demand. The setup session starts and stops its own HTTP and DNS services.

   ```sh
   sudo systemctl enable --now family-radio-wifi-recover.service family-radio-wifi-radio.socket
   sudo systemctl restart family-radio.service
   systemctl is-active family-radio-wifi-radio.socket family-radio.service
   ```

5. At the local console, confirm that the Dial can start setup, display its QR code, cancel setup and reconnect to the previous network. Test joining the intended network and reconnecting after a reboot before relying on remote access. Keep generated credentials and saved NetworkManager profiles on the device.
