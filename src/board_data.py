"""Reading components from the board and writing LCSC part numbers back to it."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from kipy.board import Board
from kipy.board_types import Field, FootprintInstance
from kipy.proto.board.board_types_pb2 import BoardLayer, FootprintMountingStyle

NM_PER_MM = 1_000_000

# Name of the footprint field the LCSC (JLCPCB) part number is stored in.
LCSC_FIELD = "LCSC"

# Fields every footprint has, plus LCSC, which gets its own column.
# All other fields are shown as custom field columns.
NON_CUSTOM_FIELDS = {"reference", "value", "footprint", "datasheet", "description", "lcsc"}

MOUNTING_NAMES = {
    FootprintMountingStyle.FMS_SMD: "SMD",
    FootprintMountingStyle.FMS_THROUGH_HOLE: "THT",
}

# Package names to pull out of a footprint name, used to prefill the part search.
# e.g. "R_0402_1005Metric" -> "0402", "SOT-23-5" -> "SOT-23-5"
PACKAGE_PATTERNS = [
    re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)"),
    re.compile(
        r"\b(SOT-\d+(?:-\d+)?|SOD-\d+[A-Z]*|SOIC-\d+|SOP-\d+|TSSOP-\d+|MSOP-\d+|"
        r"[LT]?QFP-\d+|[VUW]?QFN-\d+|DFN-\d+|SMA|SMB|SMC|TO-\d+(?:-\d+)?)",
        re.IGNORECASE,
    ),
]


@dataclass
class Component:
    key: str  # KIID as a string, used to find the footprint again
    footprint_instance: FootprintInstance
    reference: str
    value: str
    footprint: str
    side: str
    x_mm: float
    y_mm: float
    rotation: float
    mounting: str
    dnp: bool
    lcsc: str
    # Custom field name -> value, e.g. {"MPN": "RC0402FR-0710KL"}
    fields: Dict[str, str] = field(default_factory=dict)
    exclude_from_bom: bool = False

    @property
    def package(self) -> str:
        """Best guess of the package from the footprint name, or ""."""
        name = self.footprint.split(":")[-1]
        for pattern in PACKAGE_PATTERNS:
            match = pattern.search(name)
            if match:
                return match.group(1).upper()
        return ""


def natural_key(text: str):
    """Sort key so that R2 comes before R10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _fields(fp: FootprintInstance) -> List[Field]:
    return [item for item in fp.definition.items if isinstance(item, Field)]


def _is_lcsc(f: Field) -> bool:
    return f.name.strip().lower() == LCSC_FIELD.lower()


def to_component(fp: FootprintInstance) -> Component:
    lib_id = fp.definition.id
    footprint = f"{lib_id.library}:{lib_id.name}" if lib_id.library else lib_id.name
    if fp.layer == BoardLayer.BL_F_Cu:
        side = "Top"
    elif fp.layer == BoardLayer.BL_B_Cu:
        side = "Bottom"
    else:
        side = BoardLayer.Name(fp.layer)

    custom = {}
    lcsc = ""
    for f in _fields(fp):
        name = f.name.strip()
        if _is_lcsc(f):
            lcsc = f.text.value
        elif name and name.lower() not in NON_CUSTOM_FIELDS:
            custom[name] = f.text.value

    return Component(
        key=fp.id.value,
        footprint_instance=fp,
        reference=fp.reference_field.text.value,
        value=fp.value_field.text.value,
        footprint=footprint,
        side=side,
        x_mm=round(fp.position.x / NM_PER_MM, 4),
        y_mm=round(fp.position.y / NM_PER_MM, 4),
        rotation=round(fp.orientation.degrees, 2),
        mounting=MOUNTING_NAMES.get(fp.attributes.mounting_style, ""),
        dnp=fp.attributes.do_not_populate,
        lcsc=lcsc,
        fields=custom,
        exclude_from_bom=fp.attributes.exclude_from_bill_of_materials,
    )


def get_components(footprints: Sequence[FootprintInstance]) -> List[Component]:
    components = [to_component(fp) for fp in footprints]
    components.sort(key=lambda c: natural_key(c.reference))
    return components


def _set_lcsc_field(fp: FootprintInstance, lcsc: str) -> bool:
    """Set the LCSC field; returns False if nothing had to change."""
    existing = [f for f in _fields(fp) if _is_lcsc(f)]
    if existing:
        if all(f.text.value == lcsc for f in existing):
            return False
        for f in existing:
            f.text.value = lcsc
        return True
    if not lcsc:
        return False  # clearing a field that doesn't exist

    # No LCSC field yet: add a hidden one, styled like the Value field.
    new = Field()
    new.name = LCSC_FIELD
    new.text.value = lcsc
    new.visible = False
    new.layer = fp.value_field.layer
    new.text.position = fp.position
    new.proto.text.text.attributes.CopyFrom(fp.value_field.proto.text.text.attributes)
    fp.definition.add_item(new)
    return True


def assign_lcsc(board: Board, components: Sequence[Component], lcsc: str) -> List[str]:
    """Write `lcsc` to the LCSC field of each component as one undoable step
    ("" clears the field).

    Returns the references that were updated (empty if nothing changed).
    """
    # Fetch fresh copies so edits made in KiCad since the table was loaded are kept.
    items = board.get_items_by_id([c.footprint_instance.id for c in components])
    footprints = [item for item in items if isinstance(item, FootprintInstance)]
    if not footprints:
        raise LookupError("The selected footprints no longer exist on the board. Click Reload.")

    footprints = [fp for fp in footprints if _set_lcsc_field(fp, lcsc)]
    if not footprints:
        return []

    references = sorted((fp.reference_field.text.value for fp in footprints), key=natural_key)
    commit = board.begin_commit()
    try:
        board.update_items(footprints)
    except Exception:
        board.drop_commit(commit)
        raise
    action = f"Assign LCSC {lcsc} to" if lcsc else "Clear LCSC of"
    board.push_commit(commit, f"{action} {', '.join(references)}")
    return references
