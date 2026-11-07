# Prototype Pi

Before the frame is assembled, a Raspberry Pi 5 with the Inky Impression 7.3
inch (Spectra 6, panel E673) attached sits on the bench so screens can be
checked on the real panel. This note covers reaching that prototype from the Mac
and pushing an image to it.

No addresses or account names here: reach the Pi by its mDNS hostname and let
your SSH config supply the login.

## Reaching it

The Pi answers to `nightstand.local` on the LAN over mDNS, with key-based SSH.
Put the login in your `~/.ssh/config` so a bare host works:

```
ssh nightstand.local uname -m      # prints aarch64
```

## Installing and updating the software

Run the installer as root. The snaphouse bootstrap does this once on first
install; on the prototype you can also run it by hand from a checkout on the Mac:

```
scp deploy/install.sh nightstand.local:/tmp/
ssh nightstand.local 'sudo bash /tmp/install.sh'
```

It is idempotent and safe to re-run. It installs git and curl if the image lacks
them, enables SPI and I2C, adds the `spi0-0cs` device-tree overlay, installs uv
and a uv-managed Python under `/opt/uv/python` (a shared location so the
unprivileged service user can run the interpreter), syncs the environment with
the `pi` extra, and installs the display service, the self-update timer, and the
art-sync timer.

### The SPI overlay needs a reboot

The Pi 5's default `spi0` claims GPIO8 as its chip-select, which the Inky library
also needs. The `spi0-0cs` overlay frees it, but only after a reboot:

```
ssh nightstand.local 'sudo reboot'
```

Until the Pi reboots, driving the panel fails with "some pins we need are in
use" even though `inky.auto()` already detects the panel. After the reboot the
display service starts on its own and paints the panel, with no manual steps.

## Pushing one image to the panel

`nightstand show` loads a PNG and pushes it through the Inky driver. Render a
screen on the Mac, copy it over, and show it. The display service redraws on its
own cycle, so stop it (and the self-update timer, so it does not switch the
checkout under you) while you push by hand, then start both again:

```
# On the Mac: render any screen to a PNG.
uv run nightstand preview weather \
  --fixture tests/fixtures/openmeteo/forecast.json -o /tmp/x.png
scp /tmp/x.png nightstand.local:/tmp/

# On the Pi: stop the service, show the image, start the service again.
ssh nightstand.local 'sudo systemctl stop nightstand nightstand-update.timer'
ssh nightstand.local 'sudo -u nightstand /opt/nightstand/.venv/bin/nightstand show /tmp/x.png'
ssh nightstand.local 'sudo systemctl start nightstand nightstand-update.timer'
```

Run `show` as the `nightstand` service user: it has the SPI, I2C, and GPIO group
membership the panel needs. A full refresh of the six-color panel takes roughly
25 seconds.

`--saturation N` (0.0 to 1.0) tunes how vivid the six inks render; left off, the
driver's own default stands. A screen from `nightstand preview` is already
reduced to the six panel colors, so `show` sends it as given.

## Checking colors against the preview

`nightstand preview <screen> --panel ...` writes how a design should read on the
muted panel. To check the panel's color, show an image and compare it against its
`--panel` preview side by side. The muted approximations live in
`src/nightstand/palette.py` (`PANEL`); adjust them only against the real panel,
then regenerate the goldens with `UPDATE_GOLDENS=1 uv run pytest` and review each
PNG before committing.
