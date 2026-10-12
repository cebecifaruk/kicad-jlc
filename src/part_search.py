"""Window for searching JLCPCB parts and picking one to assign."""

from typing import Dict, List, Optional

import wx

from board_data import Component, natural_key
from jlcpcb_api import LIBRARY_TYPES, JlcpcbClient, JlcpcbError, Part, SearchResult
from part_details import PartDetailsPanel, image_to_bitmap, load_image, placeholder_bitmap, run_in_background

PAGE_SIZE = 50
ALL_CATEGORIES = "All categories"
THUMB_SIZE = 40

# (title, width, value getter used for display and sorting)
RESULT_COLUMNS = [
    ("LCSC", 120, lambda p: p.lcsc),  # wide enough for the thumbnail + code
    ("MFR Part", 160, lambda p: p.mfr_part),
    ("Package", 85, lambda p: p.package),
    ("Manufacturer", 140, lambda p: p.manufacturer),
    ("Type", 75, lambda p: p.library_type),
    ("Stock", 85, lambda p: p.stock),
    ("Price ($)", 70, lambda p: p.price),
    ("Category", 190, lambda p: p.category),
    ("Description", 360, lambda p: p.description),
]


def _format(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _sort_key(value):
    if value is None:
        return (1, 0)
    return (0, natural_key(value) if isinstance(value, str) else value)


class PartSearchDialog(wx.Dialog):
    """Search JLCPCB and return the chosen part via `selected_part`."""

    def __init__(self, parent: wx.Window, client: JlcpcbClient, components: List[Component]):
        super().__init__(
            parent,
            title="Assign LCSC Part Number",
            size=(1450, 780),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX | wx.STAY_ON_TOP,
        )
        self.client = client
        self.components = components
        self.selected_part: Optional[Part] = None
        self.parts: List[Part] = []
        self.page = 1
        self.page_count = 1
        self.sort_column: Optional[int] = None
        self.sort_ascending = True
        # Incremented for every search, so late replies from older searches are ignored.
        self._search_id = 0
        # Thumbnails: image access id -> index in the list's image list
        self._thumb_index: Dict[str, int] = {}
        self._preview_timer = wx.CallLater(250, self._update_preview)
        self._preview_timer.Stop()

        self._build_ui()
        self._prefill()
        self.CentreOnParent()

        run_in_background(self, self.client.categories, self._on_categories)
        self.search(page=1)

    # ---- UI -------------------------------------------------------------

    def _build_ui(self) -> None:
        refs = ", ".join(c.reference for c in self.components)
        if len(refs) > 120:
            refs = refs[:117] + "..."
        first = self.components[0]
        target = wx.StaticText(
            self,
            label=f"Assign to {len(self.components)} component(s): {refs}\n"
            f"Value: {first.value}    Footprint: {first.footprint}    "
            f"Current LCSC: {first.lcsc or '(none)'}",
        )

        self.keyword = wx.SearchCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.keyword.ShowCancelButton(True)
        self.keyword.SetDescriptiveText("Keyword, value, MFR part or LCSC number (e.g. C25744)")
        self.category = wx.Choice(self, choices=[ALL_CATEGORIES])
        self.category.SetSelection(0)
        self.package = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER, size=(110, -1))
        self.package.SetHint("e.g. 0402")
        self.library_type = wx.Choice(self, choices=list(LIBRARY_TYPES))
        self.library_type.SetSelection(0)
        self.in_stock = wx.CheckBox(self, label="In stock only")
        self.in_stock.SetValue(True)
        search_button = wx.Button(self, label="Search")

        filters = wx.FlexGridSizer(rows=2, cols=5, vgap=4, hgap=8)
        filters.AddGrowableCol(0, 1)
        for label in ("Search", "Category", "Package (exact)", "Library type", ""):
            filters.Add(wx.StaticText(self, label=label))
        filters.Add(self.keyword, 1, wx.EXPAND)
        filters.Add(self.category)
        filters.Add(self.package)
        filters.Add(self.library_type)
        row_end = wx.BoxSizer(wx.HORIZONTAL)
        row_end.Add(self.in_stock, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row_end.Add(search_button)
        filters.Add(row_end)

        # Results on the left, details of the highlighted result on the right.
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        splitter.SetMinimumPaneSize(300)
        self.results = wx.ListCtrl(
            splitter, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_HRULES | wx.LC_VRULES
        )
        self.thumbs = wx.ImageList(THUMB_SIZE, THUMB_SIZE)
        self.thumbs.Add(placeholder_bitmap(THUMB_SIZE))  # index 0: no image (yet)
        self.results.SetImageList(self.thumbs, wx.IMAGE_LIST_SMALL)
        for index, (title, width, _get) in enumerate(RESULT_COLUMNS):
            self.results.InsertColumn(index, title, width=width)
        self.preview = PartDetailsPanel(splitter, self.client, image_size=200)
        splitter.SplitVertically(self.results, self.preview, -520)
        splitter.SetSashGravity(1.0)

        self.status = wx.StaticText(self, label="")
        self.prev_button = wx.Button(self, label="< Previous")
        self.next_button = wx.Button(self, label="Next >")
        self.assign_button = wx.Button(self, wx.ID_OK, label="Assign")
        cancel_button = wx.Button(self, wx.ID_CANCEL)
        self.assign_button.SetDefault()

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        bottom.Add(self.prev_button, 0, wx.RIGHT, 4)
        bottom.Add(self.next_button, 0, wx.RIGHT, 8)
        bottom.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL)
        bottom.Add(cancel_button, 0, wx.RIGHT, 4)
        bottom.Add(self.assign_button)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(target, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(filters, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)

        search_button.Bind(wx.EVT_BUTTON, lambda _e: self.search(page=1))
        self.keyword.Bind(wx.EVT_TEXT_ENTER, lambda _e: self.search(page=1))
        self.keyword.Bind(wx.EVT_SEARCH, lambda _e: self.search(page=1))
        self.keyword.Bind(wx.EVT_SEARCH_CANCEL, self._on_keyword_cancel)
        self.package.Bind(wx.EVT_TEXT_ENTER, lambda _e: self.search(page=1))
        self.category.Bind(wx.EVT_CHOICE, lambda _e: self.search(page=1))
        self.library_type.Bind(wx.EVT_CHOICE, lambda _e: self.search(page=1))
        self.in_stock.Bind(wx.EVT_CHECKBOX, lambda _e: self.search(page=1))
        self.prev_button.Bind(wx.EVT_BUTTON, lambda _e: self.search(page=self.page - 1))
        self.next_button.Bind(wx.EVT_BUTTON, lambda _e: self.search(page=self.page + 1))
        self.assign_button.Bind(wx.EVT_BUTTON, self._on_assign)
        self.results.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_assign)  # double-click / Enter
        self.results.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_result_selected)
        self.results.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda _e: self._update_buttons())
        self.results.Bind(wx.EVT_LIST_COL_CLICK, self._on_column_click)
        self._update_buttons()

    def _prefill(self) -> None:
        """Start with a search for the first component's value and package."""
        first = self.components[0]
        if first.lcsc:
            self.keyword.SetValue(first.lcsc)
            return
        self.keyword.SetValue(first.value)
        self.package.SetValue(first.package)

    def _on_keyword_cancel(self, _event) -> None:
        self.keyword.SetValue("")
        self.search(page=1)

    def _update_buttons(self) -> None:
        self.assign_button.Enable(self.results.GetFirstSelected() != -1)
        self.prev_button.Enable(self.page > 1)
        self.next_button.Enable(self.page < self.page_count)

    # ---- Searching ------------------------------------------------------

    def search(self, page: int) -> None:
        self._search_id += 1
        search_id = self._search_id
        category = self.category.GetStringSelection()
        kwargs = dict(
            keyword=self.keyword.GetValue(),
            page=max(1, page),
            page_size=PAGE_SIZE,
            category="" if category == ALL_CATEGORIES else category,
            package=self.package.GetValue(),
            library_type=self.library_type.GetStringSelection(),
            in_stock_only=self.in_stock.GetValue(),
        )
        self.status.SetLabel("Searching JLCPCB...")
        run_in_background(
            self,
            lambda: self.client.search(**kwargs),
            lambda result: self._on_results(search_id, result),
            lambda error: self._on_error(search_id, error),
        )

    def _on_results(self, search_id: int, result: SearchResult) -> None:
        if search_id != self._search_id:
            return
        self.parts = result.parts
        self.page = result.page
        self.page_count = result.page_count
        self.sort_column = None
        self._populate()
        self._load_thumbnails()
        if result.total:
            self.status.SetLabel(
                f"{result.total:,} parts found — page {result.page} of {result.page_count}"
            )
        else:
            self.status.SetLabel("No parts found. Try fewer words or clear the package filter.")
            self.preview.clear("")
        self.Layout()

    def _on_error(self, search_id: int, error: Exception) -> None:
        if search_id != self._search_id:
            return
        message = str(error) if isinstance(error, JlcpcbError) else f"Search failed: {error}"
        self.status.SetLabel(message)

    def _on_categories(self, categories: List[str]) -> None:
        current = self.category.GetStringSelection()
        self.category.Set([ALL_CATEGORIES] + categories)
        self.category.SetStringSelection(current if current in categories else ALL_CATEGORIES)
        self.Layout()

    def _populate(self) -> None:
        self.results.Freeze()
        try:
            self.results.DeleteAllItems()
            for row, part in enumerate(self.parts):
                cells = [_format(get(part)) for _t, _w, get in RESULT_COLUMNS]
                self.results.InsertItem(row, cells[0], self._thumb_index.get(part.image_id, 0))
                for column, text in enumerate(cells[1:], start=1):
                    self.results.SetItem(row, column, text)
        finally:
            self.results.Thaw()
        if self.parts:
            self.results.Select(0)
            self.results.Focus(0)
        self._update_buttons()

    # ---- Thumbnails -----------------------------------------------------

    def _load_thumbnails(self) -> None:
        search_id = self._search_id
        for image_id in {p.image_id for p in self.parts if p.image_id} - set(self._thumb_index):
            load_image(
                self,
                self.client,
                image_id,
                lambda data, i=image_id: self._on_thumbnail(search_id, i, data),
            )

    def _on_thumbnail(self, search_id: int, image_id: str, data: bytes) -> None:
        if image_id in self._thumb_index:
            return
        bitmap = image_to_bitmap(data, THUMB_SIZE)
        if bitmap is None:
            return
        self._thumb_index[image_id] = self.thumbs.Add(bitmap)
        if search_id != self._search_id:
            return
        for row, part in enumerate(self.parts):
            if part.image_id == image_id:
                self.results.SetItemImage(row, self._thumb_index[image_id])

    # ---- Selection / preview ---------------------------------------------

    def _on_result_selected(self, _event) -> None:
        self._update_buttons()
        self._preview_timer.Start(250)  # wait until arrow-key scrolling settles

    def _update_preview(self) -> None:
        part = self._current_part()
        if part:
            self.preview.load(part.lcsc)

    def _on_column_click(self, event: wx.ListEvent) -> None:
        column = event.GetColumn()
        if column == self.sort_column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column, self.sort_ascending = column, True
        get = RESULT_COLUMNS[column][2]
        self.parts.sort(key=lambda p: _sort_key(get(p)), reverse=not self.sort_ascending)
        self._populate()

    # ---- Actions --------------------------------------------------------

    def _current_part(self) -> Optional[Part]:
        index = self.results.GetFirstSelected()
        return self.parts[index] if 0 <= index < len(self.parts) else None

    def _on_assign(self, _event) -> None:
        part = self._current_part()
        if part is None:
            return
        self.selected_part = part
        self.EndModal(wx.ID_OK)
