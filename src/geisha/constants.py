"""Constants shared by every layer; keep this module free of heavy imports."""

FITS_SUFFIXES = ('.fits', '.fit', '.fits.gz', '.fit.gz')


def object_key(name):
    """Object identity ignores capitalization, preserving other name distinctions."""
    return str(name).strip().casefold()


def is_fits(path):
    """Recognise OIFITS filenames case-insensitively, gzipped ones included."""
    return str(path).lower().endswith(FITS_SUFFIXES)
