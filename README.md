# BERM

**Biofeedback Expression Response Modulator**

BERM is a real-time embedded biofeedback-to-MIDI bridge for live music performance. A performer wears an EEG headband; an ESP32-based device reads the headband over Bluetooth Low Energy, computes per-band activity in real time, and emits MIDI Continuous Controller messages to a Digital Audio Workstation. Inside the DAW, those messages modulate effect parameters applied to the performer's own live audio. The performer's brain state shapes their own sound, in a closed loop, with no host-side software in the path.

## What's in this repo

**[`bridge/`](./bridge) — BERM Bridge.** Free, open source, GPL-3.0. Optional
software that lets your own programs read the same live EEG your rig is
running on, while BERM Base keeps driving it. See
[`bridge/README.md`](./bridge/README.md) to install and run it.

**[`session-logs/`](./session-logs) — session log decoder.** GPL-3.0, no
dependencies. Turns the recordings BERM Base makes into readable summaries or
CSV, so months of your own brain data are yours to analyse.

The rest of this file is architectural documentation of the hardware and
firmware. Hardware sales and the current product line are at
**[berm.live](https://berm.live)**.

## Why BERM exists

There is an established lineage of brain-computer music interfaces, going back to academic work by Eduardo Miranda and others, and including commercial precedent like WaveRider in the 1990s. Most of these systems share an architecture: physiological data drives a software process running on a host computer, which generates or controls music in some way.

BERM differs in two respects.

First, the device is the system. All sensing, processing, and MIDI emission happen in firmware on a small ESP32-based unit. The DAW sees a standard MIDI controller. Earlier prototypes ran a Python bridge on a laptop in the signal path; that hop is gone.

Second, the output is continuous parameter modulation rather than preset switching. Each MIDI CC sweeps smoothly across its full range as the underlying brain-state feature changes. Reverb wet, filter cutoff, delay feedback, distortion drive, modulation depth, and any other DAW-modulatable parameter can be routed to a CC stream. This is finer-grained than systems that pre-configure several effects chains in a valence-arousal grid and crossfade between them. The performer hears their physiology shaping the texture of their own playing, not selecting between fixed presets.

BERM is built for the performer's own audio loop: the music they are making, with effects driven by the state they are in while making it. Physiology to sound, sound back to physiology, recursive.

## System overview

```
   ┌──────────────┐  BLE   ┌──────────────────────┐  USB-MIDI   ┌─────┐
   │ EEG headband ├───────►│   BERM (ESP32-S3)    ├────────────►│ DAW │
   └──────────────┘        │                      │             └──┬──┘
                           │  sample → condition  │                │
                           │  → shape → combine   │                ▼
                           │  → emit              │            audio FX
                           └──────────────────────┘                │
                                                                   ▼
                                                              performer
                                                                   │
                                                                   └──► loops back to brain state
```

The performer wears a Bluetooth EEG headband. The BERM device subscribes to the headband's notification stream, runs the samples through a fixed-rate processing pipeline, and emits MIDI CC values over USB. Any DAW that accepts MIDI CC modulation (Bitwig Studio is the reference setup; Ableton, Logic, Reaper, FL Studio, others all work) can route those CCs to effect parameters. Configuration of which feature drives which CC is done on-device through an OLED-and-encoder interface, with optional setup through a self-hosted web panel.

## Hardware

The device is built around an ESP32-S3 module providing both BLE central and native USB. The user-facing controls are four detented rotary encoders with integrated push switches, a 128 by 64 pixel OLED display for live state and configuration, two indicator LEDs (power and a tri-color status indicator), and four jacks on the back panel: 5-pin DIN MIDI for hardware MIDI gear, a 3.5 mm TRS MIDI output wired to the MMA Type A standard for modern compact gear that takes MIDI on a minijack, and two 1/4 inch TRS jacks for an expression pedal and a footswitch input. A panel-mounted USB Type B connector carries USB-MIDI and power; Type B rather than USB-C because the connector and its cables survive being on a floor.

MIDI therefore leaves the device on three connectors carrying two electrical streams: USB-MIDI, and a buffered hardware MIDI signal presented on both the 5-pin DIN and the 3.5 mm TRS jack. The two hardware connectors are the same signal on different plugs, so a receiver takes whichever it has an input for.

Form factor is a small desk unit, roughly 130 by 90 by 35 mm in the current revision, in a non-conductive enclosure (3D-printed PETG in the prototype). The non-conductive choice is deliberate: BERM runs three concurrent 2.4 GHz radios (BLE central for the headband, WiFi access point for the web panel, ESP-NOW reserved for sensor expansion), and a metal case would attenuate all three.

## Signal flow on the device

The processing pipeline runs at the EEG headband's sample rate, around 240 Hz per channel. Five stages, in order.

**Sample.** The device acts as a BLE central and subscribes to the headband's notification characteristics. EEG arrives as two-channel data: left frontal and right frontal electrodes. Each notification delivers a small batch of recent samples for both channels; the firmware unpacks them into independent left and right processing pipelines. Heart rate (from the headband's optical pulse sensor, where present) is also subscribed, as an additional biofeedback signal independent of the EEG.

**Condition.** Each EEG channel is processed independently. Each sample passes through five parallel bandpass filters, one per neuroscience-standard frequency band: gamma (30 to 45 Hz), beta (13 to 30 Hz), alpha (8 to 13 Hz), theta (4 to 8 Hz), and delta (0.5 to 4 Hz). Each filter is a 2nd-order Butterworth implemented as a cascade of biquad sections in second-order-section form, designed for the headband's ~240 Hz EEG sample rate.

**Shape.** Each band's filtered output is squared and accumulated into a sliding RMS window (~0.6 seconds of history, ~145 samples). The window produces a per-band RMS power value updated every sample, then normalized as a percentage of total power across all five bands. This runs separately for the left and right channels. For the per-band display and routing values, the left and right normalized powers are averaged into a single 0-to-100 figure per band.

The two channels also feed two derived hemispheric signals:

- **Hemispheric balance.** Computed from raw band RMS values (not the normalized percentages) as a left-vs-right ratio, scaled to a 0-to-1 range with 0.5 representing perfect symmetry. Frontal asymmetry between left and right hemispheres has a long history in EEG research as a marker of approach/withdrawal motivation; BERM exposes it as its own routable signal so it can drive an effect parameter independently of any individual band's activity.
- **Total power.** The sum of both hemispheres' total band power. Useful as a global "engagement" or "alertness" signal.

**Combine.** The five per-band values are first shaped individually (per-band floor, ceiling, invert, weight, enable), then summed into a single composite signal called the Expression. The Expression is the centerpiece of BERM's playable output: a single, smoothly-modulating value that reflects the performer's overall brain-band balance, configurable to weight any band more or less heavily and to invert any band's contribution. The Expression then passes through global response-speed and linger shaping (described below) before becoming available as a routable signal source. Up to eight CC channels can be configured, each independently routing the Expression, an individual band, total power, or another available source out to a chosen MIDI CC.

**Emit.** Each channel's shaped 0-to-1 value is scaled to 0-to-127 and emitted as a MIDI Continuous Controller message on MIDI channel 1. CCs are deduplicated against the last sent value, so the device only transmits when a CC has actually changed. The DAW sees a clean stream of CC updates, throttled to actual state changes rather than the full sample rate.

## Per-band shaping

Each of the five bands has its own configurable shape, set in a band-edit screen on the OLED. The same controls are also accessible from the web panel.

**Floor.** The minimum band value the band contributes after normalization. A floor of 20 means the band's contribution never drops below 20 even at the lowest readings. Useful for compensating for headbands or sessions that read low on a particular band.

**Ceiling.** The maximum band value after normalization. A ceiling of 80 caps the band's contribution at 80 even at high readings. Useful for keeping an active band from saturating and dominating the Expression.

**Invert.** Flips the band's contribution. High band power becomes low contribution and vice versa. Delta band is inverted by default (since increasing delta typically reflects drowsiness or eye-blink artifacts and should reduce, not raise, an effect parameter), and the user can invert any other band as desired.

**Weight.** A per-band scaling factor (0.1 to 3.0) applied to the band's contribution before it is summed into the Expression. Lets the performer give some bands more or less influence on the combined signal. A weight of 0 effectively disables the band's contribution.

**Enable.** A simple on/off toggle. Disabled bands contribute zero to the Expression. Useful for testing what a single band feels like in isolation, or for muting bands that are noisy on a given session.

## Expression shaping

Once the bands are weighted-blended into the Expression, four global controls shape the combined signal. These are the four front-panel encoders: rotating an encoder changes the corresponding parameter live, with an OLED overlay showing the current value.

**Floor.** The minimum value the Expression emits. Lifts the bottom of the Expression's range so an effect never fully disengages.

**Ceiling.** The maximum value the Expression emits. Caps the top of the range so an effect never fully saturates.

**Response speed.** A smoothing parameter that controls how quickly the Expression tracks changes in the underlying band activity. Implemented as a one-pole exponential moving average with the smoothing coefficient mapped over the control range. Fast settings track moment-to-moment fluctuations and reward developed neurofeedback control; slow settings produce smooth long-form modulation that is forgiving for beginners and produces meditative-tempo changes for ambient-style use.

**Linger.** An asymmetric-decay parameter that lets rising values smooth at one rate and falling values decay more slowly. With linger off, the Expression decays symmetrically with rising and falling source values. With linger on, peaks of activity hold longer than troughs, giving an effect a "tail" that smooths brief surges into a more continuous texture.

A peak-limit control (configurable from the band-edit screen and the web panel, but not on a dedicated encoder) caps the raw Expression value before smoothing, useful for keeping a single high reading from dominating the smoothed output.

## Per-CC routing

Each of the eight CC channels independently selects a source and emits to a chosen MIDI CC number. Available sources:

- The composite **Expression** signal
- Any of the **five individual band powers** (gamma, beta, alpha, theta, delta)
- **Hemispheric balance** (left/right asymmetry)
- **Heart rate**
- **Total power**

Plus per-CC controls:

- **Invert.** Flips the channel's output. Independent of any band-level invert.
- **Smoothing.** An optional additional smoothing applied to this CC channel only, useful when one effect parameter wants slower response than the rest.

This separation lets a single Expression value drive multiple effects with different inversions and rates: for example, the same Expression routed to a filter cutoff (fast, no extra smoothing) and to a reverb wet (slow, extra smoothing) and to a delay feedback (inverted, fast). The hemispheric balance and heart rate sources let a performer route physiological signals other than band-balance to additional parameters in parallel.

## Calibration

Brain-band power readings vary substantially from session to session and person to person. Two on-device calibration flows compensate for this drift, both reachable from the encoder UI.

**Quick calibration** runs for a few seconds, observes the current band powers at rest, and proposes a per-band floor near the rest value. The user can accept the proposal or dismiss it. Useful at session start when a headband has just been seated and wettened.

**A/B calibration** runs in two passes: a "low" pass while the user is in a relaxed state, and a "high" pass while the user is in an active state. The device computes both endpoints per band and proposes floors and ceilings based on the observed range. More accurate than Quick for sustained sessions; takes about a minute total.

Both flows propose values rather than applying them silently. The user always sees and confirms the proposed result.

## Configuration and operation

Day-to-day operation is encoder-and-OLED. The four encoders carry the live shaping parameters: expression floor, expression ceiling, response speed, and linger. Each encoder also has a press function: page navigation, MIDI on/off and quick save, calibration, and per-band edit. A short OLED overlay shows the active parameter and value when an encoder is rotated, then the display returns to its primary view.

For deeper configuration (CC source assignments, per-band floor/ceiling/invert, preset save and recall, firmware update), the device hosts a self-served web panel on its WiFi access point. The performer connects to the device's WiFi, opens the web panel in any browser, and configures everything that does not need real-time access. Once configured, the panel is irrelevant to live use; everything performance-critical is on the encoders and OLED.

Up to a configurable number of presets are stored in flash. Loading a preset restores the entire CC mapping, per-band ranges, and shaping. Switching between presets mid-session is a few encoder presses.

## MIDI mapping and DAW integration

BERM emits on MIDI channel 1. The CC numbers used are configurable per channel; the default mapping starts at CC 1 and increments. A typical setup might be:

- CC 1 (Expression) -> reverb wet — the composite band-balance signal driving the lead effect
- CC 2 (alpha power) -> filter cutoff — relaxation directly opens the filter
- CC 3 (beta power) -> delay feedback — focused activity feeds into echo
- CC 4 (hemispheric balance) -> stereo panning — frontal asymmetry sweeps left/right
- CC 5 (heart rate) -> tremolo rate — physiological pulse becomes audible rhythm
- CC 6 (Expression, inverted) -> distortion drive — high Expression pulls drive down

Inside the DAW, each CC is mapped to an effect parameter using the DAW's standard MIDI learn or modulator-binding mechanism. There is nothing BERM-specific about the DAW side; if the DAW can map a MIDI CC to a parameter, it can use BERM. Bitwig Studio is the reference setup, but Ableton Live, Logic Pro, Reaper, FL Studio, Cubase, and any modern DAW work identically.

## Latency

The dominant latency contributor is the EEG headband's BLE notification rate, which is outside BERM's control. On the BERM side, the processing pipeline itself adds approximately one window length plus one sample period of group delay (about 600 ms) before a band-power change is fully reflected. For most of the effects-modulation use case, this is appropriate: brain-band changes happen on the second-to-minute scale, not the millisecond scale, and aggressive low-latency would mostly amplify noise. The "response speed" control lets the performer dial in the perceived responsiveness independently of the underlying window.

USB-MIDI emission itself is essentially zero-latency from the DAW's perspective.

## Related work

The brain-computer music interface field has a long history. Eduardo Miranda's body of work, including the BCMI-Piano and subsequent systems, established much of the academic framing. WaveRider, in the 1990s, was an early commercial multi-channel EEG-plus-GSR device with MIDI output. More recent open-source and commercial entries include EEGsynth, Brain2MIDI, and various academic prototypes.

The closest functional analog is witheFlow (Dervakos et al., 2025), an EEG-and-ECG system that emits MIDI to a DAW for real-time effects modulation. witheFlow uses gain-based crossfading between several pre-configured effects chains arranged in a valence-arousal quadrant; BERM uses continuous per-channel parameter modulation. witheFlow is a Python application running on a laptop; BERM is firmware. Both architectures are valid; they produce different feels in performance.

## Status

A working prototype exists and is in active use for the author's own practice and performance. Firmware is stable enough for everyday session use. The hardware is in its second PCB revision. The project is at the personal-validation stage of a staged commercialization plan; broader release will follow after a closed cohort of additional users has validated the design under varied conditions.

## Source code

The firmware, PCB design files, and bill of materials are not currently in this repository. They will be released at a future point that will be announced here.

Until then, this README serves as architectural documentation. The intent is that someone skilled in embedded systems, EEG signal processing, or music technology can read this document and form an accurate picture of how BERM is implemented at the architectural and signal-processing level, even without seeing the source.

## License

Two licenses, because code and documentation want different things from one.

**Code — GPL-3.0.** Everything in [`bridge/`](./bridge). Full text in
[LICENSE-GPL-3.0.txt](./LICENSE-GPL-3.0.txt).

Share-alike, deliberately: credit the original author, and release derivative
works under the same license. GPL-3.0 is the software equivalent of CC BY-SA —
Creative Commons themselves point to it for code, because CC licenses say
nothing useful about linking, derivative works, or patents.

It asks very little of anyone building *with* BERM. Bridge is a separate
process with an HTTP API, so nobody links it into their program — they talk to
it over localhost. If your software reads from Bridge, the GPL doesn't reach
your code at all. It binds only someone who modifies Bridge itself and
redistributes it, and then only to pass the improvement on.

**Documentation, hardware designs, and written material — CC BY-SA 4.0.**
Including this README. Credit the original author and release derivative works
under the same license. Full text in
[LICENSE-CC-BY-SA-4.0.txt](./LICENSE-CC-BY-SA-4.0.txt), summary at
https://creativecommons.org/licenses/by-sa/4.0/.

[LICENSE](./LICENSE) states which applies to what.

Copyright (c) 2026 Ronnie North.

## Author

Ronnie North.

Contact: ronnienorth@gmail.com

---

*Last updated: 2026-08-22*
