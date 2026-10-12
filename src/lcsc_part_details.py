"""The LCSC Part Detail window: everything JLCPCB knows about one LCSC part.

The window is independent of the board: it is opened with nothing but an LCSC
part number (e.g. "C25744") and shows that part's photos, key facts,
specifications, price breaks, all raw data, and links to the datasheet and
the JLCPCB and LCSC pages.
"""

from typing import Optional

import wx

from jlcpcb_api import JlcpcbClient, normalize_lcsc
from part_details import PartDetailsPanel

TITLE = "LCSC Part Detail"


class LcscPartDetailsFrame(wx.Frame):
    """Floats above the component table; `show_part` switches which part it shows.

    `client` is only the connection to JLCPCB (shared so parts already loaded
    for the table aren't downloaded again); the part shown depends only on the
    LCSC part number passed to `show_part`.
    """

    def __init__(self, parent: Optional[wx.Window], client: Optional[JlcpcbClient] = None):
        super().__init__(
            parent,
            title=TITLE,
            size=(820, 720),
            # The component table is an always-on-top window. A plain
            # FRAME_FLOAT_ON_PARENT child ends up below it on macOS, so this
            # window must be always-on-top too, and is raised whenever it's shown.
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT | wx.STAY_ON_TOP,
        )
        self.client = client or JlcpcbClient()
        self.lcsc = ""  # the part number currently shown

        panel = wx.Panel(self)
        self.part = PartDetailsPanel(panel, self.client, image_size=260)
        close_button = wx.Button(panel, wx.ID_CLOSE)
        close_button.Bind(wx.EVT_BUTTON, lambda _e: self.Close())

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.part, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        panel.SetSizer(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    def show_part(self, lcsc: str) -> None:
        """Show the part with this LCSC part number, e.g. show_part("C25744")."""
        code = normalize_lcsc(lcsc)
        self.lcsc = code or lcsc.strip()
        self.SetTitle(f"{TITLE} – {self.lcsc}" if self.lcsc else TITLE)
        if code:
            self.part.load(code)
        else:
            self.part.clear(f"{lcsc.strip()!r} is not an LCSC part number (expected e.g. C25744).")

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
        else:
            event.Skip()
