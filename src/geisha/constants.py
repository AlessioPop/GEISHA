"""Constants shared by every layer; keep this module free of heavy imports."""

FITS_SUFFIXES = ('.fits', '.fit', '.fits.gz', '.fit.gz')


def is_fits(path):
    """Recognise OIFITS filenames case-insensitively, gzipped ones included."""
    return str(path).lower().endswith(FITS_SUFFIXES)
