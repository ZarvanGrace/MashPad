# MashPad

Turn your phone into a gamepad over your LAN.

    python3 -m venv venv && . venv/bin/activate
    pip install -r requirements.txt
    python3 app.py        # prints a QR code: scan it with your phone (same Wi-Fi)

A controller appears when a phone joins and vanishes when it leaves. The terminal
shows `Players connected: N/max` every time that changes.
If a game never notices new controllers, set "persistent_pads": true in config.json
and start the app BEFORE launching that game. Settings live in config.json
(port, max_players, https, stick_deadzone, stick_smoothing).
Keep the folder structure as it is: app.py, config.json, assets/pad.html, js/*.js.

## Lag / freezes: turn off Wi-Fi power saving on the laptop

Wi-Fi power saving makes the laptop's card doze between packets, which shows up as
random half-second freezes followed by a burst. The app warns you at startup if it is on.

    sudo iw dev <your-wifi-interface> set power_save off     # right now (`iw dev` shows the name)

To keep it off after reboots, create /etc/NetworkManager/conf.d/wifi-powersave.conf:

    [connection]
    wifi.powersave = 2

then `sudo systemctl restart NetworkManager`. A 5 GHz network and a phone close to the router help too.
The ping line under the status text on the phone shows the round trip; green is under 40 ms.
If it turns into a growing number with a "+", the link is stalling right then.

## Stick tuning (config.json)

- `stick_deadzone` (default 0.05): tiny centre dead zone that only hides finger jitter. It is rescaled,
  so output still starts at 0 and reaches full at the edge. Games add their own, bigger one on top.
- `stick_smoothing` (default 1.0): 0 turns smoothing off, 1 adds about one frame (~15 ms) on slow
  movements and nothing on fast ones, 2 is smoother and a bit laggier.

## License

MashPad is released under the [MIT License](LICENSE).

It bundles two third-party libraries in `js/`, unmodified, under their own licenses:

- `js/qrcode.js`: [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) by Kazuhiko Arase, MIT
- `js/jsQR.js`: [jsQR](https://github.com/cozmo/jsQR) by Cosmo Wolfe and contributors, Apache-2.0

Their license texts are in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
"QR Code" is a registered trademark of DENSO WAVE INCORPORATED.
