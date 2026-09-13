"""Read-only browser over the header snapshots held by the active session."""

import curses
from pathlib import Path

from rich.cells import cell_len
from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, clip, edit_query, full_screen,
                  make_console, paint_rich, printable, read_key, scroll_step)


def header_layout(session, level, file_index, hdu_index, query, selected, offset,
                  width, height, searching=False):
    """File → HDU → header cards, with full values available in a detail view."""
    path = session.files[file_index]
    hdus = session.headers[path]
    hdu = hdus[hdu_index]
    matches = [(i, card) for i, card in enumerate(hdu['cards'])
               if query.casefold() in ' '.join(card).casefold()]
    items = session.files if level == 'files' else hdus if level == 'hdus' else matches
    selected = min(selected, max(0, len(items) - 1))
    page = max(1, height - 10)
    offset = max(0, min(offset, selected, max(0, len(items) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    title = Table.grid(expand=True)
    title.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    title.add_column(no_wrap=True, justify='right')
    title.add_row(Text('Active files' if level == 'files' else 'FITS headers', style='bold'),
                  Text(f'{len(session.files)} file' + ('s' if len(session.files) != 1 else '') + ' · in memory', style='cyan'))
    location = Text(clip(session.files[selected] if level == 'files' else path, width), style='dim', no_wrap=True)
    if level == 'cards':
        context = f'HDU {hdu_index} · {hdu["name"]} · {len(matches)}/{len(hdu["cards"])} cards'
    else:
        context = ('Choose a file to inspect' if level == 'files' else
                   f'File {file_index + 1}/{len(session.files)} · choose an HDU')
    hint = Text(clip(context, width), style='dim', no_wrap=True)
    if searching or query:
        hint = Text('Find: ', style='cyan')
        hint.append(clip(query, max(1, width - 8)) + ('▏' if searching else ''))
    table = Table(expand=True, box=None, padding=(0, 1), pad_edge=False,
                  header_style='dim', show_edge=False)
    table.add_column('#', width=4, justify='right', no_wrap=True)
    if level == 'cards':
        key_width = min(max((cell_len(printable(card[0])) for _, card in matches), default=7),
                        max(7, width // 3), 32)
        table.add_column('Keyword', width=max(7, key_width), no_wrap=True, overflow='ellipsis')
        table.add_column('Value', ratio=1, no_wrap=True, overflow='ellipsis')
        if width >= 78:
            table.add_column('Comment', width=width // 3, no_wrap=True, overflow='ellipsis')
    else:
        table.add_column('File' if level == 'files' else 'Extension', ratio=1, no_wrap=True, overflow='ellipsis')
        if level == 'hdus' and width >= 72:
            table.add_column('Instrument / type', width=24, no_wrap=True, overflow='ellipsis')
        table.add_column('HDUs' if level == 'files' else 'Cards', width=6, justify='right', no_wrap=True)
    for index in range(offset, min(len(items), offset + page)):
        focus = index == selected
        item = items[index]
        if level == 'files':
            cells = [Text(str(index + 1)), Text(clip(Path(item).name, max(8, width - 15))),
                     Text(str(len(session.headers[item])))]
        elif level == 'hdus':
            cards = {keyword: value for keyword, value, _ in item['cards']}
            label = item['name'] + (f' (v{cards["EXTVER"]})' if 'EXTVER' in cards else '')
            cells = [Text(str(index)), Text(printable(label), style='bold')]
            if width >= 72:
                cells.append(Text(printable(cards.get('INSNAME', item['kind'])), style='' if focus else 'dim'))
            cells.append(Text(str(len(item['cards']))))
        else:
            number, (keyword, value, comment) = item
            cells = [Text(str(number + 1), style='' if focus else 'dim'),
                     Text(printable(keyword or '(blank)'), style='bold' if focus else 'cyan'),
                     Text(printable(value))]
            if width >= 78:
                cells.append(Text(printable(comment), style='' if focus else 'dim'))
        table.add_row(*cells, style='reverse' if focus else '')
    if not items:
        table.add_row(Text(''), Text('No matching cards.', style='dim'))
    for _ in range(max(0, page - max(1, len(items[offset:offset + page])))):
        table.add_row(Text(' '))
    if level == 'cards' and matches:
        number, (keyword, value, comment) = matches[selected]
        detail = Text(clip(f'{keyword or "(blank)"} = {value}', width), no_wrap=True)
        extra = Text(clip(comment or 'No comment · Enter shows the complete card', width), style='dim', no_wrap=True)
    else:
        detail = Text('Header browsing keeps the active dataset unchanged.', style='dim', no_wrap=True, overflow='ellipsis')
        extra = Text('No matching cards. Edit / find or press Esc to clear.' if not items else
                     f'{selected + 1}/{len(items)} · Enter to inspect', style='dim', no_wrap=True)
    if searching:
        controls = 'Type to search · Enter done · Esc clear'
    elif level == 'cards':
        controls = ('Enter detail · / find · Esc back · Q close' if width < 72 else
                    'Enter full card · / find · [ ] HDU · Esc back · Q close')
    else:
        controls = '↑↓ move · Enter open · Esc back · Q close'
    footer = Text(controls, no_wrap=True, overflow='ellipsis')
    footer.highlight_words(['Enter', '/', '[ ]', 'Esc', 'Q', '↑↓'], 'cyan')
    return Group(title, location, hint, Rule(style='dim'), table, Rule(style='dim'), detail, extra, footer), offset, page, matches


def card_view(screen, console, card, number, accent=0, context=''):
    """Scroll the entire keyword/value/comment without shortening scientific metadata.

    Returns True when the user asked to close the whole browser.
    """
    offset = 0
    while True:
        rows, columns = screen.getmaxyx()
        width, height = max(1, columns - 4), max(1, rows - 7)
        content = Group(*[Group(Text(label, style='cyan'), Text(printable(value)), Text(''))
                          for label, value in zip(('Keyword', 'Value', 'Comment'), card)])
        lines = console.render_lines(content, console.options.update(width=width), pad=True)
        offset = max(0, min(offset, max(0, len(lines) - height)))
        screen.erase()
        try:
            if rows >= 8 and columns >= 36:
                paint_rich(screen, console, Text(f'Header card {number} · complete content', style='bold'), 1, 2, width, 1, accent)
                paint_rich(screen, console, Text(clip(context, width), style='dim'), 2, 2, width, 1, accent)
                # Reuse Rich Text segments so wrapped content keeps its exact ordering.
                visible = Group(*[Text.assemble(*[(s.text, s.style) for s in line]) for line in lines[offset:offset + height]])
                paint_rich(screen, console, visible, 4, 2, width, height, accent)
                paint_rich(screen, console, Text('↑↓/PgUp/PgDn scroll · Home/End · Esc back', style='dim', no_wrap=True), rows - 2, 2, width, 1, accent)
            else:
                screen.addnstr(0, 0, 'Enlarge terminal · Esc back', max(0, columns - 1))
            screen.refresh()
            key = read_key(screen, -1)
        except curses.error:
            continue
        if key == ESCAPE or key in ENTER_KEYS:
            return False
        if key in ('q', 'Q') or key in QUIT_KEYS:
            return True
        if key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max(0, len(lines) - height)
        else:
            offset = max(0, offset + scroll_step(key, height))


def browse_headers(screen, session, accent=0, files_first=False):
    """Inspect only the in-memory headers belonging to the current science session."""
    session.require_data()
    file_index = hdu_index = selected = offset = 0
    level = 'files' if files_first or len(session.files) > 1 else 'hdus'
    query = ''
    searching = False
    console = make_console()
    with full_screen(screen):
        while True:
            rows, columns = screen.getmaxyx()
            small = rows < 14 or columns < 44
            try:
                screen.erase()
                if small:
                    screen.addnstr(0, 0, 'Enlarge terminal (44 × 14) · Esc back', max(0, columns - 1))
                    page, matches = 1, []
                else:
                    layout, offset, page, matches = header_layout(
                        session, level, file_index, hdu_index, query, selected, offset,
                        columns - 4, rows - 2, searching)
                    paint_rich(screen, console, layout, 1, 2, columns - 4, rows - 2, accent)
                screen.refresh()
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key in QUIT_KEYS:
                return
            if small:
                if key == ESCAPE or key in ('q', 'Q'):
                    return
                continue
            if searching:
                query, state = edit_query(key, query)
                searching = state not in ('clear', 'done')
                selected = offset = 0
                continue
            if key in ('q', 'Q'):
                return
            hdus = session.headers[session.files[file_index]]
            count = len(session.files) if level == 'files' else len(hdus) if level == 'hdus' else len(matches)
            selected = min(selected, max(0, count - 1))
            if key == ESCAPE or key == curses.KEY_LEFT:
                if query:
                    query = ''
                    selected = offset = 0
                elif level == 'cards':
                    level, selected, offset = 'hdus', hdu_index, 0
                elif level == 'hdus':
                    level, selected, offset = 'files', file_index, 0
                else:
                    return
            elif key == '/' and level == 'cards':
                searching = True
            elif key in ('[', ']') and level == 'cards':
                hdu_index = max(0, min(len(hdus) - 1, hdu_index + (1 if key == ']' else -1)))
                selected = offset = 0
                query = ''
            elif (key in ENTER_KEYS or key == curses.KEY_RIGHT) and count:
                if level == 'files':
                    file_index, hdu_index, level = selected, 0, 'hdus'
                elif level == 'hdus':
                    hdu_index, level = selected, 'cards'
                elif card_view(screen, console, matches[selected][1], matches[selected][0] + 1, accent,
                               f'File {file_index + 1}/{len(session.files)} · {Path(session.files[file_index]).name} · HDU {hdu_index}'):
                    return
                else:
                    continue
                selected = offset = 0
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, count - 1)
            else:
                selected = max(0, min(max(0, count - 1), selected + scroll_step(key, page)))
