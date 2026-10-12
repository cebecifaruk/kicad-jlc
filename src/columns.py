"""Component table columns: definitions, user ordering/visibility, and CSV export.

Columns come in groups:
  - board:   data stored in the KiCad project (reference, value, footprint, ...)
  - custom:  one column per custom footprint field (MPN, LCSC Part #, ...)
  - lcsc:    data fetched from JLCPCB for the part in the LCSC field
  - spec:    one column per JLCPCB specification ("Tolerance", "Voltage Rating", ...)
  - raw:     every other simple field of JLCPCB's reply, under its API name
The spec and raw columns are discovered from the parts that have been loaded.

Which columns are shown, and in which order, is a list of column keys:
DEFAULT_COLUMNS below until the user picks other columns (then the user's list
is saved in the settings). Columns that aren't shown are listed after the
shown ones in the column chooser, in the order of `all_columns`.
"""

import csv
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import wx

from board_data import Component, natural_key
from matching import footprint_matches, value_matches
from windows import message, show_modal

GROUP_LABELS = {
    "board": "Board",
    "custom": "Custom field",
    "lcsc": "JLCPCB",
    "spec": "JLCPCB specification",
    "raw": "JLCPCB raw data",
}

# Quantities for the "LCSC Price @ N" columns
# (the price at the lowest quantity is the "LCSC Price ($)" column)
PRICE_QUANTITIES = [10, 100, 1000, 5000]

# Columns shown by default, in this order. Every other column is hidden until
# the user turns it on in the column chooser.
DEFAULT_COLUMNS = [
    "reference",
    "lcsc",
    "value",
    "jlcpcb:value",
    "footprint",
    "jlcpcb:footprint",
    "side",
    "jlcpcb:stock",
    "jlcpcb:price",
]

# Background colours, with a fixed text colour so they read well in light and dark mode
COMPUTED_COLOUR = (215, 232, 252)  # light blue: header of a column fetched from JLCPCB
UNASSIGNED_COLOUR = (220, 50, 50)  # red: the empty LCSC cell of a component without a part number
UNASSIGNED_TEXT_COLOUR = (255, 255, 255)
# yellow: warnings, i.e. Value / Footprint cells (board and LCSC) that don't agree
# with each other, and the LCSC and LCSC Stock cells of parts that are out of stock
WARNING_COLOUR = (255, 214, 64)
WARNING_TEXT_COLOUR = (20, 20, 20)


@dataclass
class Column:
    key: str  # stable id used in settings, e.g. "reference", "field:MPN", "spec:Tolerance"
    title: str
    width: int
    get: Callable[[Component], Any]
    group: str = "board"
    numeric: bool = False  # right-aligned
    editable: bool = False  # can be edited in the table
    # What makes the cell a warning (highlighted in yellow):
    #   "value" / "footprint": the board's value/footprint doesn't agree with the
    #     LCSC part (Value <-> LCSC Value, Footprint <-> LCSC Footprint)
    #   "stock": the LCSC part is out of stock
    check: str = ""

    @property
    def custom(self) -> bool:
        return self.group == "custom"

    @property
    def remote(self) -> bool:
        return self.group in ("lcsc", "spec", "raw")


BOARD_COLUMNS = [
    Column("reference", "Reference", 90, lambda c: c.reference),
    # Out-of-stock parts are also marked here, so it shows even with LCSC Stock hidden
    Column("lcsc", "LCSC", 95, lambda c: c.lcsc, editable=True, check="stock"),
    Column("value", "Value", 120, lambda c: c.value, check="value"),
    Column("footprint", "Footprint", 260, lambda c: c.footprint, check="footprint"),
    Column("side", "Side", 70, lambda c: c.side),
    Column("x", "X (mm)", 80, lambda c: c.x_mm, numeric=True),
    Column("y", "Y (mm)", 80, lambda c: c.y_mm, numeric=True),
    Column("rotation", "Rotation (°)", 90, lambda c: c.rotation, numeric=True),
    Column("mounting", "Type", 60, lambda c: c.mounting),
    Column("dnp", "DNP", 50, lambda c: c.dnp),
]


def warning(column: Column, component: Component, loader) -> bool:
    """True if this cell should be highlighted as a warning:
      - a Value or Footprint column (board or LCSC) where the component's board
        value/footprint doesn't agree with its loaded LCSC part, or
      - the LCSC or LCSC Stock column of a part that is out of stock.
    Unknown cases (part not loaded, unrecognised package...) are not warnings."""
    if not column.check:
        return False
    details = loader.get(component.lcsc)
    if details is None:
        return False
    if column.check == "value":
        return value_matches(component.value, details) is False
    if column.check == "footprint":
        return footprint_matches(component.footprint, details.package) is False
    if column.check == "stock":
        return details.stock <= 0
    return False


def _humanize(key: str) -> str:
    """ "leastNumberPrice" -> "least Number Price" (keeps the API name recognizable)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)


def _remote(loader, key, title, width, attribute, group="lcsc", **kwargs) -> Column:
    """A column whose cells come from the loaded JLCPCB details of the component's part."""

    def get(component: Component):
        details = loader.get(component.lcsc)
        if details is None:
            return loader.state_text(component.lcsc)
        value = attribute(details)
        return "" if value is None else value

    return Column(key, title, width, get, group=group, **kwargs)


def lcsc_columns(loader) -> List[Column]:
    """Fixed JLCPCB columns."""
    columns = [
        _remote(loader, "jlcpcb:value", "LCSC Value", 110, lambda d: d.value, check="value"),
        _remote(
            loader, "jlcpcb:footprint", "LCSC Footprint", 150, lambda d: d.package,
            check="footprint",
        ),
        _remote(
            loader, "jlcpcb:stock", "LCSC Stock", 90, lambda d: d.stock, numeric=True,
            check="stock",
        ),
        _remote(
            loader,
            "jlcpcb:price",
            "LCSC Price ($)",
            90,
            lambda d: d.price_breaks[0].unit_price if d.price_breaks else None,
            numeric=True,
        ),
        _remote(loader, "jlcpcb:mfr_part", "LCSC MFR Part", 170, lambda d: d.mfr_part),
        _remote(loader, "jlcpcb:manufacturer", "LCSC Manufacturer", 150, lambda d: d.manufacturer),
        _remote(loader, "jlcpcb:type", "LCSC Type", 80, lambda d: d.library_type),
    ]
    for quantity in PRICE_QUANTITIES:
        columns.append(
            _remote(
                loader,
                f"jlcpcb:price@{quantity}",
                f"LCSC Price @{quantity:,} ($)",
                110,
                lambda d, q=quantity: d.unit_price_at(q),
                numeric=True,
            )
        )
    columns += [
        _remote(loader, "jlcpcb:price_breaks", "LCSC Price Breaks", 320, lambda d: d.price_breaks_text),
        _remote(loader, "jlcpcb:category", "LCSC Category", 240, lambda d: d.category),
        _remote(loader, "jlcpcb:description", "LCSC Description", 300, lambda d: d.description),
        _remote(loader, "jlcpcb:datasheet", "LCSC Datasheet", 300, lambda d: d.datasheet_url),
        _remote(loader, "jlcpcb:url", "LCSC Page", 300, lambda d: d.jlcpcb_url),
    ]
    return columns


def discovered_columns(loader) -> List[Column]:
    """One column per specification and per raw field seen in the loaded parts."""
    spec_names: Dict[str, None] = {}
    raw_keys: Dict[str, bool] = {}  # key -> all values numeric
    for details in loader.loaded():
        for name, _value in details.attributes:
            spec_names.setdefault(name)
        for key, value in details.scalar_fields().items():
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            raw_keys[key] = raw_keys.get(key, True) and numeric

    columns = [
        _remote(loader, f"spec:{name}", f"LCSC {name}", 140, lambda d, n=name: d.attribute(n),
                group="spec")
        for name in sorted(spec_names, key=natural_key)
    ]
    columns += [
        _remote(loader, f"raw:{key}", f"LCSC raw: {_humanize(key)}", 140,
                lambda d, k=key: d.raw.get(k), group="raw",
                numeric=raw_keys[key])
        for key in sorted(raw_keys, key=str.lower)
    ]
    return columns


def custom_field_column(name: str) -> Column:
    return Column(f"field:{name}", name, 140, lambda c: c.fields.get(name, ""), group="custom")


def all_columns(components: Sequence[Component], loader) -> List[Column]:
    """Every available column: the default columns in DEFAULT_COLUMNS order,
    then the other board and JLCPCB columns, custom fields, and the discovered
    JLCPCB specification and raw-data columns."""
    names: Dict[str, None] = {}  # dict keeps first-seen order and drops duplicates
    for component in components:
        for name in component.fields:
            names.setdefault(name)
    columns = (
        BOARD_COLUMNS
        + lcsc_columns(loader)
        + [custom_field_column(n) for n in sorted(names, key=natural_key)]
        + discovered_columns(loader)
    )
    return arrange(columns, DEFAULT_COLUMNS)


def arrange(columns: Sequence[Column], shown: Iterable[str]) -> List[Column]:
    """The `shown` columns in that order, followed by all other columns in
    their order in `columns`. Keys in `shown` that aren't available (e.g. a
    specification of a part that isn't on this board) are skipped."""
    by_key = {c.key: c for c in columns}
    first = [by_key[key] for key in dict.fromkeys(shown) if key in by_key]
    placed = {c.key for c in first}
    return first + [c for c in columns if c.key not in placed]


def format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def sort_key(value):
    """Sort key that works for mixed cells (numbers, text, placeholders):
    numbers first in numeric order, then text in natural order."""
    if isinstance(value, bool):
        return (0, int(value), [])
    if isinstance(value, (int, float)):
        return (0, value, [])
    return (1, 0, natural_key("" if value is None else str(value)))


def write_csv(path: str, columns: Sequence[Column], components: Sequence[Component]) -> None:
    # utf-8-sig adds a BOM so Excel shows symbols such as Ω and ° correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([c.title for c in columns])
        for component in components:
            writer.writerow([format_cell(c.get(component)) for c in columns])


class ColumnChooserDialog(wx.Dialog):
    """Tick, filter and reorder columns. After OK, read `order` and `visible`."""

    ALL_GROUPS = "All groups"

    def __init__(self, parent: wx.Window, columns: Sequence[Column], visible: Dict[str, bool]):
        super().__init__(
            parent, title="Table Columns", size=(600, 640),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.STAY_ON_TOP,
        )
        self.columns = {c.key: c for c in columns}
        self.order = [c.key for c in columns]
        self.visible = dict(visible)
        # Shown columns are listed first, in table order.
        self.order.sort(key=lambda k: not self.visible[k])
        self.default_keys = [c.key for c in arrange(columns, DEFAULT_COLUMNS)]
        self.listed: List[str] = []  # keys currently in the list, in order

        intro = wx.StaticText(
            self,
            label="Tick the columns to show; use Move Up/Down to reorder them.\n"
            "JLCPCB columns are loaded for the part in each component's LCSC field. "
            "Specification and raw-data columns appear once parts have loaded.\n"
            "CSV export uses the same columns in the same order.",
        )
        self.search = wx.SearchCtrl(self)
        self.search.ShowCancelButton(True)
        self.search.SetDescriptiveText("Filter columns, e.g. price, tolerance, voltage")
        self.group = wx.Choice(self, choices=[self.ALL_GROUPS] + list(GROUP_LABELS.values()))
        self.group.SetSelection(0)
        self.only_shown = wx.CheckBox(self, label="Only shown columns")

        self.list = wx.CheckListBox(self)
        up = wx.Button(self, label="Move Up")
        down = wx.Button(self, label="Move Down")
        show_listed = wx.Button(self, label="Show Listed")
        hide_listed = wx.Button(self, label="Hide Listed")
        reset = wx.Button(self, label="Reset to Defaults")
        self.counter = wx.StaticText(self, label="")

        filters = wx.BoxSizer(wx.HORIZONTAL)
        filters.Add(self.search, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        filters.Add(self.group, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        filters.Add(self.only_shown, 0, wx.ALIGN_CENTER_VERTICAL)

        side = wx.BoxSizer(wx.VERTICAL)
        for button in (up, down):
            side.Add(button, 0, wx.EXPAND | wx.BOTTOM, 4)
        side.AddSpacer(12)
        for button in (show_listed, hide_listed):
            side.Add(button, 0, wx.EXPAND | wx.BOTTOM, 4)
        side.AddSpacer(12)
        side.Add(reset, 0, wx.EXPAND)

        middle = wx.BoxSizer(wx.HORIZONTAL)
        middle.Add(self.list, 1, wx.EXPAND | wx.RIGHT, 8)
        middle.Add(side, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(intro, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(filters, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(middle, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.counter, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(sizer)

        self.search.Bind(wx.EVT_TEXT, lambda _e: self.refresh())
        self.search.Bind(wx.EVT_SEARCH_CANCEL, lambda _e: self.search.SetValue(""))
        self.group.Bind(wx.EVT_CHOICE, lambda _e: self.refresh())
        self.only_shown.Bind(wx.EVT_CHECKBOX, lambda _e: self.refresh())
        self.list.Bind(wx.EVT_CHECKLISTBOX, self._on_check)
        up.Bind(wx.EVT_BUTTON, lambda _e: self._move(-1))
        down.Bind(wx.EVT_BUTTON, lambda _e: self._move(+1))
        show_listed.Bind(wx.EVT_BUTTON, lambda _e: self._set_listed(True))
        hide_listed.Bind(wx.EVT_BUTTON, lambda _e: self._set_listed(False))
        reset.Bind(wx.EVT_BUTTON, lambda _e: self._reset())
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self.refresh()
        self.CentreOnParent()

    def _label(self, column: Column) -> str:
        if column.group == "board":
            return column.title
        return f"{column.title}   ({GROUP_LABELS[column.group]})"

    def _matches(self, column: Column) -> bool:
        text = self.search.GetValue().strip().lower()
        group = self.group.GetStringSelection()
        if group != self.ALL_GROUPS and GROUP_LABELS[column.group] != group:
            return False
        if self.only_shown.GetValue() and not self.visible[column.key]:
            return False
        return not text or text in column.title.lower() or text in column.key.lower()

    def refresh(self, keep_key: Optional[str] = None) -> None:
        self.listed = [k for k in self.order if self._matches(self.columns[k])]
        self.list.Set([self._label(self.columns[k]) for k in self.listed])
        self.list.SetCheckedItems([i for i, k in enumerate(self.listed) if self.visible[k]])
        if keep_key in self.listed:
            self.list.SetSelection(self.listed.index(keep_key))
        shown = sum(1 for v in self.visible.values() if v)
        self.counter.SetLabelText(
            f"{shown} of {len(self.order)} columns shown — listing {len(self.listed)}"
        )

    def _on_check(self, event: wx.CommandEvent) -> None:
        index = event.GetInt()
        self.visible[self.listed[index]] = self.list.IsChecked(index)
        self.refresh(self.listed[index])

    def _set_listed(self, value: bool) -> None:
        for key in self.listed:
            self.visible[key] = value
        self.refresh()

    def _reset(self) -> None:
        defaults = set(DEFAULT_COLUMNS)
        self.order = list(self.default_keys)
        self.visible = {key: key in defaults for key in self.order}
        self.refresh()

    def _move(self, step: int) -> None:
        """Move the selected column past the next listed one (works while filtered)."""
        index = self.list.GetSelection()
        if index == wx.NOT_FOUND or not 0 <= index + step < len(self.listed):
            return
        key, other = self.listed[index], self.listed[index + step]
        self.order.remove(key)
        target = self.order.index(other)
        self.order.insert(target + (1 if step > 0 else 0), key)
        self.refresh(key)

    def _on_ok(self, event: wx.CommandEvent) -> None:
        if not any(self.visible.values()):
            message(self, "Please keep at least one column.", "Table Columns")
            return
        event.Skip()

    @property
    def shown(self) -> List[str]:
        """Keys of the ticked columns, in their order."""
        return [key for key in self.order if self.visible[key]]


def choose_columns(
    parent: wx.Window, columns: Sequence[Column], visible: Dict[str, bool]
) -> Optional[List[str]]:
    """Show the column chooser. Returns the keys of the shown columns in order, or None."""
    dialog = ColumnChooserDialog(parent, columns, visible)
    try:
        if show_modal(dialog) != wx.ID_OK:
            return None
        return dialog.shown
    finally:
        dialog.Destroy()
