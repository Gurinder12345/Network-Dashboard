"""
Interface identity normalization (Dell OS6 and OS10).

display name  : exactly what the switch printed (kept for the UI), e.g. "Te1/0/1",
                "Eth 1/1/1", "Port-channel 10".
canonical name: one spelling per interface, used as the database identity, e.g.
                OS6  Te1/0/1 / TenGigabitEthernet1/0/1 / te1/0/1      -> te1/0/1
                OS10 Eth 1/1/1 / Ethernet 1/1/1 / ethernet1/1/1        -> ethernet1/1/1
                     Po 10 / Port-channel 10 / port-channel10           -> port-channel10

Only the explicit aliases below are merged. Anything else is lower-cased with whitespace
removed and otherwise left alone, so two different interfaces are never folded together
by a guess. The platform matters: on OS6 "Po1" is port-channel 1 in OS6 spelling (po1),
on OS10 port-channels are spelled port-channel<N>.
"""

import re

# Long OS6 type names -> the short prefix OS6 prints in tables. Ordered longest first.
_OS6_ALIASES = (
    ("hundredgigabitethernet", "hu"),
    ("fortygigabitethernet", "fo"),
    ("twentyfivegigabitethernet", "tw"),
    ("twentyfivegige", "tw"),
    ("tengigabitethernet", "te"),
    ("gigabitethernet", "gi"),
    ("port-channel", "po"),
    ("portchannel", "po"),
)
_OS6_SHORT = ("hu", "fo", "tw", "te", "gi", "po")

_OS10_ALIASES = (
    ("ethernet", "ethernet"),
    ("eth", "ethernet"),
    ("port-channel", "port-channel"),
    ("portchannel", "port-channel"),
    ("po", "port-channel"),
    ("management", "mgmt"),
    ("mgmt", "mgmt"),
)

_PORT = re.compile(r"^\d+(/\d+)*(:\d+)?$")


def _squash(name):
    return re.sub(r"\s+", "", str(name or "")).lower()


def canonical_name(platform, name):
    """Canonical identity, or None when the name is empty."""
    text = _squash(name)
    if not text:
        return None
    aliases = _OS6_ALIASES if platform == "dell_os6" else _OS10_ALIASES if platform == "dell_os10" else ()
    for long_name, short in aliases:
        if text.startswith(long_name) and _PORT.match(text[len(long_name):]):
            return short + text[len(long_name):]
    if platform == "dell_os6":
        for short in _OS6_SHORT:
            if text.startswith(short) and _PORT.match(text[len(short):]):
                return text
    return text


def interface_type(platform, canonical):
    """ethernet | port_channel | management | other (from the canonical name only)."""
    if not canonical:
        return "other"
    if platform == "dell_os6":
        if canonical.startswith("po"):
            return "port_channel"
        if canonical.startswith(("gi", "te", "tw", "fo", "hu")):
            return "ethernet"
        if canonical.startswith("oob"):
            return "management"
        return "other"
    if canonical.startswith("port-channel"):
        return "port_channel"
    if canonical.startswith("ethernet"):
        return "ethernet"
    if canonical.startswith("mgmt"):
        return "management"
    return "other"


# Monitored V1 interface kinds (SVIs, loopbacks and null interfaces are not monitored).
MONITORED_TYPES = ("ethernet", "port_channel", "management")
