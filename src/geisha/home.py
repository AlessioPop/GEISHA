"""The title screen: wordmark, splash, active-data strip, prompt and footer."""

import curses
import math
import re
import textwrap
import time
from datetime import date
from pathlib import Path

from rich.cells import cell_len
from rich.console import Group
from rich.table import Table
from rich.text import Text

from .session import COMMANDS
from .tui import clip, cursor_cell, make_console, paint_rich, printable, wrap_input

# Short splash messages, chosen once per launch so redraws keep the same line.
# Instrument facts: ESO's VLTI, GRAVITY, METIS and ELT mirror pages.
# https://www.eso.org/sci/facilities/paranal/instruments/gravity.html
# https://www.eso.org/public/teles-instr/paranal-observatory/vlt/vlti/
# https://elt.eso.org/instrument/METIS/
# https://elt.eso.org/mirror/
# Historic observations: https://www.eso.org/public/news/eso1622/
# https://www.eso.org/public/news/eso2006/
# https://elt.eso.org/mirror/M4/
# https://www.eso.org/sci/facilities/paranal/instruments/matisse.html
# https://www.eso.org/public/news/eso1808/
# https://www.eso.org/sci/facilities/paranal/telescopes/vlti.html
SPLASHES = (
    "GRAVITY combines four telescope beams. Teamwork at light speed.",
    "Four telescopes give GRAVITY six baselines.",
    "The VLTI combines 8.2-m UTs or movable 1.8-m ATs.",
    "The longest VLTI Unit Telescope baseline is about 130 m.",
    "GRAVITY observes in the near-infrared K band.",
    "In 2016, GRAVITY observed the Galactic Centre with all four UTs.",
    "In 2020, GRAVITY helped reveal S2's relativistic orbital precession.",
    "S2 traces a rosette around our galaxy's central black hole.",
    "The ELT's 39-m primary mirror will use 798 hexagonal segments.",
    "ELT mirror segments: 798 pieces, one very demanding puzzle.",
    "METIS will bring mid-infrared imaging and spectroscopy to the ELT.",
    "METIS will study worlds nearby and active galaxies far away.",
    "Longer baselines resolve finer detail at the same wavelength.",
    "Interferometry: the space between telescopes matters too.",
    "Earth's rotation helps an interferometer sample new baselines on the sky.",
    "Fringe benefits included. Coherence required.",
    "Keep your friends close and your optical paths matched.",
    "Some assembly required. Especially for a 39-m telescope.",
    "May your fringes be stable and your skies clear.",
    "Good things come to those who calibrate.",
    "The ELT's M4 mirror will reshape itself up to 1,000 times a second.",
    "More than 5,000 actuators will flex the ELT's M4 mirror.",
    "The ELT's M5 mirror will help stabilise the image by tilting.",
    "METIS will measure wavefronts to guide the ELT's adaptive mirrors.",
    "MATISSE combines four telescope beams in the mid-infrared.",
    "MATISSE observes in the L, M and N infrared bands.",
    "MATISSE saw first light at the VLTI in 2018.",
    "VLTI delay lines equalise the paths travelled by starlight.",
    "PIONIER and GRAVITY combine beams in the near-infrared.",
    "A baseline has a direction as well as a length.",
    "A light-year measures distance. Your deadline still measures time.",
    "Looking farther into space means looking farther into the past.",
    "Atmospheric turbulence: the original blur filter.",
    "Please remain coherent while we combine your photons.",
    "My other telescope is an interferometer.",
    "Relationship status: phase-locked.",
    "Clouds have entered the chat. Astronomers have left.",
    "Warning: this conversation may contain Fourier transforms.",
    "The universe has no obligation to fit your model.",
    "Astronomy: where a dusty disc is exciting news.",
)
STARTUP_TIP = "Tip: type /, then Tab to complete a command."
PROMPT = "❯ "

# Expressions stay in the supplied outer shell. Routes start at the neutral hub;
# a blink bridges unrelated emotions before the eyes and mouth change in steps.
FACES = {
    'neutral': '(˶• - •˶)', 'smile': '(˶• ᵕ •˶)', 'happy': '(˶˃ ᵕ ˂˶)',
    'surprised': '(˶° ㅁ °˶)', 'shy': '(˶⸝⸝˘ ᵕ ˘⸝⸝˶)', 'sad': '(˶•́ ︵ •̀˶)',
    'crying': '(˶╥﹏╥˶)', 'angry': '(˶> ︿ <˶)', 'suspicious': '(˶¬‿¬˶)',
    'sleepy': '(˶－ω－˶)', 'asleep': '(˶－ω－˶) zzz', 'love': '(˶♡ ᵕ ♡˶)',
    'excited': '(˶✧ ᵕ ✧˶)', 'nervous': '(˶•﹏•˶)', 'scared': '(˶ᗒ﹏ᗕ˶)',
    'pouting': '(˶￣ ³￣˶)', 'silly': '(˶≧ڡ≦˶)', 'deadpan': '(˶－_－˶)',
    'proud': '(˶ᵔ⤙ᵔ˶)', 'working': '(˶• ᵕ •˶)',
}
BLINK = '(˶˘ ᵕ ˘˶)'
EMOTION_ROUTES = {
    'happy': ('(˶• ᵕ •˶)', '(˶ᵔ ᵕ ᵔ˶)', '(˶˃ ᵕ ᵔ˶)'),
    'surprised': ('(˶• ᵕ •˶)', '(˶• ㅁ •˶)'),
    'sad': ('(˶• ︵ •˶)',),
    'crying': ('(˶• ︵ •˶)', '(˶•́ ︵ •̀˶)', '(˶；︵；˶)', '(˶╥ ︵ ╥˶)'),
    'angry': ('(˶• ︿ •˶)', '(˶•̀ ︿ •́˶)'),
    'suspicious': ('(˶• _ •˶)', '(˶¬ _ •˶)', '(˶¬ _ ¬˶)'),
    'shy': ('(˶• ᵕ •˶)', '(˶• ᵕ •˶)♡', '(˶⸝⸝ ᵕ ⸝⸝˶)'),
    'sleepy': ('(˶• ᵕ •˶)', '(˶˘ ᵕ •˶)', BLINK, '(˶－ ᵕ －˶)'),
    'asleep': ('(˶－ω－˶) z', '(˶－ω－˶) zz'),
}
WORK_PHRASES = (
    'Working very hard…', 'Please remain coherent. I am trying my best.',
    'Counting photons. Please do not distract the photons.',
    'Giving these numbers a very serious look.', 'Tiny face. Big calculations.',
    'Still here. Still working. Mentally requesting a biscuit.',
)
SPEECH_CPS = 35


class Companion:
    """Clock-driven expressions and speech; independent of curses and science."""

    def __init__(self, now=None):
        now = time.monotonic() if now is None else now
        self.emotion = 'happy'
        self.message = 'Hello! /load brings in your FITS files. /help shows what we can do.'
        self.changed = self.last_active = now
        self.target = 'happy'
        self.since = now
        self.frames = (FACES['happy'],)
        self.face = FACES['happy']
        self.spoken_message = None
        self.speech_since = now

    def activity(self, now=None):
        self.last_active = time.monotonic() if now is None else now

    def set(self, emotion, message, now=None):
        if emotion not in FACES:
            raise ValueError(f'Unknown companion emotion: {emotion}')
        now = time.monotonic() if now is None else now
        self.emotion, self.message, self.changed = emotion, message, now
        self.activity(now)

    def result(self, lines, now=None):
        """Keep the actual failure visible, with a concrete route to more detail."""
        error = next((line for line in lines if line.startswith('Could not complete command:')), None)
        warning = next((line for line in lines if line.startswith('Warning:')), None)
        interrupted = any(line.startswith('Analysis interrupted.') for line in lines)
        if error:
            self.set('sad', error + ' · /history has the full details; /help lists commands.', now)
        elif interrupted:
            self.set('nervous', 'Stopped. Take your time. /history has the details; inspect the session before continuing.', now)
        elif warning:
            self.set('surprised', warning + ' · /history has the full details.', now)
        elif lines and lines[0].startswith('/'):
            self.set('nervous', 'Let’s check the command arguments. ' + lines[0] + ' · /history shows usage.', now)
        else:
            self.set('happy', lines[0] if lines else 'All done! Ready when you are.', now)

    def frame(self, now=None):
        now = time.monotonic() if now is None else now
        idle = max(0, now - self.last_active)
        target, speech = self.emotion, self.message
        if target in ('happy', 'smile', 'neutral', 'proud'):
            if idle >= 150:
                target, speech = 'asleep', 'Zzz… dreaming of perfectly calibrated data. Any key wakes me.'
            elif idle >= 60:
                target, speech = 'sleepy', 'Still here. Just resting my pixels. /help if you need a nudge.'
        elapsed = max(0, now - self.changed)
        if target == 'working':
            speech = WORK_PHRASES[int(elapsed // 5) % len(WORK_PHRASES)]
        if target != self.target:
            self.frames = (self.face, BLINK, FACES['neutral'],
                           *EMOTION_ROUTES.get(target, ()), FACES[target])
            self.target, self.since = target, now
        tick = max(0, int((now - self.since) / .16))
        if tick < len(self.frames):
            self.face = self.frames[tick]
        elif target == 'asleep':
            self.face = FACES['sleepy'] + ' ' + 'z' * (1 + int(now - self.since) % 3)
        elif target == 'working':
            self.face = (FACES['smile'], '(˶ᵔ ᵕ •˶)', FACES['smile'], BLINK)[int(elapsed * 2) % 4]
        else:
            self.face = BLINK if target not in ('sleepy', 'crying') and (now - self.since) % 4.8 < .16 else FACES[target]
        return self.face, speech, target, elapsed

    def speech_text(self, message, now=None, compact=False):
        """Reveal new speech without restarting on input, resize or theme changes."""
        now = time.monotonic() if now is None else now
        message = printable(message)
        if message != self.spoken_message:
            self.spoken_message, self.speech_since = message, now
        speech = Text(message, overflow='ellipsis', no_wrap=compact)
        commands = '|'.join(re.escape(name) for name in COMMANDS)
        # Color complete known tokens before slicing, so even the first slash
        # has the prompt's accent. Avoid matching command names inside paths.
        speech.highlight_regex(rf'(?i)(?<![\w/])(?:{commands})(?![\w/-]|\.\w)|\b(?:Ctrl-C|F2)\b',
                               style='bold cyan')
        visible = 1 + int(max(0, now - self.speech_since) * SPEECH_CPS)
        return speech[:visible]


def companion_layout(companion, width, now=None, compact=False):
    """Reserve a fixed face column so combining marks cannot shift the speech."""
    now = time.monotonic() if now is None else now
    face, speech, emotion, elapsed = companion.frame(now)
    table = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
    face_width = max(cell_len(value) for value in (*FACES.values(), BLINK)) + 1
    table.add_column(width=min(face_width, max(1, width - 5)), no_wrap=True, overflow='crop')
    table.add_column(ratio=1)
    table.add_row(Text(face, style='bold cyan'), companion.speech_text(speech, now, compact))
    if emotion == 'working' and not compact:
        size = min(20, max(5, width - 30))
        position = int(elapsed * 8) % (2 * (size - 3))
        position = min(position, 2 * (size - 3) - position)
        bar = '░' * position + '━━━' + '░' * (size - position - 3)
        return Group(table, Text(f'{elapsed:.0f}s · Ctrl-C · {bar} · {companion.message}',
                                 style='dim', no_wrap=True, overflow='ellipsis'))
    return table


def input_geometry(columns):
    """Prompt, its column and the width left for typed text."""
    prompt = PROMPT if columns >= 5 else ""
    left = 1 + len(prompt) if columns >= 5 else 0
    return prompt, left, max(1, columns - left - 1)
WORDMARK = r"""
             _     _
  __ _  ___ (_)___| |__   __ _
 / _` |/ _ \| / __| '_ \ / _` |
| (_| |  __/| \__ \ | | | (_| |
 \__, |\___||_|___/_| |_|\__,_|
 |___/
""".strip("\n").splitlines()

# October wordmark, transcribed from the supplied Halloween reference.
HALLOWEEN_WORDMARK = """
                  @@@          @@@
                  @@@          @@@
                               @@@
 @@@@:   @@@@@:   @@@  :@@@@@   @@@@@@@   @@@@@@@
@@@@@@@@ @@@@@@@@  @@@ !@@@@@@@  @@@@@@@@ @@@@@@@@
@@@  !@@ @@!  @!  @@@  :@!:     @@@  @@@ @@!  @@@
@!@  !@! @!@      @!@    !@!:   @!@  !@! @!@  !@!
!@!@!@!@ @!@!@!@! !@! !@!@!@!: @!@  !@! @!@!@!@!
  :!!@!!! :!!@!!:  :!  :!!@!:   !@!  @! :!!@!@!!
:!@! @@@  : :! :   :   : : :   : :  :!  : : : :
 :!@!@!:               :       :    :   : :  :
  : : :
""".strip("\n").splitlines()


def seasonal_wordmark(today=None):
    """Use the local calendar: Halloween lasts all of October."""
    return HALLOWEEN_WORDMARK if (today or date.today()).month == 10 else WORDMARK


def radio_waves(width, height, elapsed):
    """Generate expanding ASCII rings, correcting for tall terminal cells."""
    palette = " .:-=+*#"
    result = []
    for y in range(height):
        line = ""
        for x in range(width):
            radius = math.hypot((x - (width - 1) / 2) / 2, y - (height - 1) / 2)
            pulse = (0.5 + 0.5 * math.cos(radius * 2.6 - elapsed * 3)) ** 5
            fade = max(0, 1 - radius / (height * 0.8))
            line += palette[int(pulse * fade * (len(palette) - 1))]
        result.append(line)
    return result


def active_data_layout(session, width):
    """A persistent scope indicator, independent of transient command messages."""
    if not session or not session.files:
        return Group(Text('No active data · /load to choose FITS files', style='dim', no_wrap=True))
    count = len(session.files)
    prefix = f'Active data · {count} file' + ('s' if count != 1 else '') + ' · '
    suffix = f' +{count - 1}' if count > 1 else ''
    name = clip(Path(session.files[0]).name, max(1, width - cell_len(prefix + suffix)))
    title = Text(prefix, style='cyan')
    title.append(name + suffix, style='bold')
    scope = ','.join(session.settings.get('obs', []))
    if session.settings.get('wl ranges'):
        low, high = session.settings['wl ranges'][0]
        scope += f' · {low:g}–{high:g} µm'
    else:
        scope += ' · all wavelengths'
    info = Table.grid(expand=True, padding=(0, 1))
    info.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
    info.add_column(no_wrap=True)
    info.add_row(Text(printable(f'{scope} · {", ".join(session.targets)} · {", ".join(session.instruments)}'), style='dim'),
                 Text('/data · /headers', style='cyan'))
    return Group(title, info)


def draw_screen(screen, theme, splash, text='', cursor=0, message='', elapsed=0.0,
                suggestions=(), selected=0, session=None, console=None, companion=None):
    """Lay out every frame using the terminal's current dimensions."""
    rows, columns = screen.getmaxyx()
    screen.erase()

    def put(y, x, value, color=0):
        if 0 <= y < rows and 0 <= x < columns - 1:
            # Leave the last column free to avoid wrapping or scrolling.
            screen.addnstr(y, x, value, columns - x - 1, color)

    styles = theme.styles
    accent, muted = styles['commands'], curses.A_DIM
    # The input grows downwards-first: everything above it moves up to make room.
    prompt, input_x, available = input_geometry(columns)
    lines = wrap_input(text, available)
    cursor_row, cursor_column = cursor_cell(lines, cursor)
    input_height = max(1, min(len(lines), rows - 8))
    first_line = max(0, cursor_row - input_height + 1)
    input_y = max(0, rows - 2 - input_height)
    bar_y = input_y - 1
    dataset_rows = (2 if rows >= 10 else 1 if rows >= 7 else 0) if session is not None else 0
    companion_rows = (4 if rows >= 20 else 2 if rows >= 12 else 1 if rows >= 8 else 0) if companion else 0
    suggestion_count = min(len(suggestions), max(0, bar_y - 2 - dataset_rows - companion_rows))
    companion_y = bar_y - suggestion_count - companion_rows
    data_y = companion_y - dataset_rows if companion_rows or suggestion_count else bar_y - 1 - dataset_rows
    header_height = max(0, min(bar_y - 2 - suggestion_count, data_y - 1))
    title_x = 1
    wordmark = seasonal_wordmark()
    halloween = wordmark is HALLOWEEN_WORDMARK
    logo_width = max(map(len, wordmark))
    show_wordmark = columns >= logo_width + 2 and header_height >= len(wordmark) + 5
    # Keep enough width for the complete wordmark beside the animation.
    if columns >= (logo_width + 31 if show_wordmark else 52) and header_height >= 9:
        wave_width, wave_height = 25, 9
        for y, line in enumerate(radio_waves(wave_width, wave_height, elapsed)):
            put(y, 1, line, styles['animation'])
        title_x = wave_width + 4
    title_lines = wordmark if show_wordmark else ['GEISHA']
    for y, line in enumerate(title_lines, start=1):
        if y < header_height:
            logo_color = styles['logo']
            if halloween and theme.seasonal_logo:
                logo_color = theme.attributes['white' if show_wordmark and y <= 5 else 'red']
            put(y, title_x, line, logo_color | curses.A_BOLD)
    splash_y = len(title_lines) + (2 if show_wordmark else 1)
    header_width = max(1, columns - title_x - 1)
    splash_lines = textwrap.wrap(splash, width=header_width)
    # Reserve a line for the tip when there is room beneath the splash.
    for y, line in enumerate(splash_lines, start=splash_y):
        if y >= header_height:
            break
        put(y, title_x, line, styles['splash'] | curses.A_BOLD)
    tip_y = splash_y + len(splash_lines)
    if tip_y < header_height:
        put(tip_y, title_x, STARTUP_TIP, muted)

    if dataset_rows:
        if columns >= 36:
            paint_rich(screen, console or make_console(), active_data_layout(session, columns - 2),
                       data_y, 1, columns - 2, dataset_rows, accent)
        else:
            put(data_y, 1, f'Active: {len(session.files)} files', accent)

    if companion_rows:
        paint_rich(screen, console or make_console(),
                   companion_layout(companion, max(1, columns - 2), compact=companion_rows == 1),
                   companion_y, 1, max(1, columns - 2), companion_rows, accent)
    if suggestion_count:
        offset = max(0, selected - suggestion_count + 1)
        for index, name in enumerate(suggestions[offset:offset + suggestion_count]):
            y = bar_y - suggestion_count + index
            active = index + offset == selected
            style = curses.A_REVERSE if active else 0
            label = f"{'>' if active else ' '} {name:<10} {COMMANDS[name]}"
            put(y, 1, label.ljust(max(0, columns - 2)), style)
            put(y, 3, name, style | accent | curses.A_BOLD)
    elif not companion_rows:
        put(bar_y - 1, 1, message or 'Welcome back.', muted)
    put(bar_y, 1, '─' * max(0, columns - 2), styles['bars'])
    style = accent | curses.A_BOLD if text.startswith('/') else 0
    for index in range(input_height):
        number = first_line + index
        if number >= len(lines):
            break
        # Only the first line carries the prompt; continuations stay aligned.
        put(input_y + index, 1, prompt if number == 0 else ' ' * len(prompt), accent | curses.A_BOLD)
        put(input_y + index, input_x, lines[number][1], style)
    if rows >= 4:
        put(rows - 2, 1, '─' * max(0, columns - 2), styles['bars'])
    if rows >= 3:
        footer = ('↑↓ select · Tab complete · Enter run · Esc dismiss' if suggestion_count
                  else 'F2 suggestions · ↑ recall · PgUp history · Shift+Enter newline · /exit quit')
        if companion and companion.emotion == 'working':
            footer = 'Working… Ctrl-C interrupts · results and errors will appear here'
        put(rows - 1, 1, footer, muted)
        if not suggestion_count:
            for name in COMMANDS:
                if name in footer:
                    put(rows - 1, 1 + footer.index(name), name, accent | curses.A_BOLD)
    screen.move(input_y + cursor_row - first_line, min(columns - 1, input_x + cursor_column))
    screen.refresh()
