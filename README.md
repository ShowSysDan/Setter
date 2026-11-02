# Setter — ATEM Switcher Control

Flask/Python application that sits between show control and a **Blackmagic
ATEM** video switcher and sets program, preview and aux on command. It holds the switcher session open, gives you a web
panel to drive and test the switcher from any browser on the LAN, and turns
plain-text UDP strings from Q-SYS, Crestron, AMX, Companion or a shell
script into ATEM commands. Pure Python — no Blackmagic SDK, no third-party
protocol library.

Current version: **0.1.0**

---

## Features

| Feature | Summary |
|---|---|
| **Switcher panel** | Program and preview buses with the switcher's own input names, cut, auto, fade, fade to black, transition style and rate |
| **Outputs and keys** | Aux routing, downstream key on/auto, upstream keys, macro buttons (named from the switcher) |
| **Configurable layout** | Set how many inputs, MEs, aux outputs, DSKs and macros to show; the control strings validate against the same numbers |
| **UDP control** | Text commands like `FADE 2 25` on a UDP port, with `OK` / `ERR` replies back to the sender |
| **HTTP control** | The same commands at `/api/v1/cmd` for systems that prefer HTTP; optional API key |
| **In-app API library** | The **API** page in the web UI: a cookbook (fade to a source on an ME, go to black, aux routing, keys, macros) with *Send* buttons that run each example on the live switcher, the full command reference, and ready-to-paste snippets for shell, HTTP, Q-SYS, Crestron and Companion, filled in with this Setter's address and port |
| **Test tools** | A control-string box on the dashboard (same parser as UDP), a raw ATEM command sender, a switcher simulator, and a UDP send script |
| **Live state** | Dashboard polls the switcher state every second — tallies, transition progress, FTB, DSK state |
| **Reconnects** | Drops and reconnects automatically; handles the ATEM's ack / retransmit scheme |
| **Access control** | UDP sender allowlist, HTTP address allowlist, panel password, API key, cross-site request blocking, sandboxed systemd service — see [Security](#security) |
| **Syslog audit log** | Every command, connection event and incoming control string, with who sent it — to local syslog and to a remote syslog server (UDP or TCP) |
| **Responsive UI** | Usable from a phone at the back of the room; 60 px tap targets |

---

## Project Structure

```
Setter/
├── app/
│   ├── __init__.py              # App factory — settings, audit log, starts switcher + UDP threads
│   ├── __version__.py
│   ├── security.py              # HTTP allowlist, panel password, cross-site blocking, headers
│   ├── routes/
│   │   ├── main.py              # HTML pages (/ and /settings)
│   │   ├── api.py               # Internal REST API (/api/) used by the dashboard
│   │   └── external_api.py      # External integration API (/api/v1/)
│   ├── services/
│   │   ├── atem_client.py       # ATEM UDP protocol client and live state model
│   │   ├── command_parser.py    # The control string grammar (shared by UDP, UI, HTTP)
│   │   ├── controller.py        # Owns the connection and listener; high-level actions; status
│   │   ├── udp_listener.py      # UDP control listener thread
│   │   ├── settings.py          # JSON-backed settings with validation
│   │   └── audit_log.py         # Local + remote syslog, in-memory activity feed
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/main.js
│   └── templates/
│       ├── base.html
│       ├── index.html           # Switcher panel
│       └── settings.html
├── docs/
│   ├── udp_protocol.md          # Control string reference
│   └── qsys_integration.lua     # Sample Q-SYS script
├── tools/
│   ├── fake_atem.py             # Switcher simulator for testing without hardware
│   └── udp_send.py              # Send a control string and print the reply
├── tests/
│   ├── test_parser.py
│   ├── test_security.py         # Allowlists, auth, CSRF blocking, headers, log sanitising
│   ├── test_syslog.py           # Remote syslog delivery over UDP and TCP, unreachable server
│   └── test_end_to_end.py       # Simulator -> Setter -> UDP/HTTP, checks state follows
├── config.py                    # Dev / Production config classes
├── run.py                       # Dev entry point
├── requirements.txt
├── setter.service              # systemd unit reference (written by install.sh)
├── install.sh                   # production install: venv + hardened service
├── .env.example
├── CHANGELOG.md
└── README.md
```

---

## Requirements

- **Python 3.10 or newer** with `venv` and `pip` (Raspberry Pi OS Bookworm,
  Debian 12, Ubuntu 22.04 or later). On those:
  `sudo apt install python3 python3-venv python3-pip git`. Everything else
  comes from `requirements.txt`, pinned to exact tested versions (Flask,
  python-dotenv, gunicorn).
- A machine on the same network as the ATEM. Setter talks to the switcher on
  **UDP 9910** and listens for control strings on **UDP 9990** and the web
  panel on **TCP 5110** (both configurable), so open those on any host
  firewall.
- Linux with systemd for the production install. Development runs on
  Linux, macOS or Windows.

---

## Quick Start (Development)

### 1. Clone and create a virtualenv

```
git clone https://github.com/ShowSysDan/Setter.git
cd Setter

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```
cp .env.example .env
```

Nothing in `.env` is required for a LAN test. The switcher IP and all the
operational settings are on the **Settings** page.

### 3. Run

```
python run.py
```

Open `http://localhost:5110`, go to **Settings**, enter the switcher's IP
(Blackmagic's factory default is `192.168.10.240`) and the number of inputs
and aux outputs you want on the panel. Setter connects as soon as you save.

> The Werkzeug reloader is turned off in `run.py` on purpose: it would start
> the app twice and open two switcher sessions.

### Test without a switcher

```
python tools/fake_atem.py            # a pretend ATEM on 0.0.0.0:9910
```

Point Setter at `127.0.0.1` in Settings. The simulator prints every command
it receives and plays transitions in real time, so you can watch a `FADE`
arrive from the dashboard or from UDP:

```
python tools/udp_send.py 127.0.0.1 9990 "FADE 3 1s"
OK FADE 3 25 ME1
```

Run the tests with `python -m unittest` (about a second; they use the
simulator, no hardware needed).

---

## Production Deployment (systemd)

Clone as the user the service should run as (a normal unprivileged account
is fine, it only needs read access to the checkout), then run the installer
with `sudo`:

```
cd ~
git clone https://github.com/ShowSysDan/Setter.git
cd Setter
sudo bash install.sh
```

The script:

1. creates `venv/` and installs `requirements.txt` (as the calling user, not
   root);
2. creates `.env` from `.env.example` if there is none, with
   `FLASK_ENV=production` and a random `SECRET_KEY` already filled in;
3. writes `/etc/systemd/system/setter.service` with that user and path
   (sandboxed: no privileges, read-only filesystem except `instance/`,
   restart forever), then enables the service and (re)starts it.

It prints the URL when it finishes, and a reminder if `.env` sets none of
the access controls described under [Security](#security). Edit `.env`
and `sudo systemctl restart setter` to apply them.

Options:

```
PORT=8080 sudo bash install.sh          # web panel port (default 5110)
HOST=10.1.1.5 sudo bash install.sh      # bind the panel to one interface (default 0.0.0.0)
```

The service runs as the account you ran `sudo` from, which only needs to
own the checkout. Ports below 1024 work: the unit grants just
`CAP_NET_BIND_SERVICE` in that case.

Useful commands:

```
sudo systemctl status setter          # running? last log lines
sudo systemctl restart setter         # after editing .env
sudo systemctl stop setter            # e.g. to run `python run.py` on the same port
curl http://localhost:5110/healthz    # {"ok": true, "connection": "connected"} for monitoring
```

Logs:

```
sudo journalctl -u setter -f       # service output
journalctl -t setter -f            # audit log (commands, connections, UDP strings)
```

### Updating

```
cd ~/Setter
git pull
sudo bash install.sh               # re-installs requirements and restarts the service
```

Settings in `instance/settings.json` and your `.env` are kept.

### Gunicorn worker note

The service runs `--workers 1 --threads 4`. **Do not increase workers.** The
switcher session and the UDP listener live inside the worker process; a
second worker would open a second session to the ATEM and fight over the
UDP port. Threads handle HTTP concurrency.

The unit passes `--env FLASK_ENV=production` to gunicorn rather than
relying on `.env`, because systemd lets `EnvironmentFile=` override
`Environment=`; a leftover `FLASK_ENV=development` in `.env` would otherwise
run the service in debug mode.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLASK_ENV` | `development` | `development` or `production`. The systemd unit forces `production` |
| `SECRET_KEY` | insecure default | Flask session secret. `install.sh` generates a random one; set your own if you create `.env` by hand |
| `API_KEY` | *(unset)* | Key required on `/api/v1/`; leave unset for open LAN access |
| `PANEL_PASSWORD` | *(unset)* | HTTP Basic auth password for the web panel and `/api/`; unset = no login |
| `PANEL_USER` | `setter` | Username for the above |
| `ALLOWED_NETWORKS` | *(unset)* | Comma-separated IPs / CIDRs allowed to make any HTTP request; unset = any |
| `SETTINGS_FILE` | `instance/settings.json` | Where the Settings page stores its values |
| `LOG_LEVEL` | `INFO` | Application log level |
| `HOST` / `PORT` | `0.0.0.0` / `5110` | Bind address for `run.py` (dev only; the service takes them from `install.sh`) |

---

## Settings (web UI)

Stored in `instance/settings.json`; changes apply immediately.

| Setting | Default | Effect |
|---|---|---|
| Switcher IP / port | `192.168.10.240` / `9910` | Where the ATEM is. Changing it reconnects |
| Auto-connect | on | Connect at start-up and reconnect after a drop |
| Inputs | 8 | How many numbered input keys to draw, and the highest input number the control strings accept |
| Mix effects | 1 | ME tabs on the panel; `ME2` etc. in control strings |
| Aux outputs | 1 | Aux rows on the panel; `AUX n` range |
| Downstream keys | 2 | DSK rows; `DSK n` range |
| Macro buttons | 8 | Macro keys; `MACRO n` range |
| Default rate | 25 frames | Initial rate in the transition panel |
| UDP control | on, `0.0.0.0:9990` | The control listener. Bind address `0.0.0.0` is every interface; enter one interface's IP to listen only there |
| Reply | on | Send `OK` / `ERR` lines back to the UDP sender |
| Allowed senders | *(any)* | IPs / CIDRs that may send UDP control strings; anything else is dropped without a reply and logged |
| RAW over UDP / HTTP | off | Whether `RAW <NAME> <hex>` is accepted from UDP and `/api/v1/`; the dashboard's raw sender always works |
| Remote syslog | off | Server, port (514), UDP or TCP, facility (`local0`). A *Send test message* button confirms delivery |

The Settings page also shows what the switcher reports about itself
(sources, MEs, aux, DSKs) so you can match the numbers to the hardware.

---

## Control Strings

Full reference in [`docs/udp_protocol.md`](docs/udp_protocol.md), and on the
**API** page of the running app (`/reference`), where every example has a
*Send* button and the snippets already carry your Setter's address and
port. The short version:

```
PGM <src> [MEn]      PVW <src> [MEn]      CUT [MEn]      AUTO [MEn]
FADE <src> [rate] [MEn]                   STYLE <mix|dip|wipe|dve|sting> [MEn]
RATE <rate> [MEn]    FTB [MEn]            FTBRATE <rate> [MEn]
AUX <n> <src>        DSK <n> <on|off|auto|toggle>        KEY <n> <on|off> [MEn]
MACRO <n>            RAW <NAME> <hex>     PING | STATUS | HELP
```

`<src>` is an input number, a name like `BARS`, `COL1`, `MP1`, `PGM`, or
`#<id>` for any raw ATEM source id. `RAW` is refused from UDP and HTTP
control unless enabled in Settings (the dashboard can always send it). A
message may hold up to 32 commands of up to 200 characters each. `<rate>` is frames (`25`) or seconds
(`1s`). Several commands per message: `PVW 3; CUT`.

Every reply starts with `OK`, `ERR` or `PONG`:

```
$ echo "FADE 2 1s" | nc -u -w1 10.1.1.50 9990
OK FADE 2 25 ME1
$ echo "PGM 12" | nc -u -w1 10.1.1.50 9990
ERR PGM 12: input 12 is outside the configured inputs 1..8 (change it in Settings, or use #12 to force a raw source id)
```

The dashboard's *Send a control string* box uses the same parser, so it is
the quickest way to check a string before putting it in a show-control
program. Every button on the panel also goes through it, and the activity
feed shows what was sent.

---

## API Reference

### Internal API (`/api/`)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/reference` | The in-app API library (HTML) |
| GET | `/healthz` | `{"ok": true, "connection": "connected"}`; no login needed, for monitoring |
| GET | `/api/version` | `{"version": "0.1.0"}` |
| GET | `/api/status` | Connection state, settings, switcher state, inputs with names, UDP listener status |
| GET | `/api/events?since=<id>` | Activity feed entries after `<id>` |
| GET | `/api/settings` | Current settings |
| PUT | `/api/settings` | Update settings (JSON); reconnects / restarts the listener as needed |
| POST | `/api/connect` | Start the switcher connection |
| POST | `/api/disconnect` | Stop it |
| POST | `/api/command` | `{"text": "FADE 2 25"}` → `{"ok": true, "replies": ["OK FADE 2 25 ME1"]}` |
| POST | `/api/raw` | `{"name": "CPgI", "hex": "00 00 00 01"}` — send a raw ATEM command |
| GET | `/api/syslog` | Remote syslog target status: sent / failed counts, last error |
| POST | `/api/syslog/test` | Write a test entry and report whether it was delivered |

### External Integration API (`/api/v1/`)

For Q-SYS, Crestron, AMX, Companion or any HTTP client. If `API_KEY` is set
in `.env`, send it as `X-API-Key: <key>` or `?api_key=<key>`.

| Method | Endpoint | Description |
|---|---|---|
| GET / POST | `/api/v1/cmd?c=<string>` | Run a control string. POST accepts a `text/plain` body or `{"cmd": "..."}` |
| GET | `/api/v1/status` | Program, preview, aux, transition, FTB, DSK and input names |

Add `?format=text` (or `Accept: text/plain`) for plain `OK …` / `ERR …`
lines instead of JSON. The HTTP status is 200 when every command succeeded
and 422 otherwise, so a controller can check either.

```
curl "http://setter:5110/api/v1/cmd?c=FADE+2+25&format=text"
OK FADE 2 25 ME1
```

---

## Syslog Audit Log

Every action, connection event and incoming control string is an audit
entry. Entries go to:

- **Local syslog** on the Setter machine — LOCAL0, tag `setter`, socket
  auto-detected (`/dev/log` or `/var/run/syslog`), stderr where there is no
  syslog.
- **A remote syslog server**, when enabled in Settings — host, port, UDP or
  TCP, and facility are all configurable. Messages use RFC 3164 framing
  (`<PRI>Mmm dd HH:MM:SS host setter[pid]: LEVEL message`), newline-delimited
  on TCP, which rsyslog, syslog-ng, Graylog, Splunk and Q-SYS Core Manager's
  syslog input all accept as-is.
- **The dashboard activity feed** (last 300 entries, in memory).

| Level | Events |
|---|---|
| INFO | Every command sent and acknowledged, connection established, state loaded, UDP string received, settings changed, syslog target set |
| WARNING | Command rejected (bad string), command dropped (not connected), command not acknowledged, connection lost |
| ERROR | UDP listener could not bind |

The remote target also receives anything the application itself logs at
WARNING or ERROR — a crashed thread, an unhandled exception in a request —
prefixed with the logger name, so the syslog server is the one place to
look when something has gone wrong.

Each command entry carries `via=` (`ui`, `udp`, `v1`, `switcher`) and
`sender=` (the IP of the browser or control system that sent it).

```
<134>Nov 02 14:30:27 setter-pi setter[285]: INFO UDP control string received text=FADE 2 10 sender=10.1.1.20:54750 via=udp
<134>Nov 02 14:30:27 setter-pi setter[285]: INFO Fade me=1 input=Cam2 frames=10 via=udp sender=10.1.1.20:54750
<132>Nov 02 14:31:02 setter-pi setter[285]: WARNING Lost connection to 192.168.10.240: switcher stopped responding via=switcher
```

Remote delivery runs on a background queue, so a syslog server that is
slow, down, or unresolvable never delays a cue: entries are still written
locally and shown in the activity feed, and the Settings page shows sent /
failed counts and the last error. TCP connections are re-established
automatically after a failure. If the server stays down for a long time
the queue (2,000 entries) drops the oldest rather than growing without
bound.

If you would rather keep Setter's own config minimal, leave remote syslog
off and forward LOCAL0 from the machine's rsyslog instead; both routes
carry the same entries.

---

## Security

Setter can put a show to black with one datagram, so treat the box like
any other piece of show control: on its own VLAN, reachable only from the
control system and the operator positions. Everything below is on top of
that, and each layer is independent.

### Checklist before a show

1. **Settings → UDP control → Allowed senders**: the IP of the Q-SYS core /
   Crestron processor / Companion machine. Anything else is dropped silently.
2. **`.env` → `ALLOWED_NETWORKS`**: the control VLAN and the operator laptops.
   Any other address gets a 403 on every HTTP request, including `/healthz`.
3. **`.env` → `PANEL_PASSWORD`**: a login on the web panel and its `/api/`.
   Browsers remember it; control systems never touch these endpoints.
4. **`.env` → `API_KEY`** if anything uses `/api/v1/` over HTTP, sent as
   `X-API-Key: <key>` or `?api_key=<key>`.
5. Leave **RAW over UDP / HTTP** off unless a cue needs it.
6. Point **remote syslog** at the venue's log server so the audit trail
   survives the box.

Setter writes a WARNING to the audit log at start-up for each of these
that is still open, so `journalctl -t setter -b` shows the state at a glance.

```
sudo nano ~/Setter/.env                 # PANEL_PASSWORD=..., ALLOWED_NETWORKS=..., API_KEY=...
sudo systemctl restart setter
journalctl -t setter -b | grep startup   # should print nothing once all are set
```

### What is always on

- **Cross-site request blocking.** A browser marks requests that a web page
  on another site causes (an `<img src="http://setter:5110/api/v1/cmd?c=FTB">`,
  a hidden form) with `Sec-Fetch-Site: cross-site`; Setter refuses those.
  Control systems do not send that header and are unaffected. The internal
  `/api/` also only accepts JSON bodies, which an HTML form cannot produce.
- **Response headers.** A Content Security Policy that forbids inline and
  third-party script, no framing, no MIME sniffing, API responses never
  cached. The pages carry no inline script at all.
- **Input limits.** 64 KB HTTP bodies, 2 KB datagrams, 32 commands per
  message, 200 characters per command, validated host names and addresses
  in Settings. Control strings are stripped of control characters before
  they reach syslog, so a crafted datagram cannot forge log lines.
- **Constant-time** comparison of the API key and panel password.
- **No debugger.** The dev server runs without the Werkzeug console;
  production runs with `DEBUG` off regardless of `.env`.
- **The systemd unit** runs as an unprivileged user with no capabilities
  (`CAP_NET_BIND_SERVICE` only if the port is below 1024), a read-only view
  of the filesystem except `instance/`, private `/tmp` and `/dev`, the
  `@system-service` syscall filter, and `StartLimitIntervalSec=0` so it
  restarts forever. `.env` and `instance/` are mode 600 / 700.
  `systemd-analyze security setter` shows the result; if a future distro
  rejects a directive the service log names it and the line can be removed
  from `install.sh`.
- **Pinned dependencies.** `requirements.txt` names exact versions that the
  test suite was run against.

### What it does not do

- No HTTPS. Setter is meant for a LAN; if it must be reached across one,
  put it behind a reverse proxy with TLS and keep `ALLOWED_NETWORKS` to the
  proxy's address.
- No per-user accounts or audit of *who* logged in; the audit log records
  the sender's IP for every action, which on a show network is enough.
- UDP cannot be authenticated. The allowlist is by source address, so the
  control VLAN has to be one you trust.

---

## How it talks to the ATEM

The ATEM speaks a reliable-UDP protocol on port 9910: a hello handshake,
then every packet carrying commands has to be acknowledged, and the switcher
drops any client that stops acknowledging its keepalives. `atem_client.py`
implements that in about 400 lines — session handshake, acks, retransmit
requests, the initial state dump, and a state model for the fields Setter
displays. There is no TCP option on the hardware itself.

The protocol is reverse-engineered; the references used were the
[OpenSwitcher documentation](https://docs.openswitcher.org/) and the older
Skaarhoj notes. Tested against the simulator in `tools/`; field layouts for
`_top` and `InPr` have changed between firmware generations, so if a real
switcher shows odd topology numbers on the Settings page, that is where to
look. The counts you type into Settings are what the panel uses, so the
display does not depend on those fields being parsed perfectly.
