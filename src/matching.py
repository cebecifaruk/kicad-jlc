"""Checking whether a component's Value and Footprint agree with its LCSC part.

Both checks are tolerant rather than plain string comparisons:

Values
    Quantities are parsed with SI prefixes and units, so "100nF" = "0.1uF" =
    "100n", "100R" = "100Ω" = "100", "4k7" = "4.7kΩ", "1R0" = "1Ω". The first
    quantity of the KiCad value must equal the part's main value (the LCSC Value
    column, e.g. its Resistance or Capacitance). Further quantities with a unit
    ("100nF 50V 10%") must each appear among the part's specifications.
    Values that aren't quantities ("STM32F103C8Tx", "Red") are compared as
    text with the part's MFR number, value and specifications. Case and
    punctuation are ignored, a lowercase "x" in the KiCad value matches any
    character, and the MFR number may continue after the KiCad value
    ("LM358" matches "LM358DR").

Footprints
    A package key is extracted from the KiCad footprint name, which follows the
    KiCad standard library naming conventions, and from the JLCPCB package name.
    The keys are then compared, e.g. "Resistor_SMD:R_0603_1608Metric" = "0603",
    "Package_TO_SOT_SMD:SOT-23" = "SOT-23-3", "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm"
    = "QFN-32-EP(5x5)", "Diode_SMD:D_SMA" = "SMA(DO-214AC)".

Every check returns True (agrees), False (differs) or None (can't tell, e.g.
an empty value or a package name that isn't recognised). Only False is
highlighted.
"""

import math
import re
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

# ---- Values -------------------------------------------------------------

PREFIXES = {
    "p": 1e-12, "P": 1e-12,
    "n": 1e-9, "N": 1e-9,
    "u": 1e-6, "U": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3,
    "k": 1e3, "K": 1e3,
    "M": 1e6, "Meg": 1e6, "MEG": 1e6, "meg": 1e6,
    "G": 1e9,
    "R": 1.0, "r": 1.0,  # "1R0" = 1.0Ω
}

UNITS = {"ω": "Ω", "Ω": "Ω", "ohm": "Ω", "ohms": "Ω", "r": "Ω",
         "f": "F", "h": "H", "hz": "Hz", "v": "V", "a": "A", "w": "W", "%": "%"}
# Units that describe a component's main value (used when the KiCad value has no unit)
VALUE_UNITS = {"Ω", "F", "H", "Hz"}

_UNIT = r"(?P<unit>(?i:ohms?|hz)|[ΩΩRrFfHhVvAaWw%])?"
# "100nF", "0.1 uF", "10kΩ", "100R", "12MHz", "1%", "10"
_PLAIN = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?|[.,]\d+)\s?(?P<prefix>Meg|MEG|meg|[pPnNuUµμmkKMG])?" + _UNIT
)
# RKM notation: "4k7", "2n2", "1R0", "4u7"
_RKM = re.compile(r"(?P<int>\d+)(?P<prefix>[pnuµμmkKMGRr])(?P<frac>\d{1,3})" + _UNIT)
# Spaces, "/", ";", "@", "_", and commas that aren't decimal commas ("0,1uF")
_SEPARATORS = re.compile(r"[\s/;@_]+|,(?!\d)")

Quantity = Tuple[float, Optional[str]]  # (value in base units, unit or None)


def parse_quantity(token: str) -> Optional[Quantity]:
    """ "100nF" -> (1e-7, "F"), "4k7" -> (4700.0, None), "100R" -> (100.0, "Ω")."""
    token = token.strip().lstrip("±~")
    match = _RKM.fullmatch(token)
    if match:
        number = float(f"{match['int']}.{match['frac']}")
        prefix = match["prefix"]
        unit = match["unit"] or ("R" if prefix in "Rr" else None)
    else:
        match = _PLAIN.fullmatch(token)
        if not match:
            return None
        number = float(match["num"].replace(",", "."))
        prefix = match["prefix"] or ""
        unit = match["unit"]
    return number * PREFIXES.get(prefix, 1.0), UNITS[unit.lower()] if unit else None


def _tokens(text: str) -> List[str]:
    return [t for t in _SEPARATORS.split(text.strip()) if t]


def _same(a: Quantity, b: Quantity) -> bool:
    """Equal values, with the same unit if both have one."""
    if a[1] and b[1] and a[1] != b[1]:
        return False
    return math.isclose(a[0], b[0], rel_tol=1e-6, abs_tol=1e-15)


def _same_as_specification(q: Quantity, spec: Quantity) -> bool:
    """Like _same, but a specification without a unit never matches, and a
    quantity without a unit only matches value-like specifications."""
    if spec[1] is None or (q[1] is None and spec[1] not in VALUE_UNITS):
        return False
    return _same(q, spec)


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s\-_/.,()]+", "", text).casefold()


def _text_matches(text: str, lcsc_value: str, mfr_part: str, specs: Sequence[str]) -> bool:
    wanted = _normalize_text(text)
    if any(wanted == _normalize_text(other) for other in (lcsc_value, mfr_part, *specs) if other):
        return True
    if len(wanted) < 3 or not mfr_part:
        return False
    # The MFR number may continue after the value, and a lowercase "x" is a
    # wildcard as in KiCad symbol names ("STM32F103C8Tx").
    pattern = "".join(
        "." if ch == "x" else re.escape(ch.casefold())
        for ch in re.sub(r"[\s\-_/.,()]+", "", text)
    )
    return re.match(pattern, _normalize_text(mfr_part)) is not None


@lru_cache(maxsize=8192)
def _value_matches(
    value: str, lcsc_value: str, mfr_part: str, specs: Tuple[str, ...]
) -> Optional[bool]:
    value = value.strip()
    if not value or value == "~":
        return None
    tokens = _tokens(value)
    quantities = [parse_quantity(t) for t in tokens]
    if quantities[0] is None:
        return _text_matches(value, lcsc_value, mfr_part, specs)

    spec_quantities = [q for spec in specs for q in map(parse_quantity, _tokens(spec)) if q]
    lcsc_tokens = _tokens(lcsc_value)
    lcsc_main = parse_quantity(lcsc_tokens[0]) if lcsc_tokens else None
    main = quantities[0]
    if lcsc_main is not None:
        if not _same(main, lcsc_main):
            return False
    elif not any(_same_as_specification(main, q) for q in spec_quantities):
        return False
    # "50V", "1%": every further quantity with a unit must be a specification.
    for q in quantities[1:]:
        if q is not None and q[1] is not None:
            if not any(_same_as_specification(q, s) for s in spec_quantities):
                return False
    return True


def value_matches(value: str, details) -> Optional[bool]:
    """Does the KiCad `value` agree with the JLCPCB part `details`?
    (`details` is a jlcpcb_api.PartDetails)"""
    return _value_matches(
        value, details.value, details.mfr_part, tuple(v for _name, v in details.attributes)
    )


# ---- Footprints / packages ------------------------------------------------

# Package key: (kind, identifier, body size or None)
PackageKey = Tuple[str, str, Optional[Tuple[float, float]]]

_START = r"(?<![A-Z0-9])"
_END = r"(?![A-Z0-9])"

_CRYSTAL = re.compile(r"(?:CRYSTAL_SMD_|SMD[-_]?)(\d{4})[-_](\d)P")
_DO214 = re.compile(_START + r"DO-?214-?A([ABC])" + _END)
_SMX = re.compile(_START + r"(SM[ABC])" + _END)
_IC = re.compile(_START + r"([A-Z]{0,4}(?:QFN|DFN|SON|QFP|SOIC|SOP|LGA|BGA)|SO)-?(\d+)(?!\d)")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:MM|\))")
_SOT = re.compile(_START + r"T?(SOT|SC)-?(\d+)(A?)(?:-(\d+))?(?!\d)")
_SOD = re.compile(_START + r"SOD-?(\d+)(FL|F)?" + _END)
_MELF = re.compile(_START + r"(MINIMELF|MICROMELF|MELF|LL-?34|LL-?41)" + _END)
_DPAK = re.compile(_START + r"(D2PAK|D3PAK|DPAK|IPAK)" + _END)
_TO = re.compile(_START + r"TO-?(\d+)(?!\d)")
_DO = re.compile(_START + r"DO-?(\d+)(?!\d)")
_CHIP = re.compile(
    r"(?<!\d)(01005|0201|0402|0603|0805|1008|1206|1210|1806|1812|2010|2220|2512)(?!\d)"
)

# SOT/SC names -> (family, pin count); None = take the pin count from the name
_SOT_ALIASES = {
    "SOT-23": ("SOT-23", None), "SOT-346": ("SOT-23", 3), "SC-59": ("SOT-23", 3),
    "SOT-25": ("SOT-23", 5), "SOT-753": ("SOT-23", 5), "SC-74A": ("SOT-23", 5),
    "SOT-26": ("SOT-23", 6), "SOT-457": ("SOT-23", 6), "SC-74": ("SOT-23", 6),
    "SC-70": ("SC-70", None), "SOT-323": ("SC-70", 3),
    "SOT-353": ("SC-70", 5), "SC-88A": ("SC-70", 5),
    "SOT-363": ("SC-70", 6), "SC-88": ("SC-70", 6),
}
_DEFAULT_PINS = {"SOT-23": 3, "SC-70": 3, "SOT-89": 3, "SOT-143": 4}
_SMX_BY_DO214 = {"C": "SMA", "A": "SMB", "B": "SMC"}
_TO_BY_PAK = {"DPAK": "252", "D2PAK": "263", "D3PAK": "268", "IPAK": "251"}


def _ic_family(name: str) -> str:
    if name.endswith("QFN"):
        return "QFN"
    if name.endswith(("DFN", "SON")):
        return "DFN"
    if name.endswith("QFP"):
        return "QFP"
    if name.endswith(("BGA", "LGA")):
        return name[-3:]
    if name.endswith("TSSOP"):
        return "TSSOP"
    if name in ("MSOP", "VSSOP", "HVSSOP", "EMSOP", "HMSOP"):
        return "MSOP"
    if name.endswith("SSOP"):
        return "SSOP"
    if name.endswith(("QSOP", "TSOP")):
        return name[-4:]
    return "SOIC"  # SO, SOP, SOIC, ESOP, HSOP...


@lru_cache(maxsize=4096)
def package_key(text: str) -> Optional[PackageKey]:
    """Package of a KiCad footprint ("Resistor_SMD:R_0603_1608Metric") or a
    JLCPCB package name ("0603"), e.g. ("CHIP", "0603", None); None if unknown."""
    name = text.split(":")[-1].upper().replace("µ", "U")
    if not name.strip():
        return None

    match = _CRYSTAL.search(name)
    if match:
        return ("CRYSTAL", f"{match[1]}-{match[2]}", None)

    match = _DO214.search(name)
    if match:
        return ("DIODE", _SMX_BY_DO214[match[1]], None)
    match = _SMX.search(name)
    if match:
        return ("DIODE", match[1], None)

    match = _IC.search(name)
    if match:
        family = _ic_family(match[1])
        size = None
        if family in ("QFN", "DFN", "QFP", "LGA", "BGA"):
            found = _SIZE.search(name, match.end())
            if found:
                size = tuple(sorted((float(found[1]), float(found[2]))))
        return ("IC", f"{family}-{int(match[2])}", size)

    match = _SOT.search(name)
    if match:
        base = f"{match[1]}-{match[2]}{match[3]}"
        family, pins = _SOT_ALIASES.get(base, (base, None))
        if pins is None and match[4]:
            pins = int(match[4])
        if family == "SOT-223":
            pins = None  # "SOT-223", "SOT-223-3_TabPin2" and "SOT-223-4" are the same
        elif pins is None:
            pins = _DEFAULT_PINS.get(family)
        return ("SOT", f"{family}-{pins}" if pins else family, None)

    match = _SOD.search(name)
    if match:
        return ("DIODE", f"SOD-{match[1]}{'F' if match[2] else ''}", None)
    match = _MELF.search(name)
    if match:
        kind = match[1].replace("-", "")
        return ("DIODE", "SOD-80" if kind in ("MINIMELF", "LL34") else kind, None)

    match = _DPAK.search(name)
    if match:
        return ("TO", f"TO-{_TO_BY_PAK[match[1]]}", None)
    match = _TO.search(name)
    if match:
        return ("TO", f"TO-{match[1]}", None)
    match = _DO.search(name)
    if match:
        return ("DIODE", f"DO-{match[1]}", None)

    match = _CHIP.search(name)
    if match:
        return ("CHIP", match[1], None)
    return None


def footprint_matches(footprint: str, lcsc_package: str) -> Optional[bool]:
    """Does the KiCad footprint agree with the JLCPCB package name?"""
    ours, theirs = package_key(footprint), package_key(lcsc_package)
    if ours is None or theirs is None:
        return None
    if ours[:2] != theirs[:2]:
        return False
    if ours[2] and theirs[2]:
        return all(abs(a - b) < 0.06 for a, b in zip(ours[2], theirs[2]))
    return True
