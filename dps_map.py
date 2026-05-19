"""DPS (data point) maps per Dabbsson model.

DBS1400Pro — verified live on 2026-05-18 against two real units
(firmware P14PL-*, Tuya protocol 3.5, category bxsdy).

Verification sources:
  - Tuya cloud "getdps" schema: only dp1, dp10, dp25 are publicly registered.
    dp1  = battery_percentage  (Integer %, 0-100)  -> soc_pct
    dp10 = temp_current        (Integer °C)        -> temp_c
    dp25 = beep                (Boolean buzzer)    -> ignored
  - Physical probe (person at the units):
    dp109 = AC inverter output on/off   -> ac_on   (the control lever)
    dp111 = 12V DC output on/off
  - Observed:
    dp127 = working mode string ("standby_mode", ...)
    dp134 = status flag (16384 when AC off, 0 when AC on)
    dp158 = structured telemetry string (AC in/out, PV, battery, temps)

Everything else (102, 123, 135-137, 143, 145, 150, 151, ...) is an
undocumented private DP; 135/136/137/143 read 0 even under load and are
not used.

dp158 telemetry LAGS badly (tens of seconds). That is acceptable here: the
control loop is gated by SoC (dp1) and AC state (dp109), both of which read
promptly; dp158 watts are only used for slow car-count inference.

dp158 payload example:
  AC输入{0.00V,0.00A,0W,60HZ,使能:1}
  AC输出{0.00V,0.00A,0W}
  PV{0.00V,0.00A,0W}
  INV电池端{52.6V,0.0A,0W,设置:526W}
  温度{转速:0,INV:36℃,BMS:29℃,DCDC:58℃,MPPT:34℃,PD:39℃}
"""

# Legacy reference map (community DBS2300 layout — not used by the 1400 Pro).
DBS2300 = {
    1: "soc_pct",
    10: "temp_c",
    103: "solar_in_w",
    108: "ac_out_w",
    109: "ac_on",
    123: "ac_in_w",
}

# Verified DBS1400 Pro map. `telemetry` is parsed specially in the poller.
DBS1400Pro = {
    1: "soc_pct",
    10: "temp_c",
    109: "ac_on",
    127: "mode",
    158: "telemetry",
}

MAPS = {
    "DBS2300": DBS2300,
    "DBS1400Pro": DBS1400Pro,
}
