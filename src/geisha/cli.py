"""GEISHA's entry point: the prompt loop and the plain-terminal fallback.

Module map — session.py holds every command and needs no terminal; tui.py has
the shared curses/Rich primitives; home.py draws the title screen; panels.py,
browser.py and headers.py are the full-screen views; theme.py stores colors.
"""

import curses
import contextlib
import os
import random
import shlex
import sys
import time
import threading
import queue
from pathlib import Path
from types import SimpleNamespace

# Direct execution (including an editor's Run button) needs package context for
# the deferred science imports, just like `python -m geisha`.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'geisha'

from .browser import pick_fits, browse_tree, print_tree
from .dataview import browse_data
from .headers import browse_headers
from .home import Companion, SPLASHES, STARTUP_TIP, draw_screen, input_geometry
from .panels import color_menu, help_menu, science_panel, suggestion_menu
from .plotui import build_plot
from .session import COMMANDS, ScienceSession, command_matches, science_output
from .theme import Theme
from .tui import (BACK_KEYS, ENTER_KEYS, ESCAPE, FRAME_MS, NEWLINE_KEY, QUIT_KEYS,
                  cursor_cell, make_console, read_key, scroll_step, wrap_input)


class Interface:
    """Title screen state, and the dispatch of commands that open a view."""

    def __init__(self, screen):
        self.screen = screen
        self.session = ScienceSession()
        self.theme = Theme()
        self.console = make_console()
        self.splash = random.choice(SPLASHES)
        self.started = time.monotonic()
        self.companion = Companion(self.started)
        self.finished = False
        self.discovery = queue.SimpleQueue()
        self.discovery_pending = True
        self.companion.set('neutral', 'Looking in your data folder… F2 opens my suggestions.')
        root = self.session.data_root
        def discover_data():
            try:
                from .io import scan_data
                result = scan_data(root)
            except Exception as error:
                result = ([], [f'Could not scan data: {error}'])
            self.discovery.put(result)
        threading.Thread(target=discover_data, name='geisha-discovery', daemon=True).start()

    def poll_discovery(self):
        """Publish a completed read-only scan on the UI thread."""
        if not self.discovery_pending:
            return False
        try:
            catalog, errors = self.discovery.get_nowait()
        except queue.Empty:
            return True
        self.discovery_pending = False
        self.session.catalog, self.session.scan_errors = catalog, errors
        if not self.session.files and self.companion.emotion not in ('sad', 'nervous', 'surprised'):
            if self.session.previous:
                count = len(self.session.previous['files'])
                missing = sum(not Path(p).is_file() for p in self.session.previous['files'])
                message = (f'Welcome back! {missing} previous file(s) are missing. F2 shows alternatives; /load can locate them.' if missing else
                           f'Welcome back! Continue with your {count} previous file(s)? F2 shows the resume action, or use /resume.')
            else:
                gravity = sum(any(i.startswith('GRAVITY_SC') for i in r['instruments']) for r in catalog)
                models = sum(r['tellurics'] == 'model' for r in catalog)
                message = f'I found {len(catalog)} FITS file(s) in data. '
                if gravity:
                    message += f'{gravity} are GRAVITY; {models} already have telluric models. '
                message += 'F2 opens my suggested next steps.'
            if errors:
                message += ' Some folders could not be scanned; /scan shows details.'
            self.companion.set('happy', message)
        return False

    def draw(self, text='', cursor=0, message='', suggestions=(), selected=0, data=None):
        try:
            draw_screen(self.screen, self.theme, self.splash, text, cursor, message,
                        time.monotonic() - self.started, suggestions, selected,
                        self.session if data is None else data, self.console, self.companion)
        except curses.error:
            pass  # A resize mid-frame simply redraws at the new size.

    @contextlib.contextmanager
    def loading(self, command):
        """Give the renderer exclusive terminal ownership while science runs.

        Science and Matplotlib stay on the main thread (GUI backends require it).
        The animation reads a metadata snapshot, never the changing science state.
        Stop and join it before any result screen or graphical viewer opens.
        """
        snapshot = SimpleNamespace(files=self.session.files, targets=self.session.targets,
                                   instruments=self.session.instruments, settings=dict(self.session.settings))
        self.companion.set('working', command.split()[0])
        stopped = threading.Event()
        previous_view = self.session.before_view

        def animate():
            while not stopped.is_set():
                try:
                    # SIGWINCH is handled by Python on the science thread; read
                    # the actual terminal dimensions so loading can still resize.
                    columns, rows = os.get_terminal_size(sys.__stdin__.fileno())
                    if self.screen.getmaxyx() != (rows, columns):
                        curses.resizeterm(rows, columns)
                except (OSError, ValueError, curses.error):
                    pass
                self.draw(data=snapshot)
                stopped.wait(FRAME_MS / 1000)

        painter = threading.Thread(target=animate, name='geisha-loading', daemon=True)

        def stop():
            stopped.set()
            painter.join()

        def before_view():
            stop()
            self.companion.set('happy', 'Your plot is ready! Close the plot window to return here.')
            self.draw()
            if previous_view:
                previous_view()

        self.session.before_view = before_view
        painter.start()
        try:
            yield
        finally:
            stop()
            self.session.before_view = previous_view
            self.companion.activity()

    def run(self, command):
        """Run one command line; returns a status message, or '' to keep the last one."""
        accent = self.theme.accent
        while command == '/help':
            command = help_menu(self.screen, self.theme)
        if command == '/suggest':
            while True:
                command = suggestion_menu(self.screen, self.session, accent, self.poll_discovery)
                if command != '/scan':
                    break
                # A manual refresh supersedes any outstanding startup scan.
                self.discovery_pending = False
                with self.loading('/scan'):
                    lines = science_output(self.session, '/scan')
                self.companion.result(lines)
            return self.run(command) if command else ''
        if command.startswith('/suggest '):
            parts = command.split()
            actions = self.session.suggestions()
            if len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(actions):
                action = actions[int(parts[1]) - 1]
                if action['enabled']:
                    return self.run(action['command'])
        if command == '/color':
            command = color_menu(self.screen, self.theme, self.splash)
        if command == '/tree':
            command = browse_tree(self.screen, accent=accent)
        if command == '/exit':
            self.finished = True
            return ''
        if command == '/history':
            science_panel(self.screen, 'Previous messages', self.session.history_lines())
            return ''
        if command == '/data' and self.session.files:
            return self.open_data()
        if command == '/headers' and self.session.files:
            browse_headers(self.screen, self.session, accent, files_first=True)
            return ''
        if command == '/plot' and self.session.files:
            command = build_plot(self.screen, self.session, accent=accent)
            if not command:
                return 'Plot cancelled; nothing was drawn.'
        if command == '/load':
            files = pick_fits(self.screen, accent=accent)
            if not files:
                return 'File selection cancelled; current data kept.'
            command = shlex.join(['/load', *(str(path) for path in files)])
        if not command:
            return ''
        return self.report(command)

    def report(self, command):
        """Run an analysis command, then show its output or the new dataset."""
        start = len(self.session.history_lines())
        previous_data = self.session.oi
        with self.loading(command):
            lines = science_output(self.session, command)
        self.companion.result(lines)
        if self.companion.emotion in ('sad', 'nervous', 'surprised'):
            return lines[0] if lines else 'See /history for details.'
        if self.session.oi is not previous_data:
            count = len(self.session.files)
            warned = any(line.startswith('Warning:') for line in lines)
            next_action = self.session.suggestions()[0]['title']
            message = (f'Loaded {count} file' + ('s' if count != 1 else '') + '. '
                       + ('Warning: see /history for details.' if warned else f'Next: {next_action}. F2 lets you choose.'))
            # Show what was loaded as tables rather than as a wall of text.
            return self.open_data(tab='Observables') or message
        if len(lines) > 3:
            science_panel(self.screen, command, self.session.history_lines(), start=start)
            self.companion.activity()
        return lines[0] if lines else 'Ready.'

    def open_data(self, tab='Files'):
        """Browse the dataset; P or Enter there hands a scoped plot to the builder."""
        seed = browse_data(self.screen, self.session, self.theme.accent, tab)
        if seed is None:
            return ''
        command = build_plot(self.screen, self.session, seed, self.theme.accent)
        return self.report(command) if command else 'Plot cancelled; nothing was drawn.'


def run_terminal(screen):
    """Animate and handle input together so resizing never loses typed text."""
    interface = Interface(screen)
    screen.timeout(FRAME_MS)
    screen.keypad(True)
    try:
        curses.mousemask(getattr(curses, 'BUTTON4_PRESSED', 0) | getattr(curses, 'BUTTON5_PRESSED', 0))
    except curses.error:
        pass
    text = message = draft = ''
    cursor = selected = 0
    dismissed = False
    entries, recall = [], None  # Submitted commands, and where ↑/↓ are browsing.
    while True:
        interface.poll_discovery()
        suggestions = (command_matches(text) if not dismissed and cursor == len(text)
                       and '\n' not in text and screen.getmaxyx()[0] >= 6 else [])
        selected = min(selected, max(0, len(suggestions) - 1))
        interface.draw(text, cursor, message, suggestions, selected)
        try:
            key = read_key(screen)
        except curses.error:
            continue  # No key yet: advance the animation.
        if key != curses.KEY_RESIZE:
            interface.companion.activity()
        if key in QUIT_KEYS:
            return
        previous_text = text
        if key == ESCAPE:
            dismissed = True
        elif key == curses.KEY_F2:
            message = interface.run('/suggest') or message
            interface.companion.activity()
            if interface.finished:
                return
        elif key in (curses.KEY_UP, curses.KEY_DOWN) and suggestions:
            selected = (selected + (1 if key == curses.KEY_DOWN else -1)) % len(suggestions)
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            # Move inside a wrapped command first, then recall earlier ones.
            lines = wrap_input(text, input_geometry(screen.getmaxyx()[1])[2])
            row, column = cursor_cell(lines, cursor)
            step = 1 if key == curses.KEY_DOWN else -1
            if 0 <= row + step < len(lines):
                start, line = lines[row + step]
                cursor = start + min(column, len(line))
            elif key == curses.KEY_UP and entries and (recall is None or recall > 0):
                if recall is None:
                    draft, recall = text, len(entries)
                recall -= 1
                text, cursor = entries[recall], len(entries[recall])
            elif key == curses.KEY_DOWN and recall is not None:
                recall += 1
                text = entries[recall] if recall < len(entries) else draft
                recall = recall if recall < len(entries) else None
                cursor = len(text)
        elif key == curses.KEY_PPAGE or (key == curses.KEY_MOUSE and scroll_step(key) < 0):
            science_panel(screen, 'Previous messages', interface.session.history_lines())
        elif key == NEWLINE_KEY:
            text = text[:cursor] + '\n' + text[cursor:]
            cursor += 1
        elif key == '\t' and suggestions:
            text = suggestions[selected]
            cursor = len(text)
            dismissed = True
        elif key in ENTER_KEYS:
            command = suggestions[selected] if suggestions else text.strip()
            if command and command != (entries[-1] if entries else None):
                entries.append(command)
            recall, draft = None, ''
            message = interface.run(command) or message
            interface.companion.activity()
            if message and message != interface.companion.message and interface.companion.emotion not in ('sad', 'nervous', 'surprised'):
                interface.companion.set('happy', message)
            if interface.finished:
                return
            text, cursor = '', 0
        elif key in BACK_KEYS:
            if cursor:
                text = text[:cursor - 1] + text[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            text = text[:cursor] + text[cursor + 1:]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(text), cursor + 1)
        elif key in (curses.KEY_HOME, '\x01'):
            cursor = 0
        elif key in (curses.KEY_END, '\x05'):
            cursor = len(text)
        elif isinstance(key, str) and key.isprintable():
            text = text[:cursor] + key + text[cursor:]
            cursor += 1
        if text != previous_text:
            selected = 0
            # Completion and a recalled command both leave the list closed.
            dismissed = key == '\t' or key in (curses.KEY_UP, curses.KEY_DOWN)
        # KEY_RESIZE falls through; the next frame uses the updated size.


def run_plain():
    """Scriptable session for pipes, dumb terminals and editors without curses."""
    session = ScienceSession()
    print(f'GEISHA\n{random.choice(SPLASHES)}\n{STARTUP_TIP}\n'
          '/help commands · /color theme · /tree repository · /exit quit')
    if session.previous:
        print(f"Continue with {len(session.previous['files'])} previous file(s)? Use /resume, or /suggest for other actions.")
    else:
        print('/suggest discovers the data folder and suggests next steps.')
    while True:
        command = input('› ').strip()
        if command == '/exit':
            return
        if command == '/help':
            print('\n'.join(f'{name:<10} {text}' for name, text in COMMANDS.items()))
        elif command == '/color':
            print('Open GEISHA in an interactive terminal to choose and preview colors.')
        elif command == '/tree':
            print_tree()
        elif command:
            print('\n'.join(science_output(session, command)))


def main():
    """Launch the themed interface, or a scriptable plain command session."""
    # Escape closes a view; ncurses otherwise waits a second for a longer sequence.
    # read_key does its own disambiguation, so a short delay loses nothing.
    os.environ.setdefault('ESCDELAY', '50')
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb'
    try:
        curses.wrapper(run_terminal) if interactive else run_plain()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        print('\nGoodbye.')


# Packaging entry point; retain main() for module execution.
app = main


if __name__ == '__main__':
    main()
