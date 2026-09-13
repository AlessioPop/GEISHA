"""Headless analysis session: every GEISHA command lives here.

Adding a command means adding one decorated method: the decorator records its
help text in ``COMMANDS`` (used for completion, /help and suggestions) and the
data it needs before running. Nothing in this module imports curses or Rich, so
the same session serves the themed terminal and plain piped input.
"""

import contextlib
import io
import json
import math
import os
import re
import shlex
import tempfile
from pathlib import Path

# Commands the interface answers itself; listed before the analysis commands.
COMMANDS = {
    '/help': 'Search the available commands and their descriptions.',
    '/color': 'Choose a theme or customize individual colors.',
    '/tree': "Explore the repository's folders with the arrow keys.",
    '/exit': 'Close GEISHA and return to your terminal.',
}
HANDLERS = {}
PLOT_OPTIONS = ('x', 'files', 'instruments', 'baselines', 'color', 'errors', 'xlim', 'ylim', 'legend',
                'title', 'xlabel', 'ylabel', 'continuum', 'line', 'save')


class Usage(Exception):
    """Raised when arguments do not match a command's documented form."""


def command(name, summary, needs=None, detail=()):
    """Register a session command. ``needs`` is None, 'data' or 'fit'.

    ``detail`` lines are shown with the summary whenever the command raises
    Usage, which keeps long option grammars out of the one-line help.
    """
    def register(function):
        COMMANDS[name] = summary
        function.needs = needs
        function.detail = tuple(detail)
        HANDLERS[name] = function
        return function
    return register


def command_matches(text):
    """Complete slash commands from the same table used by /help."""
    if not text.startswith('/'):
        return []
    return [name for name in COMMANDS if name.startswith(text.casefold())]


def wavelengths(values, what):
    """Parse a micron interval, rejecting reversed or non-finite edges."""
    low, high = map(float, values)
    if not 0 < low < high < float('inf'):
        raise ValueError(f'Use finite positive {what} MIN < MAX in µm.')
    return low, high


def text_table(headers, rows):
    """Align columns for plain output; the visual views render the same rows."""
    cells = [[str(value) for value in row] for row in rows]
    widths = [max(len(str(headers[i])), *(len(row[i]) for row in cells)) if cells else len(str(headers[i]))
              for i in range(len(headers))]
    lines = ['  '.join(str(h).ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines += ['  '.join(value.ljust(w) for value, w in zip(row, widths)).rstrip() for row in cells]
    return lines


class ScienceSession:
    """One analysis session shared by the curses and plain terminal frontends."""

    def __init__(self, data_root=None):
        self.history = []
        self.oi = None
        self.records = []
        self.files = ()
        self.headers = {}
        self.targets = ()
        self.instruments = ()
        self.settings = {}
        self.figures = []
        self.fitted = False
        self.bootstrapped = False
        self.before_view = None  # Optional caller hook; no terminal dependency.
        default_root = Path.cwd() / 'data'
        if not default_root.is_dir():
            default_root = Path(__file__).resolve().parents[2] / 'data'
        self.data_root = Path(data_root or default_root).resolve()
        self.memory_path = Path(os.environ.get('GEISHA_STATE_DIR', self.data_root.parent / '.geisha')) / 'session.json'
        self.catalog = None
        self.scan_errors = []
        self.loaded_metadata = []
        self.load_options = {}
        self.previous = {}
        try:
            saved = json.loads(self.memory_path.read_text())
            if (isinstance(saved, dict) and saved.get('version') == 1
                    and isinstance(saved.get('files'), list) and saved['files']
                    and all(isinstance(p, str) and Path(p).is_absolute() for p in saved['files'])
                    and isinstance(saved.get('options', {}), dict)
                    and all(k in ('ins', 'target') and isinstance(v, str) for k, v in saved.get('options', {}).items())):
                self.previous = saved
        except (OSError, ValueError, TypeError):
            pass

    # ------------------------------------------------------------------ state

    def remember_files(self):
        """Save only successfully loaded paths and filters, atomically and locally."""
        saved = dict(version=1, files=list(self.files), options=self.load_options)
        temporary = None
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.memory_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(saved, stream, ensure_ascii=True, indent=2)
                stream.write('\n')
            temporary.replace(self.memory_path)
            self.previous = saved
        except OSError as error:
            return f'Warning: files loaded, but session memory could not be saved: {error}'
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return ''

    def suggestions(self):
        """Explain each actionable next step using catalogue and session facts."""
        actions = []
        def add(title, reason, command_text, enabled=True):
            actions.append(dict(title=title, reason=reason, command=command_text, enabled=enabled))
        if not self.files and self.previous:
            paths = self.previous['files']
            missing = [p for p in paths if not Path(p).is_file()]
            add(f'Resume {len(paths)} previous file(s)',
                f'{len(missing)} missing; locate them with /load.' if missing else 'Continue with the files and target/instrument filter from your last session?',
                '/resume', not missing)
        if self.files:
            candidates = [str(i) for i, row in enumerate(self.loaded_metadata, 1) if row['eligible']]
            modeled = [str(i) for i, row in enumerate(self.loaded_metadata, 1) if row['tellurics'] == 'model']
            if candidates:
                add('Fit GRAVITY tellurics on copies',
                    f'{len(candidates)} SC file(s) have no PMOIRED model. External correction is unknown; review first. Writes new copies.',
                    '/tellurics files=' + ','.join(candidates))
            if modeled:
                add('Review the telluric model', f'{len(modeled)} file(s) already have a model. Compare transmission, raw flux and corrected flux.',
                    '/tellurics view files=' + ','.join(modeled))
            invalid = [row for row in self.loaded_metadata if row['tellurics'] == 'invalid']
            if invalid:
                add('Inspect telluric metadata', invalid[0]['reason'], '/headers')
            add('Plot the active observations', 'Choose an observable; GRAVITY SC and FT get separate panels.', '/plot')
            add('Inspect files and observables', f'{len(self.files)} loaded file(s); browse tables, flags and baseline coverage.', '/data')
            if self.fitted:
                add('Review the fit', 'A fitted model is available for comparison with the data.', '/plot fit')
                add('Read the fit report', 'Inspect fitted parameters and uncertainties.', '/report')
            else:
                add('Explore model choices', 'Choose a scientific model after inspecting the observations.', '/models')
        groups = {}
        for row in self.catalog or []:
            if row['loadable']:
                for target in row['targets']:
                    groups.setdefault((row['folder'], target, row['instrument']), []).append(row)
        for (folder, target, instrument), rows in sorted(groups.items()):
            paths = sorted(r['path'] for r in rows)
            if set(paths) == set(self.files) and self.targets == (target,):
                continue
            count = sum(r['tellurics'] == 'model' for r in rows)
            reason = f'{instrument} · {target} · {Path(folder).name} · {len(paths)} file(s)'
            if any(r['tellurics'] != 'n/a' for r in rows):
                reason += f' · {count} with telluric models'
                pending = sum(r['eligible'] for r in rows)
                if pending:
                    reason += f' · load to review/fit tellurics for {pending} file(s)'
            add(f'Load {target}', reason, shlex.join(['/load', *paths, 'target=' + target]))
        add('Browse other files' if actions else 'Choose FITS files',
            'Open the file picker to choose a different dataset.' if actions else f'No loadable observations found in {self.data_root}.', '/load')
        return actions

    def scan_data(self):
        from .io import scan_data
        self.catalog, self.scan_errors = scan_data(self.data_root)
        return self.catalog

    @command('/scan', 'Refresh the FITS catalogue in the data folder.')
    def cmd_scan(self, args):
        if args:
            raise Usage()
        self.scan_data()
        return [f'Found {len(self.catalog)} FITS file(s) in {self.data_root}. /suggest shows next steps.',
                *text_table(['File', 'Instrument', 'Target', 'Tellurics'],
                            [[r['name'], r['instrument'], ', '.join(r['targets']), r['tellurics']] for r in self.catalog]),
                *('Warning: ' + e for e in self.scan_errors)]

    @command('/suggest', '[NUMBER] — browse suggested actions; F2 opens the interactive table.')
    def cmd_suggest(self, args):
        if self.catalog is None:
            self.scan_data()
        actions = self.suggestions()
        if args:
            if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= len(actions):
                raise Usage()
            action = actions[int(args[0]) - 1]
            if not action['enabled']:
                raise ValueError(action['reason'])
            return self.execute(action['command'])
        return ['Suggested actions · /suggest NUMBER runs an action.',
                *text_table(['#', 'Action', 'Why', 'Command'],
                            [[i, a['title'], a['reason'], a['command'] if a['enabled'] else 'Unavailable']
                             for i, a in enumerate(actions, 1)])]

    @command('/resume', 'Reload all files from the previous session, with its target/instrument filter.')
    def cmd_resume(self, args):
        if args:
            raise Usage()
        if not self.previous:
            raise ValueError('No previous files saved yet. Start with /load.')
        missing = [p for p in self.previous['files'] if not Path(p).is_file()]
        if missing:
            raise ValueError('Previous files are missing; use /load to locate them: ' + ', '.join(missing))
        return self.cmd_load([*self.previous['files'], *(f'{k}={v}' for k, v in self.previous.get('options', {}).items())])

    def require_data(self):
        if self.oi is None:
            raise ValueError('Start with /load PATH, then /inspect and /fit disk.')

    def require_fit(self):
        self.require_data()
        if not self.fitted:
            raise ValueError('Run /fit first. Loading or changing selections clears the previous fit.')

    def sample_count(self, settings):
        """Count usable samples under a candidate selection, before applying it."""
        count = 0
        for record in self.records:
            if record['observable'] not in settings['obs']:
                continue
            mask = ~record['mask']
            if 'wl ranges' in settings:
                low, high = settings['wl ranges'][0]
                mask = mask & (record['wavelength'] >= low) & (record['wavelength'] <= high)
            count += int(mask.sum())
        return count

    def set_figures(self, figures):
        """Replace the plot set only once the new figures exist."""
        self.clear_figures()
        self.figures = list(figures)

    def clear_figures(self):
        if self.figures:
            import matplotlib.pyplot as plt
            for figure in self.figures:
                plt.close(figure)
        self.figures = []

    def inspect(self):
        import numpy as np
        lines = []
        groups = sorted({(r['target'], r['instrument'], r['observable']) for r in self.records})
        for target, instrument, obs in groups:
            rows = [r for r in self.records if (r['target'], r['instrument'], r['observable']) == (target, instrument, obs)]
            n = sum(r['value'].size for r in rows)
            good = sum(int((~r['mask']).sum()) for r in rows)
            wave = np.concatenate([r['wavelength'] for r in rows])
            lines.append(f'{target} · {instrument} · {obs}: {good}/{n} usable; {wave.min():.6g}–{wave.max():.6g} µm')
        if any('�' in r['target'] for r in self.records):
            lines.append('Warning: malformed target-name bytes in FITS metadata. Verify the target identity before scientific interpretation.')
        lines.append('Fit selection: ' + json.dumps(self.settings))
        lines.append('Next: /models → /fit disk → /plot fit → /report')
        return lines

    def observable_rows(self):
        """One row per target, instrument and observable, with usable counts."""
        import numpy as np
        rows = []
        for key in sorted({(r['target'], r['instrument'], r['observable']) for r in self.records}):
            group = [r for r in self.records if (r['target'], r['instrument'], r['observable']) == key]
            waves = np.concatenate([r['wavelength'] for r in group])
            lengths = [r['length'] for r in group if np.isfinite(r['length'])]
            rows.append(dict(target=key[0], instrument=key[1], observable=key[2], records=len(group),
                             usable=sum(int((~r['mask']).sum()) for r in group),
                             total=sum(r['value'].size for r in group),
                             channels=max(r['value'].size for r in group),
                             wl=(float(waves.min()), float(waves.max())),
                             baselines=len({r['baseline'] for r in group if r['baseline']}),
                             length=(min(lengths), max(lengths)) if lengths else None,
                             selected=key[2] in self.settings.get('obs', ())))
        return rows

    def file_rows(self):
        """One row per active file, in the order /load and /headers number them."""
        rows = []
        for index, path in enumerate(self.files, start=1):
            group = [r for r in self.records if r['file'] == path]
            rows.append(dict(number=index, path=path, name=Path(path).name,
                             hdus=len(self.headers.get(path, ())), records=len(group),
                             instruments=sorted({r['instrument'] for r in group}),
                             observables=sorted({r['observable'] for r in group}),
                             usable=sum(int((~r['mask']).sum()) for r in group)))
        return rows

    def baseline_rows(self, records=None):
        """One row per baseline or station triplet, longest projection first."""
        import numpy as np
        records = self.records if records is None else records
        rows = []
        for key in sorted({(r['instrument'], r['baseline']) for r in records if r['baseline']}):
            group = [r for r in records if (r['instrument'], r['baseline']) == key]
            lengths = [r['length'] for r in group if np.isfinite(r['length'])]
            stations = len(key[1].split('-'))
            rows.append(dict(instrument=key[0], baseline=key[1], records=len(group),
                             kind={1: 'single station', 2: 'baseline', 3: 'closure triangle'}.get(
                                 stations, f'{stations} stations'),
                             observables=sorted({r['observable'] for r in group}),
                             files=len({r['file'] for r in group}),
                             usable=sum(int((~r['mask']).sum()) for r in group),
                             length=float(np.mean(lengths)) if lengths else float('nan')))
        return sorted(rows, key=lambda row: (-row['length'] if row['length'] == row['length'] else 0, row['baseline']))

    def data_lines(self):
        """The dataset as text; the visual view shows these same rows as tables."""
        if not self.files:
            return ['No active data. Use /load to choose FITS files.']
        selection = ','.join(self.settings.get('obs', ())) or 'none'
        window = self.settings.get('wl ranges')
        scope = f'{window[0][0]:g}–{window[0][1]:g} µm' if window else 'all wavelengths'
        lines = [f'Active data · {len(self.files)} file(s) · {", ".join(self.targets)} · '
                 f'{", ".join(self.instruments)}', f'Fit selection: {selection} · {scope}', '', 'Files']
        lines += text_table(['#', 'File', 'HDUs', 'Records', 'Usable', 'Instruments', 'Observables'],
                            [[row['number'], row['name'], row['hdus'], row['records'], row['usable'],
                              ','.join(row['instruments']), ','.join(row['observables'])]
                             for row in self.file_rows()])
        lines += ['', 'Observables']
        lines += text_table(['Target', 'Instrument', 'Obs', 'Usable/Total', 'Wavelengths (µm)', 'Chan', 'Baselines', 'Fitted'],
                            [[row['target'], row['instrument'], row['observable'],
                              f"{row['usable']}/{row['total']}", f"{row['wl'][0]:.5g}–{row['wl'][1]:.5g}",
                              row['channels'], row['baselines'], 'yes' if row['selected'] else '']
                             for row in self.observable_rows()])
        baselines = self.baseline_rows()
        if baselines:
            lines += ['', 'Baselines']
            lines += text_table(['Baseline', 'Instrument', 'Length (m)', 'Files', 'Usable', 'Observables'],
                                [[row['baseline'], row['instrument'],
                                  f"{row['length']:.2f}" if row['length'] == row['length'] else '—',
                                  row['files'], row['usable'], ','.join(row['observables'])]
                                 for row in baselines])
        return lines + ['', 'Science commands use this dataset. /load replaces it only after a successful load.',
                        'Use /headers to browse the saved headers; viewing a file does not change the dataset.']

    def history_lines(self):
        return [line for command, lines in self.history for line in [f'› {command}', *lines, '']]

    # --------------------------------------------------------------- dispatch

    def execute(self, command):
        """Run one command line; raises ValueError with an actionable message."""
        parts = shlex.split(command)
        if not parts:
            return []
        name, args = parts[0].lower(), parts[1:]
        handler = HANDLERS.get(name)
        if handler is None:
            raise ValueError('Unknown command. Use /help to see available commands.')
        if handler.needs == 'data':
            self.require_data()
        elif handler.needs == 'fit':
            self.require_fit()
        try:
            return handler(self, args)
        except Usage:
            return [f'{name} {COMMANDS[name]}', *handler.detail]

    # --------------------------------------------------------------- commands

    @command('/load', '[PATH ...] [ins=NAME] [target=NAME] — pick files visually or load paths.')
    def cmd_load(self, args):
        paths, options = [], {}
        for arg in args:
            if arg.startswith(('ins=', 'target=')):
                key, value = arg.split('=', 1)
                options[key] = value
            else:
                paths.append(arg)
        if not paths:
            return ['Use /load in an interactive terminal to select files, or /load PATH [PATH ...].']
        from .io import file_metadata, load_observations, read_headers
        oi, records = load_observations(paths, options.get('ins'), options.get('target'))
        available = {r['observable'] for r in records if (~r['mask']).any()}
        obs = [v for v in ('V2', '|V|', 'T3PHI') if v in available] or sorted(available)
        if not obs:
            raise ValueError('All samples are flagged or have invalid uncertainties.')
        settings = {'obs': obs}
        oi.setupFit(settings)
        files = tuple(sorted({r['file'] for r in records}))
        # Read headers before replacing state: a failure here keeps the old data.
        headers = read_headers(files)
        metadata = [file_metadata(path) for path in files]
        self.oi, self.records, self.settings = oi, records, settings
        self.files, self.headers = files, headers
        self.targets = tuple(sorted({r['target'] for r in records}))
        self.instruments = tuple(sorted({r['instrument'] for r in records}))
        self.loaded_metadata, self.load_options = metadata, options
        self.fitted = self.bootstrapped = False
        self.clear_figures()
        warning = self.remember_files()
        return [f'Loaded {len(files)} file(s) into memory. /headers browses their metadata.',
                *files, *self.inspect(), *([warning] if warning else [])]

    @command('/tellurics', '[view] [files=1,2] — fit GRAVITY SC on new copies, or review saved models.', needs='data',
             detail=('File numbers match /data. Without files=, use eligible active files.',
                     'Uses PMOIRED default He I and Br γ exclusions; inspect the diagnostic before using the correction.',
                     'New FITS copies go in data/tellurics/. Successful fits are loaded together; originals are preserved.',))
    def cmd_tellurics(self, args):
        from .io import file_metadata, fit_tellurics_copy
        view = bool(args and args[0] == 'view')
        options = {}
        for arg in args[1:] if view else args:
            key, sep, value = arg.partition('=')
            if not sep or key != 'files' or key in options:
                raise Usage()
            options[key] = value
        # Reinspect disk before fitting; the catalogue is only a snapshot.
        metadata = [file_metadata(p) for p in self.files]
        if 'files' in options:
            numbers = options['files'].split(',')
            if any(not n.isdigit() or not 1 <= int(n) <= len(self.files) for n in numbers):
                raise ValueError('Use file numbers from /data, e.g. files=1,2.')
            selected = sorted({int(n) - 1 for n in numbers})
        else:
            selected = [i for i, row in enumerate(metadata) if row['tellurics'] == 'model'] if view else [
                i for i, row in enumerate(metadata) if row['eligible']]
        if not selected:
            raise ValueError('No saved telluric models to view.' if view else 'No eligible GRAVITY SC files without a model. /suggest explains the available actions.')
        for i in selected:
            row = metadata[i]
            if not (row['tellurics'] == 'model' if view else row['eligible']):
                raise ValueError(f"{row['name']}: {row['reason']}")
        if view:
            import matplotlib.pyplot as plt
            from pmoired import tellcorr
            figures = []
            try:
                for i in selected:
                    figure = plt.figure()
                    figures.append(figure)
                    tellcorr.showTellurics(self.files[i], fig=figure.number)
                    figures[-1] = plt.figure(figure.number)  # PMOIRED recreates this figure.
            except BaseException:
                for figure in figures:
                    plt.close(figure)
                raise
            self.set_figures(figures)
            return [f'Showing {len(figures)} telluric diagnostic(s).', *self.show_figures()]
        import shutil
        import uuid
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        output = self.data_root / 'tellurics' / ('run-' + uuid.uuid4().hex[:12])
        with tempfile.TemporaryDirectory(prefix='tellurics-', dir=self.memory_path.parent) as staging:
            names = []
            for i in selected:
                name = f'{i + 1:03d}-' + Path(self.files[i]).name
                if name.lower().endswith('.gz'):
                    name = name[:-3]
                names.append(name)
                print(f'Fitting tellurics {len(names)}/{len(selected)}: {Path(self.files[i]).name}', flush=True)
                fit_tellurics_copy(self.files[i], Path(staging) / name)
            # Publish only after every model validates. No original is modified.
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(staging, output)
        replacement = dict(zip((self.files[i] for i in selected), (str(output / name) for name in names)))
        paths = [replacement.get(p, p) for p in self.files]
        lines = self.cmd_load([*paths, *(f'{k}={v}' for k, v in self.load_options.items())])
        self.scan_data()
        return [f'Telluric models saved in {output}. /tellurics view reviews the fits.', *lines]

    @command('/data', 'List the active files held in memory; Enter opens their headers in the visual browser.')
    def cmd_data(self, args):
        return self.data_lines()

    @command('/headers', '[FILE_NUMBER [HDU_NUMBER [SEARCH]]] — browse loaded FITS headers (HDU numbers start at 0).', needs='data')
    def cmd_headers(self, args):
        if not args:
            return self.data_lines() + ['Use /headers FILE_NUMBER to list HDUs, then /headers FILE_NUMBER HDU_NUMBER [SEARCH].']
        if len(args) > 3:
            raise Usage
        number = int(args[0])
        if not 1 <= number <= len(self.files):
            raise ValueError(f'Choose a file number from 1 to {len(self.files)}; /data lists them.')
        path = self.files[number - 1]
        hdus = self.headers[path]
        lines = [f'Header snapshot · {path}']
        if len(args) == 1:
            return lines + [f'{hdu["index"]}. {hdu["name"]} · {hdu["kind"]} · {len(hdu["cards"])} cards' for hdu in hdus]
        index = int(args[1])
        if not 0 <= index < len(hdus):
            raise ValueError(f'Choose an HDU number from 0 to {len(hdus) - 1}.')
        query = args[2].casefold() if len(args) == 3 else ''
        cards = [f'{i + 1:>4}  {key or "(blank)"} = {value}' + (f' / {comment}' if comment else '')
                 for i, (key, value, comment) in enumerate(hdus[index]['cards'])
                 if query in ' '.join((key, value, comment)).casefold()]
        return [*lines, f'HDU {index} · {hdus[index]["name"]}', *cards] if cards else [*lines, 'No matching header cards.']

    @command('/history', 'Review previous commands, results and errors from this session.')
    def cmd_history(self, args):
        return self.history_lines() or ['No previous messages yet.']

    @command('/inspect', 'Show target, instruments, observables and usable sample counts.', needs='data')
    def cmd_inspect(self, args):
        return self.inspect()

    @command('/models', 'List disk, gaussian and binary starting models (sizes/offsets in mas).')
    def cmd_models(self, args):
        from .models import PRESETS
        return [f'{k}: {v[0]} · free: {", ".join(v[1])}' for k, v in PRESETS.items()] + [
            'Override starting values: /fit disk star,ud=0.7',
            'Custom JSON: {"model": {"star,ud": 1, "star,f": 1}, "fitOnly": ["star,ud"]}',
            'Binary fits are local: repeat with different separations to test aliases.']

    @command('/select', 'OBS[,OBS] [MIN MAX] — select fit observables and wavelength range in µm.', needs='data')
    def cmd_select(self, args):
        if len(args) not in (1, 3):
            raise Usage
        obs = args[0].split(',')
        available = {r['observable'] for r in self.records}
        if not set(obs) <= available:
            raise ValueError('Available observables: ' + ', '.join(sorted(available)))
        settings = {'obs': obs}
        if len(args) == 3:
            settings['wl ranges'] = [wavelengths(args[1:], 'wavelengths')]
        if self.sample_count(settings) == 0:
            raise ValueError('No usable samples in this selection.')
        self.oi.setupFit(settings)
        self.settings = settings
        self.fitted = self.bootstrapped = False
        self.clear_figures()
        return ['Selection updated; previous fit cleared.'] + self.inspect()

    @command('/fit', 'MODEL [parameter=value ...] — fit a preset or a model JSON file with PMOIRED.', needs='data')
    def cmd_fit(self, args):
        if not args:
            raise Usage
        from .models import PRESETS, fit_feedback
        if args[0] in PRESETS:
            model, free = PRESETS[args[0]]
            model, free = model.copy(), list(free)
        else:
            config = json.loads(Path(args[0]).expanduser().read_text())
            model, free = config['model'], config['fitOnly']
        if not isinstance(model, dict) or not isinstance(free, list) or not free or not set(free) <= model.keys():
            raise ValueError('Provide a model dictionary and a nonempty fitOnly parameter list.')
        for setting in args[1:]:
            key, value = setting.split('=', 1)
            if key not in model:
                raise ValueError(f'Unknown model parameter: {key}')
            model[key] = float(value)
        if any(isinstance(v, (int, float)) and not math.isfinite(v) for v in model.values()):
            raise ValueError('Model parameters must be finite.')
        if self.sample_count(self.settings) <= len(free):
            raise ValueError('Too few usable samples for this many free parameters. Select more data or simplify the model.')
        self.fitted = self.bootstrapped = False
        self.oi.doFit(model, fitOnly=free, verbose=0)
        self.fitted = True
        self.oi.bestfit['geisha_samples'] = self.sample_count(self.settings)
        self.clear_figures()
        return fit_feedback(self.oi.bestfit)

    @command('/report', 'Explain the current fit and suggest next science checks.', needs='fit')
    def cmd_report(self, args):
        from .models import fit_feedback
        return fit_feedback(self.oi.bestfit)

    def plot_spec(self, args):
        """Validate /plot arguments into a plot specification and a save path.

        Every option is also what the interactive builder writes out, so any
        figure it produces can be repeated, scripted or recalled with ↑.
        """
        from .plot import COLOR_KEYS, DEFAULT_SPEC, X_AXES, parse_limits
        spec, positional = dict(DEFAULT_SPEC), []
        spec['save'] = None
        for arg in args:
            key, sep, value = arg.partition('=')
            if not sep or key not in PLOT_OPTIONS:
                positional.append(arg)
                continue
            if key == 'x' and value not in X_AXES:
                raise ValueError('Choose x from: ' + ', '.join(X_AXES))
            if key == 'color' and value not in COLOR_KEYS:
                raise ValueError('Choose color from: ' + ', '.join(COLOR_KEYS))
            if key in ('xlim', 'ylim', 'line'):
                parse_limits(value)
            if key == 'errors':
                value = self.switch(key, value)
            if key == 'legend':
                if value not in ('auto', 'on', 'off'):
                    raise ValueError('Use legend=auto, legend=on or legend=off.')
            if key == 'continuum':
                value = int(value)
                if not 0 <= value <= 5:
                    raise ValueError('Use a continuum degree from 0 to 5.')
            if key == 'files':
                value = self.chosen_files(value)
            if key == 'instruments':
                value = self.chosen_instruments(value)
            if key == 'baselines':
                value = self.chosen_baselines(value)
            spec[key] = value
        if len(positional) > 2:
            raise Usage
        save = spec.pop('save')
        if len(positional) == 2:
            save = positional[1]
        spec['plot'] = positional[0] if positional else self.settings['obs'][0]
        if spec['plot'] not in ('fit', 'bootstrap', 'uv'):
            available = {r['observable'] for r in self.records}
            if spec['plot'] not in available:
                raise ValueError('Choose uv, fit, bootstrap or an available observable: '
                                 + ', '.join(sorted(available)))
        if spec['plot'] in ('fit', 'bootstrap') and spec['instruments']:
            raise ValueError('instruments= filters data plots; fit and bootstrap use the fitted dataset.')
        if spec['baselines'] and spec['plot'] not in ('fit', 'bootstrap'):
            from .plot import select_records
            available = {r['baseline'] for r in select_records(self.records, {**spec, 'baselines': ()})}
            invalid = set(spec['baselines']) - available
            if invalid:
                raise ValueError(f'No {spec["plot"]} data for baseline(s) in this file/instrument selection: '
                                 + ', '.join(sorted(invalid)))
        return spec, save

    @staticmethod
    def switch(key, value):
        if value not in ('on', 'off'):
            raise ValueError(f'Use {key}=on or {key}=off.')
        return value == 'on'

    def chosen_files(self, value):
        """File numbers as shown by /data, or 'all'."""
        if value in ('', 'all'):
            return ()
        numbers = [int(part) for part in value.replace(',', ' ').split()]
        if not all(1 <= number <= len(self.files) for number in numbers):
            raise ValueError(f'Choose file numbers from 1 to {len(self.files)}; /data lists them.')
        return tuple(self.files[number - 1] for number in numbers)

    def chosen_instruments(self, value):
        """Exact INSNAME values, or the GRAVITY SC/FT channel aliases."""
        from .plot import gravity_channel
        if value in ('', 'all'):
            return ()
        available = sorted({r['instrument'] for r in self.records})
        chosen = []
        for name in value.split(','):
            matches = [item for item in available if item == name or
                       (name.upper() in ('SC', 'FT') and gravity_channel(item) == name.upper())]
            if not matches:
                raise ValueError(f'Unknown instrument {name!r}. Choose from: ' + ', '.join(available))
            chosen.extend(matches)
        return tuple(dict.fromkeys(chosen))

    def chosen_baselines(self, value):
        """Baseline labels as shown by /data, or 'all'."""
        if value in ('', 'all'):
            return ()
        names = tuple(part for part in value.replace(',', ' ').split())
        available = {r['baseline'] for r in self.records}
        unknown = [name for name in names if name not in available]
        if unknown:
            raise ValueError('Unknown baseline(s): ' + ', '.join(unknown)
                             + '. /data lists the available baselines.')
        return names

    @command('/plot', '[OBS|uv|fit|bootstrap] [option=value ...] — plot data; run it bare for the builder.',
             needs='data', detail=(
                 '  x=wavelength|frequency|baseline|channel|mjd   files=1,3|all   baselines=UT1-UT2,...|all',
                 '  instruments=SC|FT|GRAVITY_SC,...|all   (GRAVITY SC = science; FT = fringe tracker)',
                 '  xlim=MIN,MAX   ylim=MIN,MAX   color=baseline|file|instrument|target|none   errors=on|off',
                 '  legend=auto|on|off   title=TEXT   xlabel=TEXT   ylabel=TEXT   continuum=DEGREE   line=MIN,MAX',
                 '  save=FILE.png|pdf|svg   ·   example: /plot V2 x=frequency baselines=J3-D0 ylim=0,1 save=out/v2.png'))
    def cmd_plot(self, args):
        spec, save = self.plot_spec(args)
        if spec['plot'] in ('fit', 'bootstrap'):
            self.require_fit()
            if spec['plot'] == 'bootstrap' and not self.bootstrapped:
                raise ValueError('Run /bootstrap first.')
            import matplotlib.pyplot as plt
            self.clear_figures()
            previous = set(plt.get_fignums())
            if spec['plot'] == 'fit':
                self.oi.show(model='best', obs=self.settings['obs'], showChi2=True)
            else:
                from .plot import plot_bootstrap
                plot_bootstrap(self.oi.boot)
            self.figures = [plt.figure(n) for n in plt.get_fignums() if n not in previous]
        else:
            from .plot import plot_data
            self.set_figures([plot_data(self.records, spec)])
        lines = []
        if save:
            from .plot import save_figures
            lines = ['Saved ' + str(path) for path in save_figures(self.figures, save)]
        return lines + self.show_figures(saved=bool(save))

    def show_figures(self, saved=False):
        """Display completed plots immediately when a GUI backend is available."""
        if not self.figures:
            raise ValueError('Generate a plot with /plot first.')
        import matplotlib
        import matplotlib.pyplot as plt
        if str(matplotlib.get_backend()).lower() in {'agg', 'pdf', 'svg', 'ps', 'template'}:
            return [] if saved else ['Plot ready; no graphical backend is available. Add save=FILE.png to export it.']
        if self.before_view:
            self.before_view()
        plt.show(block=True)
        return ['Plot viewer closed.']

    @command('/continuum', 'OBS LINE_MIN LINE_MAX [DEGREE] — plot a weighted continuum excluding the line (µm).', needs='data')
    def cmd_continuum(self, args):
        if len(args) not in (3, 4):
            raise Usage
        from .plot import plot_data
        low, high = wavelengths(args[1:3], 'line edges')
        self.set_figures([plot_data(self.records, dict(
            plot=args[0], continuum=int(args[3]) if len(args) == 4 else 1, line=f'{low},{high}'))])
        return self.show_figures()

    @command('/bootstrap', '[COUNT] — resample the last PMOIRED fit (default 100; not MCMC).', needs='fit')
    def cmd_bootstrap(self, args):
        if len(args) > 1:
            raise Usage
        count = int(args[0]) if args else 100
        if not 10 <= count <= 10000:
            raise ValueError('Choose 10–10000 bootstrap fits.')
        self.bootstrapped = False
        self.oi.bootstrapFit(Nfits=count, multi=False, verbose=0)
        self.bootstrapped = True
        return [f'Completed {count} bootstrap resamples. Use /plot bootstrap.',
                'Bootstrap measures sensitivity to resampling; it is not MCMC and does not establish posterior convergence.',
                'Sparse baselines or correlated channels limit how informative this uncertainty estimate is.']

    @command('/save', 'FILE.json — export fit parameters, settings and source provenance.', needs='fit')
    def cmd_save(self, args):
        if len(args) != 1:
            raise Usage
        import numpy as np
        import pmoired

        def encode(value):
            if isinstance(value, np.ndarray):
                return value.tolist()
            if isinstance(value, np.generic):
                return value.item()
            raise TypeError(f'Cannot serialize {type(value).__name__}')

        result = {k: self.oi.bestfit[k] for k in ('best', 'uncer', 'chi2', 'ndof', 'fitOnly', 'geisha_samples')
                  if k in self.oi.bestfit}
        payload = dict(files=list(self.files), targets=list(self.targets), instruments=list(self.instruments),
                       settings=self.settings, result=result,
                       pmoired_version=getattr(pmoired, '__version__', 'unknown'))
        encoded = json.dumps(payload, default=encode, indent=2, allow_nan=False)
        path = Path(args[0]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x') as stream:
            stream.write(encoded + '\n')
        return [f'Saved fit and provenance to {path}.']


def science_output(session, command):
    """Run a command, capturing backend chatter so it cannot corrupt the display."""
    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            lines = session.execute(command)
    except KeyboardInterrupt:
        lines = ['Analysis interrupted. Inspect the session before continuing.']
    except Exception as error:
        lines = [f'Could not complete command: {error}']
    log = capture.getvalue().strip()
    if log:
        # Strip backend ANSI colors before displaying them in curses.
        lines += ['— PMOIRED messages —'] + re.sub(r'\x1b\[[0-9;]*m', '', log).splitlines()
    if command.strip() != '/history':
        session.history.append((command, list(lines)))
    return lines
