#!/usr/bin/env python3
"""
decode_session.py — decode a BERM Base session log (.bin) to CSV or a summary.

Companion to firmware/pedal/src/session_log.h. Keep the struct layout here in
sync with SCHEMA_VERSION there; this script refuses to decode a schema it
doesn't know rather than silently misreading fields.

Usage:
    python3 decode_session.py 12.bin              # header + summary to stdout
    python3 decode_session.py 12.bin --csv out.csv
    python3 decode_session.py 12.bin --selftest   # verify struct layout only
"""
from __future__ import annotations
import struct, argparse, sys
from pathlib import Path

SCHEMA_VERSION = 1
HEADER_FMT = '<4sHHHHIIIBBxx16s20x'   # 64 bytes
RECORD_FMT = '<H5H5HHHHHHBBBBBBBB'    # 40 bytes
BANDS = ['gamma', 'beta', 'alpha', 'theta', 'delta']
SOURCE = {0: 'none', 1: 'live', 2: 'sim'}
CLOCK  = {0: 'none', 1: 'webpanel'}

TP_LOG_MIN, TP_LOG_MAX = -2.0, 4.0

FLAG_BYPASS, FLAG_CALIBRATING, FLAG_SIM, FLAG_NO_CONTACT = 1, 2, 4, 8


def dec_pct(v):   return v / 655.35
def dec_tp(v):    return 0.0 if v == 0 else 10 ** (TP_LOG_MIN + (v / 65535.0) * (TP_LOG_MAX - TP_LOG_MIN))
def dec_u16(v):   return v / 65535.0
def dec_u8(v):    return v / 255.0


def read_header(buf: bytes) -> dict:
    (magic, schema, rec_bytes, hz, hdr_bytes, sid, start_ms,
     wall, source, clock, fw) = struct.unpack(HEADER_FMT, buf[:64])
    if magic != b'BRML':
        raise SystemExit(f'not a BERM session log (magic {magic!r})')
    if schema != SCHEMA_VERSION:
        raise SystemExit(f'schema {schema} unsupported (this decoder knows {SCHEMA_VERSION})')
    if rec_bytes != struct.calcsize(RECORD_FMT):
        raise SystemExit(f'record size {rec_bytes} != decoder {struct.calcsize(RECORD_FMT)}')
    return dict(schema=schema, record_bytes=rec_bytes, sample_hz=hz, header_bytes=hdr_bytes,
                session_id=sid, start_uptime_ms=start_ms, wall_clock_utc=wall,
                source=SOURCE.get(source, source), clock_source=CLOCK.get(clock, clock),
                fw_version=fw.rstrip(b'\0').decode('ascii', 'replace'))


def read_records(buf: bytes, hdr: dict):
    rb, hb = hdr['record_bytes'], hdr['header_bytes']
    n = (len(buf) - hb) // rb
    for i in range(n):
        f = struct.unpack_from(RECORD_FMT, buf, hb + i * rb)
        t = f[0]
        pct_l, pct_r = f[1:6], f[6:11]
        (tp_l, tp_r, expr, bal0, bal1) = f[11:16]
        (still, dom, preset, infl, mod, qual, flags, _res) = f[16:24]
        yield dict(
            t=t,
            **{f'pct_l_{b}': round(dec_pct(v), 3) for b, v in zip(BANDS, pct_l)},
            **{f'pct_r_{b}': round(dec_pct(v), 3) for b, v in zip(BANDS, pct_r)},
            tp_l=round(dec_tp(tp_l), 6), tp_r=round(dec_tp(tp_r), 6),
            expression=round(dec_u16(expr), 4),
            balance1=round(dec_u16(bal0), 4), balance2=round(dec_u16(bal1), 4),
            stillness=round(dec_u8(still), 4),
            dominant=BANDS[dom] if dom < 4 else 'none',
            preset=('none' if preset == 255 else preset),
            influence=round(infl / 2.55, 1),
            mod_activity=round(dec_u8(mod), 4),
            quality=round(dec_u8(qual), 3),
            bypass=bool(flags & FLAG_BYPASS),
            calibrating=bool(flags & FLAG_CALIBRATING),
            sim=bool(flags & FLAG_SIM),
            no_contact=bool(flags & FLAG_NO_CONTACT),
        )


def selftest():
    assert struct.calcsize(HEADER_FMT) == 64, struct.calcsize(HEADER_FMT)
    assert struct.calcsize(RECORD_FMT) == 40, struct.calcsize(RECORD_FMT)

    # Round-trip a synthetic file through the real decode path.
    hdr = struct.pack(HEADER_FMT, b'BRML', 1, 40, 1, 64, 7, 1234, 0, 2, 0, b'0.1')
    rec = struct.pack(RECORD_FMT, 5,
                      *[int(20.0 * 655.35)] * 5, *[int(20.0 * 655.35)] * 5,
                      32768, 32768, 65535, 32768, 16384,
                      255, 2, 3, 255, 128, 255, FLAG_SIM, 0)
    h = read_header(hdr)
    assert h['session_id'] == 7 and h['source'] == 'sim' and h['fw_version'] == '0.1'
    r = list(read_records(hdr + rec, h))[0]
    assert abs(r['pct_l_gamma'] - 20.0) < 0.01, r['pct_l_gamma']
    assert abs(r['expression'] - 1.0) < 1e-4
    assert abs(r['balance1'] - 0.5) < 1e-4
    assert r['dominant'] == 'alpha' and r['preset'] == 3 and r['sim'] is True
    assert abs(r['influence'] - 100.0) < 0.5
    assert abs(dec_tp(32768) - 10 ** 1.0) < 0.05, dec_tp(32768)
    print('selftest OK — header 64 B, record 40 B, round-trip clean')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', nargs='?', type=Path)
    ap.add_argument('--csv', type=Path)
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return
    if not a.path:
        ap.error('path required (or --selftest)')

    buf = a.path.read_bytes()
    hdr = read_header(buf)
    rows = list(read_records(buf, hdr))

    print(f"session {hdr['session_id']}  fw {hdr['fw_version']}  source {hdr['source']}")
    print(f"  clock: {hdr['clock_source']}"
          + (f" ({hdr['wall_clock_utc']})" if hdr['wall_clock_utc'] else ' (uptime only)'))
    print(f"  {len(rows)} samples @ {hdr['sample_hz']} Hz  ≈ {len(rows)//60}m{len(rows)%60}s")
    if not rows:
        return

    dwell = {}
    for r in rows:
        dwell[r['dominant']] = dwell.get(r['dominant'], 0) + 1
    print('  dwell: ' + ', '.join(f'{k} {v}s' for k, v in
                                  sorted(dwell.items(), key=lambda kv: -kv[1])))
    poor = sum(1 for r in rows if r['no_contact'])
    print(f"  no-contact: {poor}s ({100*poor/len(rows):.1f}%)")
    print(f"  mod_activity mean: {sum(r['mod_activity'] for r in rows)/len(rows):.3f}")
    print(f"  expression mean: {sum(r['expression'] for r in rows)/len(rows):.3f}")

    if a.csv:
        import csv
        with a.csv.open('w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'  wrote {a.csv}')


if __name__ == '__main__':
    main()
