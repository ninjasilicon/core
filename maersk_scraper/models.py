"""
SQLAlchemy models for Maersk schedule data.
"""
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ScheduleQuery(Base):
    """Represents a search query made against the Maersk schedules page."""
    __tablename__ = "schedule_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    origin_code = Column(String(10), nullable=False, comment="RKST/UN port code for origin")
    origin_name = Column(String(100), comment="Human-readable origin port name")
    origin_country = Column(String(5), comment="ISO 2-letter country code")
    destination_code = Column(String(10), nullable=False, comment="RKST/UN port code for destination")
    destination_name = Column(String(100), comment="Human-readable destination port name")
    destination_country = Column(String(5), comment="ISO 2-letter country code")
    departure_date = Column(String(20), comment="Requested departure date (YYYY-MM-DD)")
    scraped_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    results_count = Column(Integer, default=0)

    schedules = relationship(
        "Schedule", back_populates="query", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_query_origin_dest", "origin_code", "destination_code"),
        Index("ix_query_scraped_at", "scraped_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduleQuery id={self.id} "
            f"{self.origin_code} -> {self.destination_code} "
            f"on {self.departure_date}>"
        )


class Schedule(Base):
    """A single schedule option (itinerary) returned by Maersk for a query."""
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    query_id = Column(Integer, ForeignKey("schedule_queries.id"), nullable=False)

    # Overall trip info
    transit_time_days = Column(Integer, comment="Total door-to-door transit time in days")
    departure_datetime = Column(DateTime, comment="Departure from origin port")
    arrival_datetime = Column(DateTime, comment="Arrival at destination port")
    num_legs = Column(Integer, default=1, comment="Number of legs/segments")

    # Carrier info
    carrier = Column(String(50), default="Maersk")
    schedule_id_external = Column(String(100), comment="Internal Maersk schedule identifier")

    # Transshipment info
    has_transshipment = Column(Integer, default=0, comment="1 if schedule has transshipment ports")
    transshipment_ports = Column(Text, comment="Comma-separated list of via/transshipment port codes")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    query = relationship("ScheduleQuery", back_populates="schedules")
    legs = relationship("ScheduleLeg", back_populates="schedule", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_schedule_query_id", "query_id"),
        Index("ix_schedule_departure", "departure_datetime"),
    )

    def __repr__(self) -> str:
        return (
            f"<Schedule id={self.id} transit={self.transit_time_days}d "
            f"depart={self.departure_datetime} legs={self.num_legs}>"
        )


class ScheduleLeg(Base):
    """A single leg (segment) of a schedule — one vessel voyage between two ports."""
    __tablename__ = "schedule_legs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id"), nullable=False)
    sequence = Column(Integer, nullable=False, comment="Leg order within the schedule (1-based)")

    # Vessel info
    vessel_name = Column(String(100), comment="Name of the vessel")
    vessel_imo = Column(String(20), comment="IMO number of the vessel")
    voyage_number = Column(String(50), comment="Voyage number (e.g. W102E)")

    # Service info
    service_name = Column(String(100), comment="Named shipping service (e.g. AE1/Shogun)")
    service_code = Column(String(20), comment="Short service code")

    # Transport mode (VESSEL, TRUCK, RAIL, FEEDER, etc.)
    transport_mode = Column(String(50), default="VESSEL")

    # Origin of this leg
    from_port_code = Column(String(10), comment="RKST/UN code of departure port")
    from_port_name = Column(String(100))
    from_country_code = Column(String(5))
    from_terminal = Column(String(100))
    departure_datetime = Column(DateTime)
    departure_cutoff = Column(DateTime, comment="Cargo cutoff datetime")

    # Destination of this leg
    to_port_code = Column(String(10), comment="RKST/UN code of arrival port")
    to_port_name = Column(String(100))
    to_country_code = Column(String(5))
    to_terminal = Column(String(100))
    arrival_datetime = Column(DateTime)

    # Leg duration
    leg_transit_days = Column(Float, comment="Transit days for this specific leg")

    schedule = relationship("Schedule", back_populates="legs")

    __table_args__ = (
        Index("ix_leg_schedule_id", "schedule_id"),
        Index("ix_leg_vessel", "vessel_name"),
        Index("ix_leg_service", "service_code"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduleLeg id={self.id} seq={self.sequence} "
            f"vessel='{self.vessel_name}' "
            f"{self.from_port_code} -> {self.to_port_code}>"
        )
