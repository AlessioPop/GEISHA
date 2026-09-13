"""Tables of the loaded dataset: files, observables and baselines.

Opens after a successful /load and from /data. Returns a plot seed when the
user starts a plot from a row, so the builder opens already scoped to it.
"""

import curses

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, clip, full_screen, make_console,
                  paint_rich, printable, read_key, scroll_step)

TABS = ('Files', 'Observables', 'Baselines')


def tab_rows(session, tab):
    if tab == 'Files':
        return session.file_rows()
    if tab == 'Observables':
        return session.observable_rows()
    return session.baseline_rows()


def data_table(tab, rows, offset, page, selected, width):
    """One Rich table per tab, dropping the least useful columns when narrow."""
    table = Table(expand=True, box=None, padding=(0, 1), pad_edge=False,
                  header_style='dim', show_edge=False)
    wide, roomy = width >= 88, width >= 68
    if tab == 'Files':
        table.add_column('#', width=3, justify='right', no_wrap=True)
        table.add_column('File', ratio=1, no_wrap=True, overflow='ellipsis')
        table.add_column('Usable', width=9, justify='right', no_wrap=True)
        if roomy:
            table.add_column('Records', width=8, justify='right', no_wrap=True)
            table.add_column('HDUs', width=5, justify='right', no_wrap=True)
        if wide:
            table.add_column('Observables', width=26, no_wrap=True, overflow='ellipsis')
    elif tab == 'Observables':
        table.add_column('Obs', width=6, no_wrap=True)
        table.add_column('Instrument', ratio=1, no_wrap=True, overflow='ellipsis')
        table.add_column('Usable / total', width=15, justify='right', no_wrap=True)
        if roomy:
            table.add_column('Wavelengths (µm)', width=17, justify='right', no_wrap=True)
            table.add_column('Chan', width=5, justify='right', no_wrap=True)
        if wide:
            table.add_column('Baselines', width=9, justify='right', no_wrap=True)
            table.add_column('Fitted', width=6, no_wrap=True)
    else:
        table.add_column('Baseline', width=12, no_wrap=True)
        table.add_column('Instrument', ratio=1, no_wrap=True, overflow='ellipsis')
        table.add_column('Length (m)', width=10, justify='right', no_wrap=True)
        if roomy:
            table.add_column('Usable', width=9, justify='right', no_wrap=True)
            table.add_column('Files', width=6, justify='right', no_wrap=True)
        if wide:
            table.add_column('Observables', width=22, no_wrap=True, overflow='ellipsis')
    for index in range(offset, min(len(rows), offset + page)):
        row = rows[index]
        focus = index == selected
        dim = '' if focus else 'dim'
        if tab == 'Files':
            cells = [Text(str(row['number'])),
                     Text(clip(row['name'], max(10, width - 30)), style='bold' if focus else ''),
                     Text(f"{row['usable']:,}")]
            if roomy:
                cells += [Text(str(row['records'])), Text(str(row['hdus']))]
            if wide:
                cells.append(Text(','.join(row['observables']), style=dim))
        elif tab == 'Observables':
            cells = [Text(row['observable'], style='bold' if focus else 'cyan'),
                     Text(printable(f"{row['instrument']} · {row['target']}"), style=dim),
                     Text(f"{row['usable']:,} / {row['total']:,}")]
            if roomy:
                cells += [Text(f"{row['wl'][0]:.5g}–{row['wl'][1]:.5g}"), Text(str(row['channels']))]
            if wide:
                cells += [Text(str(row['baselines'])),
                          Text('fitted' if row['selected'] else '', style='cyan' if focus else 'dim')]
        else:
            length = row['length']
            cells = [Text(row['baseline'], style='bold' if focus else 'cyan'),
                     Text(row['instrument'], style=dim),
                     Text(f'{length:.2f}' if length == length else '—')]
            if roomy:
                cells += [Text(f"{row['usable']:,}"), Text(str(row['files']))]
            if wide:
                cells.append(Text(','.join(row['observables']), style=dim))
        table.add_row(*cells, style='reverse' if focus else '')
    for _ in range(max(0, page - max(1, len(rows[offset:offset + page])))):
        table.add_row(Text(' '))
    return table


def data_layout(session, tab, rows, selected, offset, width, height):
    """Header, tab strip, table and the keys that act on the current row."""
    page = max(1, height - 9)
    offset = max(0, min(offset, selected, max(0, len(rows) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    title = Table.grid(expand=True)
    title.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    title.add_column(justify='right', no_wrap=True)
    title.add_row(Text('Active dataset', style='bold'),
                  Text(f'{len(session.files)} file' + ('s' if len(session.files) != 1 else '')
                       + ' · in memory', style='cyan'))
    scope = ','.join(session.settings.get('obs', ())) or 'none'
    window = session.settings.get('wl ranges')
    scope += f" · {window[0][0]:g}–{window[0][1]:g} µm" if window else ' · all wavelengths'
    subtitle = Text(printable(f'{", ".join(session.targets)} · {", ".join(session.instruments)}'),
                    style='dim', no_wrap=True, overflow='ellipsis')
    subtitle.append(f'  ·  fitting {scope}', style='dim')
    strip = Text(no_wrap=True)
    for name in TABS:
        strip.append(f' {name} ', style='reverse bold' if name == tab else 'dim')
        strip.append(' ')
    strip.append(f'· {len(rows)} row' + ('s' if len(rows) != 1 else ''), style='dim')
    detail = Text('No rows in this view.', style='dim', no_wrap=True, overflow='ellipsis')
    if rows:
        row = rows[selected]
        if tab == 'Files':
            detail = Text(clip(row['path'], width), style='dim', no_wrap=True)
        elif tab == 'Observables':
            detail = Text(printable(f"{row['observable']} · {row['instrument']} · {row['records']} records"
                                    f" · {row['baselines']} baselines · {row['usable']:,} usable samples"),
                          style='dim', no_wrap=True, overflow='ellipsis')
        else:
            detail = Text(printable(f"{row['baseline']} · {row['kind']} · {row['instrument']}"
                                    f" · {row['files']} files · {', '.join(row['observables'])}"),
                          style='dim', no_wrap=True, overflow='ellipsis')
    action = 'Enter headers' if tab == 'Files' else 'Enter plot'
    controls = (f'←→ tabs · {action} · P plot builder · Esc close' if width < 76 else
                f'↑↓ move · ←→/Tab switch tabs · {action} · P plot builder · H headers · Esc close')
    footer = Text(controls, no_wrap=True, overflow='ellipsis')
    footer.highlight_words(['↑↓', '←→', 'Tab', 'Enter', 'P', 'H', 'Esc'], 'cyan')
    return Group(title, subtitle, strip, Rule(style='dim'),
                 data_table(tab, rows, offset, page, selected, width),
                 Rule(style='dim'), detail, footer), offset, page


def browse_data(screen, session, accent=0, tab='Files'):
    """Browse the dataset. Returns a plot seed dict, or None when closed."""
    session.require_data()
    selected = offset = 0
    console = make_console()
    with full_screen(screen):
        while True:
            rows = tab_rows(session, tab)
            selected = max(0, min(selected, len(rows) - 1))
            rows_count, columns = screen.getmaxyx()
            try:
                screen.erase()
                if rows_count < 14 or columns < 44:
                    screen.addnstr(0, 0, 'Enlarge terminal (44 × 14) · Esc back', max(0, columns - 1))
                    page = 1
                else:
                    layout, offset, page = data_layout(session, tab, rows, selected, offset,
                                                       columns - 4, rows_count - 2)
                    paint_rich(screen, console, layout, 1, 2, columns - 4, rows_count - 2, accent)
                screen.refresh()
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key in QUIT_KEYS or key == ESCAPE or key in ('q', 'Q'):
                return None
            if key in ('\t', curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_BTAB):
                step = -1 if key in (curses.KEY_LEFT, curses.KEY_BTAB) else 1
                tab = TABS[(TABS.index(tab) + step) % len(TABS)]
                selected = offset = 0
            elif key in ('h', 'H') or (key in ENTER_KEYS and tab == 'Files'):
                from .headers import browse_headers
                browse_headers(screen, session, accent, files_first=True)
            elif key in ('p', 'P') or key in ENTER_KEYS:
                return plot_seed(tab, rows[selected]) if rows else {}
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(rows) - 1)
            else:
                selected = max(0, min(max(0, len(rows) - 1), selected + scroll_step(key, page)))


def plot_seed(tab, row):
    """Scope a new plot to the row the user was looking at."""
    if tab == 'Observables':
        return {'plot': row['observable'], 'instruments': (row['instrument'],)}
    if tab == 'Baselines':
        observable = next((obs for obs in ('V2', 'T3PHI') if obs in row['observables']),
                          row['observables'][0])
        return {'plot': observable, 'baselines': (row['baseline'],),
                'instruments': (row['instrument'],)}
    return {'files': (row['path'],)}
