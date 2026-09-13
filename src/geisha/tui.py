"""Terminal primitives shared by every screen: keys, scrolling, text and Rich.

Screens use curses for input and layout, and render Rich groups into the same
window with :func:`paint_rich`, so no ANSI escape ever reaches the terminal.
"""

import curses
from contextlib import contextmanager

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from .session import is_alert
from .theme import alert_attribute

ALERT_COLORS = ('red', 'bright_red')  # Rich names that mean 'this went wrong'.
ESCAPE = '\x1b'
ENTER_KEYS = ('\n', '\r', curses.KEY_ENTER)
BACK_KEYS = (curses.KEY_BACKSPACE, '\x7f', '\b')
QUIT_KEYS = ('\x03', '\x04')  # Ctrl-C and Ctrl-D close the current screen.
FRAME_MS = 30  # Smooth face and letter-by-letter speech; subviews block instead.


def make_console():
    """One Rich configuration: fixed width, no markup guessing, curses-safe colors."""
    return Console(width=80, color_system='standard', highlight=False)


@contextmanager
def full_screen(screen):
    """Hide the cursor and wait for input while a full-screen view is open."""
    previous = None
    try:
        previous = curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(-1)  # Redraw on input/resize; subviews are not animated.
    try:
        yield
    finally:
        screen.timeout(FRAME_MS)
        if previous is not None:
            try:
                curses.curs_set(previous)
            except curses.error:
                pass


def scroll_step(key, page=10):
    """Map keyboard and supported terminal mouse-wheel events to line motion."""
    if key == curses.KEY_MOUSE:
        try:
            state = curses.getmouse()[4]
        except curses.error:
            return 0
        if state & getattr(curses, 'BUTTON4_PRESSED', 0):
            return -3
        if state & getattr(curses, 'BUTTON5_PRESSED', 0):
            return 3
    return {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
            curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page}.get(key, 0)


def edit_query(key, query):
    """Apply one keystroke to a filter string.

    Returns the new query and one of 'clear' (Escape), 'done' (Enter),
    'edit' (the text changed) or 'other' (the caller still owns this key).
    """
    if key == ESCAPE:
        return '', 'clear'
    if key in ENTER_KEYS:
        return query, 'done'
    if key in BACK_KEYS:
        return query[:-1], 'edit'
    if isinstance(key, str) and key.isprintable():
        return query + key, 'edit'
    return query, 'other'


NEWLINE_KEY = 'newline'  # Synthetic key: Shift+Enter and Alt+Enter break a line.
# Enter with a modifier, as reported by kitty-style CSI-u and xterm modifyOtherKeys.
NEWLINE_SEQUENCES = {'13;2u', '13;3u', '13;4u', '13;5u', '13;6u',
                     '27;2;13~', '27;3;13~', '27;5;13~'}


def read_key(screen, restore=FRAME_MS):
    """Read one key, decoding the escape sequences terminals send for Shift+Enter.

    curses hands unknown sequences over one character at a time, so peek after
    Escape with a short timeout. A lone Escape still returns ESCAPE, and a key
    that starts no sequence is pushed back rather than swallowed.
    """
    key = screen.get_wch()
    if key != ESCAPE:
        return key
    screen.timeout(25)
    try:
        following = screen.get_wch()
    except curses.error:
        return ESCAPE  # Nothing followed, so this was a real Escape.
    finally:
        screen.timeout(restore)
    if following in ENTER_KEYS:  # Escape then Return: Alt/Shift+Enter.
        return NEWLINE_KEY
    if following == 'O':
        screen.get_wch()  # SS3 function key: swallow its final byte.
        return ESCAPE
    if following == '[':
        sequence = ''
        while len(sequence) < 12:
            char = screen.get_wch()
            if not isinstance(char, str):
                break
            sequence += char
            if '@' <= char <= '~':  # Final byte of a CSI sequence.
                break
        return NEWLINE_KEY if sequence in NEWLINE_SEQUENCES else ESCAPE
    try:
        curses.unget_wch(following)  # An ordinary key: keep it for the next read.
    except (curses.error, ValueError):
        pass
    return ESCAPE


def wrap_input(text, width):
    """Wrap typed text without losing or shifting any character.

    Breaks after the last space that fits so words stay whole, and starts a new
    row at every explicit line break. Returns (index of first character, row).
    """
    width = max(1, width)
    rows, index = [], 0
    for paragraph in text.split('\n'):
        cursor = 0
        while cursor < len(paragraph):
            if len(paragraph) - cursor <= width:
                rows.append((index + cursor, paragraph[cursor:]))
                cursor = len(paragraph)
            else:
                space = paragraph[cursor:cursor + width].rfind(' ')
                take = space + 1 if space > 0 else width
                rows.append((index + cursor, paragraph[cursor:cursor + take]))
                cursor += take
        if cursor == 0:
            rows.append((index, ''))
        index += len(paragraph) + 1  # The line break occupies one position.
    return rows


def cursor_cell(rows, cursor):
    """Locate the cursor in wrapped rows, returning its row and column."""
    for index, (start, line) in enumerate(rows):
        end = start + len(line)
        last = index == len(rows) - 1
        # A row ended by a typed line break also owns that break's position;
        # a row ended by wrapping hands the position to the next row.
        broken = not last and rows[index + 1][0] > end
        if cursor < end or last or (broken and cursor == end):
            return index, max(0, min(cursor - start, len(line)))
    return 0, 0


def printable(value):
    """Display text literally, including control characters and Rich markup."""
    return ''.join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def clip(value, width):
    """Shorten the middle of a path, keeping its filename/extension identifiable."""
    value = printable(value)
    if cell_len(value) <= width:
        return value
    if width < 5:
        text = Text(value)
        text.truncate(max(0, width), overflow='ellipsis')
        return text.plain
    tail_width = min(32, width // 2)
    tail = value[-tail_width:]
    while cell_len(tail) > tail_width:
        tail = tail[1:]
    head = Text(value)
    head.truncate(width - cell_len(tail) - 1)
    return head.plain + '…' + tail


def paint_rich(screen, console, layout, top, left, width, height, accent=0):
    """Paint Rich's cell-aware layout into curses without emitting ANSI escapes."""
    options = console.options.update(width=width, height=height)
    alert = alert_attribute()
    for y, segments in enumerate(console.render_lines(layout, options, pad=True)[:height], start=top):
        x = left
        for segment in segments:
            if segment.control:
                continue
            style = segment.style
            attribute = 0
            if style:
                if style.bold:
                    attribute |= curses.A_BOLD
                if style.dim:
                    attribute |= curses.A_DIM
                if style.reverse:
                    attribute |= curses.A_REVERSE
                if style.color:
                    # Every colour follows the theme's accent; red is the one
                    # exception, reserved for trouble and kept neon everywhere.
                    attribute |= alert if style.color.name in ALERT_COLORS else accent
            screen.addstr(y, x, segment.text, attribute)
            x += segment.cell_length


def draw_panel(screen, title, subtitle, labels, selected, offset, footer, query=None, accent=0, status=None):
    """Draw a scrollable list panel; keep the selected row visible after resizing."""
    rows, columns = screen.getmaxyx()
    screen.erase()

    def put(y, value, style=0):
        if 0 <= y < rows and columns > 2:
            screen.addnstr(y, 1, value, columns - 2, style)

    height = max(1, rows - 7)
    offset = max(0, min(offset, selected, max(0, len(labels) - height)))
    if selected >= offset + height:
        offset = selected - height + 1
    put(0, title, curses.A_BOLD)
    # A panel that opens on bad news says so in its subtitle; keep that red too.
    put(1, subtitle, alert_attribute() if is_alert(subtitle) else curses.A_DIM)
    put(2, '─' * max(0, columns - 2), curses.A_DIM)
    for index in range(offset, min(len(labels), offset + height)):
        style = curses.A_REVERSE if index == selected else 0
        if is_alert(labels[index]):
            style |= alert_attribute()  # A failure stays findable while scrolling.
        put(3 + index - offset, ('> ' if index == selected else '  ') + labels[index], style)
        if query is not None and columns > 4 and 3 + index - offset < rows:
            name = labels[index].split()[0]
            screen.addnstr(3 + index - offset, 3, name, columns - 4, style | accent | curses.A_BOLD)
    if not labels:
        put(3, '  No matching commands.', curses.A_DIM)
    put(rows - 3, '─' * max(0, columns - 2), curses.A_DIM)
    if query is not None:
        search = 'Search: ' + query
        put(rows - 2, search[-max(1, columns - 2):])
    else:
        put(rows - 2, status or f'{selected + 1} / {len(labels)}', curses.A_DIM)
    put(rows - 1, footer, curses.A_DIM)
    if query is not None:
        screen.move(max(0, rows - 2), min(columns - 1, 1 + len(search)))
    else:
        screen.move(0, 0)
    screen.refresh()
    return offset
