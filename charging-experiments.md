# Charge-inhibit experiments on the M5 MacBook Air

**The Mac paused charging, kept running from the power bank, and resumed charging when the pause was released.** The first timed trial verified the SMC write and restore. A subsequent run of `dors_pas.py` verified the full behavior while the bank was already charging the Mac.

## Setup

| Item | Tested value |
| --- | --- |
| Hardware | MacBook Air with Apple M5 (`Mac17,3`) |
| macOS | 26.5.2 (`25F84`) |
| Firmware | `18000.121.3` |
| Normal charge limit | 80%, as configured in System Settings |
| Diagnostic | `/tmp/dors-pas-charge-diagnostic.py`, run with `sudo` for SMC access |

## Results

| Experiment | Observation | Conclusion |
| --- | --- | --- |
| SMC access | The unprivileged probe could not open either SMC interface. With `sudo`, both interfaces read `CHTE` as a four-byte `ui32` with value `0x00000000`. | The host exposes the direct charge-inhibit key. The privileged probe can read it. |
| External power at 80% | macOS reported AC power, charging off, and battery current of 0 mA. Reported input power matched system load. | The adapter was powering the Mac while the internal battery was idle. |
| Initial trials at 77% | macOS reported AC attached and charging off. Input power was zero and battery current was negative. The trial required active charging and exited before writing anything. | An attached adapter can register as AC power while the internal battery supplies the load. |
| Timed trial at 77% | The script wrote `0x01000000` to `CHTE` and read it back. During the roughly 12-second hold and subsequent recovery, the reported input stayed at zero and battery current stayed around −583 mA, equivalent to about 7.2 W supplied by the battery. | The inhibit value is writable on this firmware. The trial started with charging already off and showed no change in the reported power flow. |
| Restoration | The script restored `CHTE` to `0x00000000` and verified the value by reading it back. | The timed change was reversible on this Mac. |
| Live hold at 73% | The Mac was charging from the bank at 73%. After inhibition was enabled, the table reported `FROM_PLUG` and `IDLE` 54 seconds later. The battery stayed at 73% for over 14 minutes before the pause was released. | The bank powered the Mac while the internal battery was idle. |
| Live resume | After release, the table reported `FROM_PLUG` and `CHARGING` 44 seconds later. The battery reached 74% on the next recorded row. | Charging resumed after releasing the pause. |

The SMC probes also found `CHIE`, the separate adapter-power control. The trials did not write it. The older `CH0B` and `CH0C` controls were unavailable, as were two of the three firmware limit keys. The privileged `pmset -g battlimit` output could not be parsed by the diagnostic, so the trials did not independently verify the configured 80% ceiling through that command. [SMC control reference](https://amperebattery.app/tech.html) · [Firmware compatibility notes](https://charlie0129.github.io/batt/compatibility/)

## Reading the power state

On this firmware, `dors_pas.py` uses `CHTE` for the charging pause and verifies the value after each write. The table reports the inhibit setting separately from the measured power flow. It uses external input power and signed battery current to show what the bank and internal battery are doing.

The live timings describe when the table first reported each change. The hardware may have reacted before macOS published the new reading.
