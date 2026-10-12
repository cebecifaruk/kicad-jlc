#!/usr/bin/env python3
"""KiCad JLC plugin for KiCad 10 (IPC API).

Shows every footprint on the open board in a sortable table. The LCSC column
can be edited in place; other columns can be loaded from JLCPCB for the part
in each component's LCSC field (their headers are light blue), and components
without an LCSC part number have their LCSC cell shown in red. Clicking a row highlights
the footprint on the board. Value and Footprint cells that don't agree with the
LCSC part (see matching.py) are shown in yellow, on both the board and the LCSC
side. Double-clicking a row opens the LCSC Part Detail window for the part in
its LCSC field (photos, specifications, prices); that window only depends on
the LCSC part number. "Assign LCSC Part Number..." opens a
JLCPCB part search. "Pricing Calculator..." prices the board's parts, grouped by
LCSC part number, for a number of boards. The table can be filtered and
exported as CSV.

When launched from the PCB editor toolbar, KiCad sets the KICAD_API_SOCKET and
KICAD_API_TOKEN environment variables, which kipy.KiCad() picks up automatically.
The windows use wxPython, which ships with KiCad's bundled Python on macOS and
Windows (the same GUI toolkit KiCad itself uses).
"""

import os
import sys
from typing import List, Optional

# KiCad runs the entrypoint by path; make sibling modules importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx  # noqa: E402
import wx.grid as gridlib  # noqa: E402
from kipy import KiCad  # noqa: E402
from kipy.board import Board  # noqa: E402
from kipy.errors import ApiError, ConnectionError as KiCadConnectionError  # noqa: E402

from board_data import Component, assign_lcsc, get_components  # noqa: E402
from columns import (  # noqa: E402
    COMPUTED_COLOUR,
    DEFAULT_COLUMNS,
    UNASSIGNED_COLOUR,
    WARNING_COLOUR,
    Column,
    all_columns,
    arrange,
    choose_columns,
    sort_key,
    write_csv,
)
from component_grid import ComponentGrid  # noqa: E402
from jlcpcb_api import JlcpcbClient, normalize_lcsc  # noqa: E402
from lcsc_part_details import LcscPartDetailsFrame  # noqa: E402
from part_search import PartSearchDialog  # noqa: E402
from pricing_calculator import PricingCalculatorFrame  # noqa: E402
from remote_data import RemoteDetailsLoader  # noqa: E402
from settings import Settings, plugin_identifier  # noqa: E402
from windows import message, show_modal  # noqa: E402

KICAD_ERRORS = (ApiError, KiCadConnectionError)
# How long to wait for a reply from KiCad. kipy's default (2 s) is too short
# for reading every footprint of a large board, especially on Windows.
KICAD_TIMEOUT_MS = 30_000
# KiCad action that scrolls the board view to the current selection.
CENTER_ON_SELECTION_ACTION = "common.Control.centerSelection"
# Settings key: the keys of the columns the user chose to show, in order.
SHOWN_COLUMNS_SETTING = "shown_columns"
# Settings written by older versions, removed on start.
OBSOLETE_SETTINGS = ("hidden_columns", "column_order", "known_columns")

# Filter choices: label -> which components to show
FILTERS = {
    "All components": lambda c: True,
    "Without LCSC part number": lambda c: not c.lcsc.strip(),
    "With LCSC part number": lambda c: bool(c.lcsc.strip()),
}


def colour_swatch(parent: wx.Window, colour, text: str) -> wx.Sizer:
    """A small coloured square with a label, for the colour legend."""
    swatch = wx.Panel(parent, size=(14, 14))
    swatch.SetBackgroundColour(wx.Colour(*colour))
    sizer = wx.BoxSizer(wx.HORIZONTAL)
    sizer.Add(swatch, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
    sizer.Add(wx.StaticText(parent, label=text), 0, wx.ALIGN_CENTER_VERTICAL)
    return sizer


class ComponentsFrame(wx.Frame):
    def __init__(self, kicad: KiCad, board: Board, settings: Optional[Settings] = None):
        super().__init__(
            None,
            title=f"KiCad JLC – {board.name}",
            size=(1250, 650),
            style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP,
        )
        self.kicad = kicad
        self.board = board
        self.settings = settings or Settings(None)
        for key in OBSOLETE_SETTINGS:
            if key in self.settings.data:
                del self.settings.data[key]
                self.settings.save()
        self.client = JlcpcbClient()
        # Loads JLCPCB data in the background and updates cells as parts arrive.
        self.remote = RemoteDetailsLoader(self, self.client, self.on_remote_update)
        self.all_components: List[Component] = []
        self.components: List[Component] = []  # filtered + sorted, one per table row
        self.available_columns: List[Column] = []  # shown columns first, then the rest
        self.columns: List[Column] = []  # the shown ones, in order
        self.sort_key = "reference"
        self.sort_ascending = True
        self.details: Optional[LcscPartDetailsFrame] = None
        self.pricing: Optional[PricingCalculatorFrame] = None
        # Selection changes arrive in bursts; highlight once, shortly after the last.
        self._selection_timer = wx.CallLater(150, self._on_selection_settled)
        self._selection_timer.Stop()
        # Repaint (and re-sort, if sorted by a JLCPCB column) once per burst of
        # arriving parts rather than once per part.
        self._remote_timer = wx.CallLater(150, self._after_remote_data)
        self._remote_timer.Stop()

        panel = wx.Panel(self)
        self.grid = ComponentGrid(panel, self)
        self.grid.SetSelectionMode(gridlib.Grid.GridSelectRows)
        self.grid.SetRowLabelSize(0)
        self.grid.SetColLabelAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTRE)
        self.grid.SetDefaultRowSize(self.grid.GetDefaultRowSize() + 2, True)
        self.grid.DisableDragRowSize()
        self.grid.SetCellHighlightPenWidth(0)
        self.grid.SetCellHighlightROPenWidth(0)
        self.grid.Bind(gridlib.EVT_GRID_LABEL_LEFT_CLICK, self.on_label_click)
        self.grid.Bind(gridlib.EVT_GRID_LABEL_RIGHT_CLICK, self.on_label_right_click)
        self.grid.Bind(gridlib.EVT_GRID_RANGE_SELECTED, self.on_selection_changed)
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, self.on_selection_changed)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_cell_double_click)
        self.grid.Bind(gridlib.EVT_GRID_COL_SIZE, lambda e: (e.Skip(), self._save_column_widths()))

        self.assign_button = wx.Button(panel, label="Assign LCSC Part Number...")
        self.assign_button.Bind(
            wx.EVT_BUTTON, lambda _e: self.assign(self.selected_components(), self)
        )
        self.details_button = wx.Button(panel, label="LCSC Part Detail")
        self.details_button.SetToolTip(
            "Show the JLCPCB details of the selected row's LCSC part (or double-click a row)"
        )
        self.details_button.Bind(wx.EVT_BUTTON, lambda _e: self.open_details_for_selection())
        pricing_button = wx.Button(panel, label="Pricing Calculator...")
        pricing_button.SetToolTip(
            "Price the board's parts, grouped by LCSC part number, for a number of PCBs"
        )
        pricing_button.Bind(wx.EVT_BUTTON, lambda _e: self.show_pricing())
        columns_button = wx.Button(panel, label="Columns...")
        columns_button.SetToolTip("Choose which columns are shown and in which order")
        columns_button.Bind(wx.EVT_BUTTON, lambda _e: self.choose_columns())
        export_button = wx.Button(panel, label="Export CSV...")
        export_button.SetToolTip("Save the rows and columns currently shown as a CSV file")
        export_button.Bind(wx.EVT_BUTTON, lambda _e: self.export_csv())
        reload_button = wx.Button(panel, label="Reload")
        reload_button.Bind(wx.EVT_BUTTON, lambda _e: self.reload())

        self.filter_choice = wx.Choice(panel, choices=list(FILTERS))
        saved_filter = self.settings.get("filter", "All components")
        self.filter_choice.SetStringSelection(saved_filter if saved_filter in FILTERS else "All components")
        self.filter_choice.Bind(wx.EVT_CHOICE, self.on_filter_changed)
        self.zoom_checkbox = wx.CheckBox(panel, label="Center board view on selection")
        self.zoom_checkbox.SetValue(self.settings.get("center_on_selection", True))
        self.zoom_checkbox.Bind(
            wx.EVT_CHECKBOX,
            lambda e: self.settings.set("center_on_selection", e.IsChecked()),
        )
        self.summary = wx.StaticText(panel, label="")
        self.loading_indicator = wx.ActivityIndicator(panel)
        self.loading_text = wx.StaticText(panel, label="")

        # Toolbar: every item is vertically centred with the same top/bottom
        # margin, and horizontal gaps are spacers, so all items line up.
        top = wx.WrapSizer(wx.HORIZONTAL)
        for item, gap in (
            (self.assign_button, 4),
            (self.details_button, 16),
            (pricing_button, 16),
            (columns_button, 4),
            (export_button, 4),
            (reload_button, 16),
            (wx.StaticText(panel, label="Show:"), 4),
            (self.filter_choice, 16),
            (self.zoom_checkbox, 0),
        ):
            top.Add(item, 0, wx.ALIGN_CENTER_VERTICAL | wx.TOP | wx.BOTTOM, 3)
            if gap:
                top.AddSpacer(gap)

        legend = wx.BoxSizer(wx.HORIZONTAL)
        legend.Add(colour_swatch(panel, COMPUTED_COLOUR, "Column from JLCPCB"), 0, wx.RIGHT, 12)
        legend.Add(colour_swatch(panel, UNASSIGNED_COLOUR, "No LCSC part number"), 0, wx.RIGHT, 12)
        legend.Add(
            colour_swatch(panel, WARNING_COLOUR, "Doesn't match the LCSC part / out of stock"),
            0, wx.RIGHT, 12,
        )
        legend.Add(
            wx.StaticText(panel, label="Double-click an LCSC cell (or select it and type) to edit it."),
            0, wx.ALIGN_CENTER_VERTICAL,
        )

        close_button = wx.Button(panel, wx.ID_CLOSE)
        close_button.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        bottom.Add(self.loading_indicator, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        bottom.Add(self.loading_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        bottom.Add(self.summary, 1, wx.ALIGN_CENTER_VERTICAL)
        bottom.Add(close_button, 0)
        self.loading_indicator.Hide()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(legend, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self.reload()
        self.CentreOnScreen()

    # ---- Table ----------------------------------------------------------

    def reload(self) -> None:
        """Read the footprints from KiCad again and rebuild the table."""
        selected = {c.key for c in self.selected_components()}
        try:
            with wx.BusyCursor():  # reading a large board can take a few seconds
                footprints = self.board.get_footprints()
            self.all_components = get_components(footprints)
        except KICAD_ERRORS as e:
            self.show_message(f"Could not read the board from KiCad.\n\n{e}", wx.ICON_ERROR)
            return
        self.remote.retry_failed()  # Reload also retries parts that failed to load
        # Fetch the JLCPCB details of every component that has an LCSC part
        # number, whether or not a JLCPCB column is shown: the value/footprint
        # check, the column chooser and CSV export all use them.
        self.remote.request(c.lcsc for c in self.all_components)
        self._update_available_columns()
        self.rebuild_columns(selected)
        if self.pricing:
            self.pricing.refresh()

    @property
    def shown_column_keys(self) -> List[str]:
        """The user's chosen columns, or DEFAULT_COLUMNS if they haven't chosen."""
        return self.settings.get(SHOWN_COLUMNS_SETTING, None) or list(DEFAULT_COLUMNS)

    def _update_available_columns(self) -> bool:
        """Recompute every available column, including JLCPCB specification and
        raw-data columns discovered in the parts loaded so far.

        Returns True if the shown columns changed (e.g. a chosen specification
        column became available once its parts loaded)."""
        before = [c.key for c in self.columns]
        self.available_columns = arrange(
            all_columns(self.all_components, self.remote), self.shown_column_keys
        )
        shown = set(self.shown_column_keys)
        return before != [c.key for c in self.available_columns if c.key in shown]

    def rebuild_columns(self, selected_keys=frozenset()) -> None:
        """Recreate the table's columns from `available_columns` and the shown keys."""
        shown = set(self.shown_column_keys)
        self.columns = [c for c in self.available_columns if c.key in shown]
        if not self.columns:  # never leave the table without columns
            self.columns = self.available_columns[:1]
        self.apply_filter_and_sort(selected_keys)
        widths = self.settings.get("column_widths", {})
        for index, column in enumerate(self.columns):
            self.grid.SetColSize(index, widths.get(column.key, column.width))
        self._update_loading_status()

    def apply_filter_and_sort(self, selected_keys=frozenset()) -> None:
        keep = FILTERS[self.filter_choice.GetStringSelection()]
        self.components = [c for c in self.all_components if keep(c)]
        column = next((c for c in self.available_columns if c.key == self.sort_key), None)
        if column is None:
            self.sort_key, column = "reference", self.available_columns[0]
        get = column.get
        self.components.sort(
            key=lambda c: (sort_key(get(c)), sort_key(c.reference)),
            reverse=not self.sort_ascending,
        )
        self.populate(selected_keys)

    def populate(self, selected_keys=frozenset()) -> None:
        """Push the current rows/columns to the grid and restore the selection."""
        self._restoring_selection = True
        try:
            self.grid.sync()
            self.grid.ClearSelection()
            first = -1
            for row, component in enumerate(self.components):
                if component.key in selected_keys:
                    self.grid.SelectRow(row, addToSelected=True)
                    if first == -1:
                        first = row
            if first != -1:
                self.grid.MakeCellVisible(first, 0)
        finally:
            self._restoring_selection = False
        self._update_sort_indicator()
        self.update_summary()

    def _update_sort_indicator(self) -> None:
        for index, column in enumerate(self.columns):
            if column.key == self.sort_key:
                self.grid.SetSortingColumn(index, self.sort_ascending)
                return
        self.grid.UnsetSortingColumn()

    def on_label_click(self, event: gridlib.GridEvent) -> None:
        col = event.GetCol()
        if col < 0:
            return  # corner label
        key = self.columns[col].key
        if key == self.sort_key:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_key, self.sort_ascending = key, True
        self.apply_filter_and_sort({c.key for c in self.selected_components()})

    def on_filter_changed(self, _event) -> None:
        self.settings.set("filter", self.filter_choice.GetStringSelection())
        self.apply_filter_and_sort({c.key for c in self.selected_components()})
        self._selection_timer.Start(150)  # rows may have disappeared from the selection

    def selected_rows(self) -> List[int]:
        rows = set()
        for block in self.grid.GetSelectedRowBlocks():
            rows.update(range(block.GetTopRow(), block.GetBottomRow() + 1))
        return sorted(r for r in rows if 0 <= r < len(self.components))

    def selected_components(self) -> List[Component]:
        return [self.components[row] for row in self.selected_rows()]

    def selected_lcsc(self) -> str:
        """The LCSC part number of the selected rows if they all have the same
        valid one, otherwise ""."""
        codes = {normalize_lcsc(c.lcsc) for c in self.selected_components()}
        return codes.pop() if len(codes) == 1 else ""

    def update_summary(self) -> None:
        total = len(self.all_components)
        unassigned = sum(1 for c in self.all_components if not c.lcsc.strip())
        selected = len(self.selected_rows())
        text = f"{total} components, {unassigned} without an LCSC part number"
        if len(self.components) != total:
            text += f" — showing {len(self.components)}"
        if selected:
            text += f" — {selected} selected"
        self.summary.SetLabelText(text)
        self.assign_button.Enable(selected > 0)
        self.details_button.Enable(bool(self.selected_lcsc()))

    # ---- Editing the LCSC field ------------------------------------------

    def on_cell_double_click(self, event: gridlib.GridEvent) -> None:
        row, col = event.GetRow(), event.GetCol()
        if not 0 <= row < len(self.components):
            return
        if 0 <= col < len(self.columns) and self.columns[col].editable:
            self.grid.SetGridCursor(row, col)
            self.grid.EnableCellEditControl()
        else:
            component = self.components[row]
            code = normalize_lcsc(component.lcsc)
            if code:
                self.show_details(code)
            elif component.lcsc.strip():
                self.summary.SetLabelText(
                    f"{component.reference}: {component.lcsc.strip()!r} is not an LCSC part number."
                )
            else:
                self.summary.SetLabelText(f"{component.reference} has no LCSC part number.")

    def on_lcsc_edited(self, component: Component, text: str) -> None:
        """The LCSC cell of `component` was edited in the table."""
        if not self:
            return
        text = text.strip()
        value = normalize_lcsc(text) or text  # "c25744" -> "C25744"
        if value == component.lcsc.strip():
            return
        if value and not normalize_lcsc(value):
            answer = message(
                self,
                f"“{value}” doesn't look like an LCSC part number (e.g. C25744).\n\n"
                f"Write it to the LCSC field of {component.reference} anyway?",
                "Edit LCSC",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
            )
            if answer != wx.ID_YES:
                self.grid.ForceRefresh()
                return
        self.write_lcsc([component], value, self)

    def write_lcsc(self, components: List[Component], value: str, parent: wx.Window) -> None:
        try:
            references = assign_lcsc(self.board, components, value)
        except (LookupError,) + KICAD_ERRORS as e:
            self.show_message(f"Could not write the LCSC field.\n\n{e}", wx.ICON_ERROR, parent)
            self.grid.ForceRefresh()
            return
        self.reload()
        if not references:
            return
        if value:
            self.summary.SetLabelText(
                f"Set LCSC of {', '.join(references)} to {value}. Use Undo in KiCad to revert."
            )
        else:
            self.summary.SetLabelText(
                f"Cleared LCSC of {', '.join(references)}. Use Undo in KiCad to revert."
            )

    # ---- Columns --------------------------------------------------------

    def _save_column_widths(self) -> None:
        widths = dict(self.settings.get("column_widths", {}))
        for index, column in enumerate(self.columns):
            if index < self.grid.GetNumberCols():
                widths[column.key] = self.grid.GetColSize(index)
        self.settings.set("column_widths", widths)

    def choose_columns(self) -> None:
        self._save_column_widths()
        shown = set(self.shown_column_keys)
        visible = {c.key: c.key in shown for c in self.available_columns}
        result = choose_columns(self, self.available_columns, visible)
        if result is not None:
            self.set_shown_columns(result)

    def set_shown_columns(self, keys: List[str]) -> None:
        """Store the user's columns. Keys of columns that aren't available right
        now (e.g. a specification of a part that isn't on this board) keep their
        place, so they come back on a board that has them."""
        available = {c.key for c in self.available_columns}
        kept = [k for k in self.shown_column_keys if k not in available and k not in keys]
        keys = list(keys) + kept
        if keys == list(DEFAULT_COLUMNS):
            self.settings.data.pop(SHOWN_COLUMNS_SETTING, None)  # back to the defaults
            self.settings.save()
        else:
            self.settings.set(SHOWN_COLUMNS_SETTING, keys)
        self._update_available_columns()
        self.rebuild_columns({c.key for c in self.selected_components()})

    def on_label_right_click(self, event: gridlib.GridEvent) -> None:
        index = event.GetCol()
        menu = wx.Menu()
        if 0 <= index < len(self.columns) and len(self.columns) > 1:
            column = self.columns[index]
            hide = menu.Append(wx.ID_ANY, f"Hide “{column.title}”")
            self.Bind(
                wx.EVT_MENU,
                lambda _e: self.set_shown_columns(
                    [c.key for c in self.columns if c.key != column.key]
                ),
                hide,
            )
        choose = menu.Append(wx.ID_ANY, "Choose Columns...")
        self.Bind(wx.EVT_MENU, lambda _e: self.choose_columns(), choose)
        self.grid.PopupMenu(menu)
        menu.Destroy()

    # ---- CSV export -----------------------------------------------------

    def export_csv(self) -> None:
        if not self.components:
            self.show_message("There are no rows to export.", wx.ICON_INFORMATION)
            return
        if not self._confirm_export_while_loading():
            return
        directory = self.settings.get("export_directory", "")
        if not directory or not os.path.isdir(directory):
            directory = self._project_directory()
        name = os.path.splitext(self.board.name)[0] or "board"
        dialog = wx.FileDialog(
            self,
            f"Export {len(self.components)} rows × {len(self.columns)} columns as CSV",
            defaultDir=directory,
            defaultFile=f"{name}_components.csv",
            wildcard="CSV files (*.csv)|*.csv",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if show_modal(dialog) != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            write_csv(path, self.columns, self.components)
        except OSError as e:
            self.show_message(f"Could not write {path}.\n\n{e}", wx.ICON_ERROR)
            return
        self.settings.set("export_directory", os.path.dirname(path))
        self.summary.SetLabelText(
            f"Exported {len(self.components)} rows × {len(self.columns)} columns to {path}"
        )

    def _project_directory(self) -> str:
        try:
            return self.board.document.project.path or os.path.expanduser("~")
        except AttributeError:
            return os.path.expanduser("~")

    # ---- JLCPCB columns (loaded in the background) ----------------------

    def on_remote_update(self, _code: str) -> None:
        """A part finished loading (or failed)."""
        if not self:
            return
        if not self._remote_timer.IsRunning():
            self._remote_timer.Start(150)
        self._update_loading_status()

    def _after_remote_data(self) -> None:
        if not self:
            return
        if self.pricing:
            self.pricing.refresh()  # prices of newly loaded parts
        selected = {c.key for c in self.selected_components()}
        # Specifications and raw fields of newly loaded parts become available
        # columns; rebuild if one of them is a chosen column.
        if not self.remote.pending and self._update_available_columns():
            self.rebuild_columns(selected)
            return
        sort_column = next((c for c in self.columns if c.key == self.sort_key), None)
        if sort_column is not None and sort_column.remote:
            self.apply_filter_and_sort(selected)
        else:
            self.grid.ForceRefresh()  # cells compute their text when painted

    def _update_loading_status(self) -> None:
        pending = self.remote.pending
        if pending:
            done = self.remote.total_requested - pending
            self.loading_text.SetLabelText(
                f"Loading JLCPCB data… {done} of {self.remote.total_requested} parts"
            )
            if not self.loading_indicator.IsShown():
                self.loading_indicator.Show()
                self.loading_indicator.Start()
        else:
            if self.loading_indicator.IsShown():
                self.loading_indicator.Stop()
                self.loading_indicator.Hide()
            failed = self.remote.failed
            self.loading_text.SetLabelText(
                f"{failed} part(s) failed to load from JLCPCB — click Reload to retry"
                if failed
                else ""
            )
        self.loading_text.GetParent().Layout()

    def _confirm_export_while_loading(self) -> bool:
        visible_remote = [c.title for c in self.columns if c.remote]
        if not visible_remote or not self.remote.pending:
            return True
        answer = message(
            self,
            f"JLCPCB data is still loading for {self.remote.pending} part(s), so some cells in "
            f"{', '.join(visible_remote)} would be exported as “Loading…”.\n\n"
            "Export anyway?",
            "Export CSV",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        return answer == wx.ID_YES

    # ---- Selection: highlight on the board ----------------------------

    def on_selection_changed(self, event) -> None:
        event.Skip()
        if getattr(self, "_restoring_selection", False):
            return
        # The grid updates its selection after this event; read it afterwards.
        wx.CallAfter(self._selection_changed)

    def _selection_changed(self) -> None:
        if self:
            self.update_summary()
            self._selection_timer.Start(150)

    def _on_selection_settled(self) -> None:
        self.highlight(self.selected_components())

    def highlight(self, components: List[Component]) -> None:
        """Select these footprints on the board (and center the view on them if ticked)."""
        try:
            self.board.clear_selection()
            if components:
                self.board.add_to_selection([c.footprint_instance for c in components])
                if self.zoom_checkbox.GetValue():
                    self.kicad.run_action(CENTER_ON_SELECTION_ACTION)
        except KICAD_ERRORS as e:
            self.summary.SetLabelText(f"Could not highlight on the board: {e}")

    # ---- LCSC Part Detail window -----------------------------------------

    def open_details_for_selection(self) -> None:
        code = self.selected_lcsc()
        if code:
            self.show_details(code)

    def show_details(self, lcsc: str) -> None:
        """Open the LCSC Part Detail window for an LCSC part number, e.g. "C25744"."""
        if not self.details:
            self.details = LcscPartDetailsFrame(self, self.client)
            self._place_details_window()
        self.details.show_part(lcsc)
        self.details.Show()
        if self.details.IsIconized():
            self.details.Iconize(False)
        # Bring it in front of the table (and everything else).
        self.details.Raise()

    def _place_details_window(self) -> None:
        """Put the details window to the right of the table if it fits on screen."""
        display = wx.Display(max(0, wx.Display.GetFromWindow(self))).GetClientArea()
        rect = self.GetScreenRect()
        width, height = self.details.GetSize()
        x = min(rect.GetRight() + 10, display.GetRight() - width)
        y = max(display.GetTop(), min(rect.GetTop(), display.GetBottom() - height))
        self.details.SetPosition((max(display.GetLeft(), x), y))

    # ---- Pricing Calculator ---------------------------------------------

    def show_pricing(self) -> None:
        """Open (or bring to the front) the Pricing Calculator window."""
        if not self.pricing:
            self.pricing = PricingCalculatorFrame(self)
            self._place_below(self.pricing)
        else:
            self.pricing.refresh()
        self.pricing.Show()
        if self.pricing.IsIconized():
            self.pricing.Iconize(False)
        self.pricing.Raise()

    def _place_below(self, window: wx.Window) -> None:
        """Put `window` a little below and to the right of the table, kept on screen."""
        display = wx.Display(max(0, wx.Display.GetFromWindow(self))).GetClientArea()
        rect = self.GetScreenRect()
        width, height = window.GetSize()
        x = min(rect.GetLeft() + 40, display.GetRight() - width)
        y = min(rect.GetTop() + 60, display.GetBottom() - height)
        window.SetPosition((max(display.GetLeft(), x), max(display.GetTop(), y)))

    # ---- Assigning ------------------------------------------------------

    def assign(self, components: List[Component], parent: wx.Window) -> None:
        """Search JLCPCB and write the chosen part to `components`' LCSC field."""
        if not components:
            return
        dialog = PartSearchDialog(parent, self.client, components)
        try:
            if show_modal(dialog) != wx.ID_OK or dialog.selected_part is None:
                return
            part = dialog.selected_part
        finally:
            dialog.Destroy()
        self.write_lcsc(components, part.lcsc, parent)

    def show_message(self, text: str, icon: int, parent: Optional[wx.Window] = None) -> None:
        message(parent or self, text, "KiCad JLC", wx.OK | icon)

    # ---- Window ---------------------------------------------------------

    def _on_key(self, event: wx.KeyEvent) -> None:
        # Escape closes the window, but not while editing a cell (it cancels the edit).
        if event.GetKeyCode() == wx.WXK_ESCAPE and not self.grid.IsCellEditControlEnabled():
            self.Close()
        else:
            event.Skip()

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self.grid.IsCellEditControlEnabled():
            self.grid.DisableCellEditControl()
        self._selection_timer.Stop()
        self._remote_timer.Stop()
        self.remote.shutdown()
        self._save_column_widths()
        try:
            self.board.clear_selection()
        except KICAD_ERRORS:
            pass
        event.Skip()  # continue closing; this also closes the details window


def show_error(text: str) -> None:
    dialog = wx.MessageDialog(None, text, "KiCad JLC", wx.OK | wx.ICON_ERROR | wx.STAY_ON_TOP)
    dialog.ShowModal()
    dialog.Destroy()


def main() -> int:
    app = wx.App()  # noqa: F841 - must exist before creating any window

    try:
        kicad = KiCad(timeout_ms=KICAD_TIMEOUT_MS)
        board = kicad.get_board()
    except KICAD_ERRORS as e:
        show_error(f"Could not read the board from KiCad.\n\n{e}")
        return 1

    try:
        settings_dir = kicad.get_plugin_settings_path(plugin_identifier())
    except (OSError, ValueError, KeyError) + KICAD_ERRORS:
        settings_dir = None  # settings won't be remembered, but the plugin still works
    settings = Settings(settings_dir)

    frame = ComponentsFrame(kicad, board, settings)
    frame.Show()
    # The plugin runs as a separate process, so bring the window to the front
    # once it is on screen; otherwise it can open behind the KiCad window.
    # (Calling Raise() before the window is shown crashes wxPython on macOS.)
    wx.CallAfter(frame.Raise)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
