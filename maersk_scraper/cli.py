"""
Command-line interface for the Maersk Schedule Scraper.

Usage examples:

  # Single search
  python -m maersk_scraper search \\
      --origin CNSHA --origin-name "Shanghai" \\
      --destination NLRTM --destination-name "Rotterdam" \\
      --date 2025-04-01

  # Batch search from a JSON file
  python -m maersk_scraper batch --file routes.json --date 2025-04-01

  # Show stored schedules
  python -m maersk_scraper show --origin CNSHA --destination NLRTM --limit 5

  # Init database only
  python -m maersk_scraper init-db
"""
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from typing import Optional

import click

from .config import load_config
from .database import get_session, init_db
from .models import Schedule, ScheduleLeg, ScheduleQuery
from .scraper import MaerskScraper, SearchRequest, run_batch_search

# ── Logging setup ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("maersk_scraper")


# ── CLI root ───────────────────────────────────────────────────────────────────

@click.group()
@click.option("--debug", is_flag=True, help="Enable DEBUG logging")
def cli(debug: bool) -> None:
    """Maersk Schedule Scraper — fetch and store shipping schedules."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)


# ── init-db ────────────────────────────────────────────────────────────────────

@cli.command("init-db")
def cmd_init_db() -> None:
    """Create database tables (safe to run multiple times)."""
    config = load_config()
    try:
        init_db(config)
        click.echo("Database initialised successfully.")
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


# ── search ─────────────────────────────────────────────────────────────────────

@cli.command("search")
@click.option("--origin", required=True, help="Origin port code (e.g. CNSHA)")
@click.option("--origin-name", default="", help="Origin port name (e.g. Shanghai)")
@click.option("--destination", required=True, help="Destination port code (e.g. NLRTM)")
@click.option("--destination-name", default="", help="Destination port name (e.g. Rotterdam)")
@click.option(
    "--date",
    default=str(date.today() + timedelta(days=7)),
    show_default=True,
    help="Departure date YYYY-MM-DD (default: 7 days from today)",
)
@click.option("--no-save", is_flag=True, help="Print results without saving to database")
@click.option("--show-browser", is_flag=True, help="Run browser in visible (non-headless) mode")
def cmd_search(
    origin: str,
    origin_name: str,
    destination: str,
    destination_name: str,
    date: str,
    no_save: bool,
    show_browser: bool,
) -> None:
    """Search for schedules between two ports on a given departure date."""
    config = load_config()
    if show_browser:
        config.headless = False  # type: ignore[assignment]

    req = SearchRequest(
        origin_code=origin,
        origin_name=origin_name or origin,
        destination_code=destination,
        destination_name=destination_name or destination,
        departure_date=date,
    )

    async def _run():
        async with MaerskScraper(config) as scraper:
            query, schedules = await scraper.search(req)

        if not schedules:
            click.echo("No schedules found.")
            return

        _print_schedules(query, schedules)

        if not no_save:
            session_factory = init_db(config)
            with get_session(session_factory) as session:
                session.add(query)
            click.echo(f"\nSaved {len(schedules)} schedule(s) to database.")

    asyncio.run(_run())


# ── batch ──────────────────────────────────────────────────────────────────────

@cli.command("batch")
@click.option(
    "--file", "routes_file", required=True, type=click.Path(exists=True),
    help="JSON file with list of route objects",
)
@click.option(
    "--date",
    default=str(date.today() + timedelta(days=7)),
    show_default=True,
    help="Departure date YYYY-MM-DD (overrides date in file if not set per route)",
)
@click.option("--no-save", is_flag=True, help="Print results without saving to database")
def cmd_batch(routes_file: str, date: str, no_save: bool) -> None:
    """
    Search multiple routes from a JSON file.

    Expected file format (array of route objects):
    \\b
    [
      {
        "origin_code": "CNSHA",
        "origin_name": "Shanghai",
        "destination_code": "NLRTM",
        "destination_name": "Rotterdam",
        "departure_date": "2025-04-01"   // optional, falls back to --date
      },
      ...
    ]
    """
    config = load_config()

    with open(routes_file, encoding="utf-8") as fh:
        raw_routes = json.load(fh)

    requests = []
    for r in raw_routes:
        requests.append(SearchRequest(
            origin_code=r["origin_code"],
            origin_name=r.get("origin_name", r["origin_code"]),
            destination_code=r["destination_code"],
            destination_name=r.get("destination_name", r["destination_code"]),
            departure_date=r.get("departure_date", date),
            origin_country=r.get("origin_country", ""),
            destination_country=r.get("destination_country", ""),
        ))

    click.echo(f"Starting batch search: {len(requests)} route(s)…")

    session_factory = None if no_save else init_db(config)

    async def on_result(query: ScheduleQuery, schedules: list) -> None:
        _print_schedules(query, schedules)
        if session_factory and schedules:
            with get_session(session_factory) as session:
                session.add(query)
            click.echo(f"  -> Saved {len(schedules)} schedule(s).")

    asyncio.run(run_batch_search(requests, config, on_result=on_result))
    click.echo("Batch search complete.")


# ── show ───────────────────────────────────────────────────────────────────────

@cli.command("show")
@click.option("--origin", default=None, help="Filter by origin port code")
@click.option("--destination", default=None, help="Filter by destination port code")
@click.option("--limit", default=10, show_default=True, help="Max number of schedules to display")
def cmd_show(origin: Optional[str], destination: Optional[str], limit: int) -> None:
    """Display stored schedules from the database."""
    config = load_config()
    session_factory = init_db(config)

    with get_session(session_factory) as session:
        q = (
            session.query(Schedule)
            .join(ScheduleQuery)
            .order_by(Schedule.departure_datetime.desc())
        )
        if origin:
            q = q.filter(ScheduleQuery.origin_code == origin.upper())
        if destination:
            q = q.filter(ScheduleQuery.destination_code == destination.upper())

        schedules: list[Schedule] = q.limit(limit).all()

    if not schedules:
        click.echo("No schedules found in database.")
        return

    click.echo(f"\nFound {len(schedules)} schedule(s):\n")
    for s in schedules:
        _print_schedule_row(s)


# ── print helpers ──────────────────────────────────────────────────────────────

def _print_schedules(query: ScheduleQuery, schedules: list) -> None:
    click.echo(
        f"\n{'─'*60}\n"
        f"  {query.origin_name} ({query.origin_code}) → "
        f"{query.destination_name} ({query.destination_code})\n"
        f"  Departure date requested: {query.departure_date}\n"
        f"  Results: {len(schedules)}\n"
        f"{'─'*60}"
    )
    for i, s in enumerate(schedules, 1):
        click.echo(f"\n  [{i}] Transit: {s.transit_time_days} days  |  Legs: {s.num_legs}")
        dep = s.departure_datetime.strftime("%Y-%m-%d %H:%M") if s.departure_datetime else "N/A"
        arr = s.arrival_datetime.strftime("%Y-%m-%d %H:%M") if s.arrival_datetime else "N/A"
        click.echo(f"      ETD: {dep}  |  ETA: {arr}")
        if s.transshipment_ports:
            click.echo(f"      Via: {s.transshipment_ports}")
        for leg in s.legs:
            _print_leg(leg)


def _print_schedule_row(s: Schedule) -> None:
    dep = s.departure_datetime.strftime("%Y-%m-%d") if s.departure_datetime else "N/A"
    arr = s.arrival_datetime.strftime("%Y-%m-%d") if s.arrival_datetime else "N/A"
    via = f" via {s.transshipment_ports}" if s.transshipment_ports else ""
    click.echo(
        f"  Schedule #{s.id}: {dep} → {arr} | {s.transit_time_days}d | "
        f"{s.num_legs} leg(s){via}"
    )
    for leg in s.legs:
        _print_leg(leg, indent="    ")


def _print_leg(leg: ScheduleLeg, indent: str = "        ") -> None:
    dep = leg.departure_datetime.strftime("%m-%d %H:%M") if leg.departure_datetime else "?"
    arr = leg.arrival_datetime.strftime("%m-%d %H:%M") if leg.arrival_datetime else "?"
    vessel = leg.vessel_name or leg.transport_mode or "—"
    voyage = f" v.{leg.voyage_number}" if leg.voyage_number else ""
    service = f" [{leg.service_code or leg.service_name}]" if (leg.service_code or leg.service_name) else ""
    click.echo(
        f"{indent}Leg {leg.sequence}: {leg.from_port_code}({dep}) → "
        f"{leg.to_port_code}({arr}) | {vessel}{voyage}{service}"
    )
