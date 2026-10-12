"""The component table: a wx.grid.Grid with a virtual data model.

Cells are computed on demand from the frame's `components` and `columns`, so
refreshing the view is cheap. Colours:
  - the LCSC cell of components without an LCSC part number: red
  - Value, LCSC Value, Footprint and LCSC Footprint cells where the board's
    value/footprint doesn't agree with the LCSC part: yellow (see matching.py)
  - the LCSC and LCSC Stock cells of parts that are out of stock: yellow
  - headers of computed columns (data fetched from JLCPCB): light blue
Only the LCSC column is editable.
"""

from typing import Dict, Optional, Tuple

import wx
import wx.grid as gridlib
from wx.lib.mixins.gridlabelrenderer import GridLabelRenderer, GridWithLabelRenderersMixin

from columns import (
    COMPUTED_COLOUR, UNASSIGNED_COLOUR, UNASSIGNED_TEXT_COLOUR, WARNING_COLOUR,
    WARNING_TEXT_COLOUR, Column, format_cell, warning,
)

DARK_TEXT = wx.Colour(20, 20, 20)
# Key of the column that holds the LCSC part number
LCSC_COLUMN = "lcsc"
# Cell highlight -> (background, text colour)
HIGHLIGHTS = {
    None: None,
    "unassigned": (UNASSIGNED_COLOUR, UNASSIGNED_TEXT_COLOUR),
    "warning": (WARNING_COLOUR, WARNING_TEXT_COLOUR),
}


class ComponentTable(gridlib.GridTableBase):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame  # has .components, .columns, .remote and .on_lcsc_edited()
        self._rows = 0
        self._cols = 0
        self._attrs: Dict[Tuple, gridlib.GridCellAttr] = {}

    # ---- Size and values --------------------------------------------------

    def GetNumberRows(self):
        return len(self.frame.components)

    def GetNumberCols(self):
        return len(self.frame.columns)

    def _cell(self, row, col):
        if 0 <= row < len(self.frame.components) and 0 <= col < len(self.frame.columns):
            return self.frame.components[row], self.frame.columns[col]
        return None, None

    def column(self, col) -> Optional[Column]:
        if 0 <= col < len(self.frame.columns):
            return self.frame.columns[col]
        return None

    def GetValue(self, row, col):
        component, column = self._cell(row, col)
        return format_cell(column.get(component)) if component else ""

    def SetValue(self, row, col, value):
        component, column = self._cell(row, col)
        if component and column.editable:
            # Write to the board after the editor has fully closed.
            wx.CallAfter(self.frame.on_lcsc_edited, component, value)

    def IsEmptyCell(self, row, col):
        return False

    def GetColLabelValue(self, col):
        return self.frame.columns[col].title if col < len(self.frame.columns) else ""

    def GetRowLabelValue(self, row):
        return ""

    # ---- Appearance -----------------------------------------------------

    def GetAttr(self, row, col, kind):
        component, column = self._cell(row, col)
        if component is None:
            key = (None, False, False)
        else:
            if column.key == LCSC_COLUMN and not component.lcsc.strip():
                highlight = "unassigned"
            elif warning(column, component, self.frame.remote):
                highlight = "warning"
            else:
                highlight = None
            key = (highlight, column.editable, column.numeric)
        attr = self._attrs.get(key)
        if attr is None:
            attr = self._make_attr(*key)
            self._attrs[key] = attr
        attr.IncRef()  # the grid takes a reference
        return attr

    @staticmethod
    def _make_attr(highlight, editable, numeric) -> gridlib.GridCellAttr:
        attr = gridlib.GridCellAttr()
        if HIGHLIGHTS[highlight]:
            background, text = HIGHLIGHTS[highlight]
            attr.SetBackgroundColour(wx.Colour(*background))
            attr.SetTextColour(wx.Colour(*text))
        attr.SetReadOnly(not editable)
        attr.SetAlignment(wx.ALIGN_RIGHT if numeric else wx.ALIGN_LEFT, wx.ALIGN_CENTRE)
        return attr

    # ---- Notifying the grid --------------------------------------------

    def sync(self, grid: gridlib.Grid) -> None:
        """Tell the grid that rows/columns were added or removed and values changed."""
        grid.BeginBatch()
        try:
            for current, new, deleted, appended in (
                (self._rows, self.GetNumberRows(),
                 gridlib.GRIDTABLE_NOTIFY_ROWS_DELETED, gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED),
                (self._cols, self.GetNumberCols(),
                 gridlib.GRIDTABLE_NOTIFY_COLS_DELETED, gridlib.GRIDTABLE_NOTIFY_COLS_APPENDED),
            ):
                if new < current:
                    grid.ProcessTableMessage(gridlib.GridTableMessage(self, deleted, new, current - new))
                elif new > current:
                    grid.ProcessTableMessage(gridlib.GridTableMessage(self, appended, new - current))
            self._rows, self._cols = self.GetNumberRows(), self.GetNumberCols()
            grid.ProcessTableMessage(
                gridlib.GridTableMessage(self, gridlib.GRIDTABLE_REQUEST_VIEW_GET_VALUES)
            )
        finally:
            grid.EndBatch()


class ColumnLabelRenderer(GridLabelRenderer):
    """Draws column headers; headers of JLCPCB (computed) columns are light blue."""

    def __init__(self, table: ComponentTable):
        self.table = table

    def Draw(self, grid, dc, rect, col):
        column = self.table.column(col)
        if column is not None and column.remote:
            dc.SetBrush(wx.Brush(wx.Colour(*COMPUTED_COLOUR)))
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.DrawRectangle(rect)
            text_colour = DARK_TEXT
        else:
            text_colour = grid.GetLabelTextColour()
        self.DrawBorder(grid, dc, rect)

        h_align, v_align = grid.GetColLabelAlignment()
        dc.SetBackgroundMode(wx.TRANSPARENT)
        dc.SetTextForeground(text_colour)
        dc.SetFont(grid.GetLabelFont())
        text_rect = wx.Rect(*rect)
        text_rect.Deflate(4, 2)
        grid.DrawTextRectangle(dc, grid.GetColLabelValue(col), text_rect, h_align, v_align)


class ComponentGrid(gridlib.Grid, GridWithLabelRenderersMixin):
    """The component table's grid, showing `frame.components` × `frame.columns`."""

    def __init__(self, parent: wx.Window, frame):
        gridlib.Grid.__init__(self, parent)
        GridWithLabelRenderersMixin.__init__(self)
        self.table = ComponentTable(frame)
        self.SetTable(self.table, takeOwnership=True)
        self.SetDefaultColLabelRenderer(ColumnLabelRenderer(self.table))

    def sync(self) -> None:
        """Show the frame's current rows and columns."""
        self.table.sync(self)
        # Column headers are drawn by our renderer, so repaint them too
        # (a column's group can change without the column count changing).
        self.GetGridColLabelWindow().Refresh()
