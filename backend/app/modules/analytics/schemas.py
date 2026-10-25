from datetime import date

from pydantic import BaseModel, Field


class EventsExportQuery(BaseModel):
    date_from: date | None = Field(
        None, alias="from", description="С какой даты (UTC), включительно"
    )
    date_to: date | None = Field(None, alias="to", description="По какую дату (UTC), включительно")
