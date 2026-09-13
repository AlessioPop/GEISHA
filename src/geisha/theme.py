"""Color themes: the saved settings, the curses attributes they resolve to."""

import curses
import json
import os
import tempfile
from pathlib import Path

COLOR_NAMES = ('default', 'cyan', 'magenta', 'blue', 'green', 'yellow', 'red', 'white')
COLOR_PARTS = ('logo', 'animation', 'bars', 'splash', 'commands')
# Failures and warnings are never themed: whatever the palette, they read as alarm.
ALERT_COLOR = 197  # xterm-256 #ff005f, the neon end of the plot palette's red.
_alert = None
THEMES = {
    'Observatory': dict(zip(COLOR_PARTS, ('seasonal', 'cyan', 'default', 'cyan', 'cyan'))),
    'Nebula': dict(zip(COLOR_PARTS, ('magenta', 'blue', 'magenta', 'cyan', 'cyan'))),
    'Aurora': dict(zip(COLOR_PARTS, ('green', 'cyan', 'green', 'white', 'cyan'))),
    'Solar': dict(zip(COLOR_PARTS, ('yellow', 'red', 'yellow', 'white', 'yellow'))),
    'Monochrome': dict.fromkeys(COLOR_PARTS, 'default'),
}


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'geisha' / 'colors.json'


def load_colors():
    """Fall back to the default theme for missing, unreadable or invalid settings."""
    settings = THEMES['Observatory'].copy()
    try:
        saved = json.loads(config_path().read_text())
        if isinstance(saved, dict):
            for part in COLOR_PARTS:
                value = saved.get(part)
                if value in COLOR_NAMES or (part == 'logo' and value == 'seasonal'):
                    settings[part] = value
    except (OSError, ValueError):
        pass
    return settings


def save_colors(settings):
    """Write atomically so an interrupted save cannot leave a truncated file."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def color_background():
    """-1 keeps the terminal's own background wherever the terminal allows it."""
    try:
        curses.use_default_colors()
        return -1
    except curses.error:
        return curses.COLOR_BLACK


def colors_available():
    return curses.has_colors() and 'NO_COLOR' not in os.environ


def color_attributes():
    """Resolve every color name to a curses attribute; honours NO_COLOR."""
    attributes = dict.fromkeys(COLOR_NAMES, 0)
    if not colors_available():
        return attributes
    curses.start_color()
    background = color_background()
    for pair, name in enumerate(COLOR_NAMES[1:], start=1):
        try:
            curses.init_pair(pair, getattr(curses, 'COLOR_' + name.upper()), background)
            attributes[name] = curses.color_pair(pair)
        except curses.error:
            pass
    return attributes


def alert_attribute():
    """The neon red that marks a failure or a warning, resolved once per session.

    Terminals with a 256-color palette get the neon shade; the rest fall back to
    bold red, and a monochrome terminal or NO_COLOR to bold alone, so the line
    still stands out where no color can.
    """
    global _alert
    if _alert is not None:
        return _alert
    _alert = curses.A_BOLD
    try:
        if colors_available():
            curses.start_color()
            pair = len(COLOR_NAMES)  # The named colors occupy every pair below it.
            color = ALERT_COLOR if curses.COLORS >= 256 else curses.COLOR_RED
            curses.init_pair(pair, color, color_background())
            _alert = curses.color_pair(pair) | curses.A_BOLD
    except curses.error:
        pass
    return _alert


class Theme:
    """The active colors. Pass ``settings`` to preview a draft in /color."""

    def __init__(self, settings=None, attributes=None):
        self.settings = load_colors() if settings is None else settings
        self.attributes = color_attributes() if attributes is None else attributes

    def style(self, part):
        return self.attributes.get(self.settings[part], self.attributes['cyan'])

    @property
    def styles(self):
        return {part: self.style(part) for part in COLOR_PARTS}

    @property
    def alert(self):
        """Failures and warnings ignore the theme; they always read as alarm."""
        return alert_attribute()

    @property
    def accent(self):
        """Command color; also tints Rich output painted into curses."""
        return self.style('commands')

    @property
    def seasonal_logo(self):
        return self.settings['logo'] == 'seasonal'
