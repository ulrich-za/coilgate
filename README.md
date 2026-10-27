# CoilGate

Secure, self-hosted control of a Raspberry Pi's inputs and outputs from a browser, from anywhere. Switch relays (lights, gates, pumps), watch inputs (door contacts, buttons) and sensors (temperature, humidity, pressure), let rules do the switching for you, and manage who can do what, all behind a hardened sign-in.

- **Everything is configured in the app.** Admins add, rename, reorder and remove inputs, outputs and sensors, build rules and schedules, create accounts, and adjust security settings from the Admin area. Changes apply straight away, without a restart.
- **Rules read like sentences:** "When **Living Room Temperature** is above **26 °C**, turn on **Living Room Aircon**, and turn it off again when it drops to **24 °C**." They can wait ("only if the door has been open for 2 minutes"), be limited to a schedule, and start or switch other rules. Build them as a sentence or by dragging pieces into place.
- **A live pin map.** The Pinout page shows the Pi 5's 40-pin header with what every pin can do, adapted from [Pinout.xyz](https://pinout.xyz). The pins CoilGate uses light up as outputs switch and inputs change, there and beside the controls.
- **Schedules** are weekly time windows, such as "Weekdays 08:00 to 17:00". They can start rules, limit rules, and limit when an account may use the controls. "Always (24/7)" is built in.
- **Groups decide who can do what.** Admin, Super User and User are built in, and admins can add their own, such as "Family" or "Viewers" who can see the controls but not switch anything. Each group says whether it needs two-step verification, so people without an authenticator app can still have an account.
- **Runs on Windows for setup and testing**, with simulated pins, and on a Raspberry Pi 5 for real.
- **Built on:** Python with Flask, served by Waitress; SQLite for the database (one file, no server to run); gpiozero with lgpio for the Pi 5's GPIO; Caddy or Cloudflare Tunnel for HTTPS.

---

## Contents

- [Security measures](#security-measures)
- [Part 1: Try it on Windows](#part-1-try-it-on-windows)
- [Part 2: Install on the Raspberry Pi 5](#part-2-install-on-the-raspberry-pi-5)
- [Part 3: Reach it from any network](#part-3-reach-it-from-any-network)
- [Part 4: Wiring outputs and inputs](#part-4-wiring-outputs-and-inputs)
- [Using CoilGate](#using-coilgate)
- [Command-line tool](#command-line-tool)
- [config.toml settings](#configtoml-settings)
- [Upgrading from Gatehouse](#upgrading-from-gatehouse)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)
- [Licence](#licence)

---

## Security measures

| Area | What CoilGate does |
|---|---|
| Password storage | Argon2id (RFC 9106: 64 MiB, 3 passes). Hashes upgrade automatically at sign-in if the parameters are raised. |
| Password rules | NIST SP 800-63B-4: 15+ characters by default, no forced symbols, Unicode allowed. Rejects common and sequential passwords, the username, and passwords found in data breaches (Have I Been Pwned via k-anonymity: only 5 characters of the hash leave the Pi). |
| Two-step verification | TOTP codes from any authenticator app, **required for Admin and User accounts by default** and set per group. Super User doesn't need it by default. Used codes can't be replayed; 5 wrong codes ends the attempt. |
| Accounts | Every account is in a group with its own privileges ([Groups](#groups-and-privileges)). No public sign-up: accounts are created with a one-time temporary password (about 98 bits, valid 72 hours), and the new user must choose their own password (and set up two-step verification, if their group needs it) before anything else. Nobody can change their own group, disable or delete themselves, and the last admin can't be removed. Only admins manage admin accounts and groups, so a Super User can't make themselves an admin. |
| Admin area | Only the parts an account's group allows; everything else answers "page not found", even by address. Optionally limited to your home network or VPN (`admin_networks`). |
| Brute force | 5 failures per username or 20 per IP address within 15 minutes triggers a lock (all adjustable). Applies to non-existent usernames too, so the lock doesn't reveal which accounts exist. |
| Username discovery | Same message and the same response time whether the username exists or not. |
| Sessions | Server-side. The browser holds a random 256-bit token; only its SHA-256 hash is stored. New token at every sign-in. Signed out after 30 minutes idle or 12 hours total. The Controls page's live updates don't count as activity, so an open tab doesn't keep a session alive. |
| Cookies | `HttpOnly`, `SameSite=Lax`; over HTTPS also `Secure` with the `__Host-` prefix. |
| Forged requests (CSRF) | Per-session token on every form, plus a Fetch Metadata check that refuses cross-site form posts. |
| Browser hardening | Strict Content-Security-Policy: scripts and styles only from CoilGate itself, nothing inline, no third parties; no framing (clickjacking), `nosniff`, no referrer, `no-store` caching, HSTS over HTTPS. |
| Controls | Every switch and every input change is logged with who, when and from where; rule actions are logged with the rule's name. Per-user limit of 30 actions a minute, and a rule that acts more than 60 times in a minute (both adjustable) is paused for a minute, then catches up. Accounts can be limited to a schedule (admins never are, so nobody can lock out the last admin). Outputs start off whenever CoilGate starts. On/off buttons send explicit "on" or "off", so a double-click can't flip a relay back. |
| Reverse proxy | CoilGate listens only on `127.0.0.1`. Forwarded visitor addresses are believed only from the proxy, and only the last (proxy-added) entry is used, so a forged `X-Forwarded-For` can't dodge the per-IP lock. |
| Deployment | On the Pi it runs as an unprivileged account in a sandboxed systemd service: read-only system, write access only to its data folder, no hardware access except the GPIO chips and I²C buses. The data folder is readable only by that account. |

---

## Part 1: Try it on Windows

Windows has no GPIO pins, so inputs and outputs are simulated: everything works, but nothing physical switches. That lets you set CoilGate up completely before the Pi is involved.

### 1. Install Python

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/) (tick **Add python.exe to PATH**), or in PowerShell:

```powershell
winget install Python.Python.3.13
```

Close and reopen PowerShell, then check: `py --version`

### 2. Set up the project

Download `coilgate.zip` from the [latest release](https://github.com/ulrich-za/coilgate/releases/latest) and extract it somewhere in your user folder, for example `C:\Users\<you>\coilgate`, then:

```powershell
cd $HOME\coilgate
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

### 3. Create the first admin

```powershell
.\.venv\Scripts\python manage.py create-user admin
```

The first account is always an admin. You'll be asked for a password twice (nothing shows while you type).

### 4. Start it

```powershell
.\.venv\Scripts\python run.py
```

Open **http://127.0.0.1:8000**. Press `Ctrl+C` in PowerShell to stop it.

### 5. Sign in and set up two-step verification

Two-step verification is required by default, so after your password CoilGate takes you straight to setting it up: scan the QR code with your authenticator app and enter a code. From then on, each sign-in asks for a code.

### 6. Add inputs and outputs

Open **Admin > Inputs and outputs**:

- **Add an output** for each relay: a name, the GPIO pin, on/off or pulse, the pulse length (0.1 seconds to 24 hours), and whether the relay board triggers on high or low.
- **Add an input** for each contact or sensor: a name, the GPIO pin, how it's wired, and optional names for its two states (for example "Shut" and "Open").

- **Add a sensor** for each temperature, humidity or pressure reading: DS18B20 probes, BME280 boards and DHT22/AM2302 sensors. See [Sensors](#sensors) for wiring.

Then try them on the **Controls** page. In simulation each input has a **Simulate a change** button, and each sensor a box to type a pretend reading, which is the easiest way to try out rules.

### 7. Add schedules and rules

**Admin > Schedules** for time windows, then **Admin > Rules > Add a rule**. See [Schedules](#schedules) and [Rules](#rules) for what they can do.

### 8. Add accounts

**Admin > Accounts > Add an account**: choose a username, a group and, for anyone but admins, when they can use the controls. CoilGate shows a temporary password once. At their first sign-in the person chooses their own password and, if their group needs it, sets up two-step verification. See [Groups](#groups-and-privileges) for what each group can do.

### 9. Run the tests (optional)

```powershell
.\.venv\Scripts\python -m pip install pytest
.\.venv\Scripts\python -m pytest -q
```

All 265 tests should pass.

### HTTPS on Windows (optional)

Plain HTTP to `127.0.0.1` never leaves your PC, so it's fine for testing. To rehearse the HTTPS setup anyway:

1. Download `caddy_*_windows_amd64.zip` from the [Caddy releases page](https://github.com/caddyserver/caddy/releases) and put `caddy.exe` in the project folder.
2. Copy `deploy\Caddyfile.windows` to `Caddyfile` in the project folder.
3. Copy `config.example.toml` to `config.toml` and set `secure_cookies = true` and `trust_proxy = 1`.
4. Start CoilGate, then in a second PowerShell window run `.\caddy.exe run`. Accept the prompt to trust Caddy's certificate.
5. Open **https://localhost**.

With `secure_cookies = true`, `http://127.0.0.1:8000` shows "This form expired": browsers only keep secure cookies over HTTPS.

### Bringing your Gatehouse account across

Stop Gatehouse and copy its `instance` folder into the CoilGate folder before the first start. CoilGate renames `gatehouse.db` to `coilgate.db`, upgrades it, and makes your existing account an admin. If your Gatehouse `config.toml` had settings or `[[outputs]]`, copy it across too: they're moved into the database on first start (see [Upgrading from Gatehouse](#upgrading-from-gatehouse)).

---

## Part 2: Install on the Raspberry Pi 5

This part gets CoilGate running on the Pi as a service. **It doesn't set up HTTPS yet, and doesn't need to:** CoilGate only listens on the Pi's own loopback address (`127.0.0.1`), so nothing else on your network can reach it until Part 3 puts HTTPS in front of it. You don't need to have done Part 1 first.

Commands assume the Pi is reachable as `coilgate.lan` and your Pi login is `pi`. Substitute your own.

### 1. Prepare the Pi

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In Imager's settings, set the hostname, username and password, and enable SSH. Boot, SSH in, then:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-lgpio caddy unzip
timedatectl
```

`python3-lgpio` drives the Pi 5's GPIO; it comes from Raspberry Pi OS because PyPI has no ready-built copy for the Pi. Caddy is installed now and set up in Part 3. `timedatectl` should show `System clock synchronized: yes`, because two-step codes are time-based, and your own **Time zone**, because schedules use the Pi's local time. If the zone is wrong: `timedatectl list-timezones`, then `sudo timedatectl set-timezone Europe/Amsterdam` (with yours).

Using sensors? Turn on what they need now, then reboot (details in [Sensors](#sensors)):

```bash
sudo raspi-config nonint do_onewire 0                                  # DS18B20 probes (GPIO 4)
sudo raspi-config nonint do_i2c 0                                      # BME280 boards (GPIO 2 and 3)
echo "dtoverlay=dht11,gpiopin=17" | sudo tee -a /boot/firmware/config.txt   # DHT22 on GPIO 17
sudo reboot
```

### 2. Put CoilGate on the Pi

Copy the zip across. This is the only step on your PC. In PowerShell, from the folder holding the zip:

```powershell
scp coilgate.zip pi@coilgate.lan:~/
```

Then on the Pi:

```bash
unzip -q ~/coilgate.zip -d ~
```

That gives you a `coilgate` folder in your home directory, used in the next step.

**Moving an existing Windows setup instead?** Skip the zip. In PowerShell, in the project folder on Windows, make a consistent copy of the database and send it with the code:

```powershell
cd $HOME\coilgate
.\.venv\Scripts\python manage.py backup coilgate-backup.db
ssh pi@coilgate.lan "mkdir -p ~/coilgate"
scp -r app deploy tests manage.py run.py requirements.txt config.example.toml pytest.ini coilgate-backup.db pi@coilgate.lan:~/coilgate/
del coilgate-backup.db
```

The backup holds your accounts, inputs and outputs, and settings. `.venv` stays behind: a virtual environment only works on the machine that made it.

### 3. Install

On the Pi:

```bash
# A system account that owns the data and runs CoilGate (no login shell)
sudo useradd --system --home-dir /opt/coilgate --no-create-home --shell /usr/sbin/nologin coilgate

# Code
sudo mkdir -p /opt/coilgate/instance
sudo cp -r ~/coilgate/. /opt/coilgate/
sudo find /opt/coilgate -name __pycache__ -prune -exec rm -rf {} +
rm -rf ~/coilgate ~/coilgate.zip

# Python environment. --system-site-packages lets it use the apt-installed lgpio.
cd /opt/coilgate
sudo python3 -m venv --system-site-packages .venv
sudo .venv/bin/pip install -r requirements.txt

# Settings (Part 3 changes two of them for HTTPS)
sudo cp config.example.toml config.toml
```

Only if you copied a database backup from Windows:

```bash
sudo mv /opt/coilgate/coilgate-backup.db /opt/coilgate/instance/coilgate.db
```

Then set ownership: code read-only to CoilGate, data folder private to it.

```bash
sudo chown -R root:root /opt/coilgate
sudo chown -R coilgate:coilgate /opt/coilgate/instance
sudo chmod 700 /opt/coilgate/instance
sudo chown root:coilgate /opt/coilgate/config.toml && sudo chmod 640 /opt/coilgate/config.toml
```

### 4. Create your admin account

Admin commands must run as the `coilgate` account so files in `instance/` keep the right owner. An alias saves typing:

```bash
echo "alias coilgate='sudo -u coilgate /opt/coilgate/.venv/bin/python /opt/coilgate/manage.py'" >> ~/.bashrc
source ~/.bashrc
coilgate create-user admin
```

The first account is always an admin. (Moved a Windows setup across? Your accounts came with it: `coilgate list-users` shows them.)

### 5. Run it as a service

```bash
sudo cp /opt/coilgate/deploy/coilgate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coilgate
systemctl status coilgate --no-pager
curl -sI http://127.0.0.1:8000/login | head -1
```

`curl` should print `HTTP/1.1 200 OK`. Logs: `journalctl -u coilgate -f`. The service starts at boot, restarts if it crashes, and is sandboxed as described under [Security measures](#security-measures).

CoilGate is now running, but only the Pi itself can reach it.

### 6. First sign-in, before HTTPS (optional)

To sign in now and add your inputs and outputs before setting up Part 3, borrow SSH's encryption. On your PC, run this and leave the window open:

```powershell
ssh -L 8000:127.0.0.1:8000 pi@coilgate.lan
```

Then open **http://127.0.0.1:8000** on your PC. The traffic travels inside the SSH connection, so it's encrypted even though the address says `http`. Close the window when you're done. Once Part 3 is in place, use the `https://` address instead.

Next, Part 3 puts HTTPS in front of CoilGate so you can reach it from your other devices.

### Updating CoilGate later

With a new `coilgate.zip` from the [releases page](https://github.com/ulrich-za/coilgate/releases), copy it across as in step 2 (`scp coilgate.zip pi@coilgate.lan:~/`), then on the Pi:

```bash
coilgate backup /opt/coilgate/instance/before-update.db      # just in case
unzip -q -o ~/coilgate.zip -d ~
sudo cp -r ~/coilgate/. /opt/coilgate/
rm -rf ~/coilgate ~/coilgate.zip
sudo /opt/coilgate/.venv/bin/pip install -r /opt/coilgate/requirements.txt
sudo cp /opt/coilgate/deploy/coilgate.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart coilgate
systemctl status coilgate --no-pager
```

Your `config.toml` and everything in `instance/` (database, secret key) are left alone. The database upgrades itself at start; accounts, inputs, outputs, rules and history carry over. From a version before groups, admins go into the Admin group and everyone else into User, and both keep the two-step setting you had; Super User starts empty.

---

## Part 3: Reach it from any network

This is where HTTPS comes in. Pick one of three ways; in all of them CoilGate stays on `127.0.0.1` and Caddy or Cloudflare faces the network, handling HTTPS. If you only want CoilGate on your home network for now, use **C**: it doesn't need a VPN to work at home, and you can add one later for access from outside.

| | A. Your domain + Caddy | B. Cloudflare Tunnel | C. Home network (+ VPN) |
|---|---|---|---|
| Reachable from | Any browser | Any browser | Your home network, plus devices on your VPN |
| Open ports on your router | 80 and 443 | None | The VPN's port |
| Works behind CGNAT (no public IP) | No | Yes | No (usually) |
| Needs | A domain, a public IP | A domain on Cloudflare DNS | A VPN server (router or Pi) |
| Who sees your traffic | Nobody | Cloudflare | Nobody |
| Exposure to internet scanners | CoilGate's sign-in page | CoilGate's sign-in page (or Cloudflare Access first) | None |

If `coilgate.io` will be the product's own site, use a subdomain for your installation, such as `home.coilgate.io`. That's the example used below.

### Before CoilGate goes on the internet

- Keep two-step verification required for the Admin group (Admin > Groups; on by default). Super Users don't need it by default, so give them long, unique passwords, or tick it for them too.
- Set `admin_networks` in `config.toml` to your home network and VPN range, so a stolen admin password can't reconfigure anything from outside. You can still use the controls from anywhere.
- Have as few admins as you need; put everyone else in Super User, User or a group of your own with only what they need.
- Turn on automatic security updates: `sudo apt install -y unattended-upgrades`.
- Set up backups ([Backups](#backups)) and glance at **Admin > Activity** now and then.

### A. Your own domain with Caddy

1. **DNS:** create an A record for `home.coilgate.io` pointing at your public IP.
2. **Router:** forward TCP ports 80 and 443 to the Pi. Caddy uses port 80 to prove you own the domain and to redirect to HTTPS.
3. **Caddy:**
   ```bash
   sudo cp /opt/coilgate/deploy/Caddyfile.internet /etc/caddy/Caddyfile
   sudo nano /etc/caddy/Caddyfile        # your email address and hostname
   sudo -u caddy caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
   Caddy fetches a Let's Encrypt certificate within a minute or so and renews it automatically. `journalctl -u caddy -f` shows progress.
4. **CoilGate:** turn on HTTPS cookies and trust Caddy's forwarded addresses:
   ```bash
   sudo sed -i 's/^secure_cookies = false/secure_cookies = true/; s/^trust_proxy = 0/trust_proxy = 1/' /opt/coilgate/config.toml
   sudo systemctl restart coilgate
   ```
5. **Firewall:**
   ```bash
   sudo apt install -y ufw
   sudo ufw allow OpenSSH
   sudo ufw allow 80,443/tcp
   sudo ufw enable
   ```

Open **https://home.coilgate.io**.

(Validating as the `caddy` user matters: validation prepares certificate storage, and as root it would create a separate, unused copy under `/root`.)

### B. Cloudflare Tunnel

The Pi makes an outgoing connection to Cloudflare, and visitors reach CoilGate through it. Nothing is opened on your router. Your domain's DNS must be managed by Cloudflare (a free account is enough).

1. **Install cloudflared** from Cloudflare's repository (the dashboard in step 2 also shows these commands):
   ```bash
   sudo mkdir -p --mode=0755 /usr/share/keyrings
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install -y cloudflared
   ```
2. **Create the tunnel** in the Cloudflare dashboard: **Networking > Tunnels > Create a tunnel**, name it `coilgate`, and select **Create Tunnel**. Choose **Debian** and **arm64**, copy the command shown, and run it on the Pi; it connects cloudflared to your account as a service. When the connector shows as connected, select **Continue**.
   Then open the tunnel's **Routes** tab, select **Add route > Published application**, and enter: **Subdomain** `home` (or any name not already in your DNS records), **Domain** your domain, **Service URL** `http://127.0.0.1:8000`. Use `127.0.0.1`, not `localhost`: `localhost` can resolve to the IPv6 address, and CoilGate listens on IPv4. Cloudflare creates the DNS record itself. (Prefer the command line? `deploy/cloudflared-config.yml` is a ready config for a locally managed tunnel.)
3. **CoilGate:** same two settings as with Caddy:
   ```bash
   sudo sed -i 's/^secure_cookies = false/secure_cookies = true/; s/^trust_proxy = 0/trust_proxy = 1/' /opt/coilgate/config.toml
   sudo systemctl restart coilgate
   ```
4. **Firewall:** no ports are needed for the tunnel.
   ```bash
   sudo apt install -y ufw
   sudo ufw allow OpenSSH
   sudo ufw enable
   ```

Caddy isn't needed with a tunnel; you can `sudo systemctl disable --now caddy`. For an extra layer, Cloudflare Access can ask for an email code before anyone even sees CoilGate's sign-in page.

### C. Home network, plus a VPN for outside

The most private option: CoilGate isn't on the internet at all. At home it works straight away with the steps below. To use it from outside, run WireGuard on your router (or the Pi) and connect your phone or laptop to it first; CoilGate then works exactly as it does at home.

```bash
sudo cp /opt/coilgate/deploy/Caddyfile.lan /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile          # the name or IP you'll use
sudo -u caddy caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo sed -i 's/^secure_cookies = false/secure_cookies = true/; s/^trust_proxy = 0/trust_proxy = 1/' /opt/coilgate/config.toml
sudo systemctl restart coilgate
```

The name must resolve to the Pi (a DNS entry on your router, or `C:\Windows\System32\drivers\etc\hosts` on Windows). Caddy's certificate comes from its own private authority, so trust its root once on each device:

```bash
sudo cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt ~/caddy-root.crt
sudo chown $USER ~/caddy-root.crt
```

On Windows: `scp pi@coilgate.lan:~/caddy-root.crt .` then `certutil -user -addstore Root caddy-root.crt` and confirm. Then delete the copy on the Pi.

### Backups

Keep a rolling week of nightly copies:

```bash
sudo crontab -u coilgate -e
```

Add:

```
15 2 * * * /opt/coilgate/.venv/bin/python /opt/coilgate/manage.py backup /opt/coilgate/instance/backup-$(date +\%a).db
```

These live on the same SD card, so copy them off the Pi now and then. To restore: stop the service, copy a backup over `instance/coilgate.db` (owner `coilgate`, mode 600), and start it again.

---

## Part 4: Wiring outputs and inputs

Everything on the Pi side runs at 3.3 V or 5 V. **Never put more than 3.3 V on a GPIO pin.** Switching 230 V loads means mains wiring in a proper enclosure: have an electrician do that part, for safety and for your electrical compliance certificate.

The add/edit form lists every pin as "GPIO 17, header pin 11": the GPIO number is what you choose in CoilGate, the header pin is the physical position. The **Pinout** page shows where each one is, which way round the header sits, and where the power and ground pins are.

### Outputs: relay modules

Use a relay module with an optocoupler and a **high/low trigger jumper**, listed as working with 3.3 V logic. With the Pi off:

| Relay module | Pi 5 header |
|---|---|
| VCC (or DC+) | Pin 2, 5 V |
| GND (or DC−) | Pin 6, ground |
| IN | The GPIO you chose, e.g. pin 11 for GPIO 17 |

Set the jumper to **H** and choose **High** in CoilGate. Connect the load through **COM** and **NO** (normally open), so it's off whenever the relay isn't powered, including when the Pi is off.

**Test before connecting a load:** run `pinctrl get 17` on the Pi. It should show an output driven low (`op` … `dl`). Switch the output on in CoilGate and it shows `dh`. Stop CoilGate (`sudo systemctl stop coilgate`) and the pin drops back to an input with pull-down (`ip` … `pd`), so a high-trigger relay stays off. Then connect the relay, listen for the click, and only then add the load.

**Active-low boards** (switch on when IN is low) work with **Low** selected, with two catches. Most GPIO pins sit low while the Pi boots, which such a board reads as on, so the relay clicks on until CoilGate starts (CoilGate warns you when you save one). Some 5 V active-low boards also never switch fully off from 3.3 V. If you must use one, choose GPIO 4, 5 or 6, which are pulled high at boot.

**Pulse outputs** suit gate motors, door strikes and garage openers (a short closure, such as 1 second) and timed loads like irrigation (minutes or hours). A running pulse can be stopped early from the Controls page.

### Inputs

| Wiring in CoilGate | Connect | Typical use |
|---|---|---|
| Contact to ground | Contact between the GPIO pin and GND | Door contacts, buttons, dry contacts. Simplest. |
| Contact to 3.3 V | Contact between the GPIO pin and 3.3 V (pin 1) | Same, the other way round |
| Signal, active when high | A device's output to the GPIO pin, grounds joined | Opto-isolated input boards, 3.3 V sensors |
| Signal, active when low | As above | Devices that pull low when active |

"Active" means the contact is closed. Use the state names to describe what that means: a door contact that's closed when the door is shut could be named "Shut" and "Open".

For long cable runs, outdoor wiring, or 12/24 V signals from alarm or access-control equipment, put an **opto-isolated input board** between the field wiring and the Pi, and use a signal wiring option. Long unprotected cables pick up interference and surges that can damage the Pi. Debounce (50 ms by default) hides the flicker of a mechanical contact.

### Sensors

All three run on **3.3 V** (header pin 1 or 17). Each reading becomes its own named sensor in CoilGate, so one BME280 can be "Living Room Temperature", "Living Room Humidity" and "Living Room Pressure", and each name shows up in the rule builder. The add form shows the one-time Pi setup, lists the probes or devices the Pi can see, and has a **Correction** box for a sensor that reads a little high or low.

**DS18B20 temperature probe** (1-Wire; the waterproof probe on a cable is the usual choice)

| Probe wire | Pi 5 header |
|---|---|
| Red (VDD) | Pin 1, 3.3 V |
| Black (GND) | Pin 9, ground |
| Yellow or white (data) | Pin 7, GPIO 4 |

Put a **4.7 kΩ resistor between data and 3.3 V** (many probe kits include one on a small adapter board). Several probes can share the same three wires; each has its own ID (`28-…`). Enable 1-Wire with `sudo raspi-config nonint do_onewire 0` (this adds `dtoverlay=w1-gpio` to `/boot/firmware/config.txt`), reboot, and check `ls /sys/bus/w1/devices/`. GPIO 4 then belongs to 1-Wire, so don't use it for anything else.

**BME280 environment sensor** (I²C; temperature, humidity and pressure)

| BME280 board | Pi 5 header |
|---|---|
| VIN or VCC | Pin 1, 3.3 V |
| GND | Pin 9, ground |
| SDA | Pin 3, GPIO 2 |
| SCL | Pin 5, GPIO 3 |

Enable I²C with `sudo raspi-config nonint do_i2c 0` and reboot. `sudo apt install -y i2c-tools && i2cdetect -y 1` shows `76` or `77`: choose that address in CoilGate. Cheap boards sold as BME280 are sometimes a **BMP280** (no humidity); CoilGate recognises it and says so if you add a humidity reading. GPIO 2 and 3 then belong to I²C. Mount the board away from the Pi itself, which warms anything near it.

**DHT22 / AM2302** (temperature and humidity)

| DHT22 | Pi 5 header |
|---|---|
| VCC (pin 1 of the sensor) | Pin 1, 3.3 V |
| Data (pin 2) | Any free GPIO, e.g. pin 11 for GPIO 17 |
| GND (pin 4) | Pin 9, ground |

A bare 4-pin sensor needs a **10 kΩ resistor between data and 3.3 V**; 3-pin modules and the AM2302 have it built in. Add `dtoverlay=dht11,gpiopin=17` (your pin) to `/boot/firmware/config.txt` and reboot: the kernel's DHT driver reads both DHT11 and DHT22 and is far more reliable than reading the pin from Python. In CoilGate choose the same pin, so nothing else gets it. The DHT22 can only be read every 2 seconds or so and occasionally misses a reading; CoilGate retries, and keeps the last good value for a while.

If a sensor stops answering, it shows **Unavailable** with the reason, and rules that depend on it leave their outputs as they are until it's back.

---

## Using CoilGate

### The Controls page

Buttons work in place: pressing one doesn't reload the page or move it, so you can switch several outputs in a row, on a phone too. The **activity panel** docked along the bottom of the window shows the result of each press in its top line (green for done, red if something was refused), with recent switches, input changes and rule actions listed underneath in their own scrolling area. Changes appear within a fraction of a second, whether they come from another device, an input or a rule: the page keeps a request open and the Pi answers the moment something changes. When an admin adds, removes, renames, moves or reorders an input, output or sensor, open Controls and Pinout pages reload themselves to show the new setup. **Hide** shrinks the panel to its status line; the browser remembers that choice. Admins get a **Full log** link to Admin > Activity.

Without JavaScript the page still works: each press reloads it and returns you to the same row.

Beside the list, on a wide screen, a **pin map** shows the header with the pins in use named. A pin glows green while its output is on and blue while its input is active, and its badge shows the voltage on it: **H** for 3.3 V, **L** for 0 V. For relays that switch on when low, "on" shows as L. Clicking a lit pin jumps to its row. On a phone, the map is a strip above the list, drawn as the header sits on the board, and tapping a lit pin does the same.

### The Pinout page

**Pinout** in the top bar shows the Raspberry Pi 5's 40-pin header the way [Pinout.xyz](https://pinout.xyz) does: odd pins on the left, pin 1 at the top, power, ground and GPIO pins colour-coded. The pins in use light up live, as on Controls.

- **In use** lists every pin CoilGate is set up to use, including the ones sensors need (GPIO 4 for 1-Wire probes, GPIO 2 and 3 for I²C).
- **The interface buttons** (I²C, SPI, UART, PWM, PCM, PIO and so on) highlight that interface's pins and explain how to turn it on. Its usual pins are shaded darker; pins that can carry it as an alternative are lighter and labelled in italics.
- **Selecting a pin** shows its GPIO number, its pull at power-on, everything on CoilGate that uses it, and its nine Pi 5 functions (F0 to F8 in `pinctrl`).

The functions come from the Pi 5's RP1 chip, not the older Broadcom chips, so they're right for this board: for example PWM0 channel 2 on GPIO 18, and PIO on every GPIO pin. Addresses like `#i2c` or `#pin-11` open the page on that interface or pin.

### Appearance

Each person chooses light, dark or **Auto** (follow the device's setting) under **Account > Appearance**. It's saved to the account, so it applies on every device they sign in on.

### Groups and privileges

Every account is in one group, and the group decides what it can do and whether it needs two-step verification. Three are built in; the defaults for Super User and User can be changed under **Admin > Groups**, which only admins see.

| | User | Super User | Admin |
|---|---|---|---|
| See the Controls and Pinout pages, manage own password, two-step and sessions | ✓ | ✓ | ✓ |
| Switch outputs | ✓ | ✓ | ✓ |
| Add and change inputs, outputs and sensors; rules; schedules | | ✓ | ✓ |
| Read the full activity log and download it | | ✓ | ✓ |
| Add, change and remove accounts | | User accounts only | All |
| See all settings | | ✓ | ✓ |
| Change settings | | Controls and Rules limits only | All |
| Manage groups | | | ✓ |
| Needs two-step verification | Yes | No | Yes |

**Super User** runs things day to day without being able to take over: it can't touch admin or other Super User accounts, so it can't delete them, reset their passwords or promote anyone, and it can't change the site name or the sign-in, session and record-keeping settings. Those sections are greyed out on its Settings page with a note that it doesn't have permission. Admins can tick more for it, but admin accounts and groups always stay admin-only.

**Your own groups** (Admin > Groups > Add a group) pick from the same privileges:

- **Use the controls.** Without it the group can see the Controls page, the activity panel and the pinout, but every button is disabled and the server refuses any switch: a view-only group for family or colleagues who just want to look.
- **Setup:** inputs, outputs and sensors; rules; schedules. Each is separate.
- **Accounts:** which groups' accounts it may add, change and remove. It can only put accounts into those groups, so it can never hand out more than it was given. Admin isn't offered.
- **Activity log.**
- **Settings:** see all of them, and which sections it may change.
- **Require two-step verification.** People in a group without it can still turn it on for themselves under Account.

Changes apply from each person's next page; nobody needs to sign in again. Moving someone into a group that needs two-step verification sends them to set it up. A group with accounts in it can't be removed until they're moved; the built-in groups can't be removed.

### Managing accounts

In **Admin > Accounts > Manage** you can move someone to another group, choose when they can use the controls, issue a new temporary password (which also signs them out), reset two-step verification for a lost phone, sign them out everywhere, disable, or delete the account. Temporary passwords work once, for 72 hours. Accounts in groups you can't manage are listed but marked "Can't manage".

**When someone can use the controls** is a schedule, "Always (24/7)" unless you choose another. Outside it they can still sign in and see the Controls page, with a note saying when it's available again, but every button is disabled and the server refuses any switch. Their Account page shows the same. Admins are never limited, and moving someone to Admin clears their schedule.

### Inputs and outputs

Changes apply immediately. Renaming, relabelling, reordering or changing a pulse length leaves an output's state alone. Changing its pin, trigger level or on/off-versus-pulse behaviour resets that output to off. Removing an output switches it off and frees its pin; its history stays in the activity log.

Something a rule uses can't be removed until the rule is changed or removed, and an output can't change between on/off and pulse while a rule would give it an action it can't do. The edit page lists the rules that use it. Renames are picked up by rules straight away, since rules refer to things, not names.

### Schedules

**Admin > Schedules** lists them, each with a week chart. A schedule is one or more time windows, each on chosen days:

- "Weekdays" 07:00 to 18:00, Monday to Friday
- "Night" 22:00 to 06:00 every day. An end time earlier than the start runs into the next morning.
- "Weekends all day": 00:00 to 00:00, Saturday and Sunday. An end of 00:00 means midnight at the end of the day.

**Always (24/7)** is built in, can't be changed or removed, and is the default everywhere. A schedule in use by a rule or an account can't be removed. Times are the Pi's local time, so check its time zone (Part 2, step 1).

### Rules

There are two ways to build a rule, and you can switch between them at the top of the page (your browser remembers which you used last):

- **Sentence:** first choose what starts the rule, then fill in a sentence using the names you gave your inputs, sensors and outputs.
- **Drag and drop:** drag pieces from the list on the right into **When**, **During** and **Then**, then pick the details inside the piece ("is Pressed", "turn on", "toggle"). Tapping or clicking a piece puts it in its place too, and every piece works with the keyboard. On a phone, tap pieces, or press and hold one briefly and then drag. A bar along the bottom shows the rule as it takes shape. Dropping a piece on a filled spot swaps it, and this builder can also change what starts an existing rule.

Both save exactly the same rules. A rule can start when:

| Starts when | Example |
|---|---|
| **An input changes** | When **Front door is Open** for at least **2 minutes** during **Night**, then **Turn on Hall light**, and undo it when that's no longer the case. |
| **A sensor reading crosses a value** | When **Living Room Temperature** is **above 26**, then **Turn on Living Room Aircon**, and undo it when the reading gets back to **24**. |
| **A schedule is active** | While **Evenings** is active, then **Turn on Porch light**, and undo it when the schedule ends. |
| **Another rule is active** (or not active) | When rule **Door left open** is active for at least **5 minutes**, then **Turn on Siren**. |

And it can:

- **Turn an output on or off, or toggle it** (on/off outputs), or **pulse** it (pulse outputs). Toggle is the one for a push button: each press switches the light the other way.
- **Switch another rule on or off, or toggle it.** For example, a button that arms and disarms an alarm rule: When **Button is Pressed**, **Toggle rule Alarm**. A rule that's switched off doesn't act, and counts as "not active" for rules that follow it.

The details:

- **Undo** does the opposite when the condition ends: turns the output back, or switches the rule back. Toggles and pulses can't be undone.
- **Active** means a rule's condition is met, after its waiting time and within its schedule. That's what rules started by another rule follow, so a chain such as "door open 2 minutes → hall light" then "that, for 5 more minutes → siren" undoes as a whole when the door shuts.
- **Loops are refused:** a rule can't be started by a rule that it starts, directly or further along the chain. A rule can't start or switch itself, and a rule that others depend on can't be removed until they're changed.
- **Waiting time** ("for at least"): the condition must hold that long first. 0 acts straight away.
- **During:** the rule only acts within a schedule. When the schedule ends it counts as the condition ending, so undo applies.
- **Sensor gap:** a rule that turns on above 26 and off below 24 doesn't flick the aircon on and off around 26. Leave the undo value empty for a 1-unit gap.
- **Speed:** input changes are acted on at once, as soon as the input's debounce time has passed. Sensor readings and schedules are checked every second. A chain of rules settles in the same check.
- **Rules act on changes,** not continuously. If a rule turned the light on and someone turns it off on the Controls page, it stays off until the condition ends and happens again.
- **After a restart or power cut,** CoilGate checks every rule once. Any whose condition is already true acts then: a "While Evenings is active, turn on Porch light" rule switches the light back on at 19:00 after a power cut at 18:30. The same happens when a rule is switched on while its condition is already met.
- **Unknown means no change.** While a sensor has no recent reading, rules using it leave their outputs alone.
- **Safety:** a rule that acts more than 60 times in a minute (a chattering contact, a faulty sensor, two rules fighting) is paused for a minute. The limit is in Admin > Settings, and is far above what pressing a button by hand does. The pause shows in the activity log and on the rules list, which has a **Resume now** button. When the pause ends, the rule catches up: an output it should have switched off in the meantime is switched off then. Held-back toggles and pulses aren't repeated. Set `rule_engine = false` in `config.toml` to pause every rule at once.

The rules list shows each rule as its sentence, whether it's **active now**, and when it last acted. **Switch off** keeps a rule without it running. On the Controls page, outputs a rule looks after are marked "Automated", and their history says which rule switched them.

**Debounce** (set per input, 50 ms by default): a mechanical contact bounces open and shut for a few milliseconds when it changes, so an input only counts as changed once it has been steady for this long. Use 0 for clean electronic signals such as a PIR module's output, and 100 to 200 ms for worn contacts or long cables.

### Settings (Admin > Settings)

Admins can change everything. Other groups with **See all settings** see the whole page, with the sections they can't change greyed out and marked "Locked". Whether two-step verification is required is set per group, under Admin > Groups.

| Setting | Default | Range |
|---|---|---|
| Site name | CoilGate | Shown in the top bar, titles and authenticator apps |
| Minimum password length | 15 | 8 to 64 |
| Reject passwords found in data breaches | On | |
| Failed attempts before an account is locked | 5 | 3 to 50 |
| Failed attempts before an IP address is locked | 20 | 5 to 500 |
| Lock window | 15 minutes | 1 to 1440 |
| Sign out after inactivity | 30 minutes | 5 to 1440 |
| Sign out after this long regardless | 12 hours | 1 to 720 |
| Actions per user per minute | 30 | 1 to 600 |
| Actions per rule per minute | 60 | 10 to 600. More than this pauses the rule for a minute |
| Keep activity and sign-in history for | 90 days | 7 to 3650 |

### Activity (Admin > Activity)

Everything that happened, filterable by controls, inputs, rules (what they switched), rule and schedule changes, setup changes, accounts, settings, downloads and sign-ins. The latest 200 entries are shown.

**Downloads:** choose an area with the filter buttons, pick a date range, and download it as **CSV** (opens in Excel, with times sortable) or **PDF** (landscape A4, ready to print or send). The range can go back as far as the history is kept (Admin > Settings). A PDF holds up to 5,000 entries; use a CSV or a shorter range beyond that. Each download is itself logged, with who downloaded what. In CSV files, any text that a spreadsheet could mistake for a formula (starting with `=`, `+`, `-` or `@`) is prefixed with `'`.

---

## Command-line tool

Day-to-day management happens in the Admin area. The command line is for the first admin and for recovery. On Windows prefix commands with `.\.venv\Scripts\python manage.py`; on the Pi use the `coilgate` alias.

| Command | What it does |
|---|---|
| `create-user NAME [--group "Super User"]` | Create an account, in User unless you name a group (the first is always an admin) |
| `set-password NAME` | Set a new password and sign out all their sessions |
| `set-group NAME GROUP` | Move an account to another group (the last admin is protected) |
| `list-users` | Accounts, groups, two-step status, last sign-in |
| `list-groups` | Groups, whether they need two-step verification, and what they can do |
| `unlock NAME` | Clear a failed-attempt lock |
| `reset-2fa NAME` | Turn off two-step verification (lost phone) |
| `disable NAME` / `enable NAME` | Block or allow sign-in |
| `sign-out NAME` | End all of a user's sessions |
| `delete-user NAME` | Delete an account (asks to confirm) |
| `backup FILE` | Consistent copy of the database, safe while running |
| `prune` | Remove expired sessions and old history, compact the file |

Locked out of the only admin account? On the Pi: `coilgate set-password admin`, and if needed `coilgate reset-2fa admin` and `coilgate unlock admin`.

---

## config.toml settings

Only deployment settings live in `config.toml`. Each can also be set as an environment variable `COILGATE_<NAME>`, which wins. Restart after changes.

| Setting | Default | Notes |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8000` | Keep on loopback; Caddy or the tunnel faces the network |
| `threads` | `16` | Waitress worker threads. Each open Controls page keeps one waiting for changes |
| `secure_cookies` | `false` | `true` once reached over HTTPS |
| `trust_proxy` | `0` | `1` behind Caddy or Cloudflare Tunnel |
| `proxy_address` | `127.0.0.1` | Forwarded addresses are only believed from here |
| `database` | `instance/coilgate.db` | |
| `gpio_mode` | `auto` | `hardware` or `simulate` to force one |
| `admin_networks` | `[]` | e.g. `["192.168.1.0/24", "10.8.0.0/24"]`; empty means anywhere |
| `rule_engine` | `true` | `false` pauses every rule (e.g. while rewiring); controls keep working |

---

## Upgrading from Gatehouse

CoilGate replaces Gatehouse and upgrades its data automatically on first start:

- `instance/gatehouse.db` is renamed to `coilgate.db` and upgraded. Existing accounts become admins, and the switch history moves into the activity log.
- Settings from a Gatehouse `config.toml` (`app_name`, session and lockout settings, `require_2fa_for_controls`) are copied into the database once (the two-step setting goes to the Admin and User groups), and `[[outputs]]` blocks become outputs in Admin > Inputs and outputs. The log says so; remove them from `config.toml` afterwards and manage them in the app.
- On the Pi, CoilGate installs alongside Gatehouse under its own name (`/opt/coilgate`, service `coilgate`). Stop and disable the old one: `sudo systemctl disable --now gatehouse`.

---

## Troubleshooting

**"This form expired" on every sign-in.** The browser isn't keeping the form cookie, usually because `secure_cookies = true` while you're on a plain `http://` address. Use the `https://` address.

**Locked out after failed attempts.** Wait for the lock window (15 minutes by default), or run `unlock NAME`.

**Two-step codes are always rejected.** Check the Pi's clock (`timedatectl`) and that your phone sets its time automatically.

**Every sign-in shows 127.0.0.1 behind Caddy or the tunnel.** `trust_proxy` isn't `1` in `config.toml`. Restart after changing it.

**No Admin link, or Admin says "page not found".** The account isn't an admin, or you're outside `admin_networks`. Check `coilgate list-users` and the setting.

**An input or output says "Unavailable".** The message says why. "In use by another program" means something else holds the pin, often a second copy of `run.py` started by hand while the service runs. "No permission" means the service isn't in the `gpio` group; use the provided unit file.

**"The lgpio library isn't installed".** Run `sudo apt install python3-lgpio`, and make sure the virtual environment was made with `--system-site-packages`. If not, delete `/opt/coilgate/.venv` and repeat that part of Part 2, step 3.

**A relay clicks on while the Pi boots.** It's an active-low board on a pin that's low at boot. See [active-low boards](#outputs-relay-modules).

**Caddy can't get a certificate (option A).** Check the A record points at your public IP, ports 80 and 443 reach the Pi, and your provider doesn't block port 80. `journalctl -u caddy -f` shows the reason.

**A sensor says "Unavailable".** The message says why. "Probe … not found" or "iio:device0 not found": the overlay isn't enabled, the Pi wasn't rebooted, or the wiring or pull-up resistor is wrong (`ls /sys/bus/w1/devices/`, `ls /sys/bus/iio/devices/`). "Nothing answered at I²C address 0x76": try the other address, check `i2cdetect -y 1`, and check SDA and SCL aren't swapped. "Implausible reading (85 °C)" from a DS18B20 means it lost power during a reading: check its 3.3 V and ground. A single missed reading isn't shown: the last good value counts for three polling intervals (at least 90 seconds).

**A rule didn't act.** Its row in Admin > Rules shows whether its condition is true now and any problem (an unavailable output, or "Paused" after switching too often). Check the rule is switched on, its "during" schedule is active, the waiting time has passed, and `rule_engine` isn't `false`. A rule acts when its condition *becomes* true, not while it stays true.

**Schedules are an hour or more out.** The Pi's time zone is wrong: `timedatectl`, then `sudo timedatectl set-timezone` with yours, and restart CoilGate.

**Controls says "Simulation" on the Pi.** `gpio_mode` is set to `simulate`, or `ls /dev/gpiochip*` lists nothing.

---

## Known limits

- **Two-step secrets are stored unencrypted** in the database, as is usual for TOTP. The file is readable only by the service account; protect backups the same way.
- **No recovery codes.** A lost authenticator means an admin resets it, from the Admin area or the command line.
- **Outputs start off** after a restart or power cut, and only rules whose condition is already true switch anything back on. Nothing else switches on unexpectedly.
- **An output's state is what the Pi is driving**, not proof the load switched. Wire a sensor back to an input if you need confirmation.
- **No alerts yet.** Rules switch outputs; sending a phone or email notification when something happens would be a natural next addition.
- **Rules have one condition each.** "Door open AND after dark" is done with an input rule limited to a schedule, and one rule can start another, but a single rule can't yet combine two inputs or two sensors.
- **Passkeys (WebAuthn)** would be the next step up from TOTP codes: phishing-resistant, and they fit well now that CoilGate runs over HTTPS.
- **On Windows**, `instance/` inherits your user folder's permissions; keep the project inside your user profile.
- **Fonts:** Barlow, under the SIL Open Font License (`app/static/fonts/OFL-Barlow.txt`); also embedded in PDF downloads (`app/pdf_fonts/`). PDFs cover Latin and Latin Extended characters; anything else prints as `?`.

---

## Licence

CoilGate is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 2 of the License, or (at your option) any later version (`GPL-2.0-or-later`). It is distributed in the hope that it will be useful, but without any warranty; without even the implied warranty of merchantability or fitness for a particular purpose. See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 the CoilGate authors.

Two parts of the project are under other licences, and they're kept in their own folders with their licence files.

### Fonts

The Barlow font files (`app/static/fonts/`, `app/pdf_fonts/`) are not covered by the GPL: they're under the SIL Open Font License 1.1, included next to them.

### Pinout data from Pinout.xyz

The Pinout page uses material from **[Pinout.xyz](https://pinout.xyz)**, the Raspberry Pi GPIO pinout guide by Philip Howard and contributors ([source on GitHub](https://github.com/pinout-xyz/Pinout.xyz)), licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): the header layout, the Pi 5 function table and the interface descriptions. They were trimmed to the Raspberry Pi 5, the interface pin highlights were rebuilt from the Pi 5's RP1 function table, and the data was converted to JSON. Pinout.xyz generated that table from Raspberry Pi's `pinctrl` utility (BSD 3-Clause licence).

That material lives in `app/pinout_data/` and stays under CC BY-SA 4.0, not the GPL: if you share a changed version of `pi5.json`, it must stay under CC BY-SA 4.0 with the same credit. `app/pinout_data/NOTICE.md` lists the sources, the exact Pinout.xyz commit, every change made, and the pinctrl licence; the full CC BY-SA text is next to it. The Pinout page credits Pinout.xyz at the bottom. CoilGate's own code that draws the page (`app/pinout.py`, the templates and `app/static/pins.js`) is GPL-2.0-or-later like the rest.

### Python packages

Python packages that CoilGate uses are installed from PyPI under their own licences; they're compatible with GPL-2.0-or-later.
