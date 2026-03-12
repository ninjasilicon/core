"""
Parsers for Maersk schedule API responses.

Maersk's internal API (intercepted via Playwright) returns JSON that follows
the pattern below.  We normalise it into our SQLAlchemy models here.

Known endpoint patterns (may change without notice):
  GET /api/schedules/point-to-point
  GET /schedules/api/point-to-point-schedules
  Any URL matching: *maersk.com*schedule*pointToPoint*
                    *maersk.com*api*schedule*
"""
import logging
from datetime import datetime
from typing import Any, Optional

from .models import Schedule, ScheduleLeg, ScheduleQuery

logger = logging.getLogger(__name__)

# ── Date helpers ──────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value[:26], fmt)
        except ValueError:
            continue
    logger.debug("Could not parse datetime: %r", value)
    return None


def _str(d: dict, *keys: str) -> Optional[str]:
    """Safe nested dict access, returns str or None."""
    val = d
    for k in keys:
        if not isinstance(val, dict):
            return None
        val = val.get(k)
    return str(val).strip() if val is not None else None


def _int(d: dict, *keys: str) -> Optional[int]:
    val = _str(d, *keys)
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _float(d: dict, *keys: str) -> Optional[float]:
    val = _str(d, *keys)
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


# ── Main parsers ──────────────────────────────────────────────────────────────


def parse_schedule_response(
    json_data: Any,
    query: ScheduleQuery,
) -> list[Schedule]:
    """
    Parse the JSON payload from Maersk's schedules API and return a list of
    Schedule ORM objects (with nested ScheduleLeg objects attached).

    Supports two observed response shapes:

    Shape A — array at root:
        [ { "transitTime": 28, "legs": [...] }, ... ]

    Shape B — object with "schedules" key:
        { "schedules": [ { "transitTime": 28, "legs": [...] } ] }

    Shape C — object with "pointToPointSchedules" key:
        { "pointToPointSchedules": [ ... ] }
    """
    # Unwrap envelope
    if isinstance(json_data, dict):
        for key in ("pointToPointSchedules", "schedules", "data", "results"):
            if key in json_data and isinstance(json_data[key], list):
                json_data = json_data[key]
                break
        else:
            # Maybe it's a single schedule object
            json_data = [json_data]

    if not isinstance(json_data, list):
        logger.warning("Unexpected JSON shape — not a list: %s", type(json_data))
        return []

    schedules: list[Schedule] = []
    for idx, item in enumerate(json_data):
        if not isinstance(item, dict):
            continue
        sched = _parse_single_schedule(item, idx + 1)
        if sched is not None:
            sched.query = query
            schedules.append(sched)

    logger.info("Parsed %d schedules for %s → %s", len(schedules), query.origin_code, query.destination_code)
    return schedules


def _parse_single_schedule(data: dict, position: int) -> Optional[Schedule]:
    """Parse one schedule item from the API response."""

    # ── Legs ──────────────────────────────────────────────────────────────
    raw_legs = data.get("legs") or data.get("transportLegs") or data.get("routeLegs") or []
    if not isinstance(raw_legs, list):
        raw_legs = []

    legs: list[ScheduleLeg] = []
    for seq, leg_data in enumerate(raw_legs, start=1):
        leg = _parse_leg(leg_data, seq)
        if leg is not None:
            legs.append(leg)

    # ── Transit time ──────────────────────────────────────────────────────
    transit_days = (
        _int(data, "transitTime")
        or _int(data, "transitTimeDays")
        or _int(data, "duration")
        or _int(data, "durationDays")
    )

    # ── Overall departure / arrival ───────────────────────────────────────
    # Try top-level fields first, otherwise derive from first/last leg
    departure_dt = _parse_dt(
        _str(data, "departureDateTime")
        or _str(data, "departureDate")
        or _str(data, "etd")
    )
    arrival_dt = _parse_dt(
        _str(data, "arrivalDateTime")
        or _str(data, "arrivalDate")
        or _str(data, "eta")
    )

    if departure_dt is None and legs:
        departure_dt = legs[0].departure_datetime
    if arrival_dt is None and legs:
        arrival_dt = legs[-1].arrival_datetime

    # ── Transshipment info ────────────────────────────────────────────────
    has_transshipment = 1 if len(legs) > 1 else 0
    via_ports = ",".join(
        leg.to_port_code
        for leg in legs[:-1]
        if leg.to_port_code
    ) if len(legs) > 1 else None

    # ── External schedule id ──────────────────────────────────────────────
    ext_id = (
        _str(data, "id")
        or _str(data, "scheduleId")
        or _str(data, "routeId")
    )

    schedule = Schedule(
        transit_time_days=transit_days,
        departure_datetime=departure_dt,
        arrival_datetime=arrival_dt,
        num_legs=len(legs),
        carrier="Maersk",
        schedule_id_external=ext_id,
        has_transshipment=has_transshipment,
        transshipment_ports=via_ports,
    )
    schedule.legs = legs

    if departure_dt is None and transit_days is None:
        logger.debug("Skipping schedule at position %d — no usable data", position)
        return None

    return schedule


def _parse_leg(data: dict, sequence: int) -> Optional[ScheduleLeg]:
    """Parse a single leg from the leg list."""
    if not isinstance(data, dict):
        return None

    # ── Vessel ────────────────────────────────────────────────────────────
    vessel = data.get("vessel") or data.get("vesselInfo") or {}
    vessel_name = (
        _str(vessel, "name")
        or _str(vessel, "vesselName")
        or _str(data, "vesselName")
    )
    vessel_imo = _str(vessel, "imoNumber") or _str(vessel, "imo") or _str(data, "imoNumber")
    voyage_number = (
        _str(data, "voyageNumber")
        or _str(data, "voyage")
        or _str(data, "voyageRef")
    )

    # ── Service ───────────────────────────────────────────────────────────
    service = data.get("service") or data.get("serviceInfo") or {}
    service_name = _str(service, "name") or _str(service, "serviceName") or _str(data, "serviceName")
    service_code = _str(service, "code") or _str(service, "serviceCode") or _str(data, "serviceCode")

    # ── Transport mode ────────────────────────────────────────────────────
    transport_mode = (
        _str(data, "transportMode")
        or _str(data, "mode")
        or "VESSEL"
    ).upper()

    # ── From location ─────────────────────────────────────────────────────
    from_loc = (
        data.get("fromLocation")
        or data.get("departurePort")
        or data.get("originLocation")
        or {}
    )
    from_port_code = (
        _str(from_loc, "rkst")
        or _str(from_loc, "unCode")
        or _str(from_loc, "portCode")
        or _str(from_loc, "code")
        or _str(data, "fromPort")
        or _str(data, "portOfLoading")
    )
    from_port_name = (
        _str(from_loc, "cityName")
        or _str(from_loc, "portName")
        or _str(from_loc, "name")
    )
    from_country = _str(from_loc, "countryCode") or _str(from_loc, "country")
    from_terminal = _str(from_loc, "terminal") or _str(from_loc, "terminalName")
    departure_dt = _parse_dt(
        _str(data, "departureDateTime")
        or _str(data, "departureDate")
        or _str(data, "etd")
        or _str(from_loc, "departureDateTime")
    )
    cutoff_dt = _parse_dt(
        _str(data, "cutOffDateTime")
        or _str(data, "cargoCutoff")
        or _str(data, "vgmCutoff")
    )

    # ── To location ───────────────────────────────────────────────────────
    to_loc = (
        data.get("toLocation")
        or data.get("arrivalPort")
        or data.get("destinationLocation")
        or {}
    )
    to_port_code = (
        _str(to_loc, "rkst")
        or _str(to_loc, "unCode")
        or _str(to_loc, "portCode")
        or _str(to_loc, "code")
        or _str(data, "toPort")
        or _str(data, "portOfDischarge")
    )
    to_port_name = (
        _str(to_loc, "cityName")
        or _str(to_loc, "portName")
        or _str(to_loc, "name")
    )
    to_country = _str(to_loc, "countryCode") or _str(to_loc, "country")
    to_terminal = _str(to_loc, "terminal") or _str(to_loc, "terminalName")
    arrival_dt = _parse_dt(
        _str(data, "arrivalDateTime")
        or _str(data, "arrivalDate")
        or _str(data, "eta")
        or _str(to_loc, "arrivalDateTime")
    )

    # ── Leg transit days ──────────────────────────────────────────────────
    leg_transit = _float(data, "legTransitTime") or _float(data, "transitTime")
    if leg_transit is None and departure_dt and arrival_dt:
        delta = arrival_dt - departure_dt
        leg_transit = round(delta.total_seconds() / 86400, 1)

    return ScheduleLeg(
        sequence=sequence,
        vessel_name=vessel_name,
        vessel_imo=vessel_imo,
        voyage_number=voyage_number,
        service_name=service_name,
        service_code=service_code,
        transport_mode=transport_mode,
        from_port_code=from_port_code,
        from_port_name=from_port_name,
        from_country_code=from_country,
        from_terminal=from_terminal,
        departure_datetime=departure_dt,
        departure_cutoff=cutoff_dt,
        to_port_code=to_port_code,
        to_port_name=to_port_name,
        to_country_code=to_country,
        to_terminal=to_terminal,
        arrival_datetime=arrival_dt,
        leg_transit_days=leg_transit,
    )
