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
from .constants import object_key

# Commands the interface answers itself; listed before the analysis commands.
COMMANDS = {
    '/help': 'Search the available commands and their descriptions.',
    '/color': 'Choose a theme or customize individual colors.',
    '/tree': "Explore the repository's folders with the arrow keys.",
    '/exit': 'Close GEISHA and return to your terminal.',
}
HANDLERS = {}
PLOT_OPTIONS = ('x', 'files', 'instruments', 'baselines', 'color', 'errors', 'xlim', 'ylim', 'legend',
                'title', 'xlabel', 'ylabel', 'continuum', 'line', 'save',
                'obs', 'spectro', 'showuv', 'flagged', 'logv', 'logb')


# How GEISHA opens a line that reports trouble; the interface paints these red.
ALERT_PREFIXES = ('Warning', 'Error', 'Could not ', 'Cannot ', 'Analysis interrupted')


def is_alert(line):
    """Whether a line reports a failure or a warning rather than a result."""
    return str(line).lstrip('› ').startswith(ALERT_PREFIXES)


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


def command_matches(text, session=None):
    """Complete slash commands from the same table used by /help."""
    if not text.startswith('/'):
        return []
    choices = list(COMMANDS)
    if session is not None:
        choices += [shlex.join([command, name]) for command in ('/object', '/remove')
                    for name in sorted(session.objects)]
    return [name for name in choices if name.casefold().startswith(text.casefold())]


def wavelengths(values, what):
    """Parse a micron interval, rejecting reversed or non-finite edges."""
    low, high = map(float, values)
    if not 0 < low < high < float('inf'):
        raise ValueError(f'Use finite positive {what} MIN < MAX in µm.')
    return low, high


# The glyphs /tree draws with; a static tree here reads the same as the browser.
TREE_BRANCH, TREE_LAST, TREE_PIPE, TREE_GAP = '├─ ', '└─ ', '│  ', '   '


def clean_cell(value):
    """One display line: no embedded breaks or tabs to disturb the alignment."""
    return str(value).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')


def is_wide(line):
    """Lines whose alignment carries meaning: scroll them sideways, never wrap."""
    line = clean_cell(line)
    return line.startswith(('| ', '+-')) or line.lstrip(' ' + TREE_PIPE[0]).startswith(
        (TREE_BRANCH[:2], TREE_LAST[:2]))


def path_tree(entries):
    """Order relative paths into tree rows, in the glyphs /tree browses with.

    ``entries`` are (path, note, payload): the path relative to the tree root, a
    note to show after the name, and whatever the caller wants handed back on
    that leaf. Each row carries the guide ``prefix`` to draw at its left, so the
    review screen and the plain transcript describe exactly the same tree.
    Folders come before files, as they do in the browser.
    """
    tree = {}
    for entry in entries:
        path, note, payload = (tuple(entry) + ('', None))[:3]
        path = Path(path)
        # Drop any drive or leading slash: every entry hangs under the one root.
        parts = path.relative_to(path.anchor).parts if path.anchor else path.parts
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (clean_cell(note), payload)
    rows = []

    def leaves(node):
        return sum(leaves(value) if isinstance(value, dict) else 1 for value in node.values())

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda item: (not isinstance(item[1], dict), item[0].casefold()))
        for index, (name, value) in enumerate(items):
            last = index == len(items) - 1
            folder = isinstance(value, dict)
            rows.append(dict(prefix=prefix + (TREE_LAST if last else TREE_BRANCH),
                             name=clean_cell(name), folder=folder,
                             note='' if folder else value[0], payload=None if folder else value[1],
                             files=leaves(value) if folder else 1))
            if folder:
                walk(value, prefix + (TREE_GAP if last else TREE_PIPE))

    walk(tree, '')
    return rows


def text_tree(entries, root):
    """The same tree as plain lines, for the transcript and piped sessions."""
    lines = [clean_cell(root) + '/']
    for row in path_tree(entries):
        lines.append(row['prefix'] + row['name'] + ('/' if row['folder'] else '')
                     + (f"  — {row['note']}" if row['note'] else ''))
    return lines


def sort_entries(plan, root):
    """Split a sorting plan into where files land and what stays in place.

    Returns two lists of ``path_tree`` entries, so the review screen and the
    transcript always describe the same moves.
    """
    root = Path(root)

    def relative(path):
        path = Path(path)
        return path.relative_to(root) if path.is_relative_to(root) else path

    moves, review = [], []
    for item in plan:
        if item['reason'] or not item['destination']:
            review.append((relative(item['source']), item['reason'] or 'No destination planned.', item))
            continue
        source, destination = Path(item['source']), Path(item['destination'])
        # Only a name collision renames a file; say so rather than leave it a mystery.
        note = f'renamed from {source.name}' if source.name != destination.name else ''
        moves.append((relative(destination), note, item))
    return moves, review


def text_table(headers, rows, breaks=()):
    """Align columns for plain output; the visual views render the same rows.

    ``breaks`` holds row indices after which to rule a line, so a table split
    into instrument blocks reads the same here as in the visual browser.
    """
    cells = [[str(value).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
              for value in row] for row in rows]
    widths = [max(len(str(headers[i])), *(len(row[i]) for row in cells)) if cells else len(str(headers[i]))
              for i in range(len(headers))]
    border = '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
    def line(row):
        return '| ' + ' | '.join(value.ljust(w) for value, w in zip(row, widths)) + ' |'
    body = []
    for index, row in enumerate(cells):
        body.append(line(row))
        if index in breaks and index < len(cells) - 1:
            body.append(border)
    return [border, line(headers), border, *body, border]


def channel_breaks(rows):
    """Row indices where the instrument changes; each channel gets its own block."""
    return {index for index in range(len(rows) - 1)
            if rows[index]['instrument'] != rows[index + 1]['instrument']}


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
        self.objects = {}
        self.removed_objects = set()
        self.memory_reindex = set()
        self.memory_changed = False
        self.known_data = {}
        self.pending_sort = set()
        self.new_data = []
        self.sort_preview = None
        try:
            saved = json.loads(self.memory_path.read_text())
            if (isinstance(saved, dict) and saved.get('version') in (1, 2)
                    and isinstance(saved.get('files'), list) and saved['files']
                    and all(isinstance(p, str) and Path(p).is_absolute() for p in saved['files'])
                    and isinstance(saved.get('options', {}), dict)
                    and all(k in ('ins', 'target', 'night') and isinstance(v, str) for k, v in saved.get('options', {}).items())):
                self.previous = dict(files=saved['files'], options=saved.get('options', {}))
            if isinstance(saved, dict) and saved.get('version') == 2:
                known = saved.get('known_data', {})
                if isinstance(known, dict):
                    self.known_data = {p: digest for p, digest in known.items()
                                       if isinstance(p, str) and isinstance(digest, str)}
                pending_sort = saved.get('pending_sort', [])
                if isinstance(pending_sort, list):
                    self.pending_sort = {p for p in pending_sort if isinstance(p, str)}
                objects = saved.get('objects', {})
                if isinstance(objects, dict):
                    for name, entries in objects.items():
                        if isinstance(name, str) and name and isinstance(entries, list):
                            valid = [e for e in entries if isinstance(e, dict)
                                     and all(isinstance(e.get(k), str) for k in ('path', 'night', 'instrument'))
                                     and Path(e['path']).is_absolute()]
                            if valid:
                                self.objects[name] = valid
                removed = saved.get('removed_objects', [])
                if isinstance(removed, list):
                    self.removed_objects = {n for n in removed if isinstance(n, str)}
                for name in self.removed_objects:
                    self.objects.pop(name, None)
                pending = saved.get('reindex_paths', [])
                if isinstance(pending, list):
                    self.memory_reindex = {p for p in pending if isinstance(p, str) and Path(p).is_absolute()}
                if saved.get('night_convention') != 'UTC calendar date':
                    # Old labels cannot simply be shifted: one former night can
                    # straddle two UTC dates. Re-read timestamps off the UI startup path.
                    for entries in self.objects.values():
                        for entry in entries:
                            self.memory_reindex.add(entry['path'])
                            entry['night'] = 'Unknown'
                    self.memory_changed = True
                    if self.previous.get('options', {}).get('night') not in (None, 'all', 'Unknown'):
                        self.previous['needs_date_selection'] = True
                if saved.get('resume_needs_date_selection') and self.previous:
                    self.previous['needs_date_selection'] = True
        except (OSError, ValueError, TypeError):
            pass
        self.merge_objects()

    # ------------------------------------------------------------------ state

    def merge_objects(self):
        """Migrate case-only duplicates, retaining one display name and every entry."""
        before = json.dumps(self.objects, sort_keys=True), set(self.removed_objects)
        removed = {object_key(n) for n in self.removed_objects}
        names, merged = {}, {}
        for name, entries in self.objects.items():
            key = object_key(name)
            if key in removed:
                continue
            display = names.setdefault(key, name)
            group = merged.setdefault(display, [])
            for entry in entries:
                if entry not in group:
                    group.append(entry)
        self.objects, self.removed_objects = merged, removed
        self.memory_changed |= before != (json.dumps(self.objects, sort_keys=True), self.removed_objects)

    def remember_files(self):
        """Save only successfully loaded paths and filters, atomically and locally."""
        self.previous = dict(files=list(self.files), options=dict(self.load_options))
        if len(self.targets) == 1:
            self.previous['options'].setdefault('target', self.targets[0])
        self.register_objects(self.loaded_metadata, restore=self.targets)
        return self.save_memory()

    def save_memory(self):
        """Persist the catalogue and latest load together using an atomic replace."""
        saved = dict(version=2, files=self.previous.get('files', []),
                     options=self.previous.get('options', {}), objects=self.objects,
                     removed_objects=sorted(self.removed_objects), night_convention='UTC calendar date',
                     reindex_paths=sorted(self.memory_reindex),
                     known_data=self.known_data, pending_sort=sorted(self.pending_sort),
                     resume_needs_date_selection=self.previous.get('needs_date_selection', False))
        temporary = None
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.memory_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(saved, stream, ensure_ascii=True, indent=2)
                stream.write('\n')
            temporary.replace(self.memory_path)
            self.memory_changed = False
        except OSError as error:
            return f'Warning: memory could not be saved: {error}'
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return ''

    def register_objects(self, metadata, restore=()):
        """Merge case-insensitive object identities; retain exact display spelling."""
        before = json.dumps(self.objects, sort_keys=True), set(self.removed_objects)
        names = {object_key(name): name for name in self.objects}
        restored = {object_key(name) for name in restore}
        for row in metadata:
            path = row['path']
            for name in list(self.objects):
                self.objects[name] = [e for e in self.objects[name] if e['path'] != path]
                if not self.objects[name]:
                    del self.objects[name]
            if not row['loadable']:
                continue
            for observation in row.get('observations', []):
                key = object_key(observation['target'])
                name = names.setdefault(key, observation['target'])
                if key in restored:
                    self.removed_objects.discard(key)
                if key in self.removed_objects:
                    continue
                entry = dict(path=path, night=observation['night'], instrument=observation['instrument'])
                entries = self.objects.setdefault(name, [])
                if entry not in entries:
                    entries.append(entry)
        for entries in self.objects.values():
            entries.sort(key=lambda e: (e['night'], e['instrument'], e['path']))
        return before != (json.dumps(self.objects, sort_keys=True), self.removed_objects)

    def accept_catalog(self, catalog, errors):
        """Publish discovery on the UI thread and register new observations."""
        self.catalog, self.scan_errors = catalog, list(errors)
        known = {r['path']: r['sha256'] for r in catalog if r.get('sha256')}
        self.new_data = [p for p, digest in known.items() if self.known_data.get(p) != digest]
        if known != self.known_data:
            self.memory_changed = True
        self.known_data = known
        self.pending_sort.intersection_update(r['path'] for r in catalog)
        self.pending_sort.update(self.new_data)
        from .io import file_metadata
        metadata = list(catalog)
        present = {r['path'] for r in metadata}
        metadata += [file_metadata(p) for p in sorted(self.memory_reindex - present) if Path(p).is_file()]
        pending = set(self.memory_reindex)
        self.memory_reindex.difference_update(r['path'] for r in metadata if r['loadable'])
        changed = self.register_objects(metadata)
        if changed or self.memory_changed or pending != self.memory_reindex:
            warning = self.save_memory()
            if warning:
                self.scan_errors.append(warning)

    def object_name(self, name):
        if name in self.objects:
            return name
        matches = [n for n in self.objects if object_key(n) == object_key(name)]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f'Object {name!r} is not uniquely registered. /object lists remembered names.')

    def object_rows(self, name=None):
        """Objects first, then separate observing nights and instrument setups."""
        rows = []
        for target, entries in sorted(self.objects.items()):
            if name is not None and target != name:
                continue
            groups = {}
            for entry in entries:
                key = (entry['night'], entry['instrument']) if name is not None else (target, '')
                groups.setdefault(key, []).append(entry)
            for (night, instrument), group in sorted(groups.items()):
                paths = sorted({e['path'] for e in group})
                missing = sum(not Path(p).is_file() for p in paths)
                command = ['/object', target]
                if name is not None:
                    command += ['night=' + night, 'ins=' + instrument]
                rows.append(dict(target=target, night=night, instrument=instrument,
                                 nights=len({e['night'] for e in group}), files=len(paths), missing=missing,
                                 command=shlex.join(command), enabled=name is None or not missing))
        return rows

    def suggestions(self):
        """Explain each actionable next step using catalogue and session facts."""
        actions = []
        def add(title, reason, command_text, enabled=True):
            actions.append(dict(title=title, reason=reason, command=command_text, enabled=enabled))
        if self.pending_sort:
            add('Sort newly added data', f'{len(self.pending_sort)} new/changed files. Review instrument → object → epoch folders.', '/sort')
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
            reason = 'Choose an observable, or A for every observable at once.'
            if any(i.startswith('GRAVITY_') for i in self.instruments):
                reason += ' GRAVITY SC and FT get separate panels.'
            add('Plot the active observations', reason, '/plot')
            add('Inspect files and observables', f'{len(self.files)} loaded file(s); browse tables, flags and baseline coverage.', '/data')
            if self.fitted:
                add('Review the fit', 'A fitted model is available for comparison with the data.', '/plot fit')
                add('Read the fit report', 'Inspect fitted parameters and uncertainties.', '/report')
            else:
                add('Explore model choices', 'Choose a scientific model after inspecting the observations.', '/models')
        for row in self.object_rows():
            add(f"Browse {row['target']}", f"{row['nights']} nights · {row['files']} files · choose a night before loading.",
                row['command'])
        add('Browse other files' if actions else 'Choose FITS files',
            'Open the file picker to choose a different dataset.' if actions else f'No loadable observations found in {self.data_root}.', '/load')
        return actions

    def scan_data(self):
        from .io import scan_data
        self.accept_catalog(*scan_data(self.data_root))
        return self.catalog

    def remap_files(self, paths):
        """Keep loaded science data and persistent references valid after sorting."""
        def remap(value):
            if isinstance(value, str):
                return paths.get(value, value)
            if isinstance(value, list):
                return [remap(v) for v in value]
            if isinstance(value, tuple):
                return tuple(remap(v) for v in value)
            if isinstance(value, dict):
                return {paths.get(k, k): remap(v) for k, v in value.items()}
            return value
        self.files = remap(self.files)
        self.records = remap(self.records)
        self.headers = remap(self.headers)
        self.previous = remap(self.previous)
        self.objects = remap(self.objects)
        self.known_data = remap(self.known_data)
        self.memory_reindex = {paths.get(p, p) for p in self.memory_reindex}
        self.pending_sort = {paths.get(p, p) for p in self.pending_sort}
        for name in ('catalog', 'loaded_metadata'):
            rows = remap(getattr(self, name))
            for row in rows or []:
                row['name'], row['folder'] = Path(row['path']).name, str(Path(row['path']).parent)
            setattr(self, name, rows)
        if self.oi is not None:
            self.oi.data = remap(self.oi.data)
            if hasattr(self.oi, '_merged'):
                self.oi._merged = remap(self.oi._merged)

    def sort_tree_lines(self, plan, planned=True):
        """Show a sorting plan or its result as the folder tree it produces.

        A column of long paths hides the shape of the layout, which is the only
        thing worth reviewing before agreeing to move files.
        """
        # Name the tree by its folder, not its full path: the absolute root is
        # stated once beside the backup location, and a long root line would wrap.
        label = self.data_root.name or str(self.data_root)
        moves, review = sort_entries(plan, self.data_root)
        lines = []
        if moves:
            lines += [f'{"Planned layout" if planned else "New layout"} · '
                      f'{len(moves)} file' + ('s' if len(moves) != 1 else ''),
                      *text_tree(moves, label)]
        if review:
            stay = (f'{len(review)} file stays where it is' if len(review) == 1
                    else f'{len(review)} files stay where they are')
            lines += [*([''] if lines else []), f'Needs review · {stay}', *text_tree(review, label)]
        return lines

    @command('/sort', '[apply] — review or apply instrument/object/epoch folders; originals are backed up.',
             detail=('Run /sort to preview, then /sort apply to accept those moves.',
                     'Epoch folders use Epoch 01 — YYYY-MM-DD, numbered chronologically per instrument and object (UTC dates).',
                     'Adding an earlier date renumbers later epochs. Memory and /resume follow the files.',
                     'Verified, unmodified copies are kept in data/.backup/ and excluded from discovery.',
                     'Ambiguous objects or dates remain in place for review.'))
    def cmd_sort(self, args):
        from .io import organize_files, sorting_plan
        if not args:
            self.sort_preview = None
            self.scan_data()
            self.sort_preview = sorting_plan(self.catalog, self.data_root, self.objects)
            if not self.sort_preview:
                self.pending_sort.clear()
                warning = self.save_memory()
                return ['All data are already organized.', *([warning] if warning else [])]
            return ['Sorting preview · /sort apply accepts these moves; no files moved yet.',
                    *self.sort_tree_lines(self.sort_preview),
                    f'Original byte copies: {self.data_root / ".backup"}']
        if args != ['apply']:
            raise Usage()
        if self.sort_preview is None:
            raise ValueError('Run /sort first to review the proposed folders.')
        old_pending = set(self.pending_sort)
        with organize_files(self.sort_preview, self.data_root) as result:
            paths = result['paths']
            try:
                self.remap_files(paths)
                self.pending_sort.difference_update(paths.values())
                warning = self.save_memory()
                if warning:
                    raise ValueError(warning.removeprefix('Warning: '))
            except BaseException:
                self.remap_files({new: old for old, new in paths.items()})
                self.pending_sort = old_pending
                raise
        self.sort_preview = None
        self.scan_data()
        moved = [dict(source=source, destination=destination, reason='')
                 for source, destination in paths.items()]
        return [f'Sorted {len(paths)} files. Original byte copies are safe in {self.data_root / ".backup"}.',
                *self.sort_tree_lines(moved, planned=False), *result['warnings']]

    @command('/object', '[NAME] [night=YYYY-MM-DD|Unknown|all] [ins=NAME] — browse remembered objects and load a night.',
             detail=('Names are read from OI_TARGET and matched case-insensitively; quote names containing spaces.',
                     'Nights are grouped by UTC calendar date, midnight to midnight.',
                     'Without night=, show the nights; night=all explicitly combines them.',
                     'New data are registered at startup, by /scan, and after /load.'))
    def cmd_object(self, args):
        if self.catalog is None:
            self.scan_data()
        if not args:
            return ['Remembered objects · /object NAME shows observing nights.',
                    *text_table(['Object', 'Nights', 'Files', 'Missing'],
                                [[r['target'], r['nights'], r['files'], r['missing']] for r in self.object_rows()])]
        name = self.object_name(args[0])
        options = {}
        for arg in args[1:]:
            key, sep, value = arg.partition('=')
            if not sep or key not in ('night', 'ins') or not value or key in options:
                raise Usage()
            options[key] = value
        if 'night' not in options:
            return [f'{name} · observing nights (UTC calendar dates). Choose night=DATE to load.',
                    *text_table(['Night', 'Instrument', 'Files', 'Missing', 'Command'],
                                [[r['night'], r['instrument'], r['files'], r['missing'], r['command']]
                                 for r in self.object_rows(name)
                                 if 'ins' not in options or r['instrument'] == options['ins']])]
        entries = [e for e in self.objects[name]
                   if (options['night'] == 'all' or e['night'] == options['night'])
                   and ('ins' not in options or e['instrument'] == options['ins'])]
        paths = sorted({e['path'] for e in entries})
        if not paths:
            raise ValueError('No remembered observations match that night/instrument. /object NAME lists them.')
        if any(not Path(p).is_file() for p in paths):
            raise ValueError('Some remembered files are missing. Restore them or use /load; the selection was not partially loaded.')
        return self.cmd_load([*paths, 'target=' + name, *(f'{k}={v}' for k, v in options.items())])

    @command('/remove', 'NAME — forget an object; keep its FITS files and currently loaded data.',
             detail=('The object stays forgotten during automatic scans. Explicit /load registers it again.',))
    def cmd_remove(self, args):
        if len(args) != 1:
            raise Usage()
        name = self.object_name(args[0])
        entries = self.objects.pop(name)
        previous = self.previous
        self.removed_objects.add(object_key(name))
        previous_target = previous.get('options', {}).get('target')
        if ((previous_target is not None and object_key(previous_target) == object_key(name)) or (previous_target is None and
                set(previous.get('files', ())) & {e['path'] for e in entries})):
            self.previous = {}
        warning = self.save_memory()
        if warning:
            self.objects[name] = entries
            self.removed_objects.discard(object_key(name))
            self.previous = previous
            raise ValueError(warning.removeprefix('Warning: '))
        return [f'Forgot {name}. FITS files and active data are unchanged. /load can register it again.']

    @command('/scan', 'Refresh the FITS catalogue in the data folder.')
    def cmd_scan(self, args):
        if args:
            raise Usage()
        self.scan_data()
        message = (f'New data added: {len(self.new_data)} files backed up. Would you like me to sort them? /sort previews the folders.'
                   if self.new_data else f'Found {len(self.catalog)} FITS file(s) in {self.data_root}. /suggest shows next steps.')
        return [message,
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
        if self.previous.get('needs_date_selection'):
            raise ValueError('The saved selection used noon-to-noon nights. Use /object to select a UTC calendar date before resuming.')
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
        for path in sorted({r['file'] for r in self.records if '�' in r['target']}):
            lines.append(f'Warning: {Path(path).name}: unreadable characters in OI_TARGET.TARGET '
                         'are displayed as �. The observations are loaded and available; '
                         'check the target name with /headers.')
        lines.append('Fit selection: ' + json.dumps(self.settings))
        lines.append('Next: /models → /fit disk → /plot fit → /report')
        return lines

    def observable_rows(self):
        """One row per target, instrument and observable, by observable within a channel."""
        import numpy as np
        from .plot import instrument_order
        rows = []
        for key in sorted({(r['target'], r['instrument'], r['observable']) for r in self.records},
                          key=lambda key: (instrument_order(key[1]), key[2], key[0])):
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
                             nights=sorted({r.get('night', 'Unknown') for r in group}),
                             hdus=len(self.headers.get(path, ())), records=len(group),
                             instruments=sorted({r['instrument'] for r in group}),
                             observables=sorted({r['observable'] for r in group}),
                             usable=sum(int((~r['mask']).sum()) for r in group)))
        return rows

    def baseline_rows(self, records=None):
        """One row per baseline or station triplet, grouped by what it measures.

        Within a channel the observables decide the order, so the two-telescope
        baselines (V2, |V|, DPHI) and the closure triangles (T3PHI, T3AMP) read
        as separate blocks instead of interleaving; length breaks the ties.
        """
        import numpy as np
        from .plot import instrument_order
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
        return sorted(rows, key=lambda row: (instrument_order(row['instrument']), row['observables'],
                                            -row['length'] if row['length'] == row['length'] else 0,
                                            row['baseline']))

    def file_lines(self):
        """Numbered file table shared by load, resume and plain dataset output."""
        return text_table(['#', 'File', 'Night (UTC)', 'Folder', 'Usable', 'Instruments'],
                          [[str(row['number']), row['name'], ', '.join(row['nights']), str(Path(row['path']).parent),
                            f"{row['usable']:,}", ', '.join(row['instruments'])]
                           for row in self.file_rows()])

    def data_lines(self):
        """The dataset as text; the visual view shows these same rows as tables."""
        if not self.files:
            return ['No active data. Use /load to choose FITS files.']
        selection = ','.join(self.settings.get('obs', ())) or 'none'
        window = self.settings.get('wl ranges')
        scope = f'{window[0][0]:g}–{window[0][1]:g} µm' if window else 'all wavelengths'
        lines = [f'Active data · {len(self.files)} file(s) · {", ".join(self.targets)} · '
                 f'{", ".join(self.instruments)}', f'Fit selection: {selection} · {scope}', '', 'Files']
        lines += self.file_lines()
        observables = self.observable_rows()
        lines += ['', 'Observables']
        lines += text_table(['Target', 'Instrument', 'Obs', 'Usable/Total', 'Wavelengths (µm)', 'Chan', 'Baselines', 'Fitted'],
                            [[row['target'], row['instrument'], row['observable'],
                              f"{row['usable']}/{row['total']}", f"{row['wl'][0]:.5g}–{row['wl'][1]:.5g}",
                              row['channels'], row['baselines'], 'yes' if row['selected'] else '']
                             for row in observables], channel_breaks(observables))
        baselines = self.baseline_rows()
        if baselines:
            lines += ['', 'Baselines']
            lines += text_table(['Baseline', 'Instrument', 'Length (m)', 'Files', 'Usable', 'Observables'],
                                [[row['baseline'], row['instrument'],
                                  f"{row['length']:.2f}" if row['length'] == row['length'] else '—',
                                  row['files'], row['usable'], ','.join(row['observables'])]
                                 for row in baselines], channel_breaks(baselines))
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

    @command('/load', '[PATH ...] [ins=NAME] [target=NAME] [night=DATE] — pick files visually or load paths.')
    def cmd_load(self, args):
        paths, options = [], {}
        for arg in args:
            if arg.startswith(('ins=', 'target=', 'night=')):
                key, value = arg.split('=', 1)
                options[key] = value
            else:
                paths.append(arg)
        if not paths:
            return ['Use /load in an interactive terminal to select files, or /load PATH [PATH ...].']
        from .io import backup_file, file_metadata, load_observations, read_headers
        oi, records = load_observations(paths, options.get('ins'), options.get('target'), options.get('night'))
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
        for row in metadata:
            row.update(backup_file(row['path'], self.data_root))
        self.oi, self.records, self.settings = oi, records, settings
        self.files, self.headers = files, headers
        self.targets = tuple(sorted({r['target'] for r in records}))
        self.instruments = tuple(sorted({r['instrument'] for r in records}))
        self.loaded_metadata, self.load_options = metadata, options
        self.fitted = self.bootstrapped = False
        self.clear_figures()
        warning = self.remember_files()
        nights = sorted({r['night'] for r in records})
        return [f'Loaded {len(files)} file(s) into memory. /headers browses their metadata.',
                *self.file_lines(), *self.inspect(),
                *(['Warning: several observing nights are loaded. Use /object NAME to select one; '
                   'night=all explicitly enables a combined fit.'] if len(nights) > 1 and options.get('night') != 'all' else []),
                *([warning] if warning else [])]

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
        if len({r.get('night', 'Unknown') for r in self.records}) > 1 and self.load_options.get('night') != 'all':
            raise ValueError('Several observing nights are loaded. Select one with /object NAME night=DATE, '
                             'or explicitly combine them with /object NAME night=all before fitting.')
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
        from .plot import COLOR_KEYS, DEFAULT_SPEC, X_AXES, parse_limits, target_options
        spec, positional, given = dict(DEFAULT_SPEC), [], []
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
            if key in ('errors', 'showuv', 'flagged', 'logv', 'logb'):
                value = self.switch(key, value)
            if key == 'legend':
                if value not in ('auto', 'on', 'off'):
                    raise ValueError('Use legend=auto, legend=on or legend=off.')
            if key == 'spectro':
                if value not in ('auto', 'on', 'off'):
                    raise ValueError('Use spectro=auto, spectro=on or spectro=off.')
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
            if key == 'obs':
                value = self.chosen_observables(value)
            given.append(key)
            spec[key] = value
        if len(positional) > 2:
            raise Usage
        save = spec.pop('save')
        if len(positional) == 2:
            save = positional[1]
        spec['plot'] = positional[0] if positional else self.settings['obs'][0]
        if spec['plot'] not in ('all', 'fit', 'bootstrap', 'uv'):
            available = {r['observable'] for r in self.records}
            if spec['plot'] not in available:
                raise ValueError('Choose all, uv, fit, bootstrap or an available observable: '
                                 + ', '.join(sorted(available)))
        allowed = target_options(spec['plot'])
        unusable = [key for key in dict.fromkeys(given) if key not in allowed and key != 'save']
        if unusable:
            raise ValueError(f'{", ".join(name + "=" for name in unusable)} '
                             f'{"do" if len(unusable) > 1 else "does"} not apply to /plot {spec["plot"]}. '
                             + ('It takes no options besides save=.' if not allowed else
                                'It takes: ' + ', '.join(name + '=' for name in allowed) + ', save=.'))
        if spec['baselines']:
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

    def chosen_observables(self, value):
        """Observables to show at once; 'all' spells out everything in the data.

        An empty selection means 'whatever this plot defaults to', so 'all' has
        to resolve here rather than stay unset.
        """
        available = sorted({r['observable'] for r in self.records})
        if value in ('', 'all'):
            return tuple(available)
        names = tuple(dict.fromkeys(part for part in value.replace(',', ' ').split()))
        unknown = [name for name in names if name not in available]
        if unknown:
            raise ValueError('Unknown observable(s): ' + ', '.join(unknown)
                             + '. This dataset carries: ' + ', '.join(available))
        return names

    def show_setups(self, spec):
        """Instrument setups PMOIRED will display, science channel first.

        GRAVITY records the fringe tracker and the science spectrometer under
        separate INSNAMEs. PMOIRED's own ``perSetup=True`` groups them by the
        simplified name 'GRAVITY' and draws both in one figure, mixing six FT
        channels with about two thousand SC ones. Ask for 'insname' instead and
        drive one setup at a time, so each keeps its own wavelength axis,
        spectral mode, colours and title.
        """
        from .plot import instrument_order
        names = sorted({str(data['insname']) for data in self.oi.data}, key=instrument_order)
        if spec['instruments']:
            names = [name for name in names if name in spec['instruments']]
        if not names:
            raise ValueError('No PMOIRED data for that instrument selection. '
                             '/data lists the instruments held in memory.')
        return names

    def show_overview(self, spec, model=None):
        """Every observable at once, drawn by PMOIRED's own display.

        One figure per instrument setup, each showing the uv coverage and every
        requested observable side by side; with ``model`` the best fit is drawn
        over the data and its chi2 reported.
        """
        from .plot import instrument_label
        import matplotlib.pyplot as plt
        loaded = list(self.oi.data)
        # A fit is only meaningful against what it was fitted to; data alone shows everything.
        wanted = spec['obs'] or (tuple(self.settings['obs']) if model
                                 else tuple(sorted({r['observable'] for r in self.records})))
        spectro = {'auto': None, 'on': True, 'off': False}[spec['spectro']]
        self.clear_figures()
        figures = []
        try:
            for name in self.show_setups(spec):
                chosen = [r for r in self.records if r['instrument'] == name
                          and (not spec['files'] or r['file'] in spec['files'])]
                obs = [item for item in wanted if any(r['observable'] == item for r in chosen)]
                if not obs:
                    continue
                # PMOIRED reads self.data; restrict it to this setup, then restore.
                previous = set(plt.get_fignums())
                self.oi.data = [data for data in loaded if str(data['insname']) == name
                                and (not spec['files'] or str(data['filename']) in spec['files'])]
                try:
                    self.oi.show(model=model, obs=obs, perSetup='insname', spectro=spectro,
                                 showUV=spec['showuv'], showFlagged=spec['flagged'],
                                 logV=spec['logv'], logB=spec['logb'], showChi2=model is not None)
                finally:
                    self.oi.data = loaded
                fresh = [plt.figure(n) for n in plt.get_fignums() if n not in previous]
                for figure in fresh:
                    figure.suptitle(instrument_label(name))
                figures.extend(fresh)
        except BaseException:
            for figure in figures:
                plt.close(figure)
            raise
        if not figures:
            raise ValueError('No data to show for that selection. '
                             '/data lists the observables each instrument carries.')
        self.figures = figures
        return figures

    @command('/plot', '[OBS|all|uv|fit|bootstrap] [option=value ...] — plot data; run it bare for the builder.',
             needs='data', detail=(
                 '  all = every observable at once, one figure per instrument, as PMOIRED shows it',
                 '  x=wavelength|frequency|baseline|channel|mjd   files=1,3|all   baselines=UT1-UT2,...|all',
                 '  instruments=SC|FT|GRAVITY_SC,...|all   (GRAVITY SC = science; FT = fringe tracker)',
                 '  xlim=MIN,MAX   ylim=MIN,MAX   color=baseline|file|instrument|target|none   errors=on|off',
                 '  legend=auto|on|off   title=TEXT   xlabel=TEXT   ylabel=TEXT   continuum=DEGREE   line=MIN,MAX',
                 '  all and fit only: obs=V2,T3PHI|all   spectro=auto|on|off   showuv=on|off   flagged=on|off',
                 '                    logv=on|off   logb=on|off   (fit shows the fitted observables unless obs= says otherwise)',
                 '  save=FILE.png|pdf|svg   ·   example: /plot V2 x=frequency baselines=J3-D0 ylim=0,1 save=out/v2.png',
                 '  example: /plot all instruments=SC obs=V2,T3PHI,FLUX save=out/overview.png'))
    def cmd_plot(self, args):
        spec, save = self.plot_spec(args)
        if spec['plot'] == 'all':
            self.show_overview(spec)
        elif spec['plot'] in ('fit', 'bootstrap'):
            self.require_fit()
            if spec['plot'] == 'fit':
                self.show_overview(spec, model='best')
            else:
                if not self.bootstrapped:
                    raise ValueError('Run /bootstrap first.')
                import matplotlib.pyplot as plt
                from .plot import plot_bootstrap
                self.clear_figures()
                previous = set(plt.get_fignums())
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
                       settings=self.settings, load_options=dict(self.load_options),
                       nights=sorted({r.get('night', 'Unknown') for r in self.records}),
                       night_convention='UTC calendar date', result=result,
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
