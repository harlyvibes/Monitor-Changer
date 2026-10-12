"""MCCS (Monitor Control Command Set) helpers: input names and capability parsing.

Pure Python with no Windows dependencies so it can be unit tested anywhere.
"""

import re

#: VCP code for "Input Select" (a.k.a. input source).
VCP_INPUT_SOURCE = 0x60

#: Input source values from the MCCS 2.2a spec, plus common vendor extensions.
INPUT_NAMES = {
    0x01: "VGA 1",
    0x02: "VGA 2",
    0x03: "DVI 1",
    0x04: "DVI 2",
    0x05: "Composite 1",
    0x06: "Composite 2",
    0x07: "S-Video 1",
    0x08: "S-Video 2",
    0x09: "Tuner 1",
    0x0A: "Tuner 2",
    0x0B: "Tuner 3",
    0x0C: "Component 1",
    0x0D: "Component 2",
    0x0E: "Component 3",
    0x0F: "DisplayPort 1",
    0x10: "DisplayPort 2",
    0x11: "HDMI 1",
    0x12: "HDMI 2",
    # Not in the spec, but widely used (Dell, HP, Lenovo, ...) for USB-C / Thunderbolt.
    0x1B: "USB-C",
}

#: Shown when a monitor answers DDC/CI but doesn't list its inputs in its capabilities.
FALLBACK_INPUTS = (0x0F, 0x10, 0x11, 0x12, 0x1B, 0x03, 0x01)

# A VCP code, optionally followed by its value list (tolerating a truncated string).
_HEX_CODE = re.compile(r"([0-9A-Fa-f]{2})\s*(?:\(([^()]*)(?:\)|$))?")
_HEX_BYTE = re.compile(r"[0-9A-Fa-f]{2}")


def input_name(code):
    """Human readable name for an input source value."""
    return INPUT_NAMES.get(code, f"Input 0x{code:02X}")


def _squash(text):
    return re.sub(r"[\s_-]", "", text).casefold()


_INPUT_ALIASES = {_squash(name): code for code, name in INPUT_NAMES.items()}
_INPUT_ALIASES.update({f"dp{n}": 0x0E + n for n in (1, 2)})
_INPUT_ALIASES.update({"usbc": 0x1B, "typec": 0x1B, "thunderbolt": 0x1B})


def parse_input(text):
    """Parse an input given as a code ("0x11", "17") or a name ("HDMI 1", "hdmi1", "dp2")."""
    try:
        code = int(text, 0)
    except ValueError:
        code = _INPUT_ALIASES.get(_squash(text))
    if code is None or not 0 <= code <= 0xFF:
        raise ValueError(f"unknown input {text!r}")
    return code


def _top_level_groups(text):
    """Yield ``(key, body)`` for each ``key(body)`` group at the top level of *text*."""
    i, n = 0, len(text)
    while i < n:
        open_at = text.find("(", i)
        if open_at < 0:
            return
        key = text[i:open_at].strip()
        depth = 0
        for close_at in range(open_at, n):
            if text[close_at] == "(":
                depth += 1
            elif text[close_at] == ")":
                depth -= 1
                if depth == 0:
                    break
        else:
            # Unbalanced: treat the rest of the string as the body.
            close_at = n
        # Stray ')' characters from sloppy firmware end up in the key; drop them.
        yield key.replace(")", "").strip().lower(), text[open_at + 1:close_at]
        i = close_at + 1


def parse_capabilities(caps):
    """Parse a DDC/CI capabilities string.

    Returns a dict of the top-level groups (``model``, ``type``, ``mccs_ver``, ...)
    with the ``vcp`` group replaced by a dict mapping each VCP code to the list of
    values it supports (empty when the monitor didn't enumerate them).
    """
    caps = (caps or "").replace("\x00", "").strip()
    # The whole string is normally wrapped in one pair of parentheses.
    groups = list(_top_level_groups(caps))
    if groups and groups[0][0] == "":
        groups = list(_top_level_groups(groups[0][1])) + groups[1:]

    result = {}
    for key, body in groups:
        if key == "vcp":
            vcp = {}
            for match in _HEX_CODE.finditer(body):
                code = int(match.group(1), 16)
                values = match.group(2)
                vcp[code] = [int(v, 16) for v in _HEX_BYTE.findall(values)] if values else []
            result["vcp"] = vcp
        elif key:
            result[key] = body.strip()
    return result


def supported_inputs(caps):
    """Input source values a monitor advertises in its capabilities string."""
    return parse_capabilities(caps).get("vcp", {}).get(VCP_INPUT_SOURCE, [])
