"""Matplotlib figures built from a plain plot specification.

A specification is a dictionary (see DEFAULT_SPEC) that the /plot command, the
interactive plot builder and any script can all produce, so every figure can be
reproduced from one command line.
"""
from pathlib import Path
import numpy as np

# Axis key -> (default label, whether a projected baseline is required).
X_AXES = {
    'wavelength': ('Wavelength (µm)', False),
    'frequency': ('Spatial frequency (Mλ)', True),
    'baseline': ('Baseline length (m)', True),
    'channel': ('Channel', False),
    'mjd': ('MJD (days)', False),
}
COLOR_KEYS = ('baseline', 'file', 'instrument', 'target', 'none')
PHASE_OBSERVABLES = ('DPHI', 'T3PHI')
DEFAULT_SPEC = dict(plot='V2', x='wavelength', files=(), instruments=(), baselines=(), color='baseline',
                    errors=True, xlim=None, ylim=None, legend='auto', title=None,
                    xlabel=None, ylabel=None, continuum=None, line=None,
                    obs=(), spectro='auto', showuv=True, flagged=False, logv=False, logb=False)
# Options each plot target understands. 'all' and 'fit' hand the data to PMOIRED's
# own display, which reads a different set of settings from the figures drawn here.
DATA_OPTIONS = ('x', 'files', 'instruments', 'baselines', 'color', 'errors', 'xlim', 'ylim',
                'legend', 'title', 'xlabel', 'ylabel', 'continuum', 'line')
SHOW_OPTIONS = ('files', 'instruments', 'obs', 'spectro', 'showuv', 'flagged', 'logv', 'logb')
LEGEND_LIMIT = 16  # Beyond this many series a legend hides the data.
# Shared with the reference plotting routines in main_temp.py.
PALETTE = ('#00B8D9', '#E040FB', '#FFAB00', '#36E26B', '#2979FF', '#FF1744')


def target_options(target):
    """Which /plot options apply to a target; 'save' always does."""
    if target in ('all', 'fit'):
        return SHOW_OPTIONS
    if target == 'bootstrap':
        return ()
    return DATA_OPTIONS


def gravity_channel(instrument):
    """Recognize GRAVITY channels, including polarized INSNAME suffixes."""
    parts = instrument.upper().split('_')
    return parts[1] if len(parts) > 1 and parts[0] == 'GRAVITY' and parts[1] in ('SC', 'FT') else None


def instrument_order(instrument):
    """Sort key listing the GRAVITY science channel before the fringe tracker.

    SC carries the spectroscopy the science rests on; FT is the reference
    channel, so it reads as a companion to the SC tables rather than ahead of
    them. Anything else keeps plain alphabetical order.
    """
    return gravity_channel(instrument) != 'SC', instrument


def instrument_label(instrument):
    channel = gravity_channel(instrument)
    description = {'SC': 'science', 'FT': 'fringe tracker'}.get(channel)
    return f'{instrument} · {description}' if description else instrument


def select_records(records, spec):
    """Records matching the observable, files, instruments and baselines."""
    if spec['plot'] == 'uv':
        chosen = [r for r in records if np.isfinite(r['u']) and np.isfinite(r['v'])]
    else:
        chosen = [r for r in records if r['observable'] == spec['plot']]
    if spec['files']:
        chosen = [r for r in chosen if r['file'] in spec['files']]
    if spec.get('instruments'):
        chosen = [r for r in chosen if r['instrument'] in spec['instruments']]
    if spec['baselines']:
        chosen = [r for r in chosen if r['baseline'] in spec['baselines']]
    return chosen


def axis_values(record, axis):
    """X values for one record, on the same grid as its measurements."""
    wave = record['wavelength']
    if axis == 'frequency':
        # Metres over microns is already megawavelengths: B / (λ·1e-6) / 1e6.
        return record['length'] / wave
    if axis == 'baseline':
        return np.full(wave.shape, record['length'])
    if axis == 'channel':
        return np.arange(wave.size, dtype=float)
    if axis == 'mjd':
        return np.full(wave.shape, record['mjd'])
    return wave


def short_name(path, limit=30):
    """A legend-sized file label: printable characters, middle-clipped."""
    stem = ''.join(char for char in Path(path).stem if char.isprintable())
    return stem if len(stem) <= limit else f'{stem[:limit - 9]}…{stem[-8:]}'


def series_label(record, key):
    """Group records into legend entries by the chosen colour key."""
    if key == 'file':
        return short_name(record['file'])
    if key == 'instrument':
        return record['instrument']
    if key == 'target':
        return record['target']
    if key == 'none':
        return ''
    return record['baseline']


def group_colors(labels):
    """One stable colour per legend entry; widen the palette rather than repeat it."""
    import matplotlib.pyplot as plt
    order = list(dict.fromkeys(labels))
    palette = list(PALETTE)
    if len(order) > len(palette):
        extra = len(order) - len(palette)
        colormap = plt.get_cmap('viridis')
        palette += [colormap(index / max(1, extra - 1)) for index in range(extra)]
    return {label: palette[index % len(palette)] for index, label in enumerate(order)}


def parse_limits(value):
    """'MIN,MAX' or None; Matplotlib keeps an axis automatic when given None."""
    if value in (None, '', 'auto'):
        return None
    low, high = (float(part) for part in str(value).replace(',', ' ').split())
    if not low < high:
        raise ValueError('Limits need MIN < MAX.')
    return low, high


def plot_data(records, spec):
    """Draw one figure for a specification; raises ValueError with guidance."""
    import matplotlib.pyplot as plt
    spec = {**DEFAULT_SPEC, **spec}
    if spec['x'] not in X_AXES:
        raise ValueError('Choose x from: ' + ', '.join(X_AXES))
    chosen = select_records(records, spec)
    if not chosen:
        raise ValueError(f'No {spec["plot"]} data in this selection. '
                         'Use /data to see the observables, files and baselines available.')
    if spec['plot'] != 'uv' and X_AXES[spec['x']][1] and not any(np.isfinite(r['length']) for r in chosen):
        raise ValueError(f'{spec["plot"]} carries no projected baseline, so it cannot be '
                         f'plotted against {spec["x"]}. Use x=wavelength or x=channel.')
    # Use the full dataset for stable colours when selecting fewer baselines or
    # switching channels; the same baseline has the same colour in FT and SC.
    candidates = select_records(records, {**DEFAULT_SPEC, 'plot': spec['plot']})
    labels = sorted({series_label(record, spec['color']) for record in candidates})
    colors = group_colors(labels)
    instruments = sorted({r['instrument'] for r in chosen}, key=instrument_order)
    columns = min(2, len(instruments))
    rows = (len(instruments) + columns - 1) // columns
    fig, axes = plt.subplots(rows, columns, figsize=(10 if columns == 1 else 14, 5 * rows),
                             squeeze=False, constrained_layout=True)
    for ax, instrument in zip(axes.flat, instruments):
        group = [r for r in chosen if r['instrument'] == instrument]
        labels = [series_label(record, spec['color']) for record in group]
        seen = set()
        if spec['plot'] == 'uv':
            _draw_uv(ax, group, labels, colors, seen)
        else:
            _draw_series(ax, group, labels, colors, seen, spec)
        _finish(ax, group, spec, seen)
    for ax in list(axes.flat)[len(instruments):]:
        fig.delaxes(ax)
    return fig


def _draw_uv(ax, chosen, labels, colors, seen):
    """Both halves of the uv plane, in megawavelengths, per spectral channel."""
    for record, label in zip(chosen, labels):
        good = ~record['mask']
        wave = record['wavelength'][good]
        u, v = record['u'] / wave, record['v'] / wave
        ax.plot(u, v, '.', color=colors[label], alpha=.7, markersize=3,
                label=label if label and label not in seen else None)
        ax.plot(-u, -v, '.', color=colors[label], alpha=.7, markersize=3)
        seen.add(label)
    ax.set_aspect('equal', adjustable='datalim')
    ax.axhline(0, color='0.8', lw=.8, zorder=0)
    ax.axvline(0, color='0.8', lw=.8, zorder=0)


def _draw_series(ax, chosen, labels, colors, seen, spec):
    """Measurements with their uncertainties, plus an optional continuum fit."""
    curves = [None] * len(chosen)
    if spec['continuum'] is not None:
        from .models import poly_fit
        line = parse_limits(spec['line'])
        curves = [poly_fit(r['wavelength'], r['value'], r['error'], degree=spec['continuum'],
                           line_edges=line, flag=r['mask']) for r in chosen]
        if line:
            ax.axvspan(*line, color='0.85', alpha=.5, zorder=0, label='excluded line')
            seen.add('excluded line')
    for record, label, curve in zip(chosen, labels, curves):
        good = ~record['mask']
        x = axis_values(record, spec['x'])
        color = colors[label]
        shown = label if label and label not in seen else None
        if spec['errors']:
            ax.errorbar(x[good], record['value'][good], yerr=record['error'][good],
                        fmt='.', color=color, alpha=.7, label=shown)
        else:
            ax.plot(x[good], record['value'][good], '.', color=color, alpha=.7, label=shown)
        if curve is not None:
            ax.plot(x, curve, color=color, alpha=.9, lw=1)
        seen.add(label)


def _finish(ax, chosen, spec, seen):
    """Labels, limits and legend: explicit settings always win."""
    observable = spec['plot']
    if observable == 'uv':
        default_x, default_y = 'u (Mλ)', 'v (Mλ)'
    else:
        default_x = X_AXES[spec['x']][0]
        default_y = observable + (' (degrees)' if observable in PHASE_OBSERVABLES else '')
    targets = ', '.join(sorted({r['target'] for r in chosen}))
    default_title = f'{targets} · ' + ('uv coverage' if observable == 'uv' else observable)
    ax.set_xlabel(spec['xlabel'] or default_x)
    ax.set_ylabel(spec['ylabel'] or default_y)
    ax.set_title((spec['title'] or default_title) + '\n' + instrument_label(chosen[0]['instrument']))
    for axis, key in ((ax.set_xlim, 'xlim'), (ax.set_ylim, 'ylim')):
        limits = parse_limits(spec[key])
        if limits:
            axis(*limits)
    ax.grid(alpha=.15)
    ax.spines[['top', 'right']].set_visible(False)
    entries = [label for label in seen if label]
    if spec['legend'] == 'on' or (spec['legend'] in (True, 'auto') and 0 < len(entries) <= LEGEND_LIMIT):
        ax.legend(fontsize='small', frameon=False, ncol=2 if len(entries) > 8 else 1)


def plot_bootstrap(boot):
    """Display backend resamples, including single-parameter fits."""
    import matplotlib.pyplot as plt
    names = boot['fitOnly']
    fig, axes = plt.subplots(len(names), 1, figsize=(8, 3 * len(names)), squeeze=False,
                             constrained_layout=True)
    for ax, name in zip(axes[:, 0], names):
        values = np.asarray(boot['all best'][name], float)
        values = values[np.isfinite(values)]
        if not values.size:
            raise ValueError(f'No finite bootstrap samples for {name}.')
        ax.hist(values, bins=min(30, max(5, int(np.sqrt(values.size)))), color=PALETTE[0], alpha=.8)
        ax.set(xlabel=name, ylabel='Resamples', title='Bootstrap distribution (not an MCMC posterior)')
        ax.grid(alpha=.2)
    return fig


def save_figures(figures, destination):
    if not figures:
        raise ValueError('No figures were generated for this selection.')
    path = Path(destination).expanduser()
    if path.suffix.lower() not in {'.png', '.pdf', '.svg'}:
        raise ValueError('Choose a .png, .pdf or .svg plot filename.')
    paths = [path if len(figures) == 1 else path.with_name(f'{path.stem}-{i + 1}{path.suffix}')
             for i in range(len(figures))]
    if any(p.exists() for p in paths):
        raise ValueError('A plot already exists at that path. Choose another filename.')
    path.parent.mkdir(parents=True, exist_ok=True)
    for figure, dest in zip(figures, paths):
        figure.savefig(dest, dpi=160)
    return paths
