# AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
"""Canadian Forest Fire Weather Index System (Van Wagner 1987), vectorized with NumPy.

Inputs per day are arrays of equal shape: temperature (°C), relative humidity (%),
wind speed (km/h) and 24-hour precipitation (mm). FireCal uses daily maximum
temperature, humidity at that temperature and daily mean wind from MERRA-2 as a
stand-in for the noon observations of the original system. The codes run
continuously through the year (no overwintering adjustment).
"""

import numpy as np

# Day-length factors for latitudes north of 30°N, January to December.
DMC_DAY_LENGTH = np.array([6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0])
DC_DAY_LENGTH = np.array([-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6])

FFMC0, DMC0, DC0 = 85.0, 6.0, 15.0


def ffmc_step(previous, temp, rh, wind, rain):
    mo = 147.2 * (101.0 - previous) / (59.5 + previous)
    rf = np.maximum(rain - 0.5, 0.0)
    wet = rain > 0.5
    safe_rf = np.where(wet, rf, 1.0)
    gain = 42.5 * safe_rf * np.exp(-100.0 / (251.0 - mo)) * (1.0 - np.exp(-6.93 / safe_rf))
    gain = np.where(mo > 150.0, gain + 0.0015 * (mo - 150.0) ** 2 * np.sqrt(safe_rf), gain)
    mo = np.where(wet, np.minimum(mo + gain, 250.0), mo)

    ed = 0.942 * rh ** 0.679 + 11.0 * np.exp((rh - 100.0) / 10.0) + 0.18 * (21.1 - temp) * (1.0 - np.exp(-0.115 * rh))
    ew = 0.618 * rh ** 0.753 + 10.0 * np.exp((rh - 100.0) / 10.0) + 0.18 * (21.1 - temp) * (1.0 - np.exp(-0.115 * rh))
    ko = 0.424 * (1.0 - (rh / 100.0) ** 1.7) + 0.0694 * np.sqrt(wind) * (1.0 - (rh / 100.0) ** 8)
    kd = ko * 0.581 * np.exp(0.0365 * temp)
    k1 = 0.424 * (1.0 - ((100.0 - rh) / 100.0) ** 1.7) + 0.0694 * np.sqrt(wind) * (1.0 - ((100.0 - rh) / 100.0) ** 8)
    kw = k1 * 0.581 * np.exp(0.0365 * temp)
    m = np.where(mo > ed, ed + (mo - ed) * 10.0 ** (-kd),
                 np.where(mo < ew, ew - (ew - mo) * 10.0 ** (-kw), mo))
    return np.clip(59.5 * (250.0 - m) / (147.2 + m), 0.0, 101.0)


def dmc_step(previous, temp, rh, rain, month):
    t = np.maximum(temp, -1.1)
    rk = 1.894 * (t + 1.1) * (100.0 - rh) * DMC_DAY_LENGTH[month - 1] * 1e-4
    wet = rain > 1.5
    rw = 0.92 * rain - 1.27
    wmi = 20.0 + 280.0 / np.exp(0.023 * previous)
    safe_prev = np.maximum(previous, 1e-6)
    b = np.where(previous <= 33.0, 100.0 / (0.5 + 0.3 * previous),
                 np.where(previous <= 65.0, 14.0 - 1.3 * np.log(safe_prev), 6.2 * np.log(safe_prev) - 17.2))
    wmr = wmi + 1000.0 * rw / (48.77 + b * rw)
    pr = np.where(wet, 43.43 * (5.6348 - np.log(np.maximum(wmr - 20.0, 1e-6))), previous)
    return np.maximum(pr, 0.0) + rk


def dc_step(previous, temp, rain, month):
    t = np.maximum(temp, -2.8)
    pe = np.maximum((0.36 * (t + 2.8) + DC_DAY_LENGTH[month - 1]) / 2.0, 0.0)
    wet = rain > 2.8
    rw = 0.83 * rain - 1.27
    smi = 800.0 * np.exp(-previous / 400.0)
    dr = np.where(wet, np.maximum(previous - 400.0 * np.log(1.0 + 3.937 * np.maximum(rw, 0.0) / smi), 0.0), previous)
    return dr + pe


def isi(ffmc, wind):
    fm = 147.2 * (101.0 - ffmc) / (59.5 + ffmc)
    sf = 19.115 * np.exp(-0.1386 * fm) * (1.0 + fm ** 5.31 / 4.93e7)
    return sf * np.exp(0.05039 * wind)


def bui(dmc, dc):
    denominator = np.maximum(dmc + 0.4 * dc, 1e-6)
    low = 0.8 * dc * dmc / denominator
    high = dmc - (1.0 - 0.8 * dc / denominator) * (0.92 + (0.0114 * dmc) ** 1.7)
    return np.maximum(np.where(dmc <= 0.4 * dc, low, high), 0.0)


def fwi(isi_value, bui_value):
    bb = np.where(bui_value <= 80.0,
                  0.1 * isi_value * (0.626 * bui_value ** 0.809 + 2.0),
                  0.1 * isi_value * (1000.0 / (25.0 + 108.64 * np.exp(-0.023 * bui_value))))
    safe = np.maximum(bb, 1.0 + 1e-9)
    return np.where(bb <= 1.0, bb, np.exp(2.72 * (0.434 * np.log(safe)) ** 0.647))


def run(temp, rh, wind, rain, months):
    """Daily series of FFMC, DMC, DC, ISI, BUI and FWI.

    temp, rh, wind, rain: arrays (days, ...); months: (days,) month numbers.
    Missing inputs keep the previous day's moisture codes.
    """
    shape = temp.shape[1:]
    out = {k: np.empty(temp.shape, dtype=np.float32) for k in ("ffmc", "dmc", "dc", "isi", "bui", "fwi")}
    f, d, c = (np.full(shape, v, dtype=np.float64) for v in (FFMC0, DMC0, DC0))
    for day in range(temp.shape[0]):
        t, h, w, r = (np.asarray(a[day], dtype=np.float64) for a in (temp, rh, wind, rain))
        ok = np.isfinite(t) & np.isfinite(h) & np.isfinite(w) & np.isfinite(r)
        t, h, w, r = (np.where(ok, a, fill) for a, fill in ((t, 0.0), (h, 50.0), (w, 0.0), (r, 0.0)))
        h = np.clip(h, 0.0, 100.0)
        f = np.where(ok, ffmc_step(f, t, h, w, r), f)
        d = np.where(ok, dmc_step(d, t, h, r, months[day]), d)
        c = np.where(ok, dc_step(c, t, r, months[day]), c)
        i = isi(f, w)
        b = bui(d, c)
        out["ffmc"][day], out["dmc"][day], out["dc"][day] = f, d, c
        out["isi"][day], out["bui"][day], out["fwi"][day] = i, b, fwi(i, b)
    return out


def relative_humidity(temp, dewpoint):
    """Relative humidity (%) from temperature and dew point (Magnus formula)."""
    def saturation(x):
        return 6.112 * np.exp(17.67 * x / (x + 243.5))
    return np.clip(100.0 * saturation(dewpoint) / saturation(temp), 1.0, 100.0)


if __name__ == "__main__":
    # Reference check against the worked example in Van Wagner & Pickett (1985):
    # 17 °C, 42 % RH, 25 km/h, no rain, April, starting from 85 / 6 / 15
    # gives FFMC ≈ 87.7, DMC ≈ 8.5, DC ≈ 19.0, ISI ≈ 10.9, BUI ≈ 8.5, FWI ≈ 10.1.
    one = lambda v: np.array([[v]])
    r = run(one(17.0), one(42.0), one(25.0), one(0.0), np.array([4]))
    print({k: round(float(v[0, 0]), 1) for k, v in r.items()})
