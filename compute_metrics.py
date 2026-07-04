#!/usr/bin/env python3
"""Compute NOOP-style scores from the Fitbit/Google Health data already in
InfluxDB and write them back as new measurements.

Ported from NOOP's StrandAnalytics (github.com/NoopApp/noop — PolyForm
Noncommercial 1.0.0; these derived algorithms are for noncommercial use only):
- Recovery (0-100, RecoveryScorer): HRV vs personal baseline (60%), resting HR
  (20%), sleep performance (15%), respiration (5%), squashed through a
  logistic. Cold-starts (no score) until 4 baseline nights.
- Strain (0-21, StrainScorer): Edwards TRIMP over the day's intraday heart rate.
- IllnessScore (0-100, IllnessSignalEngine): multi-signal heads-up — RHR up,
  HRV down, respiration up, skin temp up vs personal baselines; >=2 signals
  must corroborate. level: 0 quiet, 1 mild (>=25), 2 raised (>=50). NOOP's
  journal-based confounder suppression (alcohol/travel/...) has no data source
  here and is omitted — expect occasional false raises after a night out.
- VitalBand (VitalBands): personal in-range band per vital, |z| <= 2 vs the
  trailing baseline once it is trusted (>=14 nights); no band before that
  (NOOP falls back to population ranges — omitted here).
- SleepDebt (SleepDebt): rolling 14-night ledger of (slept - need) minutes,
  need = 8 h. Nights without data are skipped, never zero-filled.

Standard library only. Reads/writes InfluxDB 1.x over HTTP.
"""

from __future__ import annotations

import math
import urllib.parse
import urllib.request
from collections import defaultdict

import os

INFLUX = os.environ.get("INFLUX_URL") or "http://localhost:8086"
DB = "FitbitHealthStats"
DEVICE = os.environ.get("DEVICENAME") or "Fitbit Air"

# Recovery weights / logistic (recovery.py)
W_HRV, W_RHR, W_RESP, W_SLEEP = 0.60, 0.20, 0.05, 0.15
LOGISTIC_K, LOGISTIC_Z0 = 1.6, -0.20
SLEEP_CENTER, SLEEP_SCALE = 0.85, 0.12
MIN_NIGHTS_SEED = 4
# Strain (StrainScorer)
MAX_STRAIN, STRAIN_DENOM, MIN_HR_READINGS = 21.0, 7201.0, 600
EDWARDS = [(90, 5), (80, 4), (70, 3), (60, 2), (50, 1)]
DEFAULT_MAX_HR = 190.0  # 220 - 30; override per profile if known
# Illness heads-up (IllnessSignalEngine constants, pinned by NOOP's tests)
ILLNESS_Z_FIRE, ILLNESS_K, ILLNESS_CAP = 2.0, 22.0, 40.0
ILLNESS_MILD, ILLNESS_RAISE, ILLNESS_MIN_SIGNALS = 25.0, 50.0, 2
MIN_NIGHTS_TRUST = 14
SKIN_SPREAD_C = 0.3  # ~0.3 degC == one personal spread for the stored deviation
# Vital bands (VitalBands.sigmaK): in-range is |z| <= 2 vs the personal baseline
BAND_SIGMA_K = 2.0
BAND_FLOORS = {"resting_hr": 2.0, "hrv": 5.0, "resp_rate": 0.5, "skin_temp_dev": 0.3}
# Sleep debt (SleepDebt): 14-night ledger vs an 8 h need
SLEEP_NEED_MIN, SLEEP_DEBT_WINDOW = 8.0 * 60.0, 14


# --------------------------------------------------------------------------- #
# InfluxDB I/O
# --------------------------------------------------------------------------- #
def query(q: str) -> list[dict]:
    url = f"{INFLUX}/query?" + urllib.parse.urlencode({"db": DB, "q": q})
    with urllib.request.urlopen(url) as r:
        import json
        series = json.load(r)["results"][0].get("series", [])
    return series


def daily(measurement: str, field: str, where: str = "") -> dict[str, float]:
    """Latest value of `field` per local day for a daily measurement."""
    rows = query(f'SELECT "{field}" FROM "{measurement}" {where}')
    out: dict[str, float] = {}
    for s in rows:
        for ts, val in s["values"]:
            if val is None:
                continue
            out[ts[:10]] = float(val)
    return out


def hr_by_day(days_back: int) -> dict[str, list[tuple[int, int]]]:
    rows = query(f'SELECT value FROM "HeartRate_Intraday" WHERE time > now() - {days_back}d')
    import datetime
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for s in rows:
        for ts, val in s["values"]:
            if val is None:
                continue
            day = ts[:10]
            epoch = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            out[day].append((epoch, int(val)))
    for d in out:
        out[d].sort()
    return out


def write_points(lines: list[str]) -> None:
    body = "\n".join(lines).encode()
    req = urllib.request.Request(f"{INFLUX}/write?db={DB}&precision=s", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"influx write {r.status}")


# --------------------------------------------------------------------------- #
# Scorers (ported from StrandAnalytics)
# --------------------------------------------------------------------------- #
def baseline(values: list[float], floor_spread: float, window: int = 30):
    """Trailing mean + sample-SD baseline. Returns (mean, spread, n)."""
    trailing = values[-window:]
    n = len(trailing)
    if n == 0:
        return None
    mean = sum(trailing) / n
    if n >= 2:
        sd = (sum((v - mean) ** 2 for v in trailing) / (n - 1)) ** 0.5
    else:
        sd = floor_spread * 1.253
    spread = max(floor_spread, sd) / 1.253
    return (mean, spread, n)


def zscore(value: float, mean: float, spread: float) -> float:
    return (value - mean) / max(1.253 * spread, 1e-9)


def recovery(hrv, rhr, resp, sleep_perf, hrv_base, rhr_base, resp_base, hrv_usable):
    if not hrv_usable or hrv_base is None:
        return None
    terms = [(zscore(hrv, hrv_base[0], hrv_base[1]), W_HRV)]
    if rhr is not None and rhr_base:
        terms.append((zscore(rhr_base[0], rhr, rhr_base[1]), W_RHR))  # lower RHR -> higher
    if resp is not None and resp_base:
        terms.append((zscore(resp_base[0], resp, resp_base[1]), W_RESP))
    if sleep_perf is not None:
        terms.append(((sleep_perf - SLEEP_CENTER) / SLEEP_SCALE, W_SLEEP))
    tw = sum(w for _, w in terms)
    z = sum(zz * w for zz, w in terms) / tw
    return max(0.0, min(100.0, 100.0 / (1.0 + math.exp(-LOGISTIC_K * (z - LOGISTIC_Z0)))))


def illness(recent: dict[str, float | None], bases: dict[str, tuple | None]):
    """IllnessSignalEngine: illness-ward z per signal (RHR up, HRV down, resp up,
    skin-temp up), fire above z=2, sub-score min(40, 22*(z-2)), composite capped
    at 100. Returns (score, fired_count, level) — level 0 quiet / 1 mild / 2 raised.
    Silent (level 0) until the RHR or HRV baseline is trusted (>=14 nights)."""
    zs = []
    if recent.get("rhr") is not None and bases.get("rhr"):
        zs.append(zscore(recent["rhr"], bases["rhr"][0], bases["rhr"][1]))
    if recent.get("hrv") is not None and bases.get("hrv"):
        zs.append(-zscore(recent["hrv"], bases["hrv"][0], bases["hrv"][1]))  # drop = illness-ward
    if recent.get("resp") is not None and bases.get("resp"):
        zs.append(zscore(recent["resp"], bases["resp"][0], bases["resp"][1]))
    if recent.get("skin") is not None:
        zs.append(recent["skin"] / SKIN_SPREAD_C)  # stored deviation, zero-centred spread

    score, fired = 0.0, 0
    for z in zs:
        over = z - ILLNESS_Z_FIRE
        if over > 0:
            fired += 1
            score += min(ILLNESS_CAP, ILLNESS_K * over)
    score = min(100.0, score)

    trusted = any(bases.get(k) and bases[k][2] >= MIN_NIGHTS_TRUST for k in ("rhr", "hrv"))
    if not trusted or fired < ILLNESS_MIN_SIGNALS or score < ILLNESS_MILD:
        return score, fired, 0
    return score, fired, (2 if score >= ILLNESS_RAISE else 1)


def vital_band(value: float | None, base):
    """VitalBands: personal in-range band once the baseline is trusted (>=14
    nights). Returns (lo, hi, in_range) or None while the baseline is forming."""
    if base is None or base[2] < MIN_NIGHTS_TRUST:
        return None
    mean, spread, _ = base
    half = BAND_SIGMA_K * 1.253 * spread
    lo, hi = mean - half, mean + half
    in_range = 1 if (value is not None and lo <= value <= hi) else 0 if value is not None else None
    return lo, hi, in_range


def sleep_debt(series: list[tuple[str, float]]):
    """SleepDebt: per day, the net balance (minutes) of the last <=14 usable
    nights' (slept - need). Returns {day: (balance_min, delta_min)}."""
    out: dict[str, tuple[float, float]] = {}
    usable = [(d, m) for d, m in series if m and m > 0]
    for i, (d, slept) in enumerate(usable):
        window = usable[max(0, i + 1 - SLEEP_DEBT_WINDOW): i + 1]
        balance = sum(m - SLEEP_NEED_MIN for _, m in window)
        out[d] = (round(balance, 1), slept - SLEEP_NEED_MIN)
    return out


def strain(hr: list[tuple[int, int]], max_hr: float, resting_hr: float):
    if len(hr) < MIN_HR_READINGS or max_hr <= resting_hr:
        return None
    reserve = max_hr - resting_hr
    sample_min = abs(hr[1][0] - hr[0][0]) / 60.0 if hr[1][0] != hr[0][0] else 1 / 60.0
    weighted = 0
    for _, bpm in hr:
        pct = (bpm - resting_hr) / reserve * 100.0
        for thr, w in EDWARDS:
            if pct >= thr:
                weighted += w
                break
    trimp = weighted * sample_min
    if trimp <= 0:
        return 0.0
    return round(MAX_STRAIN * math.log(trimp + 1.0) / math.log(STRAIN_DENOM), 2)


# --------------------------------------------------------------------------- #
def main() -> None:
    hrv = daily("HRV", "dailyRmssd")
    rhr = daily("RestingHR", "value")
    resp = daily("BreathingRate", "value")
    skin = daily("Skin Temperature Variation", "RelativeValue")
    asleep = daily("Sleep Summary", "minutesAsleep", "WHERE isMainSleep='True'")
    inbed = daily("Sleep Summary", "minutesInBed", "WHERE isMainSleep='True'")
    hr_days = hr_by_day(days_back=21)
    debt = sleep_debt(sorted(asleep.items()))

    days = sorted(set(hrv) | set(rhr) | set(resp) | set(skin))
    band_sources = (("resting_hr", rhr), ("hrv", hrv), ("resp_rate", resp), ("skin_temp_dev", skin))
    dev = DEVICE.replace(" ", "\\ ")
    lines: list[str] = []
    summary = []

    def recent2(vals: dict[str, float], i: int):
        """Mean over the last two days ending at days[i] (IllnessSignalEngine's recent window)."""
        xs = [vals[x] for x in days[max(0, i - 1): i + 1] if x in vals]
        return sum(xs) / len(xs) if xs else None

    for i, d in enumerate(days):
        # Baselines from every night up to and including d (causal — no lookahead).
        hb = baseline([hrv[x] for x in days[: i + 1] if x in hrv], 5.0)
        rb = baseline([rhr[x] for x in days[: i + 1] if x in rhr], 2.0)
        pb = baseline([resp[x] for x in days[: i + 1] if x in resp], 0.5)
        sleep_perf = (asleep[d] / inbed[d]) if d in asleep and d in inbed and inbed[d] > 0 else None
        hrv_usable = hb is not None and hb[2] >= MIN_NIGHTS_SEED

        rec = recovery(hrv.get(d), rhr.get(d), resp.get(d), sleep_perf, hb, rb, pb, hrv_usable) if d in hrv else None
        st = strain(hr_days.get(d, []), DEFAULT_MAX_HR, rhr.get(d, 60.0))

        # Illness windows per IllnessSignalEngine: recent = last 2 days, baseline = the
        # ~28 days ending 3 days ago (so the anomaly can't contaminate its own baseline).
        base_days = days[max(0, i - 30): max(0, i - 2)]
        recents = {"rhr": recent2(rhr, i), "hrv": recent2(hrv, i),
                   "resp": recent2(resp, i), "skin": recent2(skin, i)}
        bases = {k: baseline([vals[x] for x in base_days if x in vals], floor)
                 for k, vals, floor in (("rhr", rhr, 2.0), ("hrv", hrv, 5.0), ("resp", resp, 0.5))}
        ill = illness(recents, bases) if any(v is not None for v in recents.values()) else None

        # timestamp the day at 12:00 UTC so Grafana shows one point/day
        import datetime
        ts = int(datetime.datetime.fromisoformat(d + "T12:00:00+00:00").timestamp())
        if rec is not None:
            lines.append(f"Recovery,Device={dev} value={rec:.1f} {ts}")
        if st is not None:
            lines.append(f"Strain,Device={dev} value={st:.2f} {ts}")
        if ill is not None:
            score, fired, level = ill
            lines.append(f"IllnessScore,Device={dev} value={score:.1f},signals={fired},level={level} {ts}")
        for key, vals in band_sources:
            # History EXCLUDES the displayed day (VitalBands.band contract).
            b = vital_band(vals.get(d), baseline([vals[x] for x in days[:i] if x in vals], BAND_FLOORS[key]))
            if b:
                lo, hi, in_r = b
                fields = f"lo={lo:.2f},hi={hi:.2f}" + (f",in_range={in_r}" if in_r is not None else "")
                lines.append(f"VitalBand,Device={dev},metric={key} {fields} {ts}")
        if d in debt:
            balance, delta = debt[d]
            lines.append(f"SleepDebt,Device={dev} balance_min={balance:.1f},delta_min={delta:.1f} {ts}")
        if rec is not None or st is not None or ill is not None:
            summary.append((d, rec, st, ill[2] if ill else 0, debt.get(d, (None,))[0]))

    if lines:
        write_points(lines)
    print(f"Wrote {len(lines)} points for {len(summary)} days.")
    print("day         Recovery  Strain  Illness  SleepDebt(h)")
    ill_names = {0: "quiet", 1: "MILD", 2: "RAISED"}
    for d, rec, st, lvl, bal in summary[-10:]:
        print(f"{d}  {('%.0f' % rec) if rec is not None else 'cold-start':>9}"
              f"  {('%.1f' % st) if st is not None else '—':>6}"
              f"  {ill_names[lvl]:>7}"
              f"  {('%+.1f' % (bal / 60.0)) if bal is not None else '—':>11}")


if __name__ == "__main__":
    main()
