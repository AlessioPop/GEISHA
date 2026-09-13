# GEISHA

An interactive terminal workbench for optical interferometry, using PMOIRED for
model fitting. Run `geisha` after installing the project (`pip install -e .`),
or `python -m geisha`. Direct execution with `python src/geisha/cli.py` also works,
including an editor's Run button, using an environment with the dependencies installed.
Python 3.12–3.13 is required.

The themed terminal keeps command completion, searchable `/help`, `/color` and
`/tree`. Science results and errors are retained in a scrollable session history.

At the prompt, **↑** and **↓** recall the commands you have already run, like a
shell: the recalled line is put straight back on the prompt, and ↓ past the newest
one restores what you were typing. Page Up, or the mouse wheel in a supporting
terminal, opens the message history instead; `/history` does the same. In that
view, use ↑/↓, Page Up/Page Down and Home/End; Enter or Escape returns to your
unfinished input. History is kept in memory until GEISHA exits.

Long commands **wrap onto as many lines as they need** rather than scrolling
sideways, and the screen above the prompt moves up to make room. **Shift+Enter**
starts a new line without running the command; Enter runs it. Terminals that do
not distinguish Shift+Enter usually send the same code for **Alt+Enter**, which
also works. Inside a multi-line command, ↑ and ↓ move between its lines first and
reach the command history from the top or bottom line.

Commands also work through standard input, so the same workflow can be scripted.
Paths and target names with spaces must be quoted; their case is preserved.

## Project layout

Each module has one job, so most changes touch one file:

| File | Contents |
| --- | --- |
| `session.py` | Every command, its help text and the analysis state. No terminal code. |
| `cli.py` | Entry point: prompt loop, command dispatch, plain-text fallback. |
| `tui.py` | Shared terminal primitives: keys, scrolling, text clipping, Rich painting. |
| `home.py` | Title screen: wordmark, animation, active-data strip, prompt. |
| `panels.py` | List screens: `/help`, `/color` and the session history. |
| `browser.py` | File browser behind `/tree` and `/load`. |
| `dataview.py` | Dataset tables behind `/data`: files, observables, baselines. |
| `headers.py` | Header browser behind `/headers`. |
| `plotui.py` | Interactive plot builder behind a bare `/plot`. |
| `theme.py` | Color names, themes and their saved settings. |
| `io.py` | OIFITS discovery, header snapshots and record extraction. |
| `models.py` | Model presets, continuum fitting and fit feedback. |
| `plot.py` | Matplotlib figures for samples, continua and bootstrap resamples. |

Adding a command means adding one decorated method in `session.py`. The
decorator registers its help text for `/help`, completion and the suggestion
list, and declares whether the command needs loaded data or a finished fit.
Science imports stay inside the commands that use them, so startup stays fast.

## File browser

`/tree` and `/load` share a Rich layout with subtle folder guides, a highlighted
focus row, and aligned file types and sizes on wider terminals. Long paths are
shortened in the middle so their endings remain readable; the detail line shows
the focused path. The browser follows your command accent color and `NO_COLOR`.
Hidden files and folders start hidden; press **.** to show them.

Press **/** to filter filenames in opened folders. This does not scan the whole
disk; folders remain available so you can keep exploring. Enter keeps the filter,
and Escape while editing clears it. Press **?** for additional controls. Both
browsers support Page Up/Page Down, Home/End and the mouse wheel.

Run `/load` without a path in the interactive terminal to open the FITS file
tree, starting in the current working directory.

- Use ↑/↓ to navigate, → to expand folders and ← to collapse or move to a parent.
- With no files marked, Enter on a folder opens/collapses it. The parent row browses
  above the current root; ←/→ navigate folders even when files are marked.
- Highlight one FITS file and press **Enter** to load it and return to the title screen.
- For several files, mark/unmark each with Space, then press **Enter** anywhere to
  load the marked selection and return. **L** remains an alternative shortcut.
- **A** marks all FITS files in the expanded, filtered tree (including rows outside
  the current page); **C** clears the selection.
- Selections survive folder collapse, filtering and navigation. The counter includes
  selected files outside the current view. Escape cancels without changing data.

After a successful load, the main screen shows the active dataset immediately.
Detailed load messages remain available in `/history`; errors appear beside the
animated character on the main screen.

The kaomoji companion blinks and moves through neutral expressions when its mood
changes. It smiles after successful commands, looks sad on errors, and reacts to
warnings and interruptions. Its speech area preserves the actual error and points
to `/history` for the complete transcript. After 60 seconds without input it gets
sleepy; after 150 seconds it naps. Pressing a key wakes it. Unresolved errors stay
visible instead of being replaced by idle chatter. New speech types itself out
quickly, letter by letter. Suggested commands and Ctrl-C use the same accent color
as commands in the prompt, following your selected theme.

During commands, the companion cycles through working expressions and phrases
such as “Working very hard…”. A moving activity bar and elapsed time show that
work is underway; no percentage is guessed when the backend cannot report one.
Ctrl-C interrupts the command. Plot windows still open automatically, and the
companion tells you to close the viewer to return. Plain or piped sessions keep
their normal text output without animation.

On startup, the terminal assistant scans `data/` in the background, reading FITS
metadata to identify instruments, targets and saved telluric models. It uses the
current directory's `data/` when present, otherwise GEISHA's own data folder.
Hidden folders and symbolic links are skipped; malformed files remain visible
with an explanation. Discovery does not load a dataset or start a fit.

Press **F2** or use `/suggest` to open the assistant's tables. **↑/↓** selects an
action, **Enter** runs it, **1–9** runs a numbered action, and **Tab** switches to
the file catalogue. The selected row explains the recommendation and shows its
command or file path. **R** rescans the folder; **Esc** returns to the prompt.
Suggestions group files by folder, instrument and target, offer plots and data
inspection, and adapt when a fitted model becomes available. `/scan` refreshes
the catalogue from either interface; plain sessions can use `/suggest NUMBER`.

Successful loads remember their exact files and target/instrument filters in
`.geisha/session.json` beside the data folder (ignored by Git). On the next launch,
the companion offers to continue: choose **Resume** in F2 or run `/resume`.
Files are reloaded only when you accept; missing paths disable the resume action
instead of silently restoring an incomplete dataset. Fit results and unsaved
selections are not restored. Corrupt memory is ignored and a failed load leaves
the previous saved files intact. `GEISHA_STATE_DIR` can override the memory folder.

For GRAVITY SC, a valid `TELLURICS` extension is evidence of a saved PMOIRED model.
Its absence means **correction unknown**, since another tool may have corrected
the spectrum without that extension. The assistant offers `/tellurics` for
MED/HIGH resolution SC flux without a saved model, and `/tellurics view` when a
model already exists. Review the file's processing history before choosing a fit.
These commands use [PMOIRED's telluric tools](https://github.com/amerand/PMOIRED).

`/tellurics [files=1,2]` fits new copies and loads them after
all selected models validate. File numbers match `/data`; the fit uses
PMOIRED's default He I and Br γ exclusions.
Outputs go to a new folder in `data/tellurics/`; existing source files and saved
models are preserved. Other active files remain loaded. `/tellurics view [files=1,2]`
opens diagnostics comparing raw flux, transmission and corrected flux.
Inspect those diagnostics before interpreting the result. Low-resolution files,
missing SC flux, invalid models and unsupported HDU layouts receive explanations.

Loaded GRAVITY SC spectra use the saved transmission and corrected wavelength
grid for plots and fits. GEISHA also matches PMOIRED's automatic P2VM flat correction
when indicated by the processing metadata, propagating multiplicative corrections
to flux uncertainties. Telluric transmission is not applied to FT spectra.

The picker shows folders and FITS files, including `.fit`, uppercase extensions
and `.fits.gz`; symbolic links are omitted. `/load PATH` still loads a file or
recursively discovers a directory. Multiple explicit paths are also supported,
e.g. `/load "data/first file.fits" data/second.fits`; duplicate files are loaded
once. `ins=NAME` and `target=NAME` remain available with explicit paths. In plain
or piped sessions, supply paths because a visual picker requires an interactive
terminal.

## First analysis

The main terminal keeps an **Active data** strip above the command prompt, including
while command suggestions are open. It shows the file count, a shortened filename
(plus the number of additional files), and the current fit observables and wavelength
selection.

The detail lives in the **dataset view**, which opens automatically after a successful
load and whenever you run `/data`. It has three tabs, switched with ←/→ or Tab:

- **Files** — number, name, usable samples, records, HDUs and observables per file.
  Enter or **H** opens that file's headers.
- **Observables** — every target/instrument/observable with usable versus total
  samples, wavelength coverage, channel count, baseline count, and which ones the
  current fit uses.
- **Baselines** — each baseline, closure triangle or single station with its mean
  projected length in metres, the files it appears in and its usable samples.

**Enter** on an observable or baseline row, or **P** anywhere, opens the plot builder
already scoped to that row. Escape closes the view; the dataset is never modified.
In plain or piped sessions `/data` prints the same three tables as aligned text.

Loaded observations and a snapshot of every HDU header stay in memory until GEISHA
exits or another `/load` succeeds. Science commands use this dataset. Cancelling or
failing a load preserves the current data and fit; a successful load replaces them.
Opening a file's headers does **not** narrow the analysis to that file. Changes to
files on disk are only picked up by loading them again.

Use `/headers` to inspect the loaded files in a Rich browser:

- Choose a file, then an HDU (extension); HDU numbers start at **0** for the primary header.
- Browse keyword, value and comment columns with arrows, Page Up/Page Down, Home/End
  or the mouse wheel. **[** and **]** move between HDUs.
- **/** searches keywords, values and comments in the current HDU. Enter keeps the
  search; Escape clears it. Repeated HISTORY/COMMENT and blank cards retain their order.
- Enter on a card opens its complete content, with scrolling for long values.
- Escape moves back; **Q** closes the browser. Headers are read only.

In plain or piped sessions, `/data` lists file numbers, `/headers 1` lists the first
file's HDUs, and `/headers 1 0` prints its primary header. Add a quoted search term,
for example `/headers 1 0 "HISTORY"`, to filter its cards.

```text
/load data/random_data
/headers
/inspect
/models
/fit disk
/report
/plot fit output/disk-fit.png
/bootstrap 100
/plot bootstrap output/disk-bootstrap.png
/save output/disk-fit.json
/exit
```

Completing a plot opens Matplotlib's graphical viewer automatically and returns
when it closes, including when `save=` is supplied. In a
headless environment, save PNG, PDF or SVG instead. Existing export files are
never overwritten. `/plot V2` plots the input samples with their uncertainties;
`/plot fit` uses PMOIRED to show the selected fit observables and model.

## Building a plot

Run `/plot` with no arguments in the interactive terminal to open the **plot
builder**. Each row is one option; ←/→ cycle a choice, Enter edits a text field or
opens a selector, and the bottom rows draw the plot or reset the form. The command
being built is shown at all times, so nothing is hidden behind the interface:

```text
Plot          V2                          Observable, uv coverage or a fit result
X axis        frequency                   What the observable is plotted against
Files         all (4)                     Space marks files · Enter opens the list
Data / FT–SC  all: GRAVITY_SC, GRAVITY_FT   SC = science · FT = fringe tracker
Baselines     2 selected · J3-D0, D0-K0   Enter opens the tree
Colour by     file                        One colour and legend entry per group
…
Command  /plot V2 x=frequency baselines=J3-D0,D0-K0 color=file ylim=0,1
```

**Files**, **Data / FT–SC** and **Baselines** open checkbox selectors. Baselines
are grouped under their instrument with length and sample counts, and follow the
chosen observable, files and instruments. Visibility plots offer station pairs;
closure-phase plots offer triangles. Changing the selection clears incompatible
baseline picks. Nothing marked means everything in the current scope.

Each instrument gets a separate labeled panel, keeping GRAVITY science (SC) and
fringe-tracker (FT) spectra distinct. The same baseline keeps its color across
panels and selections. Opening a plot from an observable or baseline row in
`/data` preserves that row's instrument. Press **G** or choose **Draw plot** to
display the completed plot immediately.

The same options work on the command line, so any figure can be scripted, saved in
a notebook or recalled with ↑:

| Option | Values |
| --- | --- |
| *(first word)* | an observable, `uv`, `fit` or `bootstrap` |
| `x=` | `wavelength`, `frequency` (Mλ), `baseline` (m), `channel`, `mjd` |
| `files=` | file numbers from `/data`, e.g. `1,3` — or `all` |
| `instruments=` | exact `INSNAME` values separated by commas, `SC`, `FT`, or `all`; SC/FT include matching GRAVITY polarizations |
| `baselines=` | baseline labels, e.g. `J3-D0,D0-K0` — or `all` |
| `xlim=` `ylim=` | `MIN,MAX` |
| `color=` | `baseline`, `file`, `instrument`, `target`, `none` |
| `errors=` | `on`, `off` |
| `legend=` | `auto` (hidden beyond 16 series), `on`, `off` |
| `title=` `xlabel=` `ylabel=` | free text; quote anything with spaces |
| `continuum=` `line=` | polynomial degree, and the µm interval to exclude |
| `save=` | `FILE.png`, `.pdf` or `.svg` |

```text
/plot V2 x=frequency baselines=J3-D0 ylim=0,1 save=output/v2.png
/plot V2 instruments=SC
/plot uv color=file title="uv coverage, four epochs"
/plot T3PHI x=frequency legend=on errors=off
```

`x=frequency` uses each channel's own wavelength, so a single baseline spreads
across the spectral band. Closure quantities have no single uv point; their
spatial frequency uses the longest of the triangle's three baselines, and they are
left out of `uv`. Flux records have no baseline at all and can only be plotted
against wavelength, channel or MJD.

## Data and models

GEISHA discovers FITS files recursively. It matches `OI_WAVELENGTH` by `INSNAME`
and station tables by `ARRNAME`, supports varying station/channel counts, and
respects flags, nonfinite values and nonpositive errors. Each record also keeps its
projected baseline (`UCOORD`/`VCOORD`, or the longest side of a closure triangle)
and its MJD, which is what the spatial-frequency, baseline and time axes use. Supported input
observables are V2, visibility amplitude (`|V|`), differential phase (`DPHI`),
closure phase/amplitude (`T3PHI`/`T3AMP`) and flux (`FLUX`). Wavelengths in commands
are **microns**; PMOIRED model diameters and positions are **milliarcseconds**.
Ordinary image FITS files have no interferometric observables and are rejected.

Use `/load PATH ins=INSNAME target=NAME` to select an instrument or target.
Multiple targets require an explicit choice; GEISHA does not fit unrelated
objects together. `/inspect` reports available selections and any malformed
target metadata. Input FITS files are never changed.

```text
/select V2,T3PHI 2.1 2.3
/fit disk star,ud=0.7
/fit gaussian star,fwhm=0.8
/fit binary secondary,x=2 secondary,y=-1 secondary,f=0.2
```

Default fitting observables are available V2, visibility amplitude and closure
phase. `/select V2` restores the full wavelength range. Changing selection or
loading data invalidates the previous fit. The sample-count guard rejects fits
with at least as many free parameters as usable data points. Binary fitting is
local: repeat different initial separations to investigate aliases.

For custom PMOIRED models, supply a JSON file to `/fit path/to/model.json`:

```json
{"model": {"star,ud": 1.0, "star,f": 1.0}, "fitOnly": ["star,ud"]}
```

Model syntax follows [PMOIRED](https://github.com/amerand/PMOIRED). The JSON export
records source paths, target/instrument selections, settings, backend version,
best parameters, local uncertainties and backend fit statistics.

## Continuum and interpretation

`/continuum FLUX 2.162 2.172 1` plots a weighted degree-one continuum while
excluding that explicit line interval. This generalizes the prototype's
polynomial fitting without assuming Brγ, a target velocity or an epoch layout.
Flags and uncertainties participate in the fit; insufficient spectral sampling
produces an actionable error. This is a continuum diagnostic and does not modify
the data or propagate continuum uncertainty into a subsequent fit.

`/report` gives deterministic guidance based on residual scale, sample count and
parameter uncertainties. Its chi-square thresholds are heuristics, not hypothesis
tests. Backend reduced chi-square/degrees of freedom can include prior terms and
normalisation conventions. A plausible value does not validate a physical model;
inspect residual structure, calibration, spectral correlations and degeneracies.

`/bootstrap COUNT` runs PMOIRED resampling serially and plots parameter
histograms. Sparse baselines and correlated channels limit its interpretation.
Bootstrap is **not MCMC**. MCMC sampling/convergence diagnostics, automated model
comparison, velocity correction, pure-line visibility and photocentre
recovery remain future integration work from `main_temp.py`. No target-specific
corrections from that prototype are applied automatically.

The supplied CHARA/NIRO file exercises non-GRAVITY loading, a one-parameter disk
fit, bootstrap, and plot/JSON export. Its target-name field contains malformed
bytes and it has only three single-channel V2 samples: verify its provenance
before using the result for science. Multi-channel GRAVITY calibration workflows
still need validation on representative data.
