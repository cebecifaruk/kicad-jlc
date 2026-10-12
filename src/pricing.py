"""Pricing calculation for a production run of the board (no UI here).

Components are grouped by their LCSC part number, and each part number is
priced once for the whole order: per-board quantity × PCB quantity pieces.
The price break is chosen from that total, never per component: pricing R1 and
R2 separately at the 100-piece price would miss the cheaper 200-piece price
they reach together.

Price breaks follow JLCPCB: the unit price of the highest break whose minimum
quantity is at or below the ordered quantity applies to every piece. If fewer
pieces are needed than the first break's minimum, that minimum has to be
bought, so the line is priced for the minimum quantity.

Money is calculated with Decimal so totals don't pick up float rounding errors.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from board_data import Component, natural_key
from jlcpcb_api import PartDetails, PriceBreak, normalize_lcsc
from remote_data import FAILED as FAILED_STATE
from remote_data import NOT_FOUND as NOT_FOUND_STATE

# Kind of a pricing line
PART = "part"  # a valid LCSC part number
INVALID = "invalid"  # LCSC field holds text that isn't an LCSC part number
UNASSIGNED = "unassigned"  # empty LCSC field


@dataclass
class PricingLine:
    """One LCSC part number with the components that use it, priced for an order."""

    kind: str
    lcsc: str  # "C25744"; the field text for INVALID; "" for UNASSIGNED
    components: List[Component]
    pcb_quantity: int
    details: Optional[PartDetails] = None
    price_break: Optional[PriceBreak] = None  # the selected price level
    order_quantity: int = 0  # pieces bought (total, or the minimum order if higher)
    unit_price: Optional[Decimal] = None
    total_price: Optional[Decimal] = None
    notes: List[str] = field(default_factory=list)
    loading: bool = False  # the part's details haven't arrived yet

    @property
    def references(self) -> List[str]:
        return [c.reference for c in self.components]

    @property
    def quantity(self) -> int:
        """Pieces per board."""
        return len(self.components)

    @property
    def total_quantity(self) -> int:
        """Pieces needed for every board."""
        return self.quantity * self.pcb_quantity

    @property
    def priced(self) -> bool:
        return self.total_price is not None

    @property
    def warning(self) -> bool:
        """Not priced, or priced but something needs attention (stock, minimum order)."""
        return not self.loading and bool(self.notes)


@dataclass
class Quote:
    pcb_quantity: int
    lines: List[PricingLine]
    # Components that aren't bought at all, with the reason
    dnp: List[Component] = field(default_factory=list)
    excluded_from_bom: List[Component] = field(default_factory=list)

    @property
    def priced_lines(self) -> List[PricingLine]:
        return [line for line in self.lines if line.priced]

    @property
    def unpriced_lines(self) -> List[PricingLine]:
        return [line for line in self.lines if not line.priced]

    @property
    def loading(self) -> int:
        return sum(1 for line in self.lines if line.loading)

    @property
    def total_price(self) -> Decimal:
        """Sum of every priced line (the unpriced ones are listed in `unpriced_lines`)."""
        return sum((line.total_price for line in self.priced_lines), Decimal(0))

    @property
    def price_per_pcb(self) -> Decimal:
        return self.total_price / self.pcb_quantity if self.pcb_quantity else Decimal(0)

    @property
    def complete(self) -> bool:
        return not self.unpriced_lines


def select_price_break(breaks: Sequence[PriceBreak], quantity: int) -> Tuple[Optional[PriceBreak], int]:
    """The price break that applies when `quantity` pieces are needed, and the
    quantity that has to be ordered (`quantity`, or the first break's minimum
    if that is higher). Returns (None, quantity) if there are no price breaks."""
    ordered = sorted(breaks, key=lambda b: b.min_qty)
    if not ordered:
        return None, quantity
    order_quantity = max(quantity, ordered[0].min_qty)
    selected = ordered[0]
    for price_break in ordered:
        if price_break.min_qty <= order_quantity:
            selected = price_break
    return selected, order_quantity


def to_decimal(value: float) -> Decimal:
    """0.0034 -> Decimal("0.0034") (via str, so the float's binary error isn't kept)."""
    return Decimal(str(value))


def group_components(components: Sequence[Component]) -> Tuple[List[Tuple[str, str, List[Component]]], List[Component], List[Component]]:
    """Group the components that are bought by their LCSC part number.

    Returns ([(kind, lcsc, components)], dnp, excluded_from_bom). DNP components
    and components excluded from the BOM aren't placed, so they aren't bought.
    Part numbers are compared after normalising ("c25744 " = "C25744").
    """
    groups: Dict[Tuple[str, str], List[Component]] = {}
    dnp: List[Component] = []
    excluded: List[Component] = []
    for component in components:
        if component.dnp:
            dnp.append(component)
            continue
        if component.exclude_from_bom:
            excluded.append(component)
            continue
        text = component.lcsc.strip()
        code = normalize_lcsc(text)
        if code:
            key = (PART, code)
        elif text:
            key = (INVALID, text)
        else:
            key = (UNASSIGNED, "")
        groups.setdefault(key, []).append(component)

    result = []
    for (kind, lcsc), members in groups.items():
        members.sort(key=lambda c: natural_key(c.reference))
        result.append((kind, lcsc, members))
    # Parts in order of their first reference (C1's part, then R1's, ...);
    # invalid part numbers after them and components without one last.
    order = {PART: 0, INVALID: 1, UNASSIGNED: 2}
    result.sort(key=lambda g: (order[g[0]], natural_key(g[2][0].reference)))
    return result, dnp, excluded


def price_line(
    kind: str,
    lcsc: str,
    components: List[Component],
    pcb_quantity: int,
    details: Optional[PartDetails],
    state: str = "",
) -> PricingLine:
    """Price one part number for `pcb_quantity` boards.

    `details` are the part's JLCPCB details (None if not loaded) and `state`
    the loader's state for it (see remote_data.py: LOADING, NOT_FOUND, FAILED).
    """
    line = PricingLine(kind, lcsc, list(components), pcb_quantity, details)
    line.order_quantity = line.total_quantity
    if kind == UNASSIGNED:
        line.notes.append("No LCSC part number: not priced")
        return line
    if kind == INVALID:
        line.notes.append(f"“{lcsc}” is not an LCSC part number: not priced")
        return line
    if details is None:
        if state == NOT_FOUND_STATE:
            line.notes.append("JLCPCB has no such part: not priced")
        elif state == FAILED_STATE:
            line.notes.append("Could not load the part from JLCPCB (click Reload in the table)")
        else:
            line.loading = True
            line.notes.append("Loading price…")
        return line

    price_break, order_quantity = select_price_break(details.price_breaks, line.total_quantity)
    if price_break is None:
        line.notes.append("JLCPCB lists no price for this part: not priced")
        return line
    line.price_break = price_break
    line.order_quantity = order_quantity
    line.unit_price = to_decimal(price_break.unit_price)
    line.total_price = line.unit_price * order_quantity
    if order_quantity > line.total_quantity:
        line.notes.append(f"Minimum order is {order_quantity:,} pcs: priced for {order_quantity:,}")
    if details.stock <= 0:
        line.notes.append("Out of stock")
    elif details.stock < order_quantity:
        line.notes.append(f"Only {details.stock:,} in stock")
    return line


def build_quote(
    components: Sequence[Component],
    pcb_quantity: int,
    get_details: Callable[[str], Optional[PartDetails]],
    get_state: Callable[[str], str] = lambda _code: "",
) -> Quote:
    """Price every component on the board for `pcb_quantity` boards.

    `get_details(code)` returns a part's loaded JLCPCB details or None, and
    `get_state(code)` the loader's state of a part that isn't loaded.
    """
    pcb_quantity = max(1, int(pcb_quantity))
    groups, dnp, excluded = group_components(components)
    lines = []
    for kind, lcsc, members in groups:
        details = get_details(lcsc) if kind == PART else None
        state = get_state(lcsc) if kind == PART and details is None else ""
        lines.append(price_line(kind, lcsc, members, pcb_quantity, details, state))
    return Quote(pcb_quantity, lines, dnp, excluded)


def format_money(value: Optional[Decimal]) -> str:
    """Exact dollar amount with at least 2 decimals:
    300 -> "$300.00", 0.68 -> "$0.68", 0.0034 -> "$0.0034", 1234.5 -> "$1,234.50"."""
    if value is None:
        return ""
    places = max(2, -value.normalize().as_tuple().exponent)
    return f"${value:,.{places}f}"
