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
# Fitness age (FitnessAgeEngine — Nes 2011 HUNT model inverted self-consistently;
# the waist/body term cancels, so only age, sex, resting HR and activity matter).
# A fitness comparison vs an average peer, never a "biological age" claim.
FA_COEFFS = {"male": (0.296, 0.155, 0.226), "female": (0.247, 0.114, 0.198)}  # ageC, rhrC, paiC
FA_RHR_REF, FA_PAI_REF = 65.0, 5.0
FA_MIN_AGE, FA_MAX_AGE = 20.0, 80.0
FA_MIN_RHR_DAYS = 4  # of the last 7 nights
STRAIN_TO_100 = 100.0 / MAX_STRAIN  # our Strain is 0-21; NOOP v8 Effort is 0-100
USER_AGE = float(os.environ.get("USER_AGE") or 30)
USER_SEX = (os.environ.get("USER_SEX") or "male").lower()
# Vitality / Body Age (VitalityEngine — WHOOP-Age method): per-factor published
# all-cause-mortality hazard ratios vs a population reference, log-hazards summed
# with an overlap shrink, converted to years via the Gompertz doubling time
# (~8 y). A wellness comparison, never a clinical biological age.
VIT_LN_PER_YEAR = math.log(2) / 8.0
VIT_SHRINK, VIT_MIN_FACTORS, VIT_BAND = 0.75, 3, 5.0
VIT_MIN_AGE, VIT_MAX_AGE, VIT_PER_YEAR = 20.0, 90.0, 2.5
VIT_RMSSD_NORM = [(20, 47), (30, 40), (40, 33), (50, 29), (60, 25), (70, 22), (80, 20)]
# Body clock (CircadianEngine): single-component cosinor (Halberg) over the
# rest-activity rhythm; acrophase → estimated temperature-minimum → offset vs
# the user's own sleep schedule. Light/sleep TIMING advice only, never a
# supplement. Hours must be LOCAL — set LOCAL_TZ (IANA name) in .env.
CIRC_MIN_DAYS, CIRC_GOOD_DAYS = 7, 14
CIRC_MIN_REL_AMP = 0.10
CIRC_SHIFT_PER_DAY = 1.0          # hours/day re-entrainment rate
CIRC_CBT_BEFORE_WAKE = 2.5        # CBTmin sits ~2.5 h before habitual wake
CIRC_ACRO_AFTER_CBT = 12.0        # activity peak ~12 h after CBTmin
LOCAL_TZ = os.environ.get("LOCAL_TZ") or "UTC"
# Recovery forecast (RecoveryForecaster): tomorrow-morning recovery estimate
FC_WINDOW, FC_MIN_NIGHTS, FC_TRUSTED_NIGHTS = 14, 5, 10
FC_STRAIN_W, FC_EFFORT_SPREAD, FC_STRAIN_CAP = 9.0, 12.0, 12.0
FC_SLEEP_W, FC_SLEEP_OVER_CAP = 14.0, 0.25
FC_REVERSION_W, FC_REVERSION_CAP = 1.0, 8.0
FC_MIN_BAND, FC_THIN_BAND = 8.0, 6.0


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


def hourly_activity(days_back: int = 45) -> dict[str, list[tuple[float, float]]]:
    """Per LOCAL day: (local clock hour, steps) bins from Steps_Intraday."""
    rows = query(f'SELECT SUM("value") FROM "Steps_Intraday" WHERE time > now() - {days_back}d '
                 f"GROUP BY time(1h) fill(none)")
    import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(LOCAL_TZ)
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in rows:
        for ts, val in s["values"]:
            if val is None:
                continue
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
            out[t.strftime("%Y-%m-%d")].append((t.hour + t.minute / 60.0, float(val)))
    return out


def sleep_window_hours(days_back: int = 45) -> dict[str, tuple[float, float]]:
    """Per LOCAL day: (sleep-onset hour, wake hour) from the first/last main-sleep
    record of the night. Approximate but consistent — exactly what the cosinor
    schedule comparison needs."""
    rows = query(f'SELECT "level" FROM "Sleep Levels" WHERE "isMainSleep" = \'True\' '
                 f"AND time > now() - {days_back}d")
    import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(LOCAL_TZ)
    spans: dict[str, list] = defaultdict(list)
    for s in rows:
        for ts, _ in s["values"]:
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
            spans[t.strftime("%Y-%m-%d")].append(t)
    return {d: (ts_[0].hour + ts_[0].minute / 60.0, ts_[-1].hour + ts_[-1].minute / 60.0)
            for d, ts_ in ((d, sorted(v)) for d, v in spans.items())}


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


def recovery_terms(hrv, rhr, resp, sleep_perf, hrv_base, rhr_base, resp_base):
    terms = [("hrv", zscore(hrv, hrv_base[0], hrv_base[1]), W_HRV)]
    if rhr is not None and rhr_base:
        terms.append(("rhr", zscore(rhr_base[0], rhr, rhr_base[1]), W_RHR))  # lower RHR -> higher
    if resp is not None and resp_base:
        terms.append(("resp", zscore(resp_base[0], resp, resp_base[1]), W_RESP))
    if sleep_perf is not None:
        terms.append(("sleep", (sleep_perf - SLEEP_CENTER) / SLEEP_SCALE, W_SLEEP))
    return terms


def score_terms(terms):
    tw = sum(w for _, _, w in terms)
    z = sum(zz * w for _, zz, w in terms) / tw
    return max(0.0, min(100.0, 100.0 / (1.0 + math.exp(-LOGISTIC_K * (z - LOGISTIC_Z0)))))


def recovery(hrv, rhr, resp, sleep_perf, hrv_base, rhr_base, resp_base, hrv_usable):
    if not hrv_usable or hrv_base is None:
        return None
    return score_terms(recovery_terms(hrv, rhr, resp, sleep_perf, hrv_base, rhr_base, resp_base))


def recovery_drivers(terms):
    """ChargeDrivers-style 'why is my recovery X': leave-one-out attribution —
    each factor's points = full score minus the score computed without it."""
    if len(terms) < 2:
        return {}
    full = score_terms(terms)
    return {k: round(full - score_terms([t for t in terms if t[0] != k]), 1)
            for k, _, _ in terms}


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


def fitness_age(rhr7: list[float], strains7: list[float]):
    """FitnessAgeEngine: Fitness Age = age + (rhrC*(RHR-65) - paiC*(PAI-5)) / ageC,
    clamped [20, 80]. RHR is the 7-night median; the HUNT PA-index is reconstructed
    from Strain (active day = effort >= 30/100; intensity*duration = mean active
    effort / 30, capped at 3). Returns (fitness_age, delta_years — positive means
    younger than your calendar age) or None under 4 RHR nights."""
    if len(rhr7) < FA_MIN_RHR_DAYS:
        return None
    rhr_med = sorted(rhr7)[len(rhr7) // 2]
    active = [s * STRAIN_TO_100 for s in strains7 if s * STRAIN_TO_100 >= 30.0]
    freq = {0: 0.0, 1: 0.5, 2: 1.0, 3: 2.5, 4: 2.5}.get(len(active), 5.0)
    pai = freq * min(3.0, (sum(active) / len(active)) / 30.0) if active else 0.0
    age_c, rhr_c, pai_c = FA_COEFFS.get(USER_SEX, FA_COEFFS["male"])
    fa = USER_AGE + (rhr_c * (rhr_med - FA_RHR_REF) - pai_c * (pai - FA_PAI_REF)) / age_c
    fa = min(FA_MAX_AGE, max(FA_MIN_AGE, fa))
    return round(fa, 1), round(USER_AGE - fa, 1)


def forecast(recoveries: list[float], strains: list[float], sleep_hours: list[float]):
    """RecoveryForecaster: tomorrow-morning recovery = 14-night baseline mean plus
    three signed nudges — strain debt (today's effort vs recent average), sleep
    adequacy, and mean reversion against the recent slope — with an honest ± band
    (recent SD, floored, inflated while history is thin). Divergence from NOOP:
    'planned sleep tonight' isn't knowable here, so the 14-night average sleep
    stands in. Returns (value, band) or None under 5 recovery nights."""
    rec = recoveries[-FC_WINDOW:]
    if len(rec) < FC_MIN_NIGHTS:
        return None
    center = sum(rec) / len(rec)
    adj_strain = 0.0
    eff = [s * STRAIN_TO_100 for s in strains[-FC_WINDOW:]]
    if eff:
        excess = FC_STRAIN_W * (eff[-1] - sum(eff) / len(eff)) / FC_EFFORT_SPREAD
        adj_strain = -max(-FC_STRAIN_CAP, min(FC_STRAIN_CAP, excess))
    adj_sleep = 0.0
    if sleep_hours:
        window = sleep_hours[-FC_WINDOW:]
        planned = sum(window) / len(window)
        adj_sleep = FC_SLEEP_W * max(-1.0, min(FC_SLEEP_OVER_CAP,
                                               planned / (SLEEP_NEED_MIN / 60.0) - 1.0))
    n = len(rec)
    xm = (n - 1) / 2
    slope = sum((i - xm) * (rec[i] - center) for i in range(n)) / sum((i - xm) ** 2 for i in range(n))
    adj_revert = -max(-FC_REVERSION_CAP, min(FC_REVERSION_CAP, FC_REVERSION_W * slope))
    value = max(0.0, min(100.0, center + adj_strain + adj_sleep + adj_revert))
    sd = (sum((r - center) ** 2 for r in rec) / (n - 1)) ** 0.5 if n >= 2 else 0.0
    band = max(FC_MIN_BAND, sd) + (FC_THIN_BAND if n < FC_TRUSTED_NIGHTS else 0.0)
    return round(value, 1), round(band, 1)


def wrap24(h: float) -> float:
    return h % 24.0


def signed_hour_delta(a: float, b: float) -> float:
    """Signed shortest delta in hours from a to b on the 24 h clock, in (-12, 12]."""
    d = (b - a) % 24.0
    return d - 24.0 if d > 12.0 else d


def cosinor(bins: list[tuple[float, float]]):
    """Single-component cosinor: fit y = M + beta*cos(wt) + gamma*sin(wt) by OLS
    (Cramer's rule on the 3x3 normal equations). Returns (mesor, amplitude,
    acrophase_hours — clock time of the activity PEAK) or None if degenerate."""
    if len(bins) < 3:
        return None
    w = 2.0 * math.pi / 24.0
    n = float(len(bins))
    sy = sc = ss = scc = sss = scs = syc = sys_ = 0.0
    for hour, y in bins:
        c, s = math.cos(w * hour), math.sin(w * hour)
        sy += y; sc += c; ss += s
        scc += c * c; sss += s * s; scs += c * s
        syc += y * c; sys_ += y * s
    det = (n * (scc * sss - scs * scs) - sc * (sc * sss - scs * ss)
           + ss * (sc * scs - scc * ss))
    if abs(det) <= 1e-12:
        return None
    det_m = (sy * (scc * sss - scs * scs) - sc * (syc * sss - scs * sys_)
             + ss * (syc * scs - scc * sys_))
    det_b = (n * (syc * sss - scs * sys_) - sy * (sc * sss - scs * ss)
             + ss * (sc * sys_ - syc * ss))
    det_g = (n * (scc * sys_ - syc * scs) - sc * (sc * sys_ - syc * ss)
             + sy * (sc * scs - scc * ss))
    m, beta, gamma = det_m / det, det_b / det, det_g / det
    amplitude = (beta * beta + gamma * gamma) ** 0.5
    phase = wrap24(math.atan2(gamma, beta) / w)
    return m, amplitude, phase


def estimate_phase(bins, days_observed: int, wake_hour: float):
    """CircadianEngine.estimatePhase: (temp_min_h, acrophase_h, offset_min,
    confidence 0 unreadable / 1 wide / 2 solid, rel_amplitude) or None.
    offset_min > 0 = body clock later than the schedule implies (night-owl lean)."""
    fit = cosinor(bins)
    if fit is None:
        return None
    mesor, amplitude, acro = fit
    rel_amp = amplitude / abs(mesor) if mesor != 0 else 0.0
    temp_min = wrap24(acro - CIRC_ACRO_AFTER_CBT)
    if days_observed < CIRC_MIN_DAYS or rel_amp < CIRC_MIN_REL_AMP:
        return temp_min, acro, 0.0, 0, rel_amp
    ideal_temp_min = wrap24(wake_hour - CIRC_CBT_BEFORE_WAKE)
    offset_min = signed_hour_delta(ideal_temp_min, temp_min) * 60.0
    conf = 2 if days_observed >= CIRC_GOOD_DAYS else 1
    return temp_min, acro, round(offset_min, 1), conf, rel_amp


def clock(hour: float) -> str:
    h = wrap24(hour)
    hh, mm = int(h), int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = (hh + 1) % 24, 0
    return f"{hh:02d}:{mm:02d}"


def plan_shift(shift_hours: float, sleep_hour: float, wake_hour: float) -> str:
    """CircadianEngine.planShift: stepped ~1 h/day light + sleep-timing plan.
    Positive shift = ADVANCE (eastward / earlier); negative = DELAY (westward).
    Light and sleep timing only — never a supplement."""
    magnitude = abs(shift_hours)
    if magnitude < 0.5:
        return "No meaningful body-clock shift needed — you're about aligned."
    advancing = shift_hours > 0
    days = math.ceil(magnitude / CIRC_SHIFT_PER_DAY)
    out = [f"Shifting your clock {magnitude:.1f} h {'earlier' if advancing else 'later'}, "
           f"about an hour a day ({days} days). Light and sleep timing only.\n"]
    cumulative = 0.0
    for i in range(1, days + 1):
        cumulative += min(CIRC_SHIFT_PER_DAY, magnitude - cumulative)
        signed = -cumulative if advancing else cumulative
        sleep, wake = wrap24(sleep_hour + signed), wrap24(wake_hour + signed)
        if advancing:
            light = f"bright light {clock(wake)}–{clock(wake + 2)}, dim from {clock(sleep - 2)}"
        else:
            light = f"bright light {clock(sleep - 3)}–{clock(sleep - 1)}, easy on bright mornings"
        out.append(f"  Day {i}: sleep {clock(sleep)} → wake {clock(wake)} | {light}")
    return "\n".join(out)


def rmssd_norm(age: float) -> float:
    """Nocturnal RMSSD ~50th percentile by age, piecewise-linear between anchors."""
    a = VIT_RMSSD_NORM
    if age <= a[0][0]:
        return a[0][1]
    for i in range(1, len(a)):
        if age <= a[i][0]:
            (a0, v0), (a1, v1) = a[i - 1], a[i]
            return v0 + (v1 - v0) * (age - a0) / (a1 - a0)
    return a[-1][1]


def vitality(rhr14, hrv14, sleep_h14, steps14, vo2_last):
    """VitalityEngine: 0-100 Vitality + Body Age from up to 6 wearable factors.
    Returns (vitality, body_age, delta_years, {factor: years}) or None under 3
    factors. Positive years in the breakdown = ages you; negative = protective."""
    def clamp(v, lo, hi):
        return min(hi, max(lo, v))
    contribs = {}
    if rhr14:
        contribs["rhr"] = ((sum(rhr14) / len(rhr14) - 65) / 10) * 0.100
    if vo2_last is not None:
        # Reference: the Nes-expected VO2max of an average peer isn't modeled here;
        # NOOP passes it from profile norms. Skipped until a norm table is added,
        # so this factor stays out of the sum (honest omission, not a zero).
        pass
    if sleep_h14:
        dev = max(0.0, abs(sum(sleep_h14) / len(sleep_h14) - 7.5) - 0.5)
        contribs["sleep"] = clamp(dev, 0, 3) * 0.110
    good = [h for h in sleep_h14 if h > 0]
    if len(good) >= 3:
        mean = sum(good) / len(good)
        cv = (sum((h - mean) ** 2 for h in good) / len(good)) ** 0.5 / mean
        contribs["consistency"] = (0.75 - clamp(1 - cv, 0, 1)) * 0.450
    if hrv14:
        norm = rmssd_norm(USER_AGE)
        contribs["hrv"] = clamp((norm - sum(hrv14) / len(hrv14)) / norm, -1, 1) * 0.160
    if steps14:
        deficit = (7000 - clamp(sum(steps14) / len(steps14), 0, 11000)) / 1000
        contribs["steps"] = clamp(deficit, -4, 4) * 0.064
    if len(contribs) < VIT_MIN_FACTORS:
        return None
    delta_age = sum(contribs.values()) * VIT_SHRINK / VIT_LN_PER_YEAR  # +ve = ages you
    body_age = clamp(USER_AGE + delta_age, VIT_MIN_AGE, VIT_MAX_AGE)
    delta = USER_AGE - body_age
    vit = clamp(50 + delta * VIT_PER_YEAR, 0, 100)
    years = {k: round(v * VIT_SHRINK / VIT_LN_PER_YEAR, 2) for k, v in contribs.items()}
    return round(vit, 1), round(body_age, 1), round(delta, 1), years


def build_insight(recs: list[float], forecast_now, debt_h, ill, fa, strains: list[float],
                  circ=None) -> str:
    """Plain-language conclusions inferred from the current numbers — regenerated
    on every run so the dashboard text never goes stale. Rule-based and factual;
    each sentence maps to one visible metric."""
    parts = []
    if recs:
        last, mean7 = recs[-1], sum(recs[-7:]) / len(recs[-7:])
        trend = ("above" if last > mean7 + 5 else "below" if last < mean7 - 5 else "in line with")
        parts.append(f"Recovery is {last:.0f}, {trend} your 7-day average of {mean7:.0f}.")
    if forecast_now is not None:
        parts.append(f"Tomorrow morning projects around {forecast_now:.0f}.")
    if debt_h is not None:
        if debt_h <= -6:
            parts.append(f"You are carrying {-debt_h:.0f} h of sleep debt over the last two weeks — "
                         "the biggest lever on the board right now.")
        elif debt_h <= -2:
            parts.append(f"Sleep debt sits at {-debt_h:.1f} h; a couple of early nights clears it.")
        else:
            parts.append("Sleep is on target.")
    if ill is not None:
        level = {0: "No illness signals — your body signals sit inside their normal range.",
                 1: "Mild multi-signal anomaly — nothing alarming, worth a calmer day.",
                 2: "Heads-up: multiple body signals are elevated vs your baseline — consider taking it easy."}
        parts.append(level[ill])
    if fa is not None and abs(fa[1]) >= 1:
        parts.append(f"Fitness age {fa[0]:.0f} — {abs(fa[1]):.0f} years "
                     f"{'younger' if fa[1] > 0 else 'older'} than your calendar age.")
    if strains:
        active = [s for s in strains[-7:] if s * STRAIN_TO_100 >= 30.0]
        if not active:
            parts.append("No real training load this week — recovery capacity is going unused.")
    if circ is not None and circ[3] >= 1:
        off = circ[2]
        if off > 20:
            parts.append(f"Your body clock runs ~{off:.0f} min later than your schedule — a night-owl lean.")
        elif off < -20:
            parts.append(f"Your body clock runs ~{-off:.0f} min earlier than your schedule — a morning-lark lean.")
        else:
            parts.append("Your body clock is well-aligned with your schedule.")
    return " ".join(parts) or "Not enough data yet — conclusions appear as history accrues."


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
    steps_d = daily("Total Steps", "value")
    vo2 = daily("VO2Max", "value")
    asleep = daily("Sleep Summary", "minutesAsleep", "WHERE isMainSleep='True'")
    inbed = daily("Sleep Summary", "minutesInBed", "WHERE isMainSleep='True'")
    hr_days = hr_by_day(days_back=21)
    debt = sleep_debt(sorted(asleep.items()))
    activity = hourly_activity()
    sleep_windows = sleep_window_hours()

    days = sorted(set(hrv) | set(rhr) | set(resp) | set(skin))
    band_sources = (("resting_hr", rhr), ("hrv", hrv), ("resp_rate", resp), ("skin_temp_dev", skin))
    dev = DEVICE.replace(" ", "\\ ")
    lines: list[str] = []
    summary = []
    # Strain beyond the 21-day intraday window comes from what earlier runs wrote;
    # fresh in-loop values overwrite. Recoveries accumulate in-loop for the forecast.
    strain_by_day = daily("Strain", "value")
    rec_by_day: dict[str, float] = {}

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
        if rec is not None:
            rec_by_day[d] = rec
        if st is not None:
            strain_by_day[d] = st

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
        week = days[max(0, i - 6): i + 1]
        fa = fitness_age([rhr[x] for x in week if x in rhr],
                         [strain_by_day[x] for x in week if x in strain_by_day])
        if fa is not None:
            lines.append(f"FitnessAge,Device={dev} value={fa[0]},delta={fa[1]} {ts}")
        # Forecast made ON day d predicts d+1 morning — written at d+1 so it overlays
        # the actual Recovery it tried to predict.
        fc = forecast([rec_by_day[x] for x in days[: i + 1] if x in rec_by_day],
                      [strain_by_day[x] for x in days[: i + 1] if x in strain_by_day],
                      [asleep[x] / 60.0 for x in days[: i + 1] if x in asleep])
        if fc is not None:
            lines.append(f"RecoveryForecast,Device={dev} value={fc[0]},band={fc[1]} {ts + 86400}")
        fortnight = days[max(0, i - 13): i + 1]
        vit = vitality([rhr[x] for x in fortnight if x in rhr],
                       [hrv[x] for x in fortnight if x in hrv],
                       [asleep[x] / 60.0 for x in fortnight if x in asleep],
                       [steps_d[x] for x in fortnight if x in steps_d],
                       vo2.get(d))
        if vit is not None:
            lines.append(f"Vitality,Device={dev} value={vit[0]},body_age={vit[1]},delta={vit[2]} {ts}")
        act_days = [x for x in fortnight if len(activity.get(x, [])) >= 6]
        wakes = sorted(sleep_windows[x][1] for x in fortnight if x in sleep_windows)
        if act_days and wakes:
            bins = [b for x in act_days for b in activity[x]]
            circ = estimate_phase(bins, len(act_days), wakes[len(wakes) // 2])
            if circ is not None:
                tmin, acro, off, conf, ramp = circ
                lines.append(f"Circadian,Device={dev} acrophase_h={acro:.2f},temp_min_h={tmin:.2f},"
                             f"offset_min={off},rel_amplitude={ramp:.3f},confidence={conf} {ts}")
        if i == len(days) - 1:
            # Driver breakdowns for the current day only — the "why" panels.
            if vit is not None:
                for factor, years in vit[3].items():
                    lines.append(f"VitalityDriver,Device={dev},factor={factor} years={years} {ts}")
            if rec is not None:
                terms = recovery_terms(hrv.get(d), rhr.get(d), resp.get(d), sleep_perf, hb, rb, pb)
                for factor, points in recovery_drivers(terms).items():
                    lines.append(f"RecoveryDriver,Device={dev},factor={factor} points={points} {ts}")
        if rec is not None or st is not None or ill is not None:
            summary.append((d, rec, st, ill[2] if ill else 0, debt.get(d, (None,))[0]))

    # Daily conclusions, stamped on the latest scored day.
    if days:
        rec_series = [rec_by_day[x] for x in days if x in rec_by_day]
        last_i = len(days) - 1
        week = days[max(0, last_i - 6):]
        fa_now = fitness_age([rhr[x] for x in week if x in rhr],
                             [strain_by_day[x] for x in week if x in strain_by_day])
        fc_now = forecast(rec_series,
                          [strain_by_day[x] for x in days if x in strain_by_day],
                          [asleep[x] / 60.0 for x in days if x in asleep])
        debt_last = ([debt[x][0] / 60.0 for x in days if x in debt] or [None])[-1]
        ill_last = summary[-1][3] if summary else None
        last_wakes = sorted(sleep_windows[x][1] for x in days[-14:] if x in sleep_windows)
        circ_now = None
        act_now = [x for x in days[-14:] if len(activity.get(x, [])) >= 6]
        if act_now and last_wakes:
            circ_now = estimate_phase([b for x in act_now for b in activity[x]],
                                      len(act_now), last_wakes[len(last_wakes) // 2])
        text = build_insight(rec_series, fc_now[0] if fc_now else None, debt_last,
                             ill_last, fa_now, [strain_by_day[x] for x in days if x in strain_by_day],
                             circ_now)
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        import datetime
        ts = int(datetime.datetime.fromisoformat(days[-1] + "T12:00:00+00:00").timestamp())
        lines.append(f'Insight,Device={dev} text="{esc}" {ts}')
        print("insight:", text)

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
    import sys
    if "--plan-shift" in sys.argv:
        # Jet-lag / shift-work planner: `compute_metrics.py --plan-shift +6` for a
        # 6 h ADVANCE (eastward), `--plan-shift -3` for a 3 h delay (westward).
        # Uses your habitual sleep window from the last two weeks of sleep records.
        shift = float(sys.argv[sys.argv.index("--plan-shift") + 1])
        windows = sorted(sleep_window_hours(14).values())
        if not windows:
            raise SystemExit("No sleep records yet — can't derive your current sleep window.")
        sleeps = sorted(w[0] for w in windows)
        wakes = sorted(w[1] for w in windows)
        print(plan_shift(shift, sleeps[len(sleeps) // 2], wakes[len(wakes) // 2]))
    else:
        main()
