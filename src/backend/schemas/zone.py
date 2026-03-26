from pydantic import BaseModel, Field


class Zone(BaseModel):
    zone_id: str = Field(
        description="Stable zone id (category) to which the controller belongs."
    )
    display_name: str = Field(description="Friendly display name of the zone.")
