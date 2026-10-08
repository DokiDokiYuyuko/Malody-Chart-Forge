"""Monotone empirical chart-span NPS calibration; no generation side effects."""
import math

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import brentq


VERSION = 'chart-span-nps-sr-v1'


def star_band(sr):
    return min(10, max(1, int(math.floor(float(sr) + .5))))


def weighted_isotonic(values, weights):
    """Pool adjacent violating blocks, minimizing weighted squared error."""
    blocks = []
    for i, (value, weight) in enumerate(zip(values, weights)):
        blocks.append([i, i + 1, float(weight), float(value) * weight])
        while len(blocks) > 1 and blocks[-2][3] / blocks[-2][2] > blocks[-1][3] / blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    result = np.empty(len(values), dtype=float)
    for start, end, weight, total in blocks:
        result[start:end] = total / weight
    return result


def fit(rows):
    bins = []
    for i in range(36):
        lo, hi = 1 + i * .25, 1 + (i + 1) * .25
        selected = [r for r in rows if lo <= r['sr'] < hi or (i == 35 and r['sr'] == 10)]
        if not selected:
            continue
        nps = np.array([r['nps'] for r in selected])
        bins.append(dict(lower_sr=lo, upper_sr=hi, count=len(selected),
                         sr=float(np.median([r['sr'] for r in selected])),
                         nps_median=float(np.median(nps)),
                         nps_p10=float(np.quantile(nps, .1)),
                         nps_p90=float(np.quantile(nps, .9)),
                         nps_p25=float(np.quantile(nps, .25)),
                         nps_p75=float(np.quantile(nps, .75))))
    if len(bins) < 2:
        raise ValueError('At least two nonempty SR bins are required')
    fitted = weighted_isotonic([b['nps_median'] for b in bins], [b['count'] for b in bins])
    for b, y in zip(bins, fitted):
        b['fitted_nps'] = float(y)
    counts = {str(i): sum(star_band(r['sr']) == i for r in rows) for i in range(1, 11)}
    return dict(version=VERSION, bins=bins, knots_sr=[b['sr'] for b in bins],
                knots_nps=fitted.tolist(), integer_band_counts=counts,
                sparse_integer_bands=[i for i in range(1, 11) if counts[str(i)] < 500],
                observed_sr_range=[min(r['sr'] for r in rows), max(r['sr'] for r in rows)],
                supported_sr_range=[bins[0]['sr'], bins[-1]['sr']],
                parameters=dict(star_bin_width=.25, grid_step=.01, min_integer_band_count=500,
                                isotonic_weight='bin_sample_count', statistic='median',
                                interpolation='PCHIP', boundary_policy='constant_endpoint_flagged',
                                plateau_inverse='midpoint', nps_clock='first_head_to_last_head_or_tail',
                                v32_num_diff_classes=24, v32_max_difficulty=12))


def interpolator(model):
    return PchipInterpolator(model['knots_sr'], model['knots_nps'], extrapolate=False)


def fit_low_star_extrapolation(rows):
    """Fit only SR<7; continue the weighted 1–7 trend through 10 stars."""
    low = [r for r in rows if 1 <= r['sr'] < 7]
    model = fit(low)
    x, y = np.array(model['knots_sr']), np.array(model['knots_nps'])
    weights = np.array([b['count'] for b in model['bins']], dtype=float)
    center_x, center_y = np.average(x, weights=weights), np.average(y, weights=weights)
    slope = float(np.sum(weights * (x-center_x) * (y-center_y)) / np.sum(weights * (x-center_x)**2))
    if not math.isfinite(slope) or slope <= 0:
        raise ValueError('Low-star data does not support a positive extrapolation slope')
    extra = np.array([7., 8., 9., 10.])
    extra_y = y[-1] + slope * (extra-x[-1])
    model.update(version='chart-span-nps-sr-low7-extrapolation-v2',
                 empirical_supported_sr_range=model['supported_sr_range'],
                 supported_sr_range=[float(x[0]), 10.],
                 knots_sr=x.tolist()+extra.tolist(), knots_nps=y.tolist()+extra_y.tolist(),
                 empirical_sample_count=len(low), excluded_high_sr_count=len(rows)-len(low),
                 extrapolation=dict(threshold_sr=7., fit_filter='1 <= sr < 7',
                                    slope_nps_per_star=slope, trend_sr_range=[1., 7.],
                                    method='count-weighted linear trend of all low-star isotonic bin medians; anchored to last empirical knot',
                                    anchor_sr=float(x[-1]), anchor_nps=float(y[-1]),
                                    trend_intercept=center_y-slope*center_x,
                                    uncertainty='No empirical quantile or confidence band beyond the fitting range'))
    model['parameters'].update(high_star_policy='exclude_from_fit_and_extrapolate',
                               extrapolation_threshold_sr=7.)
    return model


def forward(model, sr):
    sr = float(sr)
    if not math.isfinite(sr):
        raise ValueError('SR must be finite')
    x = model['knots_sr']
    return float(interpolator(model)(np.clip(sr, x[0], x[-1])))


def inverse(model, nps):
    nps = float(nps)
    if not math.isfinite(nps) or nps < 0:
        raise ValueError('NPS must be finite and nonnegative')
    x, y = np.array(model['knots_sr']), np.array(model['knots_nps'])
    clipped = nps < y[0] or nps > y[-1]
    if nps < y[0]:
        sr = float(x[0])
    elif nps > y[-1]:
        sr = float(x[-1])
    else:
        equal = np.flatnonzero(np.isclose(y, nps, rtol=0, atol=1e-10))
        if len(equal):
            sr = float((x[equal[0]] + x[equal[-1]]) / 2)
        else:
            i = int(np.searchsorted(y, nps, side='right'))
            curve = interpolator(model)
            sr = float(brentq(lambda value: float(curve(value)) - nps, x[i - 1], x[i]))
    width = model['parameters']['v32_max_difficulty'] / model['parameters']['v32_num_diff_classes']
    diff_class = min(23, max(0, int(sr * 24 / 12)))
    return dict(input_nps=nps, sr=sr, clamped=bool(clipped),
                extrapolated=bool(model.get('extrapolation') and sr > model['extrapolation']['anchor_sr']),
                supported_nps_range=[float(y[0]), float(y[-1])],
                sparse_band=star_band(sr) in model['sparse_integer_bands'],
                v32_difficulty_class=diff_class, v32_effective_sr=diff_class * width,
                effective_sr_fitted_nps=forward(model, diff_class * width))
