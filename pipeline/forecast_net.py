# AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
"""Fully convolutional PyTorch network for the gridded hotspot outlook.

Input: one issue day as a (features, 64, 188) grid. Output: two logit maps,
next-day and next-7-day probability of at least one VIIRS hotspot per cell.
A per-pixel branch sees the local features; a dilated-convolution branch adds
context from roughly 15 cells (≈4°) around each cell.
"""

import numpy as np
import torch
from torch import nn

# Heavy-tailed, non-negative inputs are log-transformed before standardization.
LOG_FEATURES = {
    "fire_7d", "fire_30d", "fire_7d_3x3", "fire_30d_5x5", "fire_7d_9x9", "days_since_fire",
    "last_year_week_3x3", "clim_harmonized", "clim_harmonized_3x3", "clim_annual",
    "fwi", "dmc", "dc", "rain_7d", "rain_30d", "next7_rain", "next7_fwi", "next1_fwi", "next1_rain",
}


class OutlookNet(nn.Module):
    def __init__(self, n_features, width=32, base_rates=(0.01, 0.03)):
        super().__init__()
        self.pixel = nn.Sequential(
            nn.Conv2d(n_features, width, 1), nn.GELU(),
            nn.Conv2d(width, width, 1), nn.GELU(),
        )
        self.context = nn.Sequential(
            nn.Conv2d(width, width, 3, padding=1), nn.GELU(),
            nn.Conv2d(width, width, 3, padding=2, dilation=2), nn.GELU(),
            nn.Conv2d(width, width, 3, padding=4, dilation=4), nn.GELU(),
        )
        self.head = nn.Conv2d(2 * width, 2, 1)
        with torch.no_grad():
            rates = torch.tensor(base_rates, dtype=torch.float32)
            self.head.bias.copy_(torch.log(rates / (1 - rates)))

    def forward(self, x):
        local = self.pixel(x)
        return self.head(torch.cat((local, self.context(local)), dim=1))


class Normalizer:
    def __init__(self, names, mean=None, std=None):
        self.names = list(names)
        self.log = np.array([n in LOG_FEATURES for n in self.names])
        self.mean, self.std = mean, std

    def transform(self, x):
        """x: (days, features, cells) float array -> standardized float32 without NaN."""
        x = x.astype(np.float32, copy=True)
        log = self.log[None, :, None]
        x = np.where(log, np.log1p(np.maximum(x, 0)), x)
        if self.mean is not None:
            x = (x - self.mean[None, :, None]) / self.std[None, :, None]
        return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

    def fit(self, sample):
        """sample: (days, features, cells)."""
        x = self.transform(sample)
        self.mean = np.nanmean(x, axis=(0, 2)).astype(np.float32)
        self.std = np.maximum(np.nanstd(x, axis=(0, 2)), 1e-3).astype(np.float32)
        return self
