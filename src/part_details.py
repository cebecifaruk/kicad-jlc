"""Shared UI pieces: background requests, product images and the part details panel."""

import io
import json
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import wx

from jlcpcb_api import JlcpcbClient, JlcpcbError, PartDetails

# Image downloads run on small shared pools so a page of search results
# doesn't open dozens of connections at once. Large photos get their own pool
# so they don't wait behind a page full of thumbnails.
_thumbnail_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="jlcpcb-thumb")
_photo_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="jlcpcb-photo")


def run_in_background(
    window: wx.Window,
    work: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[Exception], None] = lambda _e: None,
    pool: Optional[ThreadPoolExecutor] = None,
) -> None:
    """Run `work` off the UI thread, then call on_done/on_error on the UI thread.

    The callbacks are skipped if `window` has been closed in the meantime.
    """

    def deliver(callback, value):
        if window:  # False once the window is destroyed
            callback(value)

    def runner():
        try:
            result = work()
        except Exception as e:  # report any failure in the UI
            wx.CallAfter(deliver, on_error, e)
        else:
            wx.CallAfter(deliver, on_done, result)

    if pool is not None:
        pool.submit(runner)
    else:
        threading.Thread(target=runner, daemon=True).start()


def load_image(
    window: wx.Window,
    client: JlcpcbClient,
    access_id: str,
    on_done,
    on_error=None,
    large: bool = False,
):
    """Download an image in the background and pass its bytes to `on_done`.

    Use `large=True` for the main photo so it isn't queued behind thumbnails."""
    run_in_background(
        window,
        lambda: client.get_image(access_id),
        on_done,
        on_error or (lambda _e: None),
        _photo_pool if large else _thumbnail_pool,
    )


def image_to_bitmap(data: bytes, size: int) -> Optional[wx.Bitmap]:
    """Decode image bytes and fit them into a size x size white square."""
    image = wx.Image(io.BytesIO(data), wx.BITMAP_TYPE_ANY)
    if not image.IsOk():
        return None
    scale = min(size / image.GetWidth(), size / image.GetHeight())
    width = max(1, int(image.GetWidth() * scale))
    height = max(1, int(image.GetHeight() * scale))
    image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
    image.Resize((size, size), ((size - width) // 2, (size - height) // 2), 255, 255, 255)
    return wx.Bitmap(image)


def placeholder_bitmap(size: int, text: str = "") -> wx.Bitmap:
    bitmap = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bitmap)
    dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
    dc.Clear()
    dc.SetPen(wx.Pen(wx.Colour(210, 210, 210)))
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    dc.DrawRectangle(0, 0, size, size)
    if text:
        dc.SetTextForeground(wx.Colour(140, 140, 140))
        width, height = dc.GetTextExtent(text)
        dc.DrawText(text, (size - width) // 2, (size - height) // 2)
    dc.SelectObject(wx.NullBitmap)
    return bitmap


def make_list(parent: wx.Window, columns: List[Tuple[str, int]]) -> wx.ListCtrl:
    ctrl = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES)
    for index, (title, width) in enumerate(columns):
        ctrl.InsertColumn(index, title, width=width)
    return ctrl


def fill_list(ctrl: wx.ListCtrl, rows: List[Tuple[str, ...]]) -> None:
    ctrl.Freeze()
    try:
        ctrl.DeleteAllItems()
        for row_index, row in enumerate(rows):
            ctrl.InsertItem(row_index, row[0])
            for column, text in enumerate(row[1:], start=1):
                ctrl.SetItem(row_index, column, text)
    finally:
        ctrl.Thaw()


def flatten_raw(raw: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every non-empty value of the API reply as (key, text) rows."""
    rows = []
    for key in sorted(raw, key=str.lower):
        value = raw[key]
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, (list, dict)):
            text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        else:
            text = str(value)
        rows.append((key, text))
    return rows


class ImageGallery(wx.Panel):
    """A large product photo with a row of clickable thumbnails below it."""

    THUMB_SIZE = 56

    def __init__(self, parent: wx.Window, client: JlcpcbClient, size: int):
        super().__init__(parent)
        self.client = client
        self.size = size
        self._generation = 0  # bumped on every new part, to drop stale downloads
        self.main = wx.StaticBitmap(self, bitmap=placeholder_bitmap(size, "No image"))
        self.thumbs = wx.BoxSizer(wx.HORIZONTAL)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.main, 0, wx.ALIGN_CENTER_HORIZONTAL)
        sizer.Add(self.thumbs, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 6)
        self.SetSizer(sizer)

    def clear(self, text: str = "No image") -> None:
        self._generation += 1
        self.main.SetBitmap(placeholder_bitmap(self.size, text))
        self.thumbs.Clear(delete_windows=True)
        self.Layout()

    def show(self, details: PartDetails) -> None:
        self.clear("Loading..." if details.images else "No image")
        generation = self._generation
        if len(details.images) > 1:
            for image in details.images:
                button = wx.BitmapButton(
                    self, bitmap=placeholder_bitmap(self.THUMB_SIZE), style=wx.BORDER_NONE
                )
                button.SetToolTip("Show this photo")
                button.Bind(wx.EVT_BUTTON, lambda _e, i=image.large_id: self._show_large(i))
                self.thumbs.Add(button, 0, wx.ALL, 2)
                load_image(
                    self,
                    self.client,
                    image.thumbnail_id,
                    lambda data, b=button, g=generation: self._set_thumb(b, data, g),
                )
            self.Layout()
            self.GetParent().Layout()
        if details.images:
            self._show_large(details.images[0].large_id)

    def _show_large(self, access_id: str) -> None:
        generation = self._generation

        def done(data: bytes):
            if generation == self._generation:
                bitmap = image_to_bitmap(data, self.size)
                self.main.SetBitmap(bitmap or placeholder_bitmap(self.size, "Bad image"))

        def failed(_error):
            if generation == self._generation:
                self.main.SetBitmap(placeholder_bitmap(self.size, "Image unavailable"))

        load_image(self, self.client, access_id, done, failed, large=True)

    def _set_thumb(self, button: wx.BitmapButton, data: bytes, generation: int) -> None:
        if generation == self._generation and button:
            bitmap = image_to_bitmap(data, self.THUMB_SIZE)
            if bitmap:
                button.SetBitmap(bitmap)


class PartDetailsPanel(wx.Panel):
    """Shows everything JLCPCB returns for one LCSC part: photos, key facts,
    specifications, price breaks and the full raw reply."""

    FACTS = [
        ("Manufacturer", lambda d: d.manufacturer),
        ("MFR part", lambda d: d.mfr_part),
        ("Package", lambda d: d.package),
        ("Category", lambda d: d.category),
        ("Library type", lambda d: d.library_type),
        ("Stock", lambda d: f"{d.stock:,}"),
        (
            "Unit price",
            lambda d: f"${d.price_breaks[0].unit_price:g} (qty {d.price_breaks[0].quantity_label})"
            if d.price_breaks
            else "",
        ),
    ]

    def __init__(self, parent: wx.Window, client: JlcpcbClient, image_size: int = 260):
        super().__init__(parent)
        self.client = client
        self.details: Optional[PartDetails] = None
        self._request_id = 0

        self.title = wx.StaticText(self, label="")
        self.title.SetFont(self.title.GetFont().Bold().Scaled(1.3))
        self.status = wx.StaticText(self, label="")
        self.gallery = ImageGallery(self, client, image_size)

        facts = wx.FlexGridSizer(cols=2, vgap=3, hgap=10)
        self.fact_values: List[wx.StaticText] = []
        for label, _get in self.FACTS:
            name = wx.StaticText(self, label=label + ":")
            name.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            value = wx.StaticText(self, label="", style=wx.ST_ELLIPSIZE_END)
            facts.Add(name)
            facts.Add(value, 1, wx.EXPAND)
            self.fact_values.append(value)
        facts.AddGrowableCol(1, 1)
        self.description = wx.StaticText(self, label="")

        self.datasheet_button = wx.Button(self, label="Datasheet")
        self.jlcpcb_button = wx.Button(self, label="JLCPCB page")
        self.lcsc_button = wx.Button(self, label="LCSC page")
        self.datasheet_button.Bind(wx.EVT_BUTTON, lambda _e: self._open("datasheet_url"))
        self.jlcpcb_button.Bind(wx.EVT_BUTTON, lambda _e: self._open("jlcpcb_url"))
        self.lcsc_button.Bind(wx.EVT_BUTTON, lambda _e: self._open("lcsc_url"))
        links = wx.WrapSizer(wx.HORIZONTAL)
        for button in (self.datasheet_button, self.jlcpcb_button, self.lcsc_button):
            links.Add(button, 0, wx.RIGHT | wx.BOTTOM, 4)

        info = wx.BoxSizer(wx.VERTICAL)
        info.Add(facts, 0, wx.EXPAND)
        info.Add(self.description, 0, wx.EXPAND | wx.TOP, 8)
        info.Add(links, 0, wx.TOP, 8)
        self._info = info

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(self.gallery, 0, wx.RIGHT, 12)
        top.Add(info, 1, wx.EXPAND)

        self.notebook = wx.Notebook(self)
        self.spec_list = make_list(self.notebook, [("Parameter", 200), ("Value", 260)])
        self.price_list = make_list(self.notebook, [("Quantity", 160), ("Unit price (USD)", 140)])
        self.raw_list = make_list(self.notebook, [("Field", 200), ("Value", 600)])
        self.notebook.AddPage(self.spec_list, "Specifications")
        self.notebook.AddPage(self.price_list, "Pricing")
        self.notebook.AddPage(self.raw_list, "All data")

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.title, 0, wx.EXPAND)
        sizer.Add(self.status, 0, wx.EXPAND | wx.BOTTOM, 6)
        sizer.Add(top, 0, wx.EXPAND)
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.TOP, 8)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.clear("")

    # ---- Public API -----------------------------------------------------

    def clear(self, message: str) -> None:
        self._request_id += 1
        self.details = None
        self.title.SetLabelText("")
        self.status.SetLabelText(message)
        self.gallery.clear()
        for value in self.fact_values:
            value.SetLabelText("")
        self.description.SetLabelText("")
        for ctrl in (self.spec_list, self.price_list, self.raw_list):
            ctrl.DeleteAllItems()
        self._update_buttons()
        self.Layout()

    def load(self, lcsc: str) -> None:
        """Fetch and show the details of `lcsc` (e.g. "C25744") in the background."""
        if self.details and self.details.lcsc == lcsc.strip().upper():
            return
        self.clear(f"Loading {lcsc} from JLCPCB...")
        request_id = self._request_id

        def done(details: PartDetails):
            if request_id == self._request_id:
                self.show(details)

        def failed(error: Exception):
            if request_id == self._request_id:
                message = str(error) if isinstance(error, JlcpcbError) else f"Error: {error}"
                self.status.SetLabelText(message)
                self.gallery.clear("No data")

        run_in_background(self, lambda: self.client.get_details(lcsc), done, failed)

    def show(self, details: PartDetails) -> None:
        self.details = details
        self.title.SetLabelText(f"{details.lcsc}  ·  {details.mfr_part}")
        self.status.SetLabelText("")
        for value, (_label, get) in zip(self.fact_values, self.FACTS):
            value.SetLabelText(get(details) or "—")
        self.description.SetLabelText(details.description)
        self._wrap_description()
        fill_list(self.spec_list, details.attributes)
        fill_list(
            self.price_list,
            [(b.quantity_label, f"{b.unit_price:g}") for b in details.price_breaks],
        )
        fill_list(self.raw_list, flatten_raw(details.raw))
        self.gallery.show(details)
        self._update_buttons()
        self.Layout()

    # ---- Internals ------------------------------------------------------

    def _update_buttons(self) -> None:
        d = self.details
        self.datasheet_button.Enable(bool(d and d.datasheet_url))
        self.jlcpcb_button.Enable(bool(d and d.jlcpcb_url))
        self.lcsc_button.Enable(bool(d and d.lcsc_url))

    def _open(self, attribute: str) -> None:
        url = getattr(self.details, attribute, "") if self.details else ""
        if url:
            webbrowser.open(url)

    def _wrap_description(self) -> None:
        if self.details:
            width = max(150, self.GetClientSize().width - self.gallery.size - 30)
            self.description.SetLabelText(self.details.description)
            self.description.Wrap(width)

    def _on_size(self, event) -> None:
        event.Skip()
        wx.CallAfter(self._rewrap)

    def _rewrap(self) -> None:
        if self:
            self._wrap_description()
            self.Layout()
