"""OIFITS discovery and metadata-driven extraction, independent of instrument."""
from pathlib import Path
import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import numpy as np
from astropy.io import fits
from astropy import units as u

from .constants import is_fits, object_key

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


def observing_night(mjd, date_obs=None):
    """Group observations by their UTC calendar date (midnight to midnight)."""
    from datetime import datetime, timedelta, timezone
    try:
        if np.isfinite(float(mjd)) and float(mjd) > 0:
            instant = datetime(1858, 11, 17) + timedelta(days=float(mjd))
        elif date_obs:
            instant = datetime.fromisoformat(str(date_obs).replace('Z', '+00:00'))
        else:
            return 'Unknown'
        if instant.tzinfo is not None:
            instant = instant.astimezone(timezone.utc)
        return instant.date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return 'Unknown'


def discover(path):
    """Accept one file, or search a directory tree for OIFITS files."""
    path = Path(path).expanduser().resolve()
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(p for p in path.rglob('*') if p.is_file() and is_fits(p)
                       and '.backup' not in p.relative_to(path).parts)
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
               eligible=False, reason='', loadable=False, observations=[])
    try:
        with fits.open(path, mode='readonly', memmap=False) as hdus:
            row['targets'] = tuple(sorted({target_label(r['TARGET']) for h in hdus
                                           if h.name == 'OI_TARGET' for r in h.data}))
            row['instruments'] = tuple(sorted({str(h.header['INSNAME']) for h in hdus
                                               if h.name == 'OI_WAVELENGTH' and 'INSNAME' in h.header}))
            row['instrument'] = str(hdus[0].header.get('INSTRUME', '')) or ', '.join(row['instruments']) or 'Unknown'
            targets = {int(r['TARGET_ID']): target_label(r['TARGET'])
                       for h in hdus if h.name == 'OI_TARGET' for r in h.data}
            observations = set()
            for h in hdus:
                if h.name not in {v[0] for v in OBSERVABLES.values()} or h.data is None:
                    continue
                for record in h.data:
                    name = targets.get(int(record['TARGET_ID']))
                    if name:
                        night = observing_night(record['MJD'] if 'MJD' in h.columns.names else np.nan,
                                                h.header.get('DATE-OBS', hdus[0].header.get('DATE-OBS')))
                        observations.add((name, night, str(h.header.get('INSNAME', ''))))
            row['observations'] = [dict(target=t, night=n, instrument=i) for t, n, i in sorted(observations)]
            row['loadable'] = bool(row['targets'] and row['instruments'] and
                                   any(h.name in {v[0] for v in OBSERVABLES.values()} for h in hdus))
            gravity = any(i.startswith('GRAVITY_SC') for i in row['instruments'])
            if not gravity:
                row['reason'] = 'No supported OIFITS tables.' if not row['loadable'] else 'OIFITS observations available.'
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


@contextlib.contextmanager
def data_lock(root):
    """Serialize discovery and sorting so scans never see a half-finished move."""
    import fcntl
    with (Path(root) / '.organize.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup_file(path, root):
    """Keep an independent, verified byte copy of each original file version."""
    path, root = Path(path), Path(root).resolve()
    directory = root / '.backup'
    if directory.is_symlink():
        raise ValueError('The backup folder must not be a symbolic link.')
    directory.mkdir(parents=True, exist_ok=True)
    before = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.partial', delete=False) as stream:
            temporary = Path(stream.name)
            with path.open('rb') as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        digest = file_digest(temporary)
        after = path.stat()
        if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino) or file_digest(path) != digest):
            raise ValueError(f'{path.name} is still changing; backup will be retried on the next scan.')
        destination = directory / digest / path.name
        if destination.parent.is_symlink():
            raise ValueError('A backup destination must not be a symbolic link.')
        destination.parent.mkdir(exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or file_digest(destination) != digest:
                raise ValueError(f'Existing backup failed verification: {destination}')
        else:
            os.link(temporary, destination)
        return dict(backup=str(destination), sha256=digest)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sorting_plan(catalog, root, object_names=()):
    """Plan instrument/object/chronological epoch paths without changing files."""
    root = Path(root).resolve()
    names = {object_key(name): name for name in object_names}
    for row in catalog:
        for observation in row.get('observations', []):
            names.setdefault(object_key(observation['target']), observation['target'])
    def folder(label):
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', label).strip(' .')[:100] or 'Unknown'
        return clean if clean == label else clean + '_' + hashlib.sha256(label.encode()).hexdigest()[:8]
    prepared, dates = [], {}
    for row in catalog:
        source = Path(row['path'])
        if not source.is_relative_to(root) or '.backup' in source.relative_to(root).parts:
            continue
        targets = {names[object_key(o['target'])] for o in row.get('observations', [])}
        nights = {o['night'] for o in row.get('observations', [])}
        item = dict(source=str(source), destination=None, sha256=row.get('sha256'), reason='')
        if not row.get('backup'):
            item['reason'] = 'Backup not verified; retry /scan.'
        elif not row['loadable'] or len(targets) != 1 or len(nights) != 1:
            item['reason'] = 'Needs review: unreadable, multiple objects, or multiple dates in one file.'
        elif 'Unknown' in nights or any('�' in t for t in targets):
            item['reason'] = 'Needs review: object name or UTC date is unknown.'
        else:
            instrument = row['instrument']
            if all(i.startswith('GRAVITY_') for i in row['instruments']) and row['instruments']:
                instrument = 'GRAVITY'
            elif instrument.startswith('CHARA_NIRO_'):
                instrument = 'CHARA_NIRO'
            key = folder(instrument or 'Unknown instrument'), folder(next(iter(targets)))
            night = next(iter(nights))
            item.update(group=key, night=night)
            dates.setdefault(key, set()).add(night)
        prepared.append(item)
    reserved = set()
    for item in sorted(prepared, key=lambda r: r['source']):
        if item['reason']:
            continue
        source = Path(item['source'])
        number = sorted(dates[item['group']]).index(item['night']) + 1
        parent = root.joinpath(*item['group'], f"Epoch {number:02d} — {item['night']}")
        destination = parent / source.name
        if str(destination) in reserved or (destination.exists() and destination != source):
            suffix = hashlib.sha256(str(source).encode()).hexdigest()[:10]
            destination = parent / (source.stem + '_' + suffix + source.suffix)
        if str(destination) in reserved or (destination.exists() and destination != source):
            item['reason'] = 'Destination already exists; nothing will be overwritten.'
            continue
        if not destination.resolve().is_relative_to(root) or any(p.is_symlink() for p in [destination, *destination.parents] if p.is_relative_to(root)):
            item['reason'] = 'Destination contains a symbolic link; needs review.'
            continue
        reserved.add(str(destination))
        item['destination'] = str(destination)
    return [r for r in prepared if r['reason'] or r['source'] != r['destination']]


@contextlib.contextmanager
def organize_files(plan, root):
    """Create destinations exclusively; commit memory before removing originals.

    Until the caller commits, all original paths remain available. An interruption
    can leave extra copies, but never leaves the only copy in a staging directory.
    """
    root = Path(root).resolve()
    result = dict(paths={}, warnings=[])
    created = []
    with data_lock(root):
        try:
            for item in plan:
                if item['reason'] or not item['destination']:
                    continue
                source, destination = Path(item['source']), Path(item['destination'])
                if (source.is_symlink() or not source.resolve().is_relative_to(root)
                        or not destination.resolve().is_relative_to(root)
                        or any(p.is_symlink() for p in destination.parents if p.is_relative_to(root))):
                    raise ValueError('The sorting paths changed; review /sort again.')
                if destination.exists() or destination.is_symlink():
                    raise ValueError(f'Destination exists: {destination}. Review /sort again.')
                backup = backup_file(source, root)
                if backup['sha256'] != item['sha256']:
                    raise ValueError(f'{source.name} changed since the preview. Review /sort again.')
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, destination)
                except OSError as error:
                    import errno
                    if error.errno != errno.EXDEV:
                        raise
                    with destination.open('xb') as stream:
                        created.append(destination)
                        with source.open('rb') as data:
                            shutil.copyfileobj(data, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                if destination not in created:
                    created.append(destination)
                if file_digest(destination) != item['sha256']:
                    raise ValueError(f'{source.name} changed while being sorted; originals retained.')
                result['paths'][str(source)] = str(destination)
            yield result
        except BaseException:
            for destination in reversed(created):
                destination.unlink(missing_ok=True)
            raise
        expected = {r['source']: r['sha256'] for r in plan}
        for source, destination in result['paths'].items():
            try:
                if file_digest(source) != expected[source] or file_digest(destination) != expected[source]:
                    raise ValueError('source changed during sorting')
                Path(source).unlink()
                parent = Path(source).parent
                if re.fullmatch(r'Epoch(?:_\d+_| \d+ — )\d{4}-\d{2}-\d{2}', parent.name):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            except (OSError, ValueError) as error:
                result['warnings'].append(f'Warning: sorted copy ready, but original retained at {source}: {error}')


def scan_data(root, cache=None):
    """Back up and catalogue new or changed FITS, excluding private folders."""
    root = Path(root).resolve()
    if not root.is_dir():
        return [], [f'No data folder at {root}. /load can open files elsewhere.']
    with data_lock(root):
        return _scan_data(root, cache)


def _scan_data(root, cache=None):
    """Recursively catalogue FITS, skipping hidden folders and symbolic links."""
    rows, errors = [], []
    root = Path(root).resolve()
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
                try:
                    stat = path.stat()
                    stamp = (stat.st_mtime_ns, stat.st_size)
                    previous = cache.get(str(path)) if cache is not None else None
                    if previous and previous[0] == stamp and previous[1].get('backup') and Path(previous[1]['backup']).is_file():
                        row = previous[1]
                    else:
                        try:
                            backup = backup_file(path, root)
                        except (OSError, ValueError) as error:
                            row = file_metadata(path)
                            errors.append(f'Backup failed for {path.name}: {error}')
                        else:
                            # Read the verified snapshot so its metadata and digest
                            # describe the same bytes, even during a new file copy.
                            row = file_metadata(backup['backup'])
                            row.update(path=str(path), name=path.name, folder=str(path.parent), **backup)
                    if cache is not None:
                        cache[str(path)] = stamp, row
                    rows.append(row)
                except OSError as error:
                    errors.append(str(error))
    if cache is not None:
        present = {r['path'] for r in rows}
        for path in set(cache) - present:
            del cache[path]
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
                        if target is not None and object_key(targ) != object_key(target):
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
                        mjd = float(row['MJD']) if 'MJD' in h.columns.names else np.nan
                        night = observing_night(mjd, h.header.get('DATE-OBS', hdus[0].header.get('DATE-OBS')))
                        records.append(dict(file=str(path), instrument=name, target=targ, observable=obs, night=night,
                                            wavelength=wave.copy(), value=value, error=err, mask=mask,
                                            baseline='-'.join(stations.get(int(s), str(s)) for s in sta),
                                            u=ucoord, v=vcoord, length=length,
                                            mjd=mjd))
    if not records:
        raise ValueError('No supported OIFITS observables match this selection. Image FITS cannot be fitted as interferometry.')
    return records


def load_observations(path, insname=None, target=None, night=None):
    import pmoired
    paths = [path] if isinstance(path, (str, Path)) else list(path)
    files = sorted({file for item in paths for file in discover(item)})
    if not files:
        raise ValueError('Select at least one FITS file.')
    records = inspect_files(files, insname, target)
    if night is not None and night != 'all':
        records = [r for r in records if r['night'] == night]
        if not records:
            raise ValueError(f'No observations for night {night} match this selection.')
    targets = sorted({r['target'] for r in records})
    if len({object_key(t) for t in targets}) != 1:
        raise ValueError('Select one target with target=NAME. Available: ' + ', '.join(targets))
    display_target = target if target is not None else targets[0]
    for record in records:
        record['target'] = display_target
    # Only load files with matching records; directories may contain unrelated FITS.
    selected = sorted({r['file'] for r in records})
    # PMOIRED matches exact target keys, which may differ in spelling or be bytes.
    # Load each spelling with the key actually present in those files.
    groups = {}
    for filename in selected:
        with fits.open(filename, memmap=False) as hdus:
            names = {r['TARGET'].strip() for h in hdus if h.name == 'OI_TARGET'
                     for r in h.data if object_key(target_label(r['TARGET'])) == object_key(display_target)}
        for backend_target in names:
            groups.setdefault(backend_target, []).append(filename)
    oi = pmoired.OI()
    for backend_target, filenames in groups.items():
        oi.addData(filenames, insname=insname, targname=backend_target, verbose=False, useTelluricsWl=True)
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
                if night is not None and night != 'all':
                    # Keep closure-triangle indices intact, but exclude other nights
                    # from PMOIRED fits as well as GEISHA's inspection and plots.
                    nights = [observing_night(m, data.get('header', {}).get('DATE-OBS'))
                              for m in block['MJD']]
                    block['FLAG'] |= np.asarray([n != night for n in nights])[:, None]
                for value, err in cols:
                    if value in block and err in block:
                        block['FLAG'] |= ~np.isfinite(block[value]) | ~np.isfinite(block[err]) | (block[err] <= 0)
    return oi, records
