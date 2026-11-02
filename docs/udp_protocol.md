# Setter control string protocol

One grammar for every way in: UDP datagrams to the listener (default port
9990), the dashboard's *Send a control string* box, and `GET/POST /api/v1/cmd`.
Whatever you type in the dashboard box is exactly what your control system
should send.

## Format

- Plain text, UTF-8. Case-insensitive. Tokens separated by spaces.
- Several commands per message, separated by newlines or `;`.
  `PVW 3; CUT` previews input 3 then cuts.
- A trailing newline or carriage return is ignored.
- Each command gets one reply line (sent back to the UDP sender if
  *Send an OK / ERR reply* is on in Settings):

      OK FADE 2 25 ME1
      ERR PGM 9: input 9 is outside the configured inputs 1..8 (change it in Settings, or use #9 to force a raw source id)
      ERR CUT ME1: not connected to switcher
      ERR AUTO ME1: switcher did not acknowledge
      PONG

  Replies always start with `OK`, `ERR` or `PONG`, so a controller only has
  to look at the first word.

## Commands

| Command | Does |
|---|---|
| `PING` | Replies `PONG`. Use it as a heartbeat. |
| `STATUS` | `OK CONNECTED <model> PGM=<name> PVW=<name>`, or `OK CONNECTING` / `OK DISABLED`. |
| `HELP` | Lists the commands. |
| `PGM <src> [MEn]` | Cut `<src>` straight to program (no transition). |
| `PVW <src> [MEn]` | Put `<src>` on preview. |
| `CUT [MEn]` | Cut: preview becomes program. |
| `AUTO [MEn]` | Auto transition using the switcher's current style and rate. |
| `FADE <src> [rate] [MEn]` | Preview `<src>`, select a Mix transition, optionally set the rate, then auto. One command does the whole fade. `MIX` is an alias. |
| `STYLE <mix\|dip\|wipe\|dve\|sting> [MEn]` | Set the next transition style. |
| `RATE <rate> [MEn]` | Set the mix rate. |
| `FTB [MEn]` | Toggle fade to black. |
| `FTBRATE <rate> [MEn]` | Set the fade-to-black rate. |
| `AUX <n> <src>` | Route `<src>` to aux output `n` (1-based). |
| `DSK <n> <on\|off\|auto\|toggle>` | Downstream keyer `n`. `auto` transitions it; `on`/`off` cut it. |
| `KEY <n> <on\|off> [MEn]` | Upstream keyer `n` on the given ME. |
| `MACRO <n>` | Run macro `n` (1-based, as shown in ATEM Software Control). |
| `RAW <NAME> <hex>` | Send any 4-character ATEM command with a hex payload. `RAW CPgI 00 00 00 01` cuts input 1 to ME1. |

## Arguments

**`<src>`** — a source. Any of:

| Token | Source |
|---|---|
| `1` .. `N` (also `CAM3`, `IN3`, `INPUT3`) | numbered inputs; `N` is the input count in Settings |
| `BLACK` / `BLK` | black |
| `BARS` | colour bars |
| `COL1`, `COL2` | colour generators |
| `MP1`, `MP1K`, `MP2`, `MP2K` (…`MP4K`) | media player fill / key |
| `SS`, `SS2` | SuperSource |
| `CF1` .. `CF4` | clean feeds |
| `PGM`, `PVW`, `ME2PGM`, `ME2PVW` … | ME outputs (useful for aux routing) |
| `AUX1` .. `AUXn` | aux outputs as sources |
| `#<id>` | any raw ATEM source id, no validation: `#3010` |

**`<rate>`** — frames (`25`), or seconds with an `s` suffix (`1s`, `0.5s`)
converted using the switcher's current video standard. 1–250 frames.

**`[MEn]`** — `ME1` … `ME4`; defaults to `ME1`. For `CUT`, `AUTO` and
`FTB` a bare number also works as the ME (`CUT 2`). For `FADE` and `RATE`
a bare number is the rate, so write `FADE 2 25 ME2`.

## Examples

```
FADE 2 25            fade to input 2 over 25 frames on ME1
FADE 2 1s            same, but "one second" whatever the frame rate
FADE 3               fade to input 3 at the current mix rate
PVW 4; CUT           take input 4 with a cut
PGM BARS             bars straight to program
AUX 1 PGM            aux 1 follows program
AUX 2 MP1            media player 1 to aux 2
DSK 1 AUTO           bring DSK 1 on (or off) with its transition
MACRO 3              run macro 3
RATE 0.5s; AUTO      half-second auto transition
```

## Sending from different systems

**Shell**

```
echo "FADE 2 25" | nc -u -w1 10.1.1.50 9990
python tools/udp_send.py 10.1.1.50 9990 "FADE 2 25"
```

**Q-SYS** — see `docs/qsys_integration.lua`. A `UdpSocket` is all it needs.

**Crestron** — a UDP client symbol (SIMPL) or `UDPServer` (SIMPL#) aimed at
Setter's IP and port; send the string with `\n` appended, parse the reply by
its first word.

**Bitfocus Companion** — the *Generic UDP* module, one action per button.

**Anything with HTTP but no UDP** — `GET http://setter:5000/api/v1/cmd?c=FADE+2+25`
(add `&format=text` for a plain `OK …` body, and `&api_key=…` if a key is set).
