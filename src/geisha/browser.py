"""Lazy file browser shared by /tree (read only) and /load (file selection)."""

import curses
import os
from pathlib import Path

from rich.console import Console, Group
from rich.filesize import decimal as file_size
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .constants import is_fits
from .tui import (ENTER_KEYS, ESCAPE, QUIT_KEYS, clip, edit_query, full_screen,
                  make_console, paint_rich, printable, read_key, scroll_step)

# The repository is two directories above the folder containing this file.
TREE_ROOT = Path(__file__).resolve().parent.parent.parent


def read_folder(path):
    """Read one level only. Links are shown, but never followed."""
    with os.scandir(path) as entries:
        children = [(Path(entry.path), entry.is_dir(follow_symlinks=False), entry.is_symlink())
                    for entry in entries]
    return sorted(children, key=lambda child: (not child[1], child[0].name.casefold()))


def tree_rows(root, expanded, children):
    """Build visible branches from folders the user has already opened."""
    result = []
    stack = [(root, True, False, '', '', 0)]
    while stack:
        path, directory, link, prefix, branch, depth = stack.pop()
        marker = ('▾ ' if path in expanded else '▸ ') if directory else '  '
        name = (path.name or str(path)) + ('/' if directory else '@' if link else '')
        result.append((path, prefix + branch + marker + name, depth, directory))
        if path in expanded:
            entries = children.get(path, [])
            continuation = prefix + ('   ' if branch == '└─ ' else '│  ') if depth else ''
            for index in range(len(entries) - 1, -1, -1):
                child, is_dir, is_link = entries[index]
                connector = '└─ ' if index == len(entries) - 1 else '├─ '
                stack.append((child, is_dir, is_link, continuation, connector, depth + 1))
    return result


def browser_layout(root, entries, selected, offset, height, width, *, selecting=False,
                   checked=frozenset(), query='', searching=False, show_hidden=False,
                   show_help=False, message='', metadata=None):
    """Compose a quiet Rich view; render only the current page of the lazy tree."""
    metadata = metadata if metadata is not None else {}
    page = max(1, height - 9 - (3 if show_help else 0))
    offset = max(0, min(offset, selected, max(0, len(entries) - page)))
    if selected >= offset + page:
        offset = selected - page + 1
    header = Table.grid(expand=True, padding=(0, 1))
    header.add_column(ratio=1, overflow='ellipsis', no_wrap=True)
    header.add_column(justify='right', no_wrap=True)
    header.add_row(Text('Load observations' if selecting else 'Explore files', style='bold'),
                   Text(f'{len(checked)} selected' if selecting else 'GEISHA', style='cyan' if checked else 'dim'))
    location = Text(clip(root, width), style='dim', no_wrap=True)
    if searching or query:
        hint = Text('Filter files: ', style='cyan')
        hint.append(clip(query, max(1, width - 15)) + ('▏' if searching else ''))
    else:
        hint = Text('FITS files · / filter · . hidden' if selecting else 'Read only · . hidden files',
                    style='dim', no_wrap=True, overflow='ellipsis')
    listing = Table.grid(expand=True, padding=0)
    listing.add_column(ratio=1, no_wrap=True, overflow='crop')
    if width >= 76:
        listing.add_column(width=12, justify='right', no_wrap=True)
    if width >= 56:
        listing.add_column(width=10, justify='right', no_wrap=True)
    name_width = max(1, width - (12 if width >= 76 else 0) - (10 if width >= 56 else 0))
    for index in range(offset, min(len(entries), offset + page)):
        path, label, depth, directory = entries[index]
        focus = index == selected
        prefix = '› ' if focus else '  '
        if selecting:
            prefix += ('[✓] ' if path in checked else '[ ] ') if not directory else '    '
        row = Text()
        row.append(prefix, style='bold' if focus else 'cyan' if path in checked else 'dim')
        if depth < 0:
            row.append('↑  Parent folder', style='' if focus else 'dim')
        else:
            name = (path.name or str(path)) + ('/' if directory else '@' if path.is_symlink() else '')
            # The label starts with hierarchy guides; style them independently.
            guide = label[:-len(name)]
            # Very deep folders must leave enough space for the filename.
            row.append(clip(guide, min(len(guide), max(0, name_width // 3))), style='' if focus else 'dim')
            row.append(clip(name, max(1, name_width - row.cell_len - 1)),
                       style='bold' if directory or focus else 'cyan' if path in checked else '')
        if path not in metadata:
            try:
                size = file_size(path.stat(follow_symlinks=False).st_size) if not directory else ''
            except OSError:
                size = '—'
            kind = ('Folder' if directory else 'Link' if path.is_symlink() else
                    'FITS · gz' if is_fits(path) and path.suffix.lower() == '.gz' else
                    'FITS' if is_fits(path) else 'File')
            metadata[path] = kind, size
        kind, size = metadata[path]
        cells = [row]
        if width >= 76:
            cells.append(Text('' if directory else kind, style='' if focus else 'dim'))
        if width >= 56:
            cells.append(Text(size, style='' if focus else 'dim'))
        listing.add_row(*cells, style='reverse' if focus else '')
    for _ in range(max(0, page - len(entries[offset:offset + page]))):
        listing.add_row(Text(' '))
    visible_files = {path for path, _, _, directory in entries if not directory}
    outside = len(checked - visible_files)
    counts = f'{len(visible_files)} {"FITS " if selecting else ""}file' + ('s' if len(visible_files) != 1 else '') + ' listed'
    if outside:
        counts += f' · {outside} selected outside view'
    elif not visible_files:
        counts = 'No matching files in open folders.' if query else 'No files in open folders.'
    status = Table.grid(expand=True)
    status.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    status.add_column(justify='right', no_wrap=True)
    status.add_row(Text(message or counts, style='cyan' if message else 'dim'),
                   Text(f' {selected + 1}/{len(entries)}', style='dim'))
    detail = Text(clip(entries[selected][0], width), style='dim', no_wrap=True)
    enter_action = 'load' if checked or not entries[selected][3] else 'open'
    if width < 50 and not searching:
        controls = f'Space mark · Enter {enter_action} · Esc' if selecting else 'Enter open · / filter · ? · Esc'
    elif searching:
        controls = 'Type to filter · Enter done · Esc clear'
    elif selecting:
        controls = (f'Space mark · Enter {enter_action} · ? keys · Esc cancel' if width < 76 else
                    f'↑↓ move · ←→ folders · Space mark · Enter {enter_action} · ? keys · Esc cancel')
    else:
        controls = ('Enter toggle · / filter · ? keys · Esc back' if width < 76 else
                    '↑↓ move · ←→ folders · Enter toggle · / filter · ? keys · Esc back')
    footer = Text(controls, no_wrap=True, overflow='ellipsis')
    # Only key names carry the accent; the rest stays neutral.
    footer.highlight_words(['↑↓', '←→', 'Enter', 'Space', '/', '?', 'Esc'], 'cyan')
    content = [header, location, hint, Rule(style='dim'), listing, Rule(style='dim'), detail, status, footer]
    if show_help:
        content.extend(Text(line, style='dim', no_wrap=True, overflow='ellipsis') for line in (
            '/ filter files in open folders · . toggle hidden (' + ('shown' if show_hidden else 'hidden') + ')',
            'PgUp/PgDn or wheel · Home/End jump · Enter open/load' if selecting else 'PgUp/PgDn or wheel · Home/End jump · Enter open',
            'A mark visible · C clear · L also loads' if selecting else 'Links are displayed, never followed · ? close keys',
        ))
    elif not searching and show_hidden:
        hint.append(' · hidden shown')
    return Group(*content), offset, page


def draw_browser(screen, console, *args, accent=0, **kwargs):
    """Paint the layout, or ask for a larger terminal; returns offset and page size."""
    rows, columns = screen.getmaxyx()
    screen.erase()
    if rows < 12 or columns < 36:
        screen.addnstr(0, 0, 'Enlarge terminal (36 × 12) · Esc back', max(0, columns - 1))
        screen.refresh()
        return args[3], 1
    width, height = columns - 4, rows - 2
    layout, offset, page = browser_layout(*args, height, width, **kwargs)
    paint_rich(screen, console, layout, 1, 2, width, height, accent)
    screen.refresh()
    return offset, page


def browse_files(screen, root, *, selecting=False, accent=0):
    """Shared lazy browser with persistent marks and a nonrecursive filename filter.

    Returns the marked paths when ``selecting``, otherwise '' or '/exit'.
    """
    root = Path(root).expanduser().resolve()
    children, expanded, checked, metadata = {}, set(), set(), {}
    selected = offset = 0
    query = message = ''
    show_hidden = searching = show_help = False
    console = make_console()

    def read(path):
        """In selection mode list only folders and regular FITS files."""
        return [entry for entry in read_folder(path)
                if not selecting or (not entry[2] and (entry[1] or is_fits(entry[0].name)))]

    def open_folder(path):
        nonlocal message
        try:
            children[path] = read(path)
            expanded.add(path)
            return True
        except OSError as error:
            message = f'Cannot read folder: {error.strerror or error}'
            return False

    open_folder(root)
    with full_screen(screen):
        while True:
            visible = {folder: [entry for entry in entries
                                if (show_hidden or not entry[0].name.startswith('.'))
                                and (entry[1] or query.casefold() in entry[0].name.casefold())]
                       for folder, entries in children.items()}
            entries = tree_rows(root, expanded, visible)
            if root.parent != root:
                entries.insert(0, (root.parent, '↑ Parent folder', -1, True))
            selected = min(selected, len(entries) - 1)
            try:
                offset, page = draw_browser(
                    screen, console, root, entries, selected, offset, accent=accent,
                    selecting=selecting, checked=checked, query=query, searching=searching,
                    show_hidden=show_hidden, show_help=show_help, message=message, metadata=metadata)
                key = read_key(screen, -1)
            except curses.error:
                continue
            if key in QUIT_KEYS:
                return None if selecting else '/exit'
            if searching:
                query, state = edit_query(key, query)
                if state == 'other':
                    selected = max(0, min(len(entries) - 1, selected + scroll_step(key, page)))
                else:
                    searching = state == 'edit'
                    if state == 'edit':
                        selected = offset = 0
                continue
            if key == ESCAPE:
                return None if selecting else ''
            message = ''
            step = scroll_step(key, page)
            if step:
                selected = max(0, min(len(entries) - 1, selected + step))
                continue
            path, _, depth, directory = entries[selected]
            if key == '/':
                searching = True
            elif key == '?':
                show_help = not show_help
            elif key == '.':
                show_hidden = not show_hidden
                selected = offset = 0
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(entries) - 1
            elif selecting and key in ('c', 'C'):
                checked.clear()
            elif selecting and key in ('a', 'A'):
                checked.update(p for p, _, _, is_dir in entries if not is_dir)
            elif selecting and key == ' ' and not directory:
                checked.symmetric_difference_update({path})
            elif selecting and (key in ('l', 'L') or (key in ENTER_KEYS and (checked or not directory))):
                if checked:
                    return sorted(checked)
                if not directory:
                    return [path]
                message = 'Mark files with Space, or highlight one and press Enter.'
            elif key == curses.KEY_LEFT:
                if path in expanded:
                    expanded.remove(path)
                elif depth > 0:
                    selected = next(i for i, entry in enumerate(entries) if entry[0] == path.parent)
                elif root.parent != root:
                    selected = 0
            elif (key == curses.KEY_RIGHT or key in ENTER_KEYS) and directory:
                if depth == -1:
                    if open_folder(path):
                        root = path
                        selected = offset = 0
                elif path not in expanded:
                    open_folder(path)
                elif key != curses.KEY_RIGHT:
                    expanded.remove(path)
                elif selected + 1 < len(entries) and entries[selected + 1][2] > depth:
                    selected += 1
                else:
                    message = 'No visible entries. Use . for hidden files or / to change the filter.'


def browse_tree(screen, root=TREE_ROOT, accent=0):
    """Explore folders using the same layout and navigation as /load."""
    return browse_files(screen, root, accent=accent)


def pick_fits(screen, root=None, accent=0):
    """Return selected files on acceptance, or None; loading is a separate step."""
    return browse_files(screen, root or Path.cwd(), selecting=True, accent=accent)


def print_tree(root=TREE_ROOT):
    """One-level listing for plain or piped sessions, where no browser can open."""
    console = Console(highlight=False)
    try:
        tree = Tree(Text(printable(root), style='bold'), guide_style='dim')
        for path, directory, link in read_folder(root):
            if not path.name.startswith('.'):
                tree.add(Text(printable(path.name) + ('/' if directory else '@' if link else ''),
                              style='bold' if directory else ''))
        console.print(tree)
        console.print('Open an interactive terminal to expand folders or show hidden files.', style='dim')
    except OSError as error:
        print(f'Cannot read folder: {error.strerror or error}')
