"""Background loading of JLCPCB part details for the table's JLCPCB columns.

Requests run on a small thread pool, so the table stays usable while data
arrives; each finished part is reported to the UI thread via `on_update`.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional

import wx

from jlcpcb_api import JlcpcbClient, PartDetails, PartNotFound, normalize_lcsc

LOADING = "Loading…"
NOT_FOUND = "(not found)"
FAILED = "(error)"
INVALID = "(invalid LCSC)"


class RemoteDetailsLoader:
    def __init__(
        self,
        window: wx.Window,
        client: JlcpcbClient,
        on_update: Callable[[str], None],
        max_parallel: int = 4,
    ):
        self.window = window
        self.client = client
        self.on_update = on_update  # called on the UI thread with the LCSC code
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="jlcpcb-details")
        self._details: Dict[str, PartDetails] = {}
        self._state: Dict[str, str] = {}  # code -> LOADING / NOT_FOUND / FAILED
        self._errors: Dict[str, str] = {}  # code -> error message, for tooltips/status

    # ---- Queries (UI thread) -------------------------------------------

    def get(self, lcsc: str) -> Optional[PartDetails]:
        return self._details.get(normalize_lcsc(lcsc))

    def state_text(self, lcsc: str) -> str:
        """Placeholder text for a cell whose part isn't loaded (yet)."""
        if not lcsc.strip():
            return ""
        code = normalize_lcsc(lcsc)
        if not code:
            return INVALID
        return self._state.get(code, "")

    @property
    def pending(self) -> int:
        return sum(1 for state in self._state.values() if state == LOADING)

    @property
    def total_requested(self) -> int:
        return len(self._details) + len(self._state)

    @property
    def failed(self) -> int:
        return sum(1 for state in self._state.values() if state == FAILED)

    def loaded(self) -> List[PartDetails]:
        """Every part loaded so far."""
        return list(self._details.values())

    # ---- Requests ---------------------------------------------------------

    def request(self, lcsc_numbers: Iterable[str]) -> None:
        """Start loading every part that isn't loaded, loading, or known missing."""
        for lcsc in lcsc_numbers:
            code = normalize_lcsc(lcsc)
            if not code or code in self._details or code in self._state:
                continue
            self._state[code] = LOADING
            self._pool.submit(self._fetch, code)

    def retry_failed(self) -> None:
        """Forget network errors so the next `request` tries those parts again."""
        for code in [c for c, state in self._state.items() if state == FAILED]:
            del self._state[code]
            self._errors.pop(code, None)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- Internals ------------------------------------------------------

    def _fetch(self, code: str) -> None:
        try:
            details = self.client.get_details(code)
        except PartNotFound as e:
            wx.CallAfter(self._deliver, code, None, NOT_FOUND, str(e))
        except Exception as e:  # network or unexpected reply
            wx.CallAfter(self._deliver, code, None, FAILED, str(e))
        else:
            wx.CallAfter(self._deliver, code, details, None, "")

    def _deliver(self, code: str, details: Optional[PartDetails], state: Optional[str], error: str):
        if not self.window:  # window closed meanwhile
            return
        if details is not None:
            self._details[code] = details
            self._state.pop(code, None)
        else:
            self._state[code] = state
            self._errors[code] = error
        self.on_update(code)
