"""Color themes: the saved settings, the curses attributes they resolve to."""

import curses
import json
import os
import tempfile
from pathlib import Path

COLOR_NAMES = ('default', 'cyan', 'magenta', 'blue', 'green', 'yellow', 'red', 'white')
COLOR_PARTS = ('logo', 'animation', 'bars', 'splash', 'commands')
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


def color_attributes():
    """Resolve every color name to a curses attribute; honours NO_COLOR."""
    attributes = dict.fromkeys(COLOR_NAMES, 0)
    if not curses.has_colors() or 'NO_COLOR' in os.environ:
        return attributes
    curses.start_color()
    background = -1
    try:
        curses.use_default_colors()
    except curses.error:
        background = curses.COLOR_BLACK
    for pair, name in enumerate(COLOR_NAMES[1:], start=1):
        try:
            curses.init_pair(pair, getattr(curses, 'COLOR_' + name.upper()), background)
            attributes[name] = curses.color_pair(pair)
        except curses.error:
            pass
    return attributes


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
    def accent(self):
        """Command color; also tints Rich output painted into curses."""
        return self.style('commands')

    @property
    def seasonal_logo(self):
        return self.settings['logo'] == 'seasonal'
