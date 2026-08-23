# BERM Bridge

Free, open source, and **optional**. Your BERM hardware works completely
without this — Base, Body and Bright never need a computer in the chain.
Bridge is here for when you want your *own* software to see the same brain
data your rig is running on.

## The problem it solves

A BLE headband pairs with **one device at a time**. The moment BERM Base
connects to your Muse or FlowTime, nothing else can read it — your laptop is
locked out.

Base gets around this by re-broadcasting the raw EEG it's already receiving
over WebSocket. Bridge subscribes to that stream and re-serves it as a plain
local HTTP API. So your visualizer, analyzer, patch, or game reads the same
brain data, live, while Base is driving your pedals.

```
headband ──BLE──▶ BERM Base ──WebSocket──▶ Bridge ──HTTP──▶ your software
                      │
                      └──▶ MIDI / add-ons (unaffected)
```

## Install

You need **Python 3.9 or newer**. Check with `python3 --version`
(`python --version` on Windows).

```bash
git clone https://github.com/ronniebnorth/berm.git
cd berm/bridge
python3 -m pip install -r requirements.txt
```

On Windows, use `py -m pip install -r requirements.txt`.

If pip is slow or noisy installing numpy and scipy, that's normal — they're
large scientific libraries and it can take a few minutes.

## Run

**First, enable RAW STREAM in BERM Base's web panel.** Base doesn't
re-broadcast EEG unless you ask it to, and this is the single most common
reason Bridge runs but never receives anything.

Then connect your computer to BERM Base's WiFi network (`BERM`) and run:

```bash
python3 berm_bridge.py
```

That's it. It defaults to Base at `192.168.4.1` and serves on port **8520**.
If Base is on your home network instead, point at it:

```bash
python3 berm_bridge.py --berm-host 10.0.1.42
```

Check it's alive: open <http://localhost:8520/state> in a browser. You should
see JSON with a sample rate and a channel mask.

## What it serves

All on `http://localhost:8520`:

| Endpoint | What you get |
|---|---|
| `GET /eeg` | recent per-channel samples |
| `GET /peaks` | per-band peak values |
| `GET /spectrum` | frequency spectrum |
| `GET /state` | connection state, sample rate, channel mask |
| `POST /notch` | toggle the 50/60 Hz notch filter |

CORS is open, so a page served from anywhere can fetch from it — that's the
point. Bridge binds `0.0.0.0` by default, so anything on your local network
can reach it. On an untrusted network, use `--host 127.0.0.1`.

## Options

```
--berm-host      Base's address        (default 192.168.4.1)
--berm-ws-port   Base's WebSocket port (default 81)
--host           REST bind address     (default 0.0.0.0)
--port, -p       REST port             (default 8520)
```

## Troubleshooting

**Nothing at `/state`, or it never leaves "waiting".** Bridge can't reach
Base. Confirm you're on the right WiFi and that Base's panel loads at
<http://192.168.4.1>.

**Base's panel loads but no samples arrive.** Two causes, in order of
likelihood: RAW STREAM isn't enabled in Base's panel, or Base isn't receiving
from a headband — check the panel header says `LIVE`.

**`ModuleNotFoundError`.** The install step didn't finish, or you have more
than one Python. Try `python3 -m pip install -r requirements.txt` again and
run with the same `python3`.

Stuck? Email me: ronnienorth@gmail.com. I answer these myself.

## Licence

GPL-3.0 — see [`../LICENSE-GPL-3.0.txt`](../LICENSE-GPL-3.0.txt).

**If you're writing software that reads from Bridge, this doesn't affect you.**
Bridge is a separate process with an HTTP API; you talk to it over localhost.
Your game, visualizer, patch, or commercial product is your own, closed or
open, and the GPL doesn't reach it.

The licence binds you only if you modify Bridge itself and pass it on — then
the changed version stays GPL, with source, credited. Improve it and everyone
gets the improvement, which is the deal.

If you make something with it I'd genuinely like to see it.
