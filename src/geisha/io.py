"""OIFITS discovery and metadata-driven extraction, independent of instrument."""
from pathlib import Path
import os
import numpy as np
from astropy.io import fits
from astropy import units as u

from .constants import is_fits

OBSERVABLES = {
    'V2': ('OI_VIS2', 'VIS2DATA', 'VIS2ERR'),
    '|V|': ('OI_VIS', 'VISAMP', 'VISAMPERR'),
    'DPHI': ('OI_VIS', 'VISPHI', 'VISPHIERR'),
    'T3PHI': ('OI_T3', 'T3PHI', 'T3PHIERR'),
    'T3AMP': ('OI_T3', 'T3AMP', 'T3AMPERR'),
    'FLUX': ('OI_FLUX', 'FLUXDATA', 'FLUXERR'),
}


def target_label(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return ''.join(c if c.isprintable() else '�' for c in str(value)).strip()


def discover(path):
    """Accept one file, or search a directory tree for OIFITS files."""
    path = Path(path).expanduser().resolve()
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(p for p in path.rglob('*') if p.is_file() and is_fits(p))
    else:
        files = []
    if not files:
        raise ValueError(f'No FITS files found at {path}.')
    return files


def telluric_model(hdus):
    """Validate PMOIRED's saved transmission and science wavelength correction."""
    tables = [h for h in hdus if h.name == 'TELLURICS']
    if not tables:
        return None
    if len(tables) != 1:
        raise ValueError('Multiple TELLURICS extensions; inspect the file before using it.')
    table = tables[0]
    if not {'EFF_WAVE', 'TELL_TRANS'} <= set(table.columns.names):
        raise ValueError('TELLURICS is missing EFF_WAVE or TELL_TRANS.')
    wave = np.asarray(table.data['EFF_WAVE'], float)
    trans = np.asarray(table.data['TELL_TRANS'], float)
    corrected = np.asarray(table.data['CORR_WAVE'], float) if 'CORR_WAVE' in table.columns.names else wave
    if (wave.ndim != 1 or not len(wave) or trans.shape != wave.shape or corrected.shape != wave.shape
            or any(not np.all(np.isfinite(a) & (a > 0)) for a in (wave, trans, corrected))
            or not np.all(np.diff(wave) > 0) or not np.all(np.diff(corrected) > 0)):
        raise ValueError('TELLURICS contains invalid transmission or wavelengths.')
    if not np.isfinite(float(table.header.get('PWV', float('nan')))):
        raise ValueError('TELLURICS has no valid PWV metadata required by PMOIRED.')
    science = [h for h in hdus if h.name == 'OI_WAVELENGTH'
               and str(h.header.get('INSNAME', '')).startswith('GRAVITY_SC')]
    if not science or any(h.header.get('NAXIS2') != len(wave)
                          or not np.allclose(h.data['EFF_WAVE'], wave, rtol=1e-5, atol=0) for h in science):
        raise ValueError('TELLURICS does not match the GRAVITY science wavelengths.')
    return dict(wave=wave, transmission=trans, corrected=corrected)


def gravity_flat(header, wavelength):
    """Match PMOIRED's automatic P2VM flat correction when recorded in metadata."""
    keys = [k for k in header if 'PRO REC' in k and 'PARAM' in k and 'NAME' in k
            and 'flat-flux' in str(header[k])]
    if keys and header.get(keys[0].replace('NAME', 'VALUE')) in ('false', 'pmoired'):
        from pmoired.oifits import gravityP2vm
        return np.interp(wavelength, gravityP2vm['WL'], gravityP2vm[header['ESO INS SPEC RES']])
    return np.ones_like(wavelength)


def file_metadata(path):
    """Read a small catalogue entry, without loading observations into PMOIRED."""
    path = Path(path).resolve()
    row = dict(path=str(path), name=path.name, folder=str(path.parent), targets=(),
               instruments=(), instrument='Unknown', resolution='', tellurics='n/a',
               eligible=False, reason='', loadable=False)
    try:
        with fits.open(path, mode='readonly', memmap=False) as hdus:
            row['targets'] = tuple(sorted({target_label(r['TARGET']) for h in hdus
                                           if h.name == 'OI_TARGET' for r in h.data}))
            row['instruments'] = tuple(sorted({str(h.header['INSNAME']) for h in hdus
                                               if h.name == 'OI_WAVELENGTH' and 'INSNAME' in h.header}))
            row['instrument'] = str(hdus[0].header.get('INSTRUME', '')) or ', '.join(row['instruments']) or 'Unknown'
            row['loadable'] = bool(row['targets'] and row['instruments'] and
                                   any(h.name in {v[0] for v in OBSERVABLES.values()} for h in hdus))
            gravity = any(i.startswith('GRAVITY_SC') for i in row['instruments'])
            if not gravity:
                row['reason'] = 'No supported OIFITS tables.' if not row['loadable'] else 'Telluric fitting is available for GRAVITY SC.'
                return row
            row['resolution'] = str(hdus[0].header.get('ESO INS SPEC RES', '')).upper()
            row['tellurics'] = 'missing'
            try:
                model = telluric_model(hdus)
            except (ValueError, KeyError, TypeError) as error:
                row.update(tellurics='invalid', reason=str(error))
                return row
            if model is not None:
                row.update(tellurics='model', reason='PMOIRED model found; applied to SC flux when loaded.')
                return row
            flux = [(i, h) for i, h in enumerate(hdus) if h.name == 'OI_FLUX'
                    and str(h.header.get('INSNAME', '')).startswith('GRAVITY_SC')]
            split = str(hdus[0].header.get('ESO FT POLA MODE', '')).strip() == 'SPLIT'
            supported = ('MED' in row['resolution'] or 'HIGH' in row['resolution'])
            supported &= len(flux) == (2 if split else 1)
            supported &= all(h.header.get('NAXIS2') == 4 and 'FLAG' in h.columns.names
                             and {'FLUX', 'FLUXDATA'}.intersection(h.columns.names)
                             and {'FLUXERR', 'FLUXDATAERR'}.intersection(h.columns.names) for _, h in flux)
            row['eligible'] = bool(supported)
            row['reason'] = ('No PMOIRED model found; external correction is unknown. Review before fitting.'
                             if supported else 'No model found; fitting requires MED/HIGH GRAVITY SC flux with four telescope rows.')
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        row.update(loadable=False, eligible=False, reason=f'Cannot inspect FITS: {error}')
    return row


def scan_data(root):
    """Recursively catalogue FITS, skipping hidden folders and symbolic links."""
    rows, errors = [], []
    root = Path(root)
    if not root.is_dir():
        return rows, [f'No data folder at {root}. /load can open files elsewhere.']
    def failed(error):
        errors.append(str(error))
    for folder, directories, files in os.walk(root, followlinks=False, onerror=failed):
        directories[:] = sorted(name for name in directories
                                if not name.startswith('.') and not (Path(folder) / name).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if not name.startswith('.') and is_fits(path) and not path.is_symlink():
                rows.append(file_metadata(path))
    return rows, errors


def fit_tellurics_copy(source, destination):
    """Fit only a disposable copy, selecting SC flux extensions by INSNAME."""
    from pmoired import tellcorr
    with fits.open(source, memmap=False) as hdus:
        # PMOIRED currently chooses wavelength HDUs positionally. Check its
        # choice before calling it; never accidentally fit the FT spectrum.
        try:
            wave_hdu = hdus[4]
            wave = wave_hdu.data['EFF_WAVE']
            if len(wave) < 10:
                wave_hdu = hdus[3]
                wave = wave_hdu.data['EFF_WAVE']
        except (KeyError, IndexError, TypeError):
            wave_hdu = next(h for h in hdus if h.name == 'OI_WAVELENGTH')
            wave = wave_hdu.data['EFF_WAVE']
        flux = [(i, h) for i, h in enumerate(hdus) if h.name == 'OI_FLUX'
                and str(h.header.get('INSNAME', '')).startswith('GRAVITY_SC')]
        if not str(wave_hdu.header.get('INSNAME', '')).startswith('GRAVITY_SC') or len(wave) < 10:
            raise ValueError('This wavelength HDU layout is unsupported by PMOIRED tellcorr; SC must be selected.')
        for _, h in flux:
            column = 'FLUX' if 'FLUX' in h.columns.names else 'FLUXDATA'
            if h.data[column].shape != (4, len(wave)):
                raise ValueError('SC flux dimensions do not match the science wavelengths.')
        hdus.writeto(destination, overwrite=False)
    ext = tuple(i for i, _ in flux) if len(flux) == 2 else flux[0][0]
    tellcorr.gravity(str(destination), quiet=True, save=True, ext=ext)
    result = file_metadata(destination)
    if result['tellurics'] != 'model':
        raise ValueError('PMOIRED did not produce a valid model: ' + result['reason'])


def read_headers(paths):
    """Snapshot every HDU header without loading its data or retaining file handles.

    Keep cards in order: duplicate COMMENT/HISTORY and blank cards are meaningful.
    Plain strings detach the viewer from both Astropy objects and later disk edits.
    """
    snapshots = {}
    kinds = {'PrimaryHDU': 'Primary', 'ImageHDU': 'Image', 'CompImageHDU': 'Compressed image',
             'BinTableHDU': 'Binary table', 'TableHDU': 'ASCII table', 'GroupsHDU': 'Random groups'}
    for path in paths:
        with fits.open(path, mode='readonly', memmap=False) as hdus:
            snapshots[str(path)] = tuple(
                dict(index=index, name=hdu.name or ('PRIMARY' if index == 0 else 'UNNAMED'),
                     kind=kinds.get(type(hdu).__name__, type(hdu).__name__),
                     cards=tuple((card.keyword,
                                  '' if isinstance(card.value, fits.card.Undefined) else str(card.value),
                                  card.comment) for card in hdu.header.cards))
                for index, hdu in enumerate(hdus)
            )
    return snapshots


def baseline_geometry(row, columns):
    """Projected baseline of one row in metres: (u, v, length).

    A closure triangle has three baselines and no single uv point, so its
    coordinates stay undefined and its length is the longest of the three.
    """
    if 'U1COORD' in columns and 'U2COORD' in columns:
        u1, v1 = float(row['U1COORD']), float(row['V1COORD'])
        u2, v2 = float(row['U2COORD']), float(row['V2COORD'])
        sides = [(u1, v1), (u2, v2), (-(u1 + u2), -(v1 + v2))]
        return np.nan, np.nan, max(float(np.hypot(*side)) for side in sides)
    if 'UCOORD' in columns and 'VCOORD' in columns:
        ucoord, vcoord = float(row['UCOORD']), float(row['VCOORD'])
        return ucoord, vcoord, float(np.hypot(ucoord, vcoord))
    return np.nan, np.nan, np.nan


def inspect_files(paths, insname=None, target=None):
    """Copy spectral records; match INSNAME/ARRNAME, never extension numbers.

    Wavelengths are microns, phases degrees, baselines metres. Invalid values,
    nonpositive errors and OIFITS FLAG are masked together.
    """
    records = []
    for path in paths:
        with fits.open(path, memmap=False) as hdus:
            model = telluric_model(hdus) if any(str(h.header.get('INSNAME', '')).startswith('GRAVITY_SC') for h in hdus) else None
            targets = {int(r['TARGET_ID']): target_label(r['TARGET'])
                       for h in hdus if h.name == 'OI_TARGET' for r in h.data}
            waves = {}
            arrays = {}
            for h in hdus:
                if h.name == 'OI_WAVELENGTH':
                    name = h.header.get('INSNAME', '')
                    wave = np.asarray(h.data['EFF_WAVE'], float)
                    unit = h.columns['EFF_WAVE'].unit or 'm'
                    waves[name] = (wave * u.Unit(unit)).to_value(u.um)
                elif h.name == 'OI_ARRAY':
                    arrays[h.header.get('ARRNAME', '')] = {
                        int(r['STA_INDEX']): str(r['STA_NAME']).strip() for r in h.data}
            for h in hdus:
                name = h.header.get('INSNAME', '')
                if insname is not None and name != insname:
                    continue
                for obs, (ext, column, error) in OBSERVABLES.items():
                    if h.name != ext:
                        continue
                    if obs == 'FLUX' and column not in h.columns.names and 'FLUX' in h.columns.names:
                        column = 'FLUX'
                    if column not in h.columns.names or error not in h.columns.names:
                        continue
                    if name not in waves:
                        raise ValueError(f'{path.name}: {ext} has no matching OI_WAVELENGTH for {name!r}.')
                    corrected = model is not None and name.startswith('GRAVITY_SC')
                    wave = model['corrected'] * 1e6 if corrected else waves[name]
                    divisor = gravity_flat(hdus[0].header, wave) if obs == 'FLUX' and name.startswith('GRAVITY') else np.ones_like(wave)
                    if corrected and obs == 'FLUX':
                        divisor = divisor * model['transmission']
                    if not np.all(np.isfinite(wave) & (wave > 0)):
                        raise ValueError(f'{path.name}: invalid wavelengths for {name}.')
                    stations = arrays.get(h.header.get('ARRNAME', ''), {})
                    for row in h.data:
                        targ = targets.get(int(row['TARGET_ID']), str(row['TARGET_ID']))
                        if target is not None and targ != target:
                            continue
                        value = np.atleast_1d(np.array(row[column], float))
                        err = np.atleast_1d(np.array(row[error], float))
                        flag = np.atleast_1d(np.array(row['FLAG'], bool))
                        if value.shape != wave.shape or err.shape != wave.shape or flag.shape != wave.shape:
                            raise ValueError(f'{path.name}: {ext} channel dimensions do not match wavelengths.')
                        if obs == 'FLUX':
                            value /= divisor
                            err /= divisor
                        mask = flag | ~np.isfinite(value) | ~np.isfinite(err) | (err <= 0)
                        sta = np.atleast_1d(row['STA_INDEX']) if 'STA_INDEX' in h.columns.names else []
                        ucoord, vcoord, length = baseline_geometry(row, h.columns.names)
                        records.append(dict(file=str(path), instrument=name, target=targ, observable=obs,
                                            wavelength=wave.copy(), value=value, error=err, mask=mask,
                                            baseline='-'.join(stations.get(int(s), str(s)) for s in sta),
                                            u=ucoord, v=vcoord, length=length,
                                            mjd=float(row['MJD']) if 'MJD' in h.columns.names else np.nan))
    if not records:
        raise ValueError('No supported OIFITS observables match this selection. Image FITS cannot be fitted as interferometry.')
    return records


def load_observations(path, insname=None, target=None):
    import pmoired
    paths = [path] if isinstance(path, (str, Path)) else list(path)
    files = sorted({file for item in paths for file in discover(item)})
    if not files:
        raise ValueError('Select at least one FITS file.')
    records = inspect_files(files, insname, target)
    targets = sorted({r['target'] for r in records})
    if len(targets) != 1:
        raise ValueError('Select one target with target=NAME. Available: ' + ', '.join(targets))
    # Only load files with matching records; directories may contain unrelated FITS.
    selected = sorted({r['file'] for r in records})
    # Some files contain non-ASCII target bytes. Preserve the backend's exact key
    # while exposing a safe display label to the terminal.
    with fits.open(selected[0], memmap=False) as hdus:
        backend_target = next(r['TARGET'].strip() for h in hdus if h.name == 'OI_TARGET'
                              for r in h.data if target_label(r['TARGET']) == targets[0])
    oi = pmoired.OI(selected, insname=insname, targname=backend_target, verbose=False, useTelluricsWl=True)
    if not oi.data:
        raise ValueError('PMOIRED could not load the selected OIFITS data.')
    # Apply the same validity policy to the backend as the inspection/plot views.
    for data in oi.data:
        # PMOIRED corrects FLUX but leaves EFLUX unchanged. Propagate both
        # transmission and its automatic P2VM flat correction to uncertainties.
        if str(data.get('insname', '')).startswith('GRAVITY'):
            divisor = gravity_flat(data.get('header', {}), data['WL']) * data.get('TELLURICS', 1)
            for block in data.get('OI_FLUX', {}).values():
                block['EFLUX'] /= divisor[None, :]
        for table, cols in [('OI_VIS2', [('V2', 'EV2')]), ('OI_VIS', [('|V|', 'E|V|'), ('PHI', 'EPHI')]),
                            ('OI_T3', [('T3PHI', 'ET3PHI'), ('T3AMP', 'ET3AMP')]), ('OI_FLUX', [('FLUX', 'EFLUX')])]:
            for block in data.get(table, {}).values():
                for value, err in cols:
                    if value in block and err in block:
                        block['FLAG'] |= ~np.isfinite(block[value]) | ~np.isfinite(block[err]) | (block[err] <= 0)
    return oi, records
