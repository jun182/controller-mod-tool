# controller-mod-tool (beta)

Unofficial tool to add mods to game controller firmware. First supported controller: **GameSir G8+ (Bluetooth
version)**, firmware 2.20. Terminal app for Linux, used with the arrow keys, Enter and Esc.

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
| Back buttons | stock (copy a face button) / independent: L4 = BUTTON_C, R4 = BUTTON_Z | tested |
| Input speed | Standard (~27 updates/s, stock) / Fast (~58/s) / Fastest (~115/s) | tested together with independent back buttons |

Faster input speed makes the Bluetooth radio work more often, so it probably uses more battery (not measured yet).
It can also disturb DS4 mode if your phone is paired with the controller in both modes. Combinations without the
back-button mod have not been tested on a controller yet; the app warns about them.

## What it does NOT include

**No GameSir firmware is included or distributed here**, only our own code and small edit files (lists of
"position: new bytes"). The app gets the official firmware 2.20 one of these ways:

1. you put `g8-bluetooth-firmware-2.20.zip` next to `g8tui.py` or in a `firmware/` folder there; or
2. you pick the file in the app; or
3. only if you choose so in the app: it downloads it from GameSir's own update server and saves it to `firmware/`.

Whatever the source, the app only applies mods to a file that is byte-identical to the official 2.20
(sha256 `8e5d2d0c49eecbc9e9bad78adf11e9e43a501c088693c9fdd4eea46978768160`).

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

Inspired by enij90's firmware patches for the Lenovo Legion G9 controller (https://github.com/enij90/legion-g9-firmware).

## Licence

Not decided yet. Until a licence file is added, no licence is granted.
