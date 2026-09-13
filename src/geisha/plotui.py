"""Interactive plot builder: compose a /plot command without memorising options.

Every field maps to one /plot option, and the screen shows the exact command it
will run, so a figure made here can be saved, scripted or recalled with ↑.
"""

import curses
import shlex
from pathlib import Path

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .plot import COLOR_KEYS, DEFAULT_SPEC, X_AXES, instrument_label, select_records
from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, clip, edit_query, full_screen,
                  make_console, paint_rich, printable, read_key, scroll_step)

# key, label, kind, choices, hint. 'pick' fields open a selector.
FIELDS = (
    ('plot', 'Plot', 'choice', None, 'Observable, uv coverage or a fit result'),
    ('x', 'X axis', 'choice', tuple(X_AXES), 'What the observable is plotted against'),
    ('files', 'Files', 'pick', None, 'Space marks files · Enter opens the list'),
    ('instruments', 'Data / FT–SC', 'pick', None, 'SC = science · FT = fringe tracker · one panel per instrument'),
    ('baselines', 'Baselines', 'pick', None, 'Only baselines or triangles carrying the chosen observable'),
    ('color', 'Colour by', 'choice', COLOR_KEYS, 'One colour and legend entry per group'),
    ('errors', 'Error bars', 'choice', ('on', 'off'), 'Draw the stated uncertainties'),
    ('legend', 'Legend', 'choice', ('auto', 'on', 'off'), 'Auto hides it beyond 16 series'),
    ('xlim', 'X limits', 'text', None, 'MIN,MAX · empty keeps the full range'),
    ('ylim', 'Y limits', 'text', None, 'MIN,MAX · empty keeps the full range'),
    ('title', 'Title', 'text', None, 'Empty uses target · observable'),
    ('xlabel', 'X label', 'text', None, 'Empty uses the axis name and unit'),
    ('ylabel', 'Y label', 'text', None, 'Empty uses the observable name'),
    ('continuum', 'Continuum', 'choice', ('off', '0', '1', '2', '3'), 'Weighted polynomial degree'),
    ('line', 'Exclude line', 'text', None, 'MIN,MAX µm kept out of the continuum fit'),
    ('save', 'Save to', 'text', None, 'FILE.png, .pdf or .svg · empty only displays'),
    ('draw', '▶ Draw plot', 'action', None, 'Run the command shown below'),
    ('reset', '↺ Reset fields', 'action', None, 'Back to the current fit selection'),
)
DEFAULTS = dict(x='wavelength', color='baseline', errors='on', legend='auto', continuum='off',
                xlim='', ylim='', title='', xlabel='', ylabel='', line='', save='')


def plot_choices(session):
    """Everything this dataset can draw, in the order the builder cycles them."""
    observables = sorted({r['observable'] for r in session.records})
    return tuple(observables) + ('uv',) + (('fit', 'bootstrap') if session.fitted else ())


def initial_state(session, seed):
    """Start from the fit selection, then apply whatever the caller scoped."""
    choices = plot_choices(session)
    preferred = next((o for o in session.settings.get('obs', ()) if o in choices), choices[0])
    state = dict(DEFAULTS, plot=preferred, files=(), instruments=(), baselines=(), draw='', reset='')
    for key, value in (seed or {}).items():
        state[key] = value
    return normalize_selection(session, state)


def scoped_records(session, state):
    """Candidate baselines follow the observable, files and instrument selection."""
    return select_records(session.records, {**DEFAULT_SPEC, **state, 'baselines': ()})


def normalize_selection(session, state):
    """Discard stale picks when changing observable or data scope."""
    if state['plot'] in ('fit', 'bootstrap'):
        state.update(files=(), instruments=(), baselines=())
        return state
    instruments = {r['instrument'] for r in session.records
                   if not state['files'] or r['file'] in state['files']}
    state['instruments'] = tuple(name for name in state['instruments'] if name in instruments)
    baselines = {r['baseline'] for r in scoped_records(session, state)}
    state['baselines'] = tuple(name for name in state['baselines'] if name in baselines)
    return state


def value_text(session, state, key):
    """How a field's current value reads in the form."""
    value = state[key]
    if key in ('files', 'instruments', 'baselines') and state['plot'] in ('fit', 'bootstrap'):
        return 'fitted dataset'
    if key == 'files':
        if not value:
            return f'all ({len(session.files)})'
        names = [Path(path).name for path in value]
        return f'{len(names)} selected · ' + ', '.join(names)
    if key == 'instruments':
        names = value or tuple(entry[0] for entry in instrument_entries(session, state))
        return ('selected: ' if value else 'all: ') + ', '.join(instrument_label(name) for name in names)
    if key == 'baselines':
        available = len({r['baseline'] for r in scoped_records(session, state) if r['baseline']})
        return f'all ({available})' if not value else f'{len(value)} selected · ' + ', '.join(value)
    return str(value) if str(value) else '—'


def command_text(session, state):
    """The /plot command this form is building; defaults stay implicit."""
    parts = ['/plot', str(state['plot'])]
    for key, _, kind, _, _ in FIELDS:
        value = state.get(key)
        if key == 'plot' or kind == 'action' or not value or value == DEFAULTS.get(key):
            continue
        if key == 'files':
            numbers = [str(session.files.index(path) + 1) for path in value if path in session.files]
            parts.append('files=' + ','.join(numbers))
        elif key in ('baselines', 'instruments'):
            parts.append(key + '=' + ','.join(value))
        else:
            parts.append(f'{key}={value}')
    return shlex.join(parts)


def cycle(choices, value, step):
    index = choices.index(value) if value in choices else 0
    return choices[(index + step) % len(choices)]


def form_layout(session, state, selected, offset, editing, draft, message, width, height):
    """The field table, the command preview and the keys that apply."""
    page = max(1, height - 10)
    offset = max(0, min(offset, selected, max(0, len(FIELDS) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    title = Table.grid(expand=True)
    title.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    title.add_column(justify='right', no_wrap=True)
    title.add_row(Text('Plot builder', style='bold'),
                  Text(f'{len(session.records)} records · {len(session.files)} files', style='cyan'))
    table = Table(expand=True, box=None, padding=(0, 1), pad_edge=False, show_header=False, show_edge=False)
    table.add_column(width=14, no_wrap=True)
    table.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    if width >= 78:
        table.add_column(width=max(20, width // 3), no_wrap=True, overflow='ellipsis')
    for index in range(offset, min(len(FIELDS), offset + page)):
        key, label, kind, _, hint = FIELDS[index]
        focus = index == selected
        if kind == 'action':
            shown = Text('')
        elif focus and editing:
            shown = Text(draft + '▏', style='bold')
        else:
            shown = Text(printable(value_text(session, state, key)),
                         style='bold' if focus else 'cyan' if str(state[key]) not in ('', str(DEFAULTS.get(key))) else '')
        cells = [Text(('› ' if focus else '  ') + label,
                      style='bold' if focus or kind == 'action' else 'dim'), shown]
        if width >= 78:
            marker = {'choice': '←→', 'text': 'Enter', 'pick': 'Enter', 'action': 'Enter'}[kind]
            cells.append(Text(f'{marker}  {hint}' if focus else hint, style='' if focus else 'dim'))
        table.add_row(*cells, style='reverse' if focus and not editing else '')
    for _ in range(max(0, page - len(FIELDS[offset:offset + page]))):
        table.add_row(Text(' '))
    preview = Text('Command  ', style='dim', no_wrap=True, overflow='ellipsis')
    preview.append(clip(command_text(session, state), max(1, width - 9)), style='cyan')
    status = Text(message or 'Enter runs the plot from the action row at the bottom.',
                  style='cyan' if message else 'dim', no_wrap=True, overflow='ellipsis')
    if editing:
        controls = 'Type a value · Enter accept · Esc cancel'
    else:
        controls = ('←→ change · Enter edit · G draw · Esc cancel' if width < 76 else
                    '↑↓ move · ←→ change · Enter edit/open/run · G draw anywhere · Esc cancel')
    footer = Text(controls, no_wrap=True, overflow='ellipsis')
    footer.highlight_words(['↑↓', '←→', 'Enter', 'G', 'R', 'Esc'], 'cyan')
    return Group(title, Rule(style='dim'), table, Rule(style='dim'), preview, status, footer), offset, page


def choose_many(screen, console, title, entries, checked, accent=0):
    """Checkbox tree with group headers. Returns the marked values, or None."""
    selectable = [index for index, entry in enumerate(entries) if entry[0] is not None]
    if not selectable:
        return None
    marked = set(checked)
    position = 0
    offset = 0
    while True:
        rows, columns = screen.getmaxyx()
        width, height = max(20, columns - 4), max(6, rows - 2)
        page = max(1, height - 7)
        selected = selectable[position]
        offset = max(0, min(offset, selected, max(0, len(entries) - page)))
        if selected >= offset + page:
            offset = selected - page + 1
        table = Table(expand=True, box=None, padding=(0, 1), pad_edge=False,
                      show_header=False, show_edge=False)
        table.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
        if width >= 60:
            table.add_column(width=24, no_wrap=True, overflow='ellipsis')
        for index in range(offset, min(len(entries), offset + page)):
            value, label, detail = entries[index]
            focus = index == selected
            if value is None:
                row = Text(printable(label), style='bold')
            else:
                row = Text(('› ' if focus else '  ') + ('[✓] ' if value in marked else '[ ] '),
                           style='bold' if focus else 'cyan' if value in marked else 'dim')
                row.append(printable(label), style='bold' if focus else '')
            cells = [row]
            if width >= 60:
                cells.append(Text(printable(detail), style='' if focus else 'dim'))
            table.add_row(*cells, style='reverse' if focus and value is not None else '')
        for _ in range(max(0, page - len(entries[offset:offset + page]))):
            table.add_row(Text(' '))
        status = Text(f'{len(marked)} selected · empty means every one', style='dim', no_wrap=True)
        footer = Text('↑↓ move · Space mark · A all · C clear · Enter accept · Esc cancel',
                      no_wrap=True, overflow='ellipsis')
        footer.highlight_words(['↑↓', 'Space', 'A', 'C', 'Enter', 'Esc'], 'cyan')
        screen.erase()
        try:
            paint_rich(screen, console, Group(Text(title, style='bold'), Rule(style='dim'), table,
                                              Rule(style='dim'), status, footer),
                       1, 2, width, height, accent)
            screen.refresh()
            key = read_key(screen, -1)
        except curses.error:
            continue
        if key == ESCAPE or key in QUIT_KEYS:
            return None
        if key in ENTER_KEYS:
            # A baseline can appear under several instruments; return it once.
            return tuple(dict.fromkeys(e[0] for e in entries if e[0] in marked))
        if key == ' ':
            marked.symmetric_difference_update({entries[selected][0]})
        elif key in ('a', 'A'):
            marked.update(entry[0] for entry in entries if entry[0] is not None)
        elif key in ('c', 'C'):
            marked.clear()
        else:
            step = scroll_step(key, page)
            if step:
                position = max(0, min(len(selectable) - 1, position + step))


def file_entries(session):
    return [(row['path'], f"{row['number']}. {row['name']}",
             f"{row['usable']:,} usable · {','.join(row['instruments'])}")
            for row in session.file_rows()]


def instrument_entries(session, state):
    records = [r for r in session.records if not state['files'] or r['file'] in state['files']]
    return [(name, instrument_label(name), 'separate plot panel')
            for name in sorted({r['instrument'] for r in records})]


def baseline_entries(session, state):
    """Only offer baselines carrying the chosen observable in this data scope."""
    entries = []
    rows = session.baseline_rows(scoped_records(session, state))
    for instrument in sorted({row['instrument'] for row in rows}):
        entries.append((None, instrument_label(instrument), ''))
        for row in rows:
            if row['instrument'] == instrument:
                length = f"{row['length']:.1f} m" if row['length'] == row['length'] else '—'
                entries.append((row['baseline'], '  ' + row['baseline'],
                                f"{length} · {row['usable']:,} usable · {','.join(row['observables'])}"))
    return entries


def build_plot(screen, session, seed=None, accent=0):
    """Run the builder. Returns a /plot command to execute, or '' if cancelled."""
    session.require_data()
    state = initial_state(session, seed)
    selected = offset = 0
    editing = False
    draft = message = ''
    console = make_console()
    with full_screen(screen):
        while True:
            key_name, label, kind, choices, _ = FIELDS[selected]
            rows, columns = screen.getmaxyx()
            try:
                screen.erase()
                if rows < 14 or columns < 44:
                    screen.addnstr(0, 0, 'Enlarge terminal (44 × 14) · Esc back', max(0, columns - 1))
                else:
                    layout, offset, page = form_layout(session, state, selected, offset, editing,
                                                       draft, message, columns - 4, rows - 2)
                    paint_rich(screen, console, layout, 1, 2, columns - 4, rows - 2, accent)
                screen.refresh()
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key in QUIT_KEYS:
                return ''
            if editing:
                draft, state_of_edit = edit_query(key, draft)
                if state_of_edit == 'done':
                    state[key_name] = draft.strip()
                    editing = False
                elif state_of_edit == 'clear':
                    editing = False
                continue
            message = ''
            if key == ESCAPE:
                return ''
            if key in ('g', 'G') or (key in ENTER_KEYS and key_name == 'draw'):
                return command_text(session, state)
            if key in ENTER_KEYS and key_name == 'reset':
                state = initial_state(session, seed)
                message = 'Reset to the fit selection.'
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(FIELDS) - 1
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and kind == 'choice':
                options = choices or plot_choices(session)
                state[key_name] = cycle(tuple(options), str(state[key_name]), 1 if key == curses.KEY_RIGHT else -1)
            elif key in ENTER_KEYS and kind == 'text':
                draft, editing = str(state[key_name]), True
            elif key in ENTER_KEYS and kind == 'pick':
                if state['plot'] in ('fit', 'bootstrap'):
                    message = 'Fit plots use the fitted dataset; these filters apply to data plots.'
                    continue
                entries = (file_entries(session) if key_name == 'files' else
                           instrument_entries(session, state) if key_name == 'instruments' else
                           baseline_entries(session, state))
                chosen = choose_many(screen, console, f'Plot builder / {label.lower()}',
                                     entries, state[key_name], accent)
                if chosen is not None:
                    state[key_name] = chosen
            elif key in ENTER_KEYS and kind == 'choice':
                options = choices or plot_choices(session)
                state[key_name] = cycle(tuple(options), str(state[key_name]), 1)
            else:
                step = scroll_step(key, max(1, len(FIELDS) - 1))
                if step:
                    selected = max(0, min(len(FIELDS) - 1, selected + step))
            normalize_selection(session, state)
