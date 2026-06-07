# Disposable Home Assistant test instance

A turnkey `docker compose` setup to run the **aps_zigbee** integration against
real hardware without touching your production Home Assistant. The container:

- listens on **port 8124** of the host (so prod HA on 8123 keeps working);
- mounts your CH340 USB-TTL bridge as `/dev/ttyUSB0` inside the container;
- bind-mounts a local `./config/` directory you can wipe between tests.

## Layout on the test server

The instructions below assume the file layout shown below on the test
server. Anywhere this README mentions `~/docker/ha-test-aps/`, substitute
your actual path.

```
~/docker/ha-test-aps/
├── docker-compose.yml          # copy of this directory's docker-compose.yml
└── config/                     # created on first run; HA state lives here
    └── custom_components/
        └── aps_zigbee/         # we push the plugin here from the laptop
```

## Prerequisites

- Docker Engine + `docker compose` plugin on the test server.
- The CC2530 dongle flashed with Kadsol firmware, plugged in, and **not
  used by anything else** (ZHA / Zigbee2MQTT prod / another container).
  Quick check:
  ```bash
  ls -l /dev/serial/by-id/
  # → usb-1a86_USB_Serial-if00-port0 → /dev/ttyUSB0
  sudo fuser /dev/ttyUSB0
  # → empty output means the device is free
  ```
- Network reach from your laptop to the test server (any VPN / LAN /
  Tailscale setup works — WireGuard is what the reference build uses).
- An entry for the test server in your laptop's `~/.ssh/config` so the
  commands below can use a short alias. The placeholder `<server>` below
  stands for that alias.

## One-time bootstrap on the test server

```bash
# Run on the test server (over SSH).
mkdir -p ~/docker/ha-test-aps/config/custom_components
cd ~/docker/ha-test-aps

# Copy this directory's docker-compose.yml into place. Two ways:
#   (a) clone the repo and copy:
git clone https://github.com/technoo10201/hacs-aps-ds3.git /tmp/hacs-aps-ds3
cp /tmp/hacs-aps-ds3/docker/test-instance/docker-compose.yml .

#   (b) or just scp it from your laptop:
# (on laptop) scp docker/test-instance/docker-compose.yml <server>:~/docker/ha-test-aps/
```

If your CH340 doesn't show up as `usb-1a86_USB_Serial-if00-port0`, edit the
`devices:` line in `docker-compose.yml` accordingly.

## Push the plugin from your laptop (the iterating step)

From the **laptop**, inside the repo clone, after every code change:

```bash
# Replace `<server>` with your SSH alias, or use user@<vpn-ip>.
# --exclude='__pycache__' keeps your local CPython-3.10 .pyc out of the
# container's CPython-3.13 site — they would otherwise sit there cold and
# clutter the diff between iterations.
rsync -avz --delete --exclude='__pycache__' \
    custom_components/aps_zigbee/ \
    <server>:~/docker/ha-test-aps/config/custom_components/aps_zigbee/
```

The first push will create `aps_zigbee/`; subsequent pushes will update it.

## Start (or restart) Home Assistant

```bash
# On the test server.
cd ~/docker/ha-test-aps
docker compose up -d --build   # initial start (builds the local image: thin
                                # layer over HA stable that fixes the numpy
                                # null-bytes bug — see Dockerfile).
# or, after a plugin update:
docker compose restart homeassistant
```

> If you ever skip `--build`, an `up` will only rebuild when the Dockerfile
> changes. `docker compose down && up -d` keeps the rebuilt image and is the
> right way to recycle the container without losing the numpy fix.

Logs:

```bash
docker compose logs -f homeassistant
# Look for:
#   "Setting up aps_zigbee"
#   "ZNP TX FE03 26050301 03 21"  (only with --debug logging enabled)
```

Open `http://<server-ip>:8124/` from your laptop, finish the HA onboarding
wizard (create the test user, **set the location so `sun.sun` exists** —
the night-mode gating in the integration relies on it).

## Add the integration

1. **Settings → Devices & Services → Add integration**
2. Search for *APsystems Zigbee*.
3. Serial port: enter `/dev/ttyUSB0` (we mapped the by-id symlink to this
   stable name in `docker-compose.yml`).
4. ECU id: keep the default `D8A3011B9780` (it matches the upstream
   firmware) — unless you have already paired your inverters against
   another coordinator that uses a different value.
5. Polling interval: 60 s is fine to start with.

Then pair each inverter through **Configure → Pair a new inverter**.

## Verbose logging while testing

Add this to `~/docker/ha-test-aps/config/configuration.yaml` (create the
file if it doesn't exist):

```yaml
logger:
  default: warning
  logs:
    custom_components.aps_zigbee: debug
    custom_components.aps_zigbee.aps_protocol: debug
```

Then `docker compose restart homeassistant`.

## Resetting between iterations

Wipe everything except the docker-compose:

```bash
cd ~/docker/ha-test-aps
docker compose down
rm -rf config
docker compose up -d
```

Then rsync the plugin back and onboard again.

## Tearing it all down

```bash
cd ~/docker/ha-test-aps
docker compose down
# Optional: drop the image too.
docker rmi ghcr.io/home-assistant/home-assistant:stable
```

The production Home Assistant on port 8123 is **never** touched by any of
the above.
