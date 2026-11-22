# controller-mod-tool (beta)

Unofficial tool to add mods to game controller firmware. Supported controllers: **GameSir G8+ (Bluetooth
version)**, firmware 2.20, and **GameSir G8+ MFi (wired)**, firmware 8900 (build-only, see below). Pick the controller
type in the top row of the app. Terminal app for Linux, used with the arrow keys, Enter and Esc.

> [!CAUTION]
> **USE AT YOUR OWN RISK. NO WARRANTY. WE ARE NOT RESPONSIBLE FOR ANY DAMAGE.**
>
> - This is an **unofficial, experimental beta**. It is **not made, endorsed or supported by GameSir** (or any other
>   controller maker). "GameSir" and "G8+" are names of their owners, used only to say which hardware this works with.
> - Flashing modified firmware **can make your controller permanently unusable ("bricked")** and **may void its
>   warranty**.
> - The software is provided **"as is", without warranty of any kind**. The authors and contributors are **not
>   liable for any damage or loss of any kind**, whatever the cause, including but not limited to: mistakes or bugs
>   in this tool or its mods, a flat or low controller battery, a laptop that goes to sleep, loses power or loses
>   Bluetooth, a flash that is cancelled or interrupted for any reason, wrong use, or a controller or firmware
>   version this tool was not tested with.
> - Only use it if you accept that you alone are responsible for what happens to your controller.

## What it does

- Finds your controller over Bluetooth and shows which of the mods below it has.
- Lets you pick mods, builds that firmware from GameSir's **official firmware 2.20**, flashes it over Bluetooth and
  checks afterwards that the controller reports the new mods.
- Can also save a modded firmware file without flashing (to flash later, e.g. with GameSir's phone app),
  flash a firmware file as it is, or put the controller back on stock firmware.

### Mods (G8+ Bluetooth, Android mode = green light)

| Mod | Options | Status |
|---|---|---|
| Back buttons | stock (copy a face button) / independent: L4 = BUTTON_C, R4 = BUTTON_Z (set both paddles to unmapped, "null", in the GameSir app first) | tested |
| Input speed | Standard (~27 updates/s, stock) / Fast (~58/s) / Fastest (~115/s) | tested together with independent back buttons |
| Stick smoothing | Stock (two filters: ~24 ms + ~8 ms lag on slow moves) / Light (first filter off) / Off (both off) | tested on one controller: no idle jitter measured, feels good in games |

Faster input speed makes the Bluetooth radio work more often, so it probably uses more battery (not measured yet).
It can also disturb DS4 mode if your phone is paired with the controller in both modes. Combinations without the
back-button mod have not been tested on a controller yet; the app warns about them.

### G8+ MFi (wired, Android/HID mode): build-only

The app builds `G8-Plus-MFi-firmware-99xx.zip` from GameSir's **official firmware 8900**; you copy it to your phone
and flash it with the GameSir phone app ("local file"). The app does not flash the MFi itself. With the controller
plugged into the laptop it shows the USB mode and, for builds made by this app, which mods are installed (read from
what Linux already knows about the device; nothing is sent to the controller).

| Mod | Options | Status |
|---|---|---|
| Back buttons | stock (copy a face button) / independent: L4 = BUTTON_C, R4 = BUTTON_Z (set both paddles to unmapped, "null", in the GameSir app first) | tested |
| Home button | stock (held back ~0.8 s, Home + D-pad = volume) / remove Home button delay (Home + D-pad volume shortcut removed) | tested together with independent back buttons |
| Stick smoothing | stock / stick averaging off (the first stick filter passes moves straight through, est. 1-3 ms faster; its tiny noise deadband and the second filter stay) | not tested on a controller yet |

**File names:** `G8-Plus-MFi-firmware-99<number>.zip`, where the number is the sum of the chosen mods:

| Mod | Number |
|---|---|
| Independent back buttons | 1 |
| Remove Home button delay | 2 |
| Stick averaging off | 4 |

So 9901 = back buttons only, 9902 = Home delay fix only, 9903 = both, 9907 = all three. Starting at 9900 keeps the number above GameSir's
own firmware (8900), so the phone app accepts the file as a newer version, and the name tells you what's inside.

Files from this app also carry a one-byte build marker in the controller's descriptor, so the app can show which mods
are installed (tested: 9903 on a G8+ MFi, Android phone and Linux).

## What it does NOT include

**No GameSir firmware is included or distributed here**, only our own code and small edit files (lists of
"position: new bytes"). The app gets the official firmware 2.20 one of these ways:

1. you put `g8-bluetooth-firmware-2.20.zip` next to `g8tui.py` or in a `firmware/` folder there; or
2. you pick the file in the app; or
3. only if you choose so in the app: it downloads it from GameSir's own update server and saves it to `firmware/`.

Whatever the source, the app only applies mods to a file that is byte-identical to the official 2.20
(sha256 `8e5d2d0c49eecbc9e9bad78adf11e9e43a501c088693c9fdd4eea46978768160`). For the MFi the same applies to
`G8-Plus-MFi-firmware-8900.zip` (ota.bin sha256 `3c35f03453e97eedcc0e47bc59423f8c4c27d242e651918114737a98c0774fc5`).

## Updates

The app shows its version in the top line (`python3 g8tui.py --version` prints it too). **Check for updates** in the
menu asks GitHub for the newest release and tells you; it never downloads or installs anything. Update with
`git pull` in the app's folder. Versions follow [Semantic Versioning](https://semver.org); 0.x versions are beta.

## Requirements

- Linux with BlueZ (`bluetoothctl`) and Bluetooth LE. Tested on Arch-based CachyOS.
- Python **3.14** or newer (no extra packages, no root).
- The controller in **Android mode** (A + Home, green light). Turn your phone's Bluetooth off while flashing: if the
  controller is connected to the phone, the laptop can't see it.

## Use

```
python3 g8tui.py
```

Up/Down move, Left/Right or Enter change a mod, Enter runs an action, Esc goes back / quits. Before flashing:
charge the controller, plug the laptop in, keep both close together, and don't touch the controller until the
app says Done (about 2 minutes).

Optional: name your own controllers in `known_controllers.json` next to the script, for example
`{"A0:5A:59:12:34:56": "spare"}`. If you name one "spare" or "test", the app warns before flashing any other.

## If something goes wrong

- The flash failed but the controller still turns on in Android mode: flash again from the app.
- The controller doesn't work any more: hold **screenshot + Start for ~1 s** while it's on (recovery mode, blinking
  light blue) and update it with the **GameSir phone app**, or reset it to stock from this app.
- There is no guarantee that recovery works in every case (see the disclaimer above).

## How it works (short)

Firmware images are encrypted and protected by checksums. `g8fw.py` decrypts the official image, applies the edit
files in `mods/`, writes a one-byte marker (in the controller's HID descriptor, so the app can later read which mods
are installed), fixes the checksums, encrypts it again and checks that nothing else changed. `g8ota.py` sends it to the
controller over Bluetooth LE with the same update protocol the GameSir app uses.
`python3 g8tui.py --selftest` builds every mod combination and checks its marker (needs the official file, no network).

## Credits

Built on the work of **enij90** and their firmware patches for the Lenovo Legion G9 controller
(https://github.com/enij90/legion-g9-firmware), which runs on the same Jieli chip family. From that project we learned:

- how these Jieli firmware images are scrambled and how to unscramble them (the SFC keystream; the G8+ uses the same
  scheme with a different key, which we found ourselves),
- the approach of shipping no firmware: download the official image and apply small edit files (`offset:bytes`),
- how the back buttons are wired and remapped on a Jieli controller, which guided our G8+ work.

Our code is our own implementation; no code was copied from that project. Thank you, enij90!

## Licence

[MIT](LICENSE) for this project's own code and edit files. It covers nothing of GameSir's: their firmware stays
theirs, is not included here, and is downloaded by you from their own server.

This is an independent hobby project made for interoperability (letting the back buttons and sticks work as their
own inputs on Android and Linux). It is not affiliated with, endorsed by or supported by GameSir. If you represent
GameSir and have a concern, please open an issue and we will respond.
