# BERM session logs

Your BERM Base records what your brain was doing, once a second, for every
session you play. This turns those recordings into something you can actually
work with.

**No dependencies.** Python 3.9+ and nothing else — `decode_session.py` uses
only the standard library, so there is nothing to install.

## Get a session off the pedal

Open BERM Base's web panel, follow the **session logs** link, and download the
session you want. You'll get a `.bin` file.

## Read it

```bash
python3 decode_session.py 12.bin
```

```
session 12  fw 1.0  source live
  clock: webpanel (1755900000)
  2731 samples @ 1 Hz  ≈ 45m31s
  dwell: alpha 1620s, beta 604s, theta 402s, gamma 105s
  no-contact: 38s (1.4%)
  mod_activity mean: 0.214
  expression mean: 0.437
```

## Turn it into a CSV

```bash
python3 decode_session.py 12.bin --csv session-12.csv
```

One row per second, with a column for every band on each side, hemispheric
balance, Expression, stillness, contact quality, which preset was loaded, and
how much your CCs were moving. Open it in a spreadsheet, load it in pandas or R,
or feed it to your own visualizer.

## The format

64-byte header, then one 40-byte record per second, little-endian. The layout
is at the top of `decode_session.py` and the full specification — every field,
its encoding, and why it was chosen — is in the firmware repo's
`docs/SESSION_LOG_FORMAT.md`.

The decoder checks the schema version and **refuses to decode a file it doesn't
recognise** rather than silently misreading fields. `--selftest` round-trips a
synthetic file to confirm the struct layout matches.

## What's in a record

| | |
|---|---|
| `pct_l_*` / `pct_r_*` | Each band's share of total power, per hemisphere |
| `tp_l` / `tp_r` | Absolute total power, log-scaled |
| `balance1`, `balance2` | Hemispheric balance, 0.5 = even |
| `expression` | The shaped Expression value |
| `stillness` | 1 = still mind |
| `dominant` | Strongest active band (delta excluded — it dominates at forehead sites and says nothing about state) |
| `quality`, `no_contact` | Electrode contact; a lifted electrode is visible rather than averaged in |
| `preset`, `influence` | What the rig was set to |
| `mod_activity` | How much your CCs actually moved that second |

## Your data is yours

None of this is uploaded anywhere. It lives in flash on your pedal until you
download it. That's the whole design — see the privacy note in the Base manual
at [berm.live](https://berm.live).

## Licence

GPL-3.0, same as the rest of the code here. See
[`../LICENSE-GPL-3.0.txt`](../LICENSE-GPL-3.0.txt).
