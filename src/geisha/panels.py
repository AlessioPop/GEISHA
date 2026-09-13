"""List screens built on :func:`draw_panel`: /help, /color and the transcript."""

import curses
import textwrap
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text

from .home import draw_screen
from .session import COMMANDS
from .theme import COLOR_NAMES, COLOR_PARTS, THEMES, Theme, save_colors
from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, draw_panel, edit_query, read_key, scroll_step,
                  full_screen, make_console, paint_rich, printable)


def suggestion_layout(session, tab, selected, offset, width, height, scanning=False):
    """Show both the reason for an action and the FITS evidence behind it."""
    rows = session.suggestions() if tab == 'Actions' else session.catalog or []
    page = max(1, height - 8)
    selected = min(selected, max(0, len(rows) - 1))
    offset = max(0, min(offset, selected, max(0, len(rows) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    table = Table(expand=True, box=None, padding=(0, 1), pad_edge=False, header_style='dim')
    table.add_column('#', width=3, no_wrap=True)
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
            cells.append(Text(printable(row['name'])))
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
    """Browse the session transcript; rewrap on resize and retain every message."""
    selected = offset = None
    while True:
        try:
            width = max(1, screen.getmaxyx()[1] - 6)
            wrapped = [textwrap.wrap(str(line), width) or [''] for line in lines]
            labels = [part for group in wrapped for part in group] or ['No previous messages yet.']
            if selected is None:
                selected = len(labels) - 1 if start is None else sum(len(group) for group in wrapped[:start])
                offset = max(0, selected - max(1, screen.getmaxyx()[0] - 7) + 1) if start is None else selected
            selected = min(selected, max(0, len(labels) - 1))
            offset = draw_panel(screen, 'GEISHA / message history', command, labels, selected, offset,
                                '↑↓/wheel scroll · PgUp/PgDn page · Home/End · Enter/Esc return',
                                status=f'Line {selected + 1} / {len(labels)} · messages retained for this session')
            key = read_key(screen)
        except curses.error:
            continue
        if key in ENTER_KEYS or key == ESCAPE or key in QUIT_KEYS:
            return
        selected = max(0, min(len(labels) - 1, selected + scroll_step(key, max(1, screen.getmaxyx()[0] - 7))))
        if key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(labels) - 1
