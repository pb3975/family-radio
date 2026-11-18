# Family Radio

Family Radio is a tabletop music player with a rotary dial and a small round screen. A Raspberry Pi plays the audio. An M5Stack Dial sends controls over USB and shows the current track. The listener can browse a local library, choose a track, skip or pause without using a phone.

A parent can manage music through a local dashboard. The software also supports approved Spotify content and includes a phone-based Wi-Fi setup portal. You can try the dashboard and setup screens on a computer with sample data.

## Try the dashboard

On Linux with Python 3 and FFmpeg installed, run these commands from the repository root.

```sh
demo_dir=$(mktemp -d)
python3 host/dashboard.py demo --data-dir "$demo_dir"
python3 host/dashboard.py serve --data-dir "$demo_dir"
```

Open the single-use login link printed in the terminal. Press Ctrl+C to stop the server.

## Build and hardware status

The original prototype demonstrated stereo playback, browsing, selection, skipping and quiet pause on hardware. This public edition has different service and device names. Its host and firmware have not been tested together on a physical radio. Check the fit coupons against your own parts before printing a full enclosure.

- [Development guide](docs/development.md) covers the test suite, firmware build, portal demo and enclosure checks.
- [Device interface](docs/device-interface.md) describes the host and Dial protocol.
- [Public release scope](docs/public-release.md) explains compatibility and the contents of this edition.
- [MIT license](LICENSE) and [dependency notice](NOTICE) cover use and redistribution.
