# Attendance Biometric Middleware (Odoo Bridge)

Local network bridge that pulls attendance punches from biometric devices on
the LAN and pushes them to Odoo over HTTPS. It is the companion of the Odoo
module **`smt_attendance_biometric`** and is designed for cloud Odoo
deployments where the server cannot reach the devices directly.

- **Repository:** https://github.com/Bk-Soft10/Attendance-biometric-middleware.git
- **Polls all devices in parallel** — a dead/offline device never blocks the others.
- **Timezone-correct** — each device's local punch time is converted to UTC using
  the device's `device_tz` (from Odoo) before transmission.
- **Strict punch mapping** — `0`=Check In, `1`=Check Out, `2`=Break Out,
  `3`=Break In, `4`=Overtime In, `5`=Overtime Out; anything unexpected → `255`.

---

## 1. This deployment at a glance

| Setting | Value |
|---------|-------|
| Odoo URL | `https://www.samtia.com/` |
| Database | `adv-photonix-main-23293882` |
| Transport | `json2` (Odoo 19 JSON-2 API, USER API key) |
| Discovery | `auto_discover = true` (sync every active device from Odoo) |
| Sync interval | every 5 minutes |

### Devices on site (auto-provisioned in Odoo)

| Device | Model | IP : Port | Serial | Timezone |
|--------|-------|-----------|--------|----------|
| Main Gate | iWEB / ZKTeco **F18** | `192.168.2.12 : 4370` | `BAY5235001172` | `Asia/Riyadh` |
| Back Door | **BIOSENSE-T(EM)** | `192.168.2.49 : 2000` | `044ff5` (MAC `00:0e:e3:04:4f:f5`) | `Asia/Riyadh` |

> Both devices already exist in Odoo under **Attendances → Biometric → Devices**
> after the module is installed. You only need to add **Employee Mappings**
> (device PIN → Odoo employee) on each device.

### Architecture

```
+---------------------------------+        HTTPS (JSON-2)        +------------------+
|  Local bridge (this script)     |  ------------------------->  |  Odoo @ samtia   |
|  /opt/biometric-bridge          |   POST /json/2/biometric.log |  smt_attendance_ |
|  - ThreadPoolExecutor (parallel)|        /push_attendance       |  biometric       |
|  - pyzk over LAN                |                               +------------------+
+---------------------------------+
        |  TCP 4370 / 2000 (LAN)
        v
  [F18 192.168.2.12]   [BIOSENSE-T 192.168.2.49]
```

---

## 2. Prerequisites

1. An always-on **Linux PC / Raspberry Pi** (or Windows PC) **on the same LAN as
   the devices** — it must be able to reach `192.168.2.12` and `192.168.2.49`.
2. **Python 3.9+** and outbound HTTPS access to `https://www.samtia.com/`.
3. An Odoo **User API key**:
   **Preferences → Account Security → New API Key**. The owning user must have
   *HR Attendance* create rights.
4. Per device in Odoo: **Active** state, **IP/Port** set, and **Employee
   Mappings** configured (PIN → employee).

---

## 3. Installation

### Get the code

```bash
git clone https://github.com/Bk-Soft10/Attendance-biometric-middleware.git
cd Attendance-biometric-middleware
```

### Linux (systemd service)

```bash
sudo bash install.sh
```

Interactive prompts pre-fill from `config.ini`. For unattended provisioning:

```bash
sudo NONINTERACTIVE=1 \
     ODOO_URL=https://www.samtia.com/ \
     ODOO_DB=adv-photonix-main-23293882 \
     TRANSPORT=json2 \
     API_KEY=YOUR_USER_API_KEY \
     AUTO_DISCOVER=true \
     INTERVAL=5 \
     bash install.sh
```

The installer creates a virtualenv at `/opt/biometric-bridge/venv`, installs
`pyzk` + `requests`, writes `/opt/biometric-bridge/config.ini`, and registers a
systemd service.

### Windows (Scheduled Task)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 `
  -OdooUrl https://www.samtia.com/ -OdooDb adv-photonix-main-23293882 `
  -Transport json2 -ApiKey YOUR_USER_API_KEY -AutoDiscover true -Interval 5
```

### Manual (no installer)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# edit config.ini and set api_key
./venv/bin/python biometric_middleware.py --config config.ini --once -v
```

---

## 4. Configuration reference (`config.ini`)

```ini
[odoo]
url = https://www.samtia.com/
db = adv-photonix-main-23293882      ; required for this multi-DB host
transport = json2                    ; json2 (user key) | custom (device key)
api_key = REPLACE_WITH_YOUR_API_KEY
auto_discover = true                 ; sync all active devices from Odoo
device_code =                        ; only for single-device mode
timeout = 30

[device]                             ; ignored when auto_discover = true
host =
port = 4370
password = 0
type = auto

[sync]
interval_minutes = 5
batch_size = 100
clear_after_sync = false             ; keep false in production
since_hours = 24
retry_attempts = 3
retry_delay_seconds = 10

[logging]
level = INFO
file =
```

> **Why `db` matters here:** `www.samtia.com` hosts more than one Odoo database,
> so the bridge must name `adv-photonix-main-23293882` on every request. On a
> single-database / Odoo.sh host you would leave `db` blank.

---

## 5. Step-by-step verification & rollout

### Step 1 — Sandbox validation (once mode)

Run a single cycle with verbose logging to watch the live device handshakes,
pings, and JSON payloads before enabling the always-on service:

```bash
/opt/biometric-bridge/venv/bin/python biometric_middleware.py --config config.ini --once -v
```

A healthy run shows:

- `Using Odoo 19 JSON-2 transport (/json/2)` then `JSON-2 API reachable`
- `Auto-discovering devices from Odoo...` → `Discovered 2 device(s) to sync (parallel)`
- Interleaved `=== Syncing device: Main Gate - iWEB F18 (192.168.2.12:4370) ===`
  and `=== Syncing device: Back Door - BIOSENSE-T (192.168.2.49:2000) ===`
- `Retrieved N logs` and `Device ...: N sent, 0 failed` per device

If one device is unplugged you'll see `Failed to connect to ...` for **that
device only** — the other still syncs in the same cycle. That is the
concurrency guarantee.

### Step 2 — Enable the always-on service

**Linux:**
```bash
sudo systemctl enable --now biometric-bridge
sudo systemctl status biometric-bridge
sudo journalctl -u biometric-bridge -f     # live logs
```

**Windows:**
```powershell
Get-ScheduledTask -TaskName BiometricBridge | Get-ScheduledTaskInfo
```

### Step 3 — Verify ingestion in Odoo

1. **Attendances → Biometric → Attendance Logs** — punches appear (state
   **Processed**, or **Pending** if the PIN isn't mapped).
2. **Attendances → Attendances** — `hr.attendance` records show the **Biometric
   Device** and **Authentication Type**.
3. **Timezone check:** a punch at **08:00 Riyadh** is stored as **05:00 UTC** and
   displayed back in the user's timezone.

---

## 6. Follow-up & day-2 operations

### Routine checks

| Cadence | Action |
|---------|--------|
| Daily | Glance at `journalctl -u biometric-bridge --since today` for `failed` lines |
| Weekly | Confirm both devices reported recently (Odoo device **Last Seen**) |
| On staff change | Add/disable the **Employee Mapping** on the relevant device |
| On new device | Configure + activate it in Odoo only — the bridge auto-discovers it (no local change) |

### Common service commands (Linux)

```bash
sudo systemctl restart biometric-bridge      # apply config.ini changes
sudo systemctl stop biometric-bridge         # pause syncing
sudo journalctl -u biometric-bridge -f       # follow logs
```

### Re-run a one-off diagnostic safely

```bash
sudo systemctl stop biometric-bridge
/opt/biometric-bridge/venv/bin/python biometric_middleware.py --config config.ini --once -v
sudo systemctl start biometric-bridge
```

### Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Cannot connect to Odoo` | Wrong `url`; no outbound HTTPS; check `db` value |
| HTTP 403 / auth failed | Bad/expired `api_key`; for `json2` the user lacks attendance create rights |
| `database not found` / login page | `db` missing or misspelled in `config.ini` |
| One device never syncs | Ping its IP from the bridge host; confirm it is **Active** with IP set in Odoo |
| Punches stuck **Pending** | Add the **Employee Mapping** (PIN → employee) in Odoo |
| Punch shows **Undefined (255)** | Device button/status mapping; record stored but not counted as in/out |
| Wrong times in Odoo | Verify the device's **Device Timezone** in Odoo matches its real clock |

---

## 7. Command-line reference

```bash
# One cycle, verbose (diagnostics)
biometric_middleware.py --config config.ini --once -v

# Continuous daemon
biometric_middleware.py --config config.ini --daemon

# Override transport / database from the CLI
biometric_middleware.py --config config.ini --transport json2 --db adv-photonix-main-23293882 --once

# Single device (no auto-discovery)
biometric_middleware.py --no-auto-discover --host 192.168.2.12 --device-code DEV0001 \
  --url https://www.samtia.com/ --db adv-photonix-main-23293882 --api-key YOUR_KEY --once

# Generate a fresh sample config
biometric_middleware.py --create-config config.ini
```

## 8. Tests

```bash
python3 -m unittest test_middleware.py -v   # no Odoo / no device required
```

## License

LGPL-3 (matches the `smt_attendance_biometric` Odoo module).
