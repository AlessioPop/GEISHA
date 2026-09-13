"""Reusable continuum processing and PMOIRED model/fit guidance."""
import numpy as np
from numpy.polynomial import Polynomial

PRESETS = {
    'disk': ({'star,ud': 1.0, 'star,f': 1.0}, ['star,ud']),
    'gaussian': ({'star,fwhm': 1.0, 'star,f': 1.0}, ['star,fwhm']),
    'binary': ({'primary,ud': 0.0, 'primary,f': 1.0, 'secondary,ud': 0.0,
                'secondary,f': 0.1, 'secondary,x': 1.0, 'secondary,y': 1.0},
               ['secondary,f', 'secondary,x', 'secondary,y']),
}


def poly_fit(x, y, yerr=None, degree=1, line_edges=None, flag=None):
    """Fit a continuum in explicit input wavelength units; exclude a chosen line.

    Returns the continuum on the original grid. No target, line, or unit guessing.
    """
    x, y = np.asarray(x, float), np.asarray(y, float)
    if x.ndim != 1 or x.shape != y.shape or degree < 0:
        raise ValueError('Continuum needs matching one-dimensional arrays and degree >= 0.')
    mask = np.isfinite(x) & np.isfinite(y)
    if flag is not None:
        mask &= ~np.asarray(flag, bool)
    if line_edges is not None:
        mask &= (x < line_edges[0]) | (x > line_edges[1])
    err = None if yerr is None else np.asarray(yerr, float)
    if err is not None:
        if err.shape != x.shape:
            raise ValueError('Errors must match the wavelength grid.')
        mask &= np.isfinite(err) & (err > 0)
    if np.unique(x[mask]).size <= degree:
        raise ValueError(f'Need at least {degree + 1} distinct valid continuum wavelengths.')
    return Polynomial.fit(x[mask], y[mask], degree, w=None if err is None else 1 / err[mask])(x)


def fit_feedback(result):
    """Heuristic advice, not a claim of model validity or posterior convergence."""
    chi = float(result.get('chi2', np.nan))
    dof = result.get('ndof', 0)
    lines = [f'PMOIRED reduced chi² = {chi:.3g}; reported degrees of freedom = {dof}.']
    if 'geisha_samples' in result:
        samples = result['geisha_samples']
        free = len(result.get('fitOnly', []))
        lines.append(f'{samples} usable selected samples; {free} free parameters. Backend statistics may include priors and normalisation conventions.')
        if samples < 10:
            lines.append('Very sparse data: fit-quality and uncertainty estimates have limited diagnostic power.')
    if not np.isfinite(chi) or dof <= 0:
        lines.append('Fit is not assessable: too few constraints or a non-finite objective. Reduce free parameters.')
    elif chi > 3:
        lines.append('This fit looks poor relative to the supplied errors. Inspect residuals, calibration and flags; try another model only when the structure supports it.')
    elif chi < 0.5:
        lines.append('Scatter is smaller than the stated errors. Check error scaling, correlated channels and overfitting.')
    else:
        lines.append('Residual scale is plausible. Inspect residual structure and parameter degeneracies before interpreting the model.')
    for name in result.get('fitOnly', []):
        val = result.get('best', {}).get(name)
        err = result.get('uncer', {}).get(name)
        lines.append(f'  {name} = {val} ± {err}')
        if err is None or not np.isfinite(err) or err <= 0:
            lines.append(f'  {name}: uncertainty is not reliable; check identifiability and starting values.')
    lines.append('Local least-squares errors are not MCMC convergence evidence. Try /bootstrap for resampling uncertainty.')
    return lines
