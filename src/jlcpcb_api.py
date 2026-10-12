"""Small client for JLCPCB's public parts endpoints.

Every request goes straight to JLCPCB: there is no local or downloaded parts
database. These are the same (unofficial, undocumented) endpoints the JLCPCB
website uses, so they can change without notice:
  - part search:   POST {BASE_URL}/selectSmtComponentList
  - part details:  GET  {DETAIL_URL}?componentCode=C25744
  - images:        GET  {IMAGE_URL}{access id}

`requests` is used instead of urllib because KiCad's bundled Python on macOS
cannot verify HTTPS certificates with urllib, while `requests` ships its own
CA bundle (certifi).
"""

import re
import socket
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3.util.connection

BASE_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood"
DETAIL_URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail"
IMAGE_URL = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/"
HOST = "jlcpcb.com"
PART_PAGE_URL = "https://jlcpcb.com/partdetail/"
# (connect, read) timeouts in seconds. JLCPCB sometimes answers slowly when it
# is busy or throttling, so allow a generous read timeout.
TIMEOUT_S = (10, 60)
USER_AGENT = "Mozilla/5.0 (KiCad JLCPCB plugin)"

LCSC_PATTERN = re.compile(r"^C\d+$")

# Library type filter values shown in the UI -> request fields
LIBRARY_TYPES = {
    "All": {},
    "Basic": {"componentLibraryType": "base"},
    "Extended": {"componentLibraryType": "expand"},
    "Preferred (Extended)": {"preferredComponentFlag": True},
}


_family_lock = threading.Lock()
_family_checked = False


def _choose_address_family() -> None:
    """Avoid long hangs on networks where IPv6 is advertised but doesn't work.

    Python (unlike browsers and curl) tries every IPv6 address before falling
    back to IPv4, waiting for a full timeout on each. This races one IPv4 and
    one IPv6 connection to JLCPCB once and restricts this process to whichever
    family connects first. If neither connects, nothing is changed.
    """
    global _family_checked
    with _family_lock:
        if _family_checked:
            return
        _family_checked = True

        try:
            infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        except OSError:
            return
        targets = {}
        for family, _type, _proto, _name, address in infos:
            targets.setdefault(family, address)
        if len(targets) < 2:
            return

        def try_connect(family, address):
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(address)
            return family

        pool = ThreadPoolExecutor(max_workers=len(targets))
        futures = [pool.submit(try_connect, f, a) for f, a in targets.items()]
        working = set()
        pending = set(futures)
        while pending and not working:
            done, pending = wait(pending, timeout=4, return_when=FIRST_COMPLETED)
            if not done:
                break
            working |= {f.result() for f in done if f.exception() is None}
        pool.shutdown(wait=False)

        if working == {socket.AF_INET}:
            urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
        elif working == {socket.AF_INET6}:
            urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET6


class JlcpcbError(Exception):
    pass


class PartNotFound(JlcpcbError):
    """JLCPCB doesn't know this part number."""


# Specifications that best describe a part's "value", most specific first.
# Used for the LCSC Value column; parts with none of these (ICs, connectors...)
# use their manufacturer part number instead.
VALUE_ATTRIBUTES = [
    "Resistance",
    "Capacitance",
    "Inductance",
    "Impedance @ Frequency",
    "Frequency",
    "Zener Voltage(Nom)",
    "Output Voltage",
    "Hold Current",
    "Reverse Stand-Off Voltage (Vrwm)",
    "Illumination Color",
]


def normalize_lcsc(text: str) -> str:
    """ "c25744 " -> "C25744". Returns "" if it isn't an LCSC part number."""
    text = text.strip().upper()
    return text if LCSC_PATTERN.match(text) else ""


def _library_type(data: Dict[str, Any]) -> str:
    if data.get("componentLibraryType") == "base":
        return "Basic"
    if data.get("preferredComponentFlag"):
        return "Preferred"
    return "Extended"


def _part_url(data: Dict[str, Any]) -> str:
    suffix = data.get("urlSuffix")
    if suffix:
        return PART_PAGE_URL + suffix
    return PART_PAGE_URL + (data.get("componentCode") or "")


@dataclass
class Part:
    """One row of a search result."""

    lcsc: str
    mfr_part: str
    package: str
    manufacturer: str
    library_type: str  # "Basic", "Extended" or "Preferred"
    stock: int
    price: Optional[float]  # unit price at the lowest quantity break, USD
    description: str
    category: str
    url: str
    image_id: str = ""  # access id of the small product photo, "" if none

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "Part":
        prices = data.get("componentPrices") or []
        price = min(prices, key=lambda p: p.get("startNumber", 0))["productPrice"] if prices else None

        # Note: in search results JLCPCB puts the top-level category in
        # "secondSortName" and the sub-category in "firstSortName".
        category = " / ".join(
            name for name in (data.get("secondSortName"), data.get("firstSortName")) if name
        )

        return cls(
            lcsc=data.get("componentCode") or "",
            mfr_part=data.get("componentModelEn") or "",
            package=data.get("componentSpecificationEn") or "",
            manufacturer=data.get("componentBrandEn") or "",
            library_type=_library_type(data),
            stock=int(data.get("stockCount") or 0),
            price=price,
            description=data.get("describe") or "",
            category=category,
            url=_part_url(data),
            image_id=data.get("minImageAccessId") or "",
        )


@dataclass
class PriceBreak:
    min_qty: int
    max_qty: Optional[int]  # None = no upper limit
    unit_price: float  # USD

    @property
    def quantity_label(self) -> str:
        if self.max_qty is None:
            return f"{self.min_qty:,}+"
        return f"{self.min_qty:,} – {self.max_qty:,}"


@dataclass
class PartImage:
    thumbnail_id: str
    large_id: str


@dataclass
class PartDetails:
    """Everything the JLCPCB detail endpoint returns for one part."""

    lcsc: str
    mfr_part: str
    manufacturer: str
    package: str
    description: str
    category: str
    library_type: str
    stock: int
    price_breaks: List[PriceBreak]
    attributes: List[Tuple[str, str]]  # e.g. ("Resistance", "10kΩ")
    images: List[PartImage]
    datasheet_url: str
    lcsc_url: str
    jlcpcb_url: str
    raw: Dict[str, Any] = field(default_factory=dict)  # the full reply

    @property
    def value(self) -> str:
        """The part's main value, e.g. "10kΩ", "100nF", "12MHz", or its MFR part number."""
        by_name = {name.lower(): value for name, value in self.attributes}
        for name in VALUE_ATTRIBUTES:
            value = by_name.get(name.lower(), "").strip()
            if value and value != "-":
                return value
        return self.mfr_part

    def attribute(self, name: str) -> str:
        """One specification by name, e.g. attribute("Tolerance") -> "±1%"."""
        for attribute_name, value in self.attributes:
            if attribute_name == name:
                return value
        return ""

    def unit_price_at(self, quantity: int) -> Optional[float]:
        """Unit price (USD) when ordering `quantity` pieces, from the price breaks."""
        price = None
        for b in self.price_breaks:  # sorted by min_qty
            if b.min_qty <= max(quantity, self.price_breaks[0].min_qty):
                price = b.unit_price
        return price

    @property
    def price_breaks_text(self) -> str:
        """All price breaks in one line, e.g. "1+: $0.0034 · 1,000+: $0.0026"."""
        return " · ".join(f"{b.min_qty:,}+: ${b.unit_price:g}" for b in self.price_breaks)

    def scalar_fields(self) -> Dict[str, Any]:
        """Every simple (non-list, non-empty) value of the raw API reply."""
        return {
            key: value
            for key, value in self.raw.items()
            if value not in (None, "") and not isinstance(value, (list, dict))
        }

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "PartDetails":
        breaks = []
        for p in data.get("prices") or data.get("jlcPrices") or []:
            if p.get("productPrice") is None:
                continue
            end = p.get("endNumber")
            breaks.append(
                PriceBreak(
                    int(p.get("startNumber") or 0),
                    None if end in (None, -1) else int(end),
                    float(p["productPrice"]),
                )
            )
        breaks.sort(key=lambda b: b.min_qty)

        images: List[PartImage] = []
        seen = set()
        candidates = [
            (i.get("productImageAccessId"), i.get("productBigImageAccessId"))
            for i in data.get("imageList") or []
        ] + [(data.get("minImageAccessId"), data.get("productBigImageAccessId"))]
        for small, large in candidates:
            if (small or large) and (small, large) not in seen:
                seen.add((small, large))
                images.append(PartImage(small or large, large or small))

        # In the detail reply "first" is the top-level category (unlike search results).
        category = " / ".join(
            name
            for name in (data.get("firstTypeNameEn"), data.get("secondTypeNameEn"))
            if name
        )

        return cls(
            lcsc=data.get("componentCode") or "",
            mfr_part=data.get("componentModelEn") or "",
            manufacturer=data.get("componentBrandEn") or "",
            package=data.get("componentSpecificationEn") or "",
            description=data.get("describe") or "",
            category=category,
            library_type=_library_type(data),
            stock=int(data.get("stockCount") or 0),
            price_breaks=breaks,
            attributes=[
                (a.get("attribute_name_en") or "", str(a.get("attribute_value_name") or ""))
                for a in data.get("attributes") or []
                if a.get("attribute_name_en")
            ],
            images=images,
            datasheet_url=data.get("dataManualUrl") or "",
            lcsc_url=data.get("lcscGoodsUrl") or "",
            jlcpcb_url=_part_url(data),
            raw=dict(data),
        )


@dataclass
class SearchResult:
    parts: List[Part]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(1, -(-self.total // self.page_size))


class JlcpcbClient:
    """Thread-safe client: each call uses its own HTTP connection, so requests
    can run in background threads at the same time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # XSRF token. The search currently works without one, so it is only
        # fetched if JLCPCB rejects a request.
        self._token: Optional[str] = None
        self._categories: Optional[List[str]] = None
        # Caches for this session, so reopening a part doesn't download it again.
        self._details: Dict[str, PartDetails] = {}
        self._images: Dict[str, bytes] = {}

    def _fetch_token(self) -> str:
        _choose_address_family()
        with requests.Session() as session:
            session.get(
                f"{BASE_URL}/getXSRFToken", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S
            )
            return session.cookies.get("XSRF-TOKEN") or ""

    def _post(self, payload: Dict[str, Any], token: Optional[str]) -> requests.Response:
        _choose_address_family()
        headers = {"User-Agent": USER_AGENT}
        if token:
            headers["X-XSRF-TOKEN"] = token
            headers["Cookie"] = f"XSRF-TOKEN={token}"
        return requests.post(
            f"{BASE_URL}/selectSmtComponentList", json=payload, headers=headers, timeout=TIMEOUT_S
        )

    def _component_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            token = self._token
        try:
            response = self._post(payload, token)
            body = _json(response)
            if body.get("code") != 200 and response.status_code in (401, 403, 200):
                # Possibly a missing or expired token: get a new one and retry once.
                token = self._fetch_token()
                with self._lock:
                    self._token = token
                response = self._post(payload, token)
                body = _json(response)
        except requests.Timeout:
            raise JlcpcbError("JLCPCB did not answer in time. Please try again in a moment.")
        except requests.RequestException as e:
            raise JlcpcbError(f"Network error: {e}") from e

        if body.get("code") == 200:
            return body.get("data") or {}
        if response.status_code == 429 or body.get("code") == 429:
            raise JlcpcbError("JLCPCB is limiting requests. Please wait a minute and try again.")
        message = body.get("message") or f"HTTP {response.status_code}"
        raise JlcpcbError(f"JLCPCB search failed: {message}")

    def search(
        self,
        keyword: str = "",
        page: int = 1,
        page_size: int = 50,
        category: str = "",
        package: str = "",
        library_type: str = "All",
        in_stock_only: bool = True,
    ) -> SearchResult:
        payload: Dict[str, Any] = {
            "keyword": keyword.strip(),
            "currentPage": page,
            "pageSize": page_size,
            "searchType": 2,
            "presaleTypes": ["stock"] if in_stock_only else [],
        }
        if category:
            payload["firstSortName"] = category
        if package.strip():
            payload["componentSpecificationList"] = [package.strip()]
        payload.update(LIBRARY_TYPES.get(library_type, {}))

        data = self._component_list(payload)
        info = data.get("componentPageInfo") or {}
        parts = [Part.from_api(item) for item in info.get("list") or []]
        return SearchResult(parts, int(info.get("total") or 0), page, page_size)

    def categories(self) -> List[str]:
        """Top-level part categories, e.g. "Resistors", "Capacitors". Fetched once."""
        if self._categories is None:
            data = self._component_list({"searchType": 1, "currentPage": 1, "pageSize": 1})
            names = sorted(item["sortName"] for item in data.get("sortAndCountVoList") or [])
            if names:
                self._categories = names
        return self._categories or []

    def get_details(self, lcsc: str) -> PartDetails:
        """Full details of one part, e.g. get_details("C25744")."""
        code = normalize_lcsc(lcsc)
        if not code:
            raise JlcpcbError(f"{lcsc!r} is not an LCSC part number (expected e.g. C25744).")
        with self._lock:
            cached = self._details.get(code)
        if cached:
            return cached

        response = _get(DETAIL_URL, params={"componentCode": code})
        body = _json(response)
        data = body.get("data")
        if body.get("code") != 200 or not data:
            if body.get("code") == 400 or (body.get("code") == 200 and not data):
                raise PartNotFound(f"JLCPCB has no part {code}.")
            message = body.get("message") or f"HTTP {response.status_code}"
            raise JlcpcbError(f"Could not load {code} from JLCPCB: {message}")

        details = PartDetails.from_api(data)
        with self._lock:
            self._details[code] = details
        return details

    def get_image(self, access_id: str) -> bytes:
        """Download a product photo (JPEG bytes) by its access id."""
        with self._lock:
            cached = self._images.get(access_id)
        if cached is not None:
            return cached
        response = _get(IMAGE_URL + access_id)
        if response.status_code != 200 or not response.content:
            raise JlcpcbError(f"Image download failed (HTTP {response.status_code}).")
        with self._lock:
            self._images[access_id] = response.content
        return response.content


def _get(url: str, **kwargs) -> requests.Response:
    _choose_address_family()
    try:
        return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, **kwargs)
    except requests.Timeout:
        raise JlcpcbError("JLCPCB did not answer in time. Please try again in a moment.")
    except requests.RequestException as e:
        raise JlcpcbError(f"Network error: {e}") from e


def _json(response: requests.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
