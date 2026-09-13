"""List screens built on :func:`draw_panel`: /help, /color and the transcript."""

import curses
import textwrap
import time
from pathlib import Path

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from .home import draw_screen
from .session import COMMANDS, is_wide, path_tree, sort_entries
from .theme import COLOR_NAMES, COLOR_PARTS, THEMES, Theme, save_colors
from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, draw_panel, edit_query, read_key, scroll_step,
                  full_screen, make_console, paint_rich, printable)


def sort_rows(plan, root):
    """Every line of the review screen: the new layout, then what stays in place.

    The folders are the whole point of sorting, so draw them as the tree /tree
    browses rather than as a column of paths nobody can read side by side.
    """
    moves, review = sort_entries(plan, root)
    label = Path(root).name or str(root)
    rows = []

    def add(title, entries, alert):
        if not entries:
            return
        if rows:
            rows.append(dict(text=Text(''), detail='', selectable=False))
        rows.append(dict(text=Text(title, style='bold'), detail='', selectable=False))
        rows.append(dict(text=Text(printable(label) + '/', style='bold cyan'),
                         detail=printable(str(root)), selectable=False))
        for node in path_tree(entries):
            # Only the guide is dim; the names it leads to stay at full strength.
            text = Text(no_wrap=True, overflow='ellipsis')
            text.append(node['prefix'], style='dim')
            text.append(printable(node['name']) + ('/' if node['folder'] else ''),
                        style='bold' if node['folder'] else '')
            if node['folder']:
                detail = f"{node['files']} file" + ('s' if node['files'] != 1 else '')
            else:
                detail = ('stays at ' if alert else 'moves from ') + printable(node['payload']['source'])
            if node['note']:
                text.append('  — ' + printable(node['note']), style='red' if alert else 'dim')
                detail = printable(node['note']) + ' · ' + detail
            rows.append(dict(text=text, detail=detail, selectable=True))

    add('New layout', moves, False)
    stay = ('1 file stays where it is' if len(review) == 1
            else f'{len(review)} files stay where they are')
    add(f'Needs review · {stay}', review, True)
    return rows or [dict(text=Text('No files to move.', style='dim'), detail='', selectable=False)]


def sort_menu(screen, plan, root, accent=0):
    """Review the proposed folder tree before accepting any filesystem moves."""
    rows = sort_rows(plan, root)
    order = [index for index, row in enumerate(rows) if row['selectable']] or [0]
    position = offset = column = 0
    console = make_console()
    moves = sum(bool(r['destination']) and not r['reason'] for r in plan)
    widest = max((row['text'].cell_len for row in rows), default=0)
    with full_screen(screen):
        while True:
            try:
                height, width = screen.getmaxyx()
                page = max(1, height - 8)
                selected = order[min(position, len(order) - 1)]
                offset = max(0, min(offset, selected, max(0, len(rows) - page)))
                if selected >= offset + page:
                    offset = selected - page + 1
                body = Table.grid(expand=True)
                body.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
                for index in range(offset, min(len(rows), offset + page)):
                    # One shared offset shifts the whole tree, keeping it aligned.
                    body.add_row(rows[index]['text'][column:],
                                 style='reverse' if index == selected else '')
                for _ in range(max(0, page - len(rows[offset:offset + page]))):
                    body.add_row(Text(' '))
                detail = rows[selected]['detail']
                column = min(column, max(0, max(widest, len(detail)) - max(1, width - 4)))
                layout = Group(Text('Sort new data', style='bold'),
                               Text(f'{moves} moves · verified originals in .backup · epochs ordered by UTC date',
                                    style='dim', no_wrap=True, overflow='ellipsis'), body,
                               Text(detail[column:], style='dim', no_wrap=True, overflow='ellipsis'),
                               Text('↑↓ review · ←→ shift · Enter sort · Esc later' if moves else
                                    'Files needing review stay in place · Esc back', style='cyan', no_wrap=True, overflow='ellipsis'))
                screen.erase()
                if height >= 12 and width >= 44:
                    paint_rich(screen, console, layout, 1, 2, width - 4, height - 2, accent)
                else:
                    screen.addnstr(0, 0, 'Enlarge terminal (44 × 12) · Esc back', max(0, width - 1))
                screen.refresh()
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key == ESCAPE or key in QUIT_KEYS:
                return False
            if key in ENTER_KEYS and moves:
                return True
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                column = max(0, column + (12 if key == curses.KEY_RIGHT else -12))
            if key == curses.KEY_HOME:
                position = 0
            elif key == curses.KEY_END:
                position = len(order) - 1
            else:
                position = max(0, min(len(order) - 1, position + scroll_step(key, page)))


def object_layout(session, name, selected, offset, width, height):
    """A persistent object catalogue, drilled down into nights and instruments."""
    rows = session.object_rows(name)
    selected = min(selected, max(0, len(rows) - 1))
    page = max(1, height - 8)
    offset = max(0, min(offset, selected, max(0, len(rows) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    table = Table(expand=True, box=box.ROUNDED, border_style='dim cyan', header_style='bold cyan')
    if name:
        table.add_column('Night', width=10, no_wrap=True)
    else:
        table.add_column('Object', ratio=2, no_wrap=True, overflow='ellipsis')
    if name:
        table.add_column('Instrument', ratio=1, no_wrap=True, overflow='ellipsis')
    else:
        table.add_column('Nights', width=6, justify='right')
    table.add_column('Files', width=5, justify='right')
    if width >= 60:
        table.add_column('Missing', width=7, justify='right')
    for i in range(offset, min(len(rows), offset + page)):
        row = rows[i]
        cells = [Text(printable(row['night'] if name else row['target']), style='bold'),
                 Text(printable(row['instrument']) if name else str(row['nights'])), Text(str(row['files']))]
        if width >= 60:
            cells.append(Text(str(row['missing']), style='yellow' if row['missing'] else 'dim'))
        table.add_row(*cells, style='reverse' if i == selected else '' if row['enabled'] else 'dim')
    detail = 'No remembered objects. /scan discovers data; /load also registers files.'
    if rows:
        row = rows[selected]
        detail = row['command'] if row['enabled'] else 'Files missing: restore them before loading this group.'
    title = Text('Objects in memory' if name is None else name, style='bold', no_wrap=True, overflow='ellipsis')
    scope = Text('Choose an object to browse its nights.' if name is None else
                 'Nights: UTC calendar dates · Enter loads only this date and instrument.',
                 style='dim', no_wrap=True, overflow='ellipsis')
    footer = Text('↑↓ choose · Enter open · Esc back · /remove NAME forgets an object',
                  style='dim', no_wrap=True, overflow='ellipsis')
    return Group(title, scope, table, Text(printable(detail), style='cyan', no_wrap=True, overflow='ellipsis'), footer), selected, offset, page


def object_menu(screen, session, accent=0, name=None):
    selected = offset = 0
    console = make_console()
    with full_screen(screen):
        while True:
            try:
                height, width = screen.getmaxyx()
                screen.erase()
                layout, selected, offset, page = object_layout(session, name, selected, offset,
                                                               max(1, width - 4), max(1, height - 2))
                if width >= 44 and height >= 12:
                    paint_rich(screen, console, layout, 1, 2, width - 4, height - 2, accent)
                else:
                    screen.addnstr(0, 0, 'Enlarge terminal (44 × 12) · Esc back', max(0, width - 1))
                screen.refresh()
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key in QUIT_KEYS:
                return ''
            if key == ESCAPE:
                if name is None:
                    return ''
                name, selected, offset = None, 0, 0
                continue
            rows = session.object_rows(name)
            if key in ENTER_KEYS and rows:
                row = rows[selected]
                if name is None:
                    name, selected, offset = row['target'], 0, 0
                elif row['enabled']:
                    return row['command']
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(rows) - 1)
            else:
                selected = max(0, selected + scroll_step(key, page))


def suggestion_layout(session, tab, selected, offset, width, height, scanning=False):
    """Show both the reason for an action and the FITS evidence behind it."""
    rows = session.suggestions() if tab == 'Actions' else session.catalog or []
    page = max(1, height - 11)
    selected = min(selected, max(0, len(rows) - 1))
    offset = max(0, min(offset, selected, max(0, len(rows) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    table = Table(expand=True, box=box.ROUNDED, padding=(0, 1),
                  header_style='bold cyan', border_style='dim cyan')
    table.add_column('#', width=3, justify='right', no_wrap=True)
    if tab == 'Actions':
        table.add_column('Suggested action', ratio=2, no_wrap=True, overflow='ellipsis')
        if width >= 60:
            table.add_column('Why', ratio=3, no_wrap=True, overflow='ellipsis')
    else:
        table.add_column('File', ratio=2, no_wrap=True, overflow='ellipsis')
        if width >= 60:
            table.add_column('Instrument / target', ratio=2, no_wrap=True, overflow='ellipsis')
        if width >= 42:
            table.add_column('Tellurics', width=14, no_wrap=True)
    statuses = {'model': 'Model found', 'missing': 'Unknown', 'invalid': 'Invalid model', 'n/a': '—'}
    for i in range(offset, min(len(rows), offset + page)):
        row = rows[i]
        cells = [Text(str(i + 1))]
        if tab == 'Actions':
            cells.append(Text(printable(row['title']), style='bold' if row['enabled'] else 'dim'))
            if width >= 60:
                cells.append(Text(printable(row['reason'])))
        else:
            cells.append(Text(printable(row['name']), style='bold'))
            if width >= 60:
                cells.append(Text(printable(row['instrument'] + ' · ' + ', '.join(row['targets']))))
            if width >= 42:
                cells.append(Text(statuses[row['tellurics']]))
        table.add_row(*cells, style='reverse' if i == selected else '')
    detail = Text('Scanning FITS headers…' if scanning else f'{len(session.catalog or [])} FITS files discovered.', style='dim')
    command = Text('')
    if rows:
        row = rows[selected]
        detail = Text(printable(row['reason']), overflow='ellipsis')
        command = Text(printable(row['command'] if tab == 'Actions' else row['path']),
                       style='bold cyan' if tab == 'Actions' else 'dim', no_wrap=True, overflow='ellipsis')
    tabs = Text('Actions  /  Files · Tab switches', style='dim')
    tabs.stylize('bold cyan', 0 if tab == 'Actions' else 11, 7 if tab == 'Actions' else 16)
    detail = Group(*detail.wrap(make_console(), max(1, width))[:2])
    layout = Group(Text('GEISHA / assistant suggestions', style='bold'), tabs,
                   Text(printable(str(session.data_root)), style='dim', no_wrap=True, overflow='ellipsis'),
                   table, detail, command,
                   Text('↑↓ choose · Enter / 1–9 run · R rescan · Esc back', style='dim', no_wrap=True, overflow='ellipsis'))
    return layout, selected, offset, page


def suggestion_menu(screen, session, accent=0, poll=None):
    tab, selected, offset = 'Actions', 0, 0
    console = make_console()
    with full_screen(screen):
        screen.timeout(100)  # Discovery completes without requiring a keystroke.
        while True:
            scanning = poll() if poll else False
            try:
                height, width = screen.getmaxyx()
                layout, selected, offset, page = suggestion_layout(
                    session, tab, selected, offset, max(1, width - 4), max(1, height - 2), scanning)
                screen.erase()
                if width >= 12 and height >= 5:
                    paint_rich(screen, console, layout, 1, 2, width - 4, height - 2, accent)
                screen.refresh()
                key = read_key(screen, 100)
            except curses.error:
                continue
            if key in QUIT_KEYS or key == ESCAPE:
                return ''
            if key in ('r', 'R'):
                return '/scan'
            if key in ('\t', curses.KEY_BTAB, curses.KEY_LEFT, curses.KEY_RIGHT):
                tab = 'Files' if tab == 'Actions' else 'Actions'
                selected = offset = 0
            elif tab == 'Actions' and (key in ENTER_KEYS or isinstance(key, str) and key in '123456789'):
                actions = session.suggestions()
                index = int(key) - 1 if isinstance(key, str) and key in '123456789' else selected
                if index < len(actions) and actions[index]['enabled']:
                    return actions[index]['command']
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(session.suggestions() if tab == 'Actions' else session.catalog or []) - 1)
            else:
                selected = max(0, selected + scroll_step(key, page))


def help_menu(screen, theme):
    """Search both command names and their one-sentence descriptions."""
    query = ''
    selected = offset = 0
    while True:
        matches = [name for name, description in COMMANDS.items()
                   if query.casefold() in f'{name} {description}'.casefold()]
        selected = min(selected, max(0, len(matches) - 1))
        try:
            offset = draw_panel(
                screen, 'GEISHA / help', 'Type to find a command; Enter runs the selection.',
                [f'{name:<10} {COMMANDS[name]}' for name in matches], selected, offset,
                '↑↓ select · Enter run · Esc back', query, theme.accent,
            )
            key = read_key(screen)
        except curses.error:
            continue
        if key == ESCAPE:
            return ''
        if key in QUIT_KEYS:
            return '/exit'
        if key in ENTER_KEYS:
            if matches:
                return matches[selected]
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(max(0, len(matches) - 1), selected + 1)
        else:
            query, state = edit_query(key, query)
            if state == 'edit':
                selected = offset = 0


def color_menu(screen, theme, splash):
    """Edit a draft; only Enter saves it. Escape leaves the theme untouched."""
    draft = theme.settings.copy()
    selected = offset = 0
    notice = 'Seasonal logo: cyan normally, white/red in October.'
    while True:
        preview = Theme(draft, theme.attributes)
        preset = next((name for name, values in THEMES.items() if values == draft), 'Custom')
        labels = ['Theme       ' + preset] + [f'{part.title():<12}{draft[part]}' for part in COLOR_PARTS]
        try:
            offset = draw_panel(screen, 'GEISHA / color', notice, labels, selected, offset,
                                '↑↓ choose · ←→ change · P preview · Enter save · Esc cancel',
                                status='Changes are saved when you press Enter.')
            rows, columns = screen.getmaxyx()
            for index in range(1, len(labels)):
                y = 3 + index - offset
                if 3 <= y < min(rows - 4, 3 + max(1, rows - 7)) and columns > 4:
                    style = preview.style(COLOR_PARTS[index - 1]) | curses.A_BOLD
                    if index == selected:
                        style |= curses.A_REVERSE
                    screen.addnstr(y, 3, labels[index], columns - 4, style)
            screen.refresh()
            key = read_key(screen)
        except curses.error:
            continue
        if key == ESCAPE:
            return ''
        if key in QUIT_KEYS:
            return '/exit'
        if key == curses.KEY_UP:
            selected = (selected - 1) % len(labels)
        elif key == curses.KEY_DOWN:
            selected = (selected + 1) % len(labels)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            direction = 1 if key == curses.KEY_RIGHT else -1
            if selected == 0:
                choices = list(THEMES)
                index = choices.index(preset) if preset in choices else (-1 if direction == 1 else 0)
                draft = THEMES[choices[(index + direction) % len(choices)]].copy()
            else:
                part = COLOR_PARTS[selected - 1]
                choices = (('seasonal',) + COLOR_NAMES) if part == 'logo' else COLOR_NAMES
                draft[part] = choices[(choices.index(draft[part]) + direction) % len(choices)]
        elif key in ('p', 'P'):
            while True:
                try:
                    draw_screen(screen, preview, splash, elapsed=time.monotonic(),
                                message='Theme preview · press any key to return')
                    screen.get_wch()
                    break
                except curses.error:
                    continue
        elif key in ENTER_KEYS:
            try:
                save_colors(draft)
            except OSError:
                notice = 'Could not save colors. Try again, or Esc to cancel.'
                continue
            theme.settings.update(draft)
            return ''


def science_panel(screen, command, lines, start=None):
    """Wrap prose on resize; scroll table columns without breaking their rows."""
    selected = offset = None
    column = 0
    # Tables and trees mean nothing once rewrapped: scroll them sideways instead.
    table_lines = [str(line) for line in lines if is_wide(line)]
    while True:
        try:
            width = max(1, screen.getmaxyx()[1] - 6)
            max_column = max(0, max((len(line) for line in table_lines), default=0) - width)
            column = min(column, max_column)
            wrapped = [[str(line)[column:column + width]] if is_wide(line)
                       else textwrap.wrap(str(line), width) or [''] for line in lines]
            labels = [part for group in wrapped for part in group] or ['No previous messages yet.']
            if selected is None:
                selected = len(labels) - 1 if start is None else sum(len(group) for group in wrapped[:start])
                offset = max(0, selected - max(1, screen.getmaxyx()[0] - 7) + 1) if start is None else selected
            selected = min(selected, max(0, len(labels) - 1))
            offset = draw_panel(screen, 'GEISHA / message history', command, labels, selected, offset,
                                '↑↓ scroll · ←→ shift tables and trees · PgUp/PgDn · Enter/Esc return' if table_lines else
                                '↑↓/wheel scroll · PgUp/PgDn page · Home/End · Enter/Esc return',
                                status=f'Line {selected + 1} / {len(labels)} · messages retained for this session')
            key = read_key(screen)
        except curses.error:
            continue
        if key in ENTER_KEYS or key == ESCAPE or key in QUIT_KEYS:
            return
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            column = max(0, min(max_column, column + (12 if key == curses.KEY_RIGHT else -12)))
        selected = max(0, min(len(labels) - 1, selected + scroll_step(key, max(1, screen.getmaxyx()[0] - 7))))
        if key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(labels) - 1
