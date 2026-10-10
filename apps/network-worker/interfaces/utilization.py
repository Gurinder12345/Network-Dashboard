"""
Counter-delta utilization and error-delta detection. Pure functions (no I/O).

    rx_bps = (rx_bytes_now - rx_bytes_prev) * 8 / elapsed_seconds
    rx_utilization_pct = rx_bps / speed_bps * 100            (same for tx)

Returns None -- never a fabricated 0 -- whenever no trustworthy value exists:

  first sample (no previous)            -> None
  elapsed <= 0 or timestamps invalid    -> None
  elapsed > max_gap_seconds             -> None (an average over a long outage misleads)
  counter went backwards                -> None: counter clear, device reboot or a 64-bit
                                           wrap are indistinguishable without extra data,
                                           and a wrong guess shows a huge false spike
  speed unknown / 0                     -> None (the delta is still valid; % is not)
  result > 100 % (+ OVERSHOOT tolerance)-> None (implausible: speed changed, counters
                                           reset mid-interval, or a parse mismatch)
  result between 100 % and the tolerance-> clamped to 100.00 (timing jitter of the CLI read)

Error detection uses the same rules: an interface is "erroring" only when its error/CRC
counters INCREASED between two valid consecutive samples, never because a lifetime
counter is non-zero.
"""

from datetime import datetime

OVERSHOOT = 1.05


def _seconds(previous_at, current_at):
    if not isinstance(previous_at, datetime) or not isinstance(current_at, datetime):
        return None
    return (current_at - previous_at).total_seconds()


def counter_delta(previous, current):
    """Non-negative increase, or None (missing value or counter went backwards)."""
    if previous is None or current is None:
        return None
    if current < previous:
        return None
    return current - previous


def valid_interval(previous_at, current_at, max_gap_seconds):
    elapsed = _seconds(previous_at, current_at)
    if elapsed is None or elapsed <= 0 or elapsed > max_gap_seconds:
        return None
    return elapsed


def utilization_pct(previous_bytes, current_bytes, elapsed_seconds, speed):
    if elapsed_seconds is None or not speed or speed <= 0:
        return None
    delta = counter_delta(previous_bytes, current_bytes)
    if delta is None:
        return None
    pct = delta * 8 / elapsed_seconds / speed * 100
    if pct > 100 * OVERSHOOT:
        return None
    return round(min(pct, 100.0), 2)


def compute(previous, current, collected_at, max_gap_seconds):
    """
    previous: {"collected_at", "rx_bytes", ...} from the last stored sample, or None.
    current : normalized interface dict (counters + speed_bps).
    Returns {"rx_utilization_pct", "tx_utilization_pct", "errors_delta", "crc_delta",
             "discards_delta", "erroring"}; every value may be None.
    """
    result = {"rx_utilization_pct": None, "tx_utilization_pct": None, "errors_delta": None, "crc_delta": None,
              "discards_delta": None, "erroring": False}
    if not previous:
        return result
    elapsed = valid_interval(previous.get("collected_at"), collected_at, max_gap_seconds)
    if elapsed is None:
        return result

    speed = current.get("speed_bps")
    result["rx_utilization_pct"] = utilization_pct(previous.get("rx_bytes"), current.get("rx_bytes"), elapsed, speed)
    result["tx_utilization_pct"] = utilization_pct(previous.get("tx_bytes"), current.get("tx_bytes"), elapsed, speed)

    def total(*fields):
        # Fields missing on either side are ignored; any counter that went backwards
        # (clear/reboot) makes the whole delta unknown rather than an understated number.
        deltas = []
        for field in fields:
            before, after = previous.get(field), current.get(field)
            if before is None or after is None:
                continue
            if after < before:
                return None
            deltas.append(after - before)
        return sum(deltas) if deltas else None

    result["errors_delta"] = total("rx_errors", "tx_errors")
    result["crc_delta"] = total("crc_errors")
    result["discards_delta"] = total("input_discards", "output_discards")
    result["erroring"] = bool((result["errors_delta"] or 0) > 0 or (result["crc_delta"] or 0) > 0)
    return result


def status_of(admin_status, oper_status):
    """
    up         admin up (or unknown) and oper up -- a link cannot be up while shut down
    down       admin up and oper down
    admin_down admin down
    unknown    anything else (e.g. oper down but admin state not reported): never guessed
    """
    if admin_status == "down":
        return "admin_down"
    if oper_status == "up":
        return "up"
    if oper_status == "down" and admin_status == "up":
        return "down"
    return "unknown"
