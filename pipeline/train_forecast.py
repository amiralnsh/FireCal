"""Train and evaluate the FireCal hotspot outlook, then export the 2023–2025 backtest.

Run after pipeline/forecast_features.py:
    python pipeline/train_forecast.py

Models, from simplest to richest (each one adds a source of information):
  climatology   logistic regression on the harmonized and VIIRS seasonal climatology
  persistence   climatology + recent detections in and around the cell
  lgbm_history  LightGBM on fire history, climatology, land cover and season
  lgbm_past     + fire weather observed up to the issue date (FWI codes, rain, soil moisture)
  lgbm_full     + weather during the forecast window (reanalysis used as a perfect forecast)
  cnn_full      PyTorch fully convolutional network on the same grids as lgbm_full

Splits: train 2013–2021, validation 2022 (early stopping and model selection),
test 2023–2025. 20% of 4°×4° blocks are excluded from training and scored separately.
"""

import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
import torch
from torch import nn

from forecast_common import NCELL, NCOL, NROW, OUT, SEED, TEST_YEARS, TRAIN_YEARS, VALID_YEAR
from forecast_net import Normalizer, OutlookNet

NEG_KEEP = 0.08          # share of negative cell-days kept for the tabular models
TARGETS = ("y1", "y7")
CLIM = ["clim_harmonized", "clim_harmonized_3x3", "clim_viirs", "clim_viirs_3x3", "clim_annual"]
PERSIST = CLIM + ["fire_today", "fire_7d", "fire_30d", "fire_7d_3x3", "fire_30d_5x5"]
LABELS = {
    "climatology": "Seasonal climatology (harmonized MODIS+VIIRS)",
    "persistence": "Climatology + recent detections",
    "lgbm_history": "LightGBM: fire history only",
    "lgbm_past": "LightGBM: + observed fire weather",
    "lgbm_full": "LightGBM: + forecast-window weather",
    "cnn_full": "PyTorch CNN: + forecast-window weather",
}


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def load():
    grid = dict(np.load(OUT / "grid.npz"))
    targets = dict(np.load(OUT / "targets.npz"))
    features = np.load(OUT / "features.npy", mmap_mode="r")
    dates = pd.DatetimeIndex(targets.pop("dates").astype(str))
    return grid, targets, features, dates


def day_rows(features, days):
    """(len(days) * NCELL, F) float32 table for whole-grid prediction."""
    block = np.asarray(features[np.sort(days)], dtype=np.float32)
    return block.transpose(0, 2, 1).reshape(-1, block.shape[1])


def sample_rows(features, targets, days, cells, rng):
    xs, y1, y7, w = [], [], [], []
    for n in days:
        t7 = targets["y7"][n, cells]
        keep = (t7 == 1) | (rng.random(len(cells)) < NEG_KEEP)
        xs.append(np.asarray(features[n][:, cells[keep]], dtype=np.float32).T)
        y1.append(targets["y1"][n, cells[keep]])
        y7.append(t7[keep])
        w.append(np.where(t7[keep] == 1, 1.0, 1.0 / NEG_KEEP))
    return (np.vstack(xs), {"y1": np.concatenate(y1), "y7": np.concatenate(y7)},
            np.concatenate(w).astype(np.float32))


def fit_logistic(x, y, w):
    model = make_pipeline(FunctionTransformer(lambda v: np.log1p(np.maximum(v, 0))), StandardScaler(),
                          LogisticRegression(C=1.0, max_iter=400))
    model.fit(np.nan_to_num(x), y, logisticregression__sample_weight=w)
    return model


def fit_lgbm(x, y, w, xv, yv, wv, names):
    params = dict(objective="binary", learning_rate=0.05, num_leaves=63, min_child_samples=200,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
                  num_threads=6, seed=SEED, verbosity=-1)
    train = lgb.Dataset(x, y, weight=w, feature_name=names, free_raw_data=True)
    valid = lgb.Dataset(xv, yv, weight=wv, reference=train)
    return lgb.train(params, train, num_boost_round=1500, valid_sets=[valid],
                     callbacks=[lgb.early_stopping(75, verbose=False)])


def train_cnn(features, targets, train_days, valid_days, train_cells, names, rng):
    torch.manual_seed(SEED)
    torch.set_num_threads(6)
    sample = np.sort(rng.choice(train_days, min(60, len(train_days)), replace=False))
    norm = Normalizer(names).fit(np.asarray(features[sample], dtype=np.float32))
    mask = torch.zeros(NCELL)
    mask[train_cells] = 1
    mask = mask.reshape(1, 1, NROW, NCOL)
    base = tuple(float(targets[t][train_days][:, train_cells].mean()) for t in TARGETS)
    model = OutlookNet(len(names), 32, base)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    loss_fn = nn.BCEWithLogitsLoss(reduction="none")

    def batch(days):
        days = np.sort(days)
        x = norm.transform(np.asarray(features[days], dtype=np.float32))
        y = np.stack([targets[t][days] for t in TARGETS], axis=1).astype(np.float32)
        return (torch.from_numpy(x.reshape(len(days), -1, NROW, NCOL)),
                torch.from_numpy(y.reshape(len(days), 2, NROW, NCOL)))

    def masked_loss(logits, y):
        return (loss_fn(logits, y) * mask).sum() / (mask.sum() * len(y) * 2)

    best, best_loss, stale = None, float("inf"), 0
    for epoch in range(15):
        model.train()
        order = rng.permutation(train_days)[:1000]
        for start in range(0, len(order), 8):
            x, y = batch(order[start:start + 8])
            optimizer.zero_grad()
            masked_loss(model(x), y).backward()
            optimizer.step()
        model.eval()
        total = 0.0
        with torch.inference_mode():
            for start in range(0, len(valid_days), 16):
                x, y = batch(valid_days[start:start + 16])
                total += masked_loss(model(x), y).item() * len(y)
        total /= len(valid_days)
        log(f"  CNN epoch {epoch + 1}: validation loss {total:.5f}")
        if total < best_loss - 1e-6:
            best_loss, stale = total, 0
            best = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            stale += 1
            if stale == 3:
                break
    model.load_state_dict(best)
    model.eval()
    return model, norm


def predict_cnn(model, norm, features, days):
    out = np.empty((len(days), 2, NCELL), dtype=np.float32)
    with torch.inference_mode():
        for start in range(0, len(days), 16):
            chunk = days[start:start + 16]
            x = norm.transform(np.asarray(features[chunk], dtype=np.float32))
            logits = model(torch.from_numpy(x.reshape(len(chunk), -1, NROW, NCOL)))
            out[start:start + len(chunk)] = torch.sigmoid(logits).reshape(len(chunk), 2, -1).numpy()
    return out


def predict_tabular(model, features, days, columns, chunk=20):
    out = np.empty((len(days), NCELL), dtype=np.float32)
    for start in range(0, len(days), chunk):
        part = days[start:start + chunk]
        x = day_rows(features, part)[:, columns]
        p = model.predict(x) if isinstance(model, lgb.Booster) else model.predict_proba(np.nan_to_num(x))[:, 1]
        out[start:start + len(part)] = p.reshape(len(part), NCELL)
    return out


def top_capture(y, p, share=0.10):
    """Share of positives that fall in the top `share` of cells, ranked each day."""
    k = max(1, int(np.ceil(share * p.shape[1])))
    top = np.argpartition(-p, k - 1, axis=1)[:, :k]
    hits = np.take_along_axis(y, top, axis=1).sum()
    total = y.sum()
    return float(hits / total) if total else None


def score(y, p, reference):
    """y, p, reference: (days, cells) arrays restricted to the evaluated cells and days."""
    yf, pf, rf = y.ravel().astype(np.float32), p.ravel().astype(np.float32), reference.ravel().astype(np.float32)
    brier = float(np.mean((pf - yf) ** 2))
    reference_brier = float(np.mean((rf - yf) ** 2))
    return {
        "average_precision": round(float(average_precision_score(yf, pf)), 4),
        "roc_auc": round(float(roc_auc_score(yf, pf)), 4),
        "brier": round(brier, 6),
        "brier_skill_vs_climatology": round(1 - brier / reference_brier, 4),
        "top10_capture": round(top_capture(y, p), 4),
    }


def reliability(y, p):
    edges = np.array([0, .005, .01, .02, .05, .1, .2, .3, .5, 1.0001])
    yf, pf = y.ravel(), p.ravel().astype(np.float32)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        take = (pf >= lo) & (pf < hi)
        if take.sum() >= 50:
            rows.append({"from": float(lo), "to": float(min(hi, 1)), "n": int(take.sum()),
                         "predicted": round(float(pf[take].mean()), 4),
                         "observed": round(float(yf[take].mean()), 4)})
    return rows


def main():
    rng = np.random.default_rng(SEED)
    grid, targets, features, dates = load()
    names = [str(n) for n in grid["features"]]
    col = {n: i for i, n in enumerate(names)}
    groups = {
        "climatology": CLIM, "persistence": PERSIST,
        "lgbm_history": [str(n) for n in grid["history"]],
        "lgbm_past": [str(n) for n in grid["history"]] + [str(n) for n in grid["past_weather"]],
        "lgbm_full": names, "cnn_full": names,
    }
    holdout = grid["holdout"].astype(bool)
    known_cells, held_cells = np.flatnonzero(~holdout), np.flatnonzero(holdout)
    years = dates.year.to_numpy()
    valid7 = targets["valid7"]
    train_days = np.flatnonzero(np.isin(years, list(TRAIN_YEARS)) & valid7)
    valid_days = np.flatnonzero((years == VALID_YEAR) & valid7)
    test_days = np.flatnonzero(np.isin(years, list(TEST_YEARS)))
    log(f"Days: train {len(train_days)}, validation {len(valid_days)}, test {len(test_days)}; "
        f"cells: {len(known_cells)} known, {len(held_cells)} held out")

    log("Sampling training rows…")
    x, y, w = sample_rows(features, targets, train_days[::2], known_cells, rng)
    xv, yv, wv = sample_rows(features, targets, valid_days[::3], known_cells, rng)
    log(f"  train rows {len(x):,} (positives: next day {int(y['y1'].sum()):,}, 7 days {int(y['y7'].sum()):,}); "
        f"validation rows {len(xv):,}")

    models = {}
    for name in ("climatology", "persistence"):
        cols = [col[n] for n in groups[name]]
        for target in TARGETS:
            models[name, target] = fit_logistic(x[:, cols], y[target], w)
        log(f"Fitted {name}")
    for name in ("lgbm_history", "lgbm_past", "lgbm_full"):
        cols = [col[n] for n in groups[name]]
        for target in TARGETS:
            booster = fit_lgbm(x[:, cols], y[target], w, xv[:, cols], yv[target], wv, groups[name])
            booster.save_model(str(OUT / f"{name}_{target}.txt"))
            models[name, target] = booster
            log(f"Fitted {name} {target}: {booster.best_iteration} trees")
    del x, xv

    log("Training the PyTorch CNN…")
    net, norm = train_cnn(features, targets, train_days, valid_days[::2], known_cells, names, rng)
    torch.save({"state_dict": net.state_dict(), "features": names, "mean": norm.mean, "std": norm.std},
               OUT / "cnn_full.pt")

    # Model selection on the 2022 validation year (whole grid, known cells).
    selection_days = valid_days[::2]
    validation = {}
    cnn_valid = predict_cnn(net, norm, features, selection_days)
    for (name, target), model in models.items():
        p = predict_tabular(model, features, selection_days, [col[n] for n in groups[name]])
        y_true = targets[target][selection_days][:, known_cells]
        validation.setdefault(name, {})[target] = round(float(
            average_precision_score(y_true.ravel(), p[:, known_cells].ravel())), 4)
    for i, target in enumerate(TARGETS):
        y_true = targets[target][selection_days][:, known_cells]
        validation.setdefault("cnn_full", {})[target] = round(float(
            average_precision_score(y_true.ravel(), cnn_valid[:, i, known_cells].ravel())), 4)
    selected = max(("lgbm_full", "cnn_full"), key=lambda m: validation[m]["y7"])
    log(f"Validation AP: {validation}; selected {selected}")

    log("Predicting 2023–2025…")
    predictions = {}
    for (name, target), model in models.items():
        predictions[name, target] = predict_tabular(model, features, test_days,
                                                    [col[n] for n in groups[name]]).astype(np.float16)
    cnn_test = predict_cnn(net, norm, features, test_days)
    for i, target in enumerate(TARGETS):
        predictions["cnn_full", target] = cnn_test[:, i].astype(np.float16)
    del cnn_test

    forest = np.flatnonzero(grid["share_forest"] >= 0.25)
    regions = {"known": known_cells, "holdout": held_cells, "forest": forest}
    test = {}
    for target in TARGETS:
        ok = targets["valid" + target[1:]][test_days]
        y_true = targets[target][test_days][ok]
        reference = predictions["climatology", target][ok].astype(np.float32)
        test[target] = {}
        for region, cells in regions.items():
            yt = y_true[:, cells]
            entry = {"days": int(ok.sum()), "cells": int(len(cells)), "positives": int(yt.sum())}
            for name in LABELS:
                entry[name] = score(yt, predictions[name, target][ok][:, cells].astype(np.float32),
                                    reference[:, cells])
            test[target][region] = entry
            log(f"{target} {region}: " + ", ".join(
                f"{m} AP {entry[m]['average_precision']:.3f}" for m in LABELS))

    ok7 = targets["valid7"][test_days]
    best7 = predictions[selected, "y7"][ok7][:, known_cells].astype(np.float32)
    importance = models["lgbm_full", "y7"].feature_importance("gain")
    importance = sorted(zip(names, importance / importance.sum()), key=lambda r: -r[1])[:15]
    metrics = {
        "grid": {"south": float(grid["bounds"][0]), "west": float(grid["bounds"][1]),
                 "step": float(grid["bounds"][2]), "rows": NROW, "cols": NCOL},
        "split": {"train": "2013–2021", "validation": str(VALID_YEAR), "test": "2023–2025",
                  "holdout": "20% of 4°×4° blocks, never used for training"},
        "models": LABELS, "selected": selected, "validation_ap": validation, "test": test,
        "reliability_y7": reliability(targets["y7"][test_days][ok7][:, known_cells], best7),
        "importance_y7": [{"feature": n, "share": round(float(v), 4)} for n, v in importance],
        "notes": [
            "Forecast-window weather comes from MERRA-2 reanalysis, so lgbm_full and cnn_full show skill "
            "with a perfect weather forecast. lgbm_past uses only information available on the issue date.",
            "Targets are Suomi NPP VIIRS detections. Days with an instrument gap are excluded from scoring.",
        ],
    }
    (OUT / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")

    def quantize(p):
        return np.round(np.clip(p.astype(np.float32), 0, 1) * 65535).astype(np.uint16)

    np.savez_compressed(
        OUT / "predictions.npz",
        dates=np.asarray(dates[test_days].strftime("%Y-%m-%d"), dtype="U10"),
        p1=quantize(predictions[selected, "y1"]), p7=quantize(predictions[selected, "y7"]),
        c1=quantize(predictions["climatology", "y1"]), c7=quantize(predictions["climatology", "y7"]),
        y1=targets["y1"][test_days], y7=targets["y7"][test_days],
        valid1=targets["valid1"][test_days], valid7=targets["valid7"][test_days],
        holdout=holdout, share_forest=grid["share_forest"],
    )
    log(f"Saved {OUT / 'metrics.json'} and {OUT / 'predictions.npz'}")


if __name__ == "__main__":
    main()
