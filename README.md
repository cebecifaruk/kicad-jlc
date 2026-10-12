# KiCad JLC

A KiCad 10 plugin (IPC API, `kicad-python`) for assigning JLCPCB/LCSC part numbers. It adds a
**KiCad JLC** button (LCSC logo) to the PCB Editor toolbar. Clicking it opens a table of
every component (footprint) on the board.

## Features

- **Component table**: reference, value, LCSC part number, footprint, side, X/Y position,
  rotation, mounting type (SMD/THT), DNP, and one column for each custom field used on any
  footprint. Rows start in reference order (R2 before R10); click a column header to sort by
  it, and click again to reverse.
- **Live JLCPCB data**: when the table opens, the details of every part in a component's
  `LCSC` field are fetched from JLCPCB in the background (each distinct part once). The
  table is usable immediately; cells show "Loading…" and a spinner with progress appears at
  the bottom until every part has arrived. Unknown part numbers show "(not found)", and
  failed requests are retried with **Reload**.
- **Edit LCSC in the table**: double-click an LCSC cell (or select it and start typing),
  enter a part number such as `C25744` and press Enter. It is written to the footprint's
  `LCSC` field as one undoable step; clearing the cell clears the field. Lower-case input is
  corrected (`c25744` → `C25744`), and text that isn't an LCSC number asks for confirmation.
- **Colours**: columns loaded from JLCPCB (computed, not stored in the project) have a light
  blue header; the LCSC cell of components without an LCSC part number is red (the rest of
  the row is not highlighted); Value / LCSC Value and Footprint / LCSC Footprint cells that
  don't agree are yellow (both cells of the pair), and so are the LCSC and LCSC Stock cells
  of parts that are currently out of stock at JLCPCB. A legend sits below the table.
- **Value and footprint check** (`src/matching.py`): the board's Value and Footprint are
  compared with the LCSC part, tolerantly rather than as plain text. Values are parsed with
  SI prefixes and units (`100nF` = `0.1uF` = `100n`, `100R` = `100Ω` = `100`, `4k7` = `4.7kΩ`);
  extra quantities such as `100nF 50V` must match the part's specifications, and non-numeric
  values (`STM32F103C8Tx`, `LM358`) are matched against the MFR part number. Footprints are
  reduced to a package following KiCad's standard library naming, so
  `Resistor_SMD:R_0603_1608Metric` = `0603`, `Package_TO_SOT_SMD:SOT-23` = `SOT-23-3`,
  `Package_QFP:LQFP-48_7x7mm_P0.5mm` = `LQFP-48(7x7)`, `Diode_SMD:D_SMA` = `SMA(DO-214AC)`.
  Cases that can't be decided (unrecognised packages, e.g. connectors) are not highlighted.
- **Default columns**: Reference, LCSC, Value, LCSC Value, Footprint, LCSC Footprint, Side,
  LCSC Stock and LCSC Price ($). This list is `DEFAULT_COLUMNS` in `src/columns.py`.
- **More columns from JLCPCB**, available in **Columns...**: MFR Part, Manufacturer, Type
  (Basic/Extended), unit price at 10 / 100 / 1,000 / 5,000 pieces, all price breaks, Category,
  Description, Datasheet, JLCPCB page, every specification JLCPCB lists (Tolerance, Voltage
  Rating, ...) and every other field of JLCPCB's reply ("raw data").
- **Choose columns**: **Columns...** (or right-click a column header) opens a column chooser
  with a search box, a group filter and "Only shown columns". Tick columns to show them, use
  Move Up/Down to reorder, and **Reset to Defaults** to go back to the default columns.
  Only the list of shown columns, in order, is saved; everything else stays hidden.
- **Filter**: the **Show** menu switches between all components, only those without an LCSC
  part number, and only those with one.
- **CSV export**: **Export CSV...** saves exactly what the table shows: the filtered rows, in
  the current sort order, with the visible columns in their current order. The file is UTF-8
  so symbols such as Ω open correctly in Excel.
- **Highlight on the board**: selecting rows selects those footprints in the PCB Editor.
  With "Center board view on selection" ticked, the view scrolls to them.
- **LCSC Part Detail**: double-click a row (outside the LCSC cell) or select it and click
  **LCSC Part Detail** to open the window for the part in its LCSC field: product photos,
  specifications, price breaks, every field JLCPCB returns, and links to the datasheet and
  the JLCPCB and LCSC pages. The window doesn't depend on the board: it is opened with just
  an LCSC part number (`LcscPartDetailsFrame.show_part("C25744")`). The button is enabled when
  the selected rows share one valid LCSC part number.
- **Assign LCSC part number**: select one or more rows and click
  **Assign LCSC Part Number...**. A search window opens with a
  keyword search (prefilled from the component), filters for category, package, library type
  and stock, results with product photos, and a details preview. **Assign** writes the part
  number to the footprints' `LCSC` field (creating a hidden one if needed) in one undoable step.
- **Pricing Calculator**: **Pricing Calculator...** opens a window that prices the board for a
  production run. Enter the **PCB Quantity** (remembered); components are grouped by LCSC part
  number and each row shows the part number, its components (e.g. `R1, R2`), the quantity per
  board, the total quantity (per board × PCB quantity), the selected price level, the unit
  price and the line total, with the **Total Price** (and price per PCB) below. Changing the
  PCB quantity recalculates everything immediately.
  - Each part number is priced **once, for its total quantity**: if R1 and R2 share a part,
    100 boards need 200 pieces, priced at the 200-piece price level, not twice at the
    100-piece one. The price level is the highest JLCPCB price break whose minimum is at or
    below the total quantity.
  - If fewer pieces are needed than the part's first price break (e.g. a 50-piece minimum),
    the minimum is priced, and the row says so. Rows also note parts with too little stock.
  - DNP components and components excluded from the BOM aren't counted (they are listed
    below the table). Components without an LCSC part number, with an invalid one, or whose
    part JLCPCB doesn't know are shown in yellow and left out of the total, which says so.
  - Selecting rows highlights their footprints on the board; double-click opens the part.

All part data comes from live requests to JLCPCB's public website endpoints (search, part
details and images); there is no downloaded parts database. These endpoints are not an
official API and may change without notice. Results are cached only while the window is open.

Only the field named `LCSC` is read and written. Other fields such as `LCSC Part #` are shown
as ordinary custom field columns.

The shown columns and their order, column widths, the filter, the "center view" option, the
last export folder and the pricing calculator's PCB quantity are remembered in `settings.json`
inside KiCad's settings folder for this plugin (on macOS:
`~/Library/Preferences/kicad/10.0/plugins/com.github.cebecifaruk.kicad-jlc/`). Delete that
file to go back to the defaults.

## Install

1. In KiCad, enable **Preferences → Plugins → Enable KiCad API**.
2. Download `kicad-jlc-<version>.zip` from the
   [Releases](https://github.com/cebecifaruk/kicad-jlc/releases) page (or the
   `kicad-jlc-package` artifact of a GitHub Actions run).
3. In the KiCad project window, open **Plugin and Content Manager → Install from File...**
   and choose the zip.
4. Open a board in the PCB Editor. The first time, KiCad sets up a Python environment for
   the plugin, so the button can take a little while to appear. If it doesn't show up,
   check **Preferences → PCB Editor → Action Plugins**.

## Repository layout

The repository root is the plugin folder: KiCad finds `plugin.json` and resolves the
entrypoint (`src/list_components.py`), the icons and `requirements.txt` relative to it.

- `plugin.json`: plugin metadata and toolbar action ([schema](https://go.kicad.org/api/schemas/v1))
- `requirements.txt`: Python dependencies KiCad installs into the plugin's environment
- `icons/`: toolbar icons (24 px and 48 px)
- `src/`: the plugin's Python code, and nothing else (CI fails on other files here)
  - `list_components.py`: the entrypoint and the component table window
  - `columns.py`: column definitions, default columns, the column chooser and CSV export
  - `component_grid.py`: the table grid and its data model (cell values, colours, LCSC editing)
  - `matching.py`: tolerant comparison of board values/footprints with the LCSC part
  - `windows.py`: keeps dialogs in front of the always-on-top plugin windows
  - `remote_data.py`: background loading of JLCPCB data for the table
  - `lcsc_part_details.py`: the LCSC Part Detail window (opened with an LCSC part number)
  - `part_details.py`: part details panel with photos, plus shared UI helpers
  - `part_search.py`: the JLCPCB part search window
  - `pricing.py`: the pricing calculation: grouping by LCSC part number, price level
    selection and totals (no UI)
  - `pricing_calculator.py`: the Pricing Calculator window
  - `jlcpcb_api.py`: client for JLCPCB's part search, part details and images
  - `board_data.py`: reads components from the board and writes LCSC fields
  - `settings.py`: remembered settings
- `packaging/`: only needed for the installable zip
  - `metadata.json`: the package description the Plugin and Content Manager requires
    (name, description, author, license, KiCad version); CI fills in the version and size
  - `icon.png`: 64 px icon shown in the Plugin and Content Manager
- `.github/workflows/build.yml`: CI that checks, validates and builds the zip

## Building the package

The package is built by GitHub Actions only. Every push and pull request:

1. checks that `src/` compiles and holds only Python files, and that `plugin.json` and
   `packaging/metadata.json` have the same identifier;
2. builds `kicad-jlc-<version>.zip` (`metadata.json`, the plugin under `plugins/`, and
   `resources/icon.png`);
3. validates the built `plugin.json` and `metadata.json` against KiCad's JSON schemas and
   uploads the zip as the `kicad-jlc-package` artifact.

KiCad only accepts numeric versions (`1`, `1.2` or `1.2.3`; no `-dev`/`-rc` suffixes).
Untagged builds get version `0.0.<run number>`. Pushing a tag such as `v0.1.0` builds
version `0.1.0` and attaches the zip to a GitHub release for that tag.

## Development

Link the repository into KiCad's plugins folder so changes take effect without reinstalling:

```sh
# macOS
ln -s "$PWD" ~/Documents/KiCad/10.0/plugins/kicad-jlc
# Linux
ln -s "$PWD" ~/.local/share/KiCad/10.0/plugins/kicad-jlc
```

Don't link it and install the zip at the same time: both would register the same plugin.

To run it from a terminal (KiCad running, a board open in the PCB Editor):

```sh
pip install -r requirements.txt
python src/list_components.py
```
