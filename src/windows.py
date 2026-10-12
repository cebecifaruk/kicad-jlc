"""Showing dialogs in front of the always-on-top plugin windows.

The component table (and the details window) use wx.STAY_ON_TOP so they stay in
front of KiCad. A normal dialog then opens *behind* them on macOS. Our own
dialogs are also created with wx.STAY_ON_TOP, and `show_modal` additionally
lowers every always-on-top window while a dialog is open, which also covers
native dialogs (message boxes, the file dialog) whose level can't be set.
"""

import contextlib
from typing import Optional

import wx

# Native dialogs must not be raised before they are on screen (that crashes
# wxPython on macOS), and don't need to be: lowering the other windows suffices.
_NATIVE_DIALOGS = (wx.MessageDialog, wx.FileDialog, wx.DirDialog)


@contextlib.contextmanager
def lowered_always_on_top(exclude: Optional[wx.Window] = None):
    """Temporarily drop wx.STAY_ON_TOP from every shown top-level window."""
    lowered = []
    for window in wx.GetTopLevelWindows():
        if window is exclude or not window or not window.IsShown():
            continue
        style = window.GetWindowStyleFlag()
        if style & wx.STAY_ON_TOP:
            window.SetWindowStyleFlag(style & ~wx.STAY_ON_TOP)
            lowered.append(window)
    try:
        yield
    finally:
        for window in lowered:
            if window:  # may have been destroyed meanwhile
                window.SetWindowStyleFlag(window.GetWindowStyleFlag() | wx.STAY_ON_TOP)


def show_modal(dialog: wx.Dialog) -> int:
    """dialog.ShowModal(), with the dialog in front of every plugin window."""
    with lowered_always_on_top(exclude=dialog):
        if not isinstance(dialog, _NATIVE_DIALOGS):
            wx.CallAfter(lambda: dialog and dialog.IsShown() and dialog.Raise())
        return dialog.ShowModal()


def message(
    parent: Optional[wx.Window],
    text: str,
    caption: str = "KiCad JLC",
    style: int = wx.OK | wx.ICON_INFORMATION,
) -> int:
    """A message box in front of every plugin window. Returns wx.ID_OK/ID_YES/ID_NO..."""
    dialog = wx.MessageDialog(parent, text, caption, style)
    try:
        return show_modal(dialog)
    finally:
        dialog.Destroy()
