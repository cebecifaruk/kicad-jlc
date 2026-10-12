"""The Pricing Calculator window: what the board's parts cost for N boards.

Components are grouped by LCSC part number (see pricing.py). Each row shows a
part number, the components that use it, the pieces per board and for the
whole order, the price level (JLCPCB price break) chosen for that whole-order
quantity, the unit price and the line total. Changing the PCB quantity
recalculates everything, including the price level of every part.

The window reads the component table's components and its JLCPCB loader, so
parts are fetched only once, and it is refreshed by the table when the board
is reloaded or prices arrive.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

import wx
import wx.grid as gridlib

from board_data import Component
from columns import WARNING_COLOUR, WARNING_TEXT_COLOUR
from jlcpcb_api import normalize_lcsc
from pricing import PART, PricingLine, Quote, build_quote, format_money

TITLE = "Pricing Calculator"
PCB_QUANTITY_SETTING = "pricing_pcb_quantity"
DEFAULT_PCB_QUANTITY = 100
MAX_PCB_QUANTITY = 10_000_000
# The per-PCB price is a division, so it is rounded for display
PER_PCB_PLACES = Decimal("0.0001")

# (title, width, numeric)
COLUMNS = [
    ("LCSC Part", 90, False),
    ("Components", 220, False),
    ("Qty / PCB", 75, True),
    ("Total Qty", 85, True),
    ("Price Level (pcs)", 125, True),
    ("Unit Price", 85, True),
    ("Total Price", 95, True),
    ("Part", 270, False),
    ("Notes", 300, False),
]
COMPONENTS_COL = 1
NOTES_COL = len(COLUMNS) - 1


def _part_text(line: PricingLine) -> str:
    if line.kind != PART:
        return line.lcsc or "(none)"
    return line.lcsc


def _description(line: PricingLine) -> str:
    """Short description of the part: value · package · MFR part."""
    d = line.details
    if d is None:
        return ""
    return " · ".join(t for t in dict.fromkeys((d.value, d.package, d.mfr_part)) if t)


class PricingCalculatorFrame(wx.Frame):
    """`table` is the component table window (list_components.ComponentsFrame):
    it provides `all_components`, `remote` (the JLCPCB loader), `settings`,
    `reload()`, `highlight()` and `show_details()`."""

    def __init__(self, table):
        super().__init__(
            table,
            title=f"{TITLE} – {table.board.name}",
            size=(1380, 560),
            # Always on top like the component table, so it isn't hidden behind it.
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT | wx.STAY_ON_TOP,
        )
        self.table = table
        self.quote: Optional[Quote] = None
        saved = table.settings.get(PCB_QUANTITY_SETTING, DEFAULT_PCB_QUANTITY)
        self.pcb_quantity = saved if isinstance(saved, int) and saved >= 1 else DEFAULT_PCB_QUANTITY
        self._filling = False  # True while the grid is rewritten (ignore selection events)

        panel = self.panel = wx.Panel(self)

        quantity_label = wx.StaticText(panel, label="PCB Quantity:")
        quantity_label.SetFont(quantity_label.GetFont().Bold())
        self.quantity = wx.SpinCtrl(
            panel, min=1, max=MAX_PCB_QUANTITY, initial=self.pcb_quantity, size=(110, -1),
            style=wx.SP_ARROW_KEYS | wx.TE_PROCESS_ENTER,
        )
        self.quantity.SetToolTip("Number of boards to produce. Every part quantity is multiplied by it.")
        self.quantity.Bind(wx.EVT_SPINCTRL, self._on_quantity)
        self.quantity.Bind(wx.EVT_TEXT, self._on_quantity)
        self.quantity.Bind(wx.EVT_TEXT_ENTER, self._on_quantity)
        reload_button = wx.Button(panel, label="Reload")
        reload_button.SetToolTip("Read the board again and retry parts that failed to load")
        reload_button.Bind(wx.EVT_BUTTON, lambda _e: self.table.reload())
        hint = wx.StaticText(
            panel,
            label="Each part number is priced at the price level of its total quantity "
            "for all boards. Double-click a row for the part's details.",
        )
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(quantity_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        top.Add(self.quantity, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        top.Add(reload_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        top.Add(hint, 1, wx.ALIGN_CENTER_VERTICAL)

        self.grid = gridlib.Grid(panel)
        self.grid.CreateGrid(0, len(COLUMNS))
        self.grid.EnableEditing(False)
        self.grid.SetSelectionMode(gridlib.Grid.GridSelectRows)
        self.grid.SetRowLabelSize(0)
        self.grid.SetColLabelAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTRE)
        self.grid.SetCellHighlightPenWidth(0)
        self.grid.SetCellHighlightROPenWidth(0)
        self.grid.DisableDragRowSize()
        for index, (title, width, _numeric) in enumerate(COLUMNS):
            self.grid.SetColLabelValue(index, title)
            self.grid.SetColSize(index, width)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_double_click)
        self.grid.Bind(gridlib.EVT_GRID_RANGE_SELECTED, self._on_selection)
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, self._on_selection)

        self.total = wx.StaticText(panel, label="")
        self.total.SetFont(self.total.GetFont().Bold().Scaled(1.25))
        self.per_pcb = wx.StaticText(panel, label="")
        self.status = wx.StaticText(panel, label="")
        self.excluded = wx.StaticText(panel, label="")
        self.excluded.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        self.loading_indicator = wx.ActivityIndicator(panel)
        self.loading_indicator.Hide()

        totals = wx.BoxSizer(wx.HORIZONTAL)
        totals.Add(self.total, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        totals.Add(self.per_pcb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        totals.Add(self.loading_indicator, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        totals.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL)

        close_button = wx.Button(panel, wx.ID_CLOSE)
        close_button.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        bottom.Add(self.excluded, 1, wx.ALIGN_CENTER_VERTICAL)
        bottom.Add(close_button, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(totals, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.refresh()

    # ---- Calculation ----------------------------------------------------

    def refresh(self) -> None:
        """Recalculate from the table's current components and loaded prices."""
        if not self:
            return
        remote = self.table.remote
        self.quote = build_quote(
            self.table.all_components, self.pcb_quantity, remote.get, remote.state_text
        )
        self._fill_grid()
        self._update_totals()

    def _on_quantity(self, event: wx.CommandEvent) -> None:
        # While typing, the control's value isn't updated yet: parse the text.
        text = event.GetString() if event.GetEventType() == wx.wxEVT_TEXT else ""
        try:
            value = int(text.replace(",", "").strip()) if text else self.quantity.GetValue()
        except ValueError:
            return  # not a number (yet); keep the current quote
        if not 1 <= value <= MAX_PCB_QUANTITY or value == self.pcb_quantity:
            return
        self.pcb_quantity = value
        self.table.settings.set(PCB_QUANTITY_SETTING, value)
        self.refresh()

    # ---- Display ----------------------------------------------------------

    def _fill_grid(self) -> None:
        lines = self.quote.lines
        grid = self.grid
        self._filling = True
        grid.BeginBatch()
        try:
            rows = grid.GetNumberRows()
            if rows > len(lines):
                grid.DeleteRows(len(lines), rows - len(lines))
            elif rows < len(lines):
                grid.AppendRows(len(lines) - rows)
            for row, line in enumerate(lines):
                values = [
                    _part_text(line),
                    ", ".join(line.references),
                    f"{line.quantity:,}",
                    f"{line.total_quantity:,}",
                    line.price_break.quantity_label if line.price_break else "",
                    format_money(line.unit_price),
                    format_money(line.total_price),
                    _description(line),
                    "; ".join(line.notes),
                ]
                unpriced = not line.priced and not line.loading
                for col, (value, (_title, _width, numeric)) in enumerate(zip(values, COLUMNS)):
                    grid.SetCellValue(row, col, value)
                    grid.SetCellAlignment(
                        row, col, wx.ALIGN_RIGHT if numeric else wx.ALIGN_LEFT, wx.ALIGN_TOP
                    )
                    # Unpriced rows are yellow; priced rows only in Notes if there is a note
                    warn = unpriced or (col == NOTES_COL and line.warning)
                    if warn:
                        grid.SetCellBackgroundColour(row, col, wx.Colour(*WARNING_COLOUR))
                        grid.SetCellTextColour(row, col, wx.Colour(*WARNING_TEXT_COLOUR))
                    else:
                        grid.SetCellBackgroundColour(row, col, grid.GetDefaultCellBackgroundColour())
                        grid.SetCellTextColour(row, col, grid.GetDefaultCellTextColour())
                # Long lists of references wrap instead of being cut off.
                grid.SetCellRenderer(row, COMPONENTS_COL, gridlib.GridCellAutoWrapStringRenderer())
            grid.AutoSizeRows(setAsMin=False)
        finally:
            grid.EndBatch()
            self._filling = False
        grid.ForceRefresh()

    def _update_totals(self) -> None:
        quote = self.quote
        # The exact sum of the line totals shown above
        self.total.SetLabelText(f"Total Price: {format_money(quote.total_price)}")
        per_pcb = quote.price_per_pcb.quantize(PER_PCB_PLACES, rounding=ROUND_HALF_UP)
        self.per_pcb.SetLabelText(
            f"≈ {format_money(per_pcb)} per PCB × {quote.pcb_quantity:,} PCBs"
        )

        loading = quote.loading
        unpriced = [line for line in quote.unpriced_lines if not line.loading]
        messages = []
        if loading:
            messages.append(f"Loading prices for {loading} part number(s)…")
        if unpriced:
            count = sum(line.quantity for line in unpriced)
            messages.append(
                f"{len(unpriced)} row(s) ({count} component(s) per PCB) not priced: "
                "the total doesn't include them"
            )
        self.status.SetLabelText(" — ".join(messages))
        if unpriced:
            self.status.SetForegroundColour(wx.Colour(200, 60, 0))
        else:
            self.status.SetForegroundColour(wx.NullColour)

        if loading and not self.loading_indicator.IsShown():
            self.loading_indicator.Show()
            self.loading_indicator.Start()
        elif not loading and self.loading_indicator.IsShown():
            self.loading_indicator.Stop()
            self.loading_indicator.Hide()

        skipped = []
        if quote.dnp:
            skipped.append(f"DNP: {_references(quote.dnp)}")
        if quote.excluded_from_bom:
            skipped.append(f"excluded from BOM: {_references(quote.excluded_from_bom)}")
        self.excluded.SetLabelText(
            ("Not counted (not placed) — " + "; ".join(skipped)) if skipped else ""
        )
        self.excluded.SetToolTip(self.excluded.GetLabelText())
        self.panel.Layout()

    # ---- Interaction ------------------------------------------------------

    def _line_at(self, row: int) -> Optional[PricingLine]:
        if self.quote and 0 <= row < len(self.quote.lines):
            return self.quote.lines[row]
        return None

    def _on_double_click(self, event: gridlib.GridEvent) -> None:
        line = self._line_at(event.GetRow())
        if line is not None and normalize_lcsc(line.lcsc):
            self.table.show_details(line.lcsc)

    def _on_selection(self, event) -> None:
        event.Skip()
        if not self._filling:
            wx.CallAfter(self._highlight_selection)

    def _highlight_selection(self) -> None:
        if not self:
            return
        rows = set()
        for block in self.grid.GetSelectedRowBlocks():
            rows.update(range(block.GetTopRow(), block.GetBottomRow() + 1))
        components: List[Component] = []
        for row in sorted(rows):
            line = self._line_at(row)
            if line is not None:
                components += line.components
        if components:
            self.table.highlight(components)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
        else:
            event.Skip()


def _references(components: List[Component]) -> str:
    return ", ".join(c.reference for c in components)
