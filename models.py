from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class WeatherData:
    temperature_c: Optional[float] = None
    description: str = "Unknown"
    wind_speed_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    precipitation_mm: Optional[float] = None

    @property
    def wind_direction_label(self) -> str:
        if self.wind_direction_deg is None:
            return ""
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        ix = round(self.wind_direction_deg / 22.5) % 16
        return dirs[ix]

    def summary(self) -> str:
        parts = [self.description]
        if self.temperature_c is not None:
            parts.append(f"{self.temperature_c:.1f}°C")
        if self.wind_speed_kmh is not None:
            parts.append(f"Wind {self.wind_speed_kmh:.0f} km/h {self.wind_direction_label}")
        if self.precipitation_mm is not None and self.precipitation_mm > 0:
            parts.append(f"Precip {self.precipitation_mm:.1f} mm")
        return " | ".join(parts)


@dataclass
class PhotoData:
    file_path: str = ""
    filename: str = ""
    datetime_taken: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None
    direction_degrees: Optional[float] = None
    direction_ref: str = "T"   # T=True North, M=Magnetic
    make: str = ""             # Camera make (Apple, Samsung, etc.)
    model: str = ""            # Camera model
    weather: Optional[WeatherData] = None

    # User annotations (three separate fields)
    what_inspected: str = ""
    issues_found: str = ""
    actions_required: str = ""

    # Computed flags
    has_gps: bool = False
    has_direction: bool = False
    is_duplicate: bool = False
    similar_to: str = ""       # filename of the earlier similar photo
    phash: Optional[str] = None  # perceptual hash string

    @property
    def direction_label(self) -> str:
        if self.direction_degrees is None:
            return "Not available"
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        ix = round(self.direction_degrees / 22.5) % 16
        ref = "T" if self.direction_ref != "M" else "M"
        return f"{dirs[ix]} ({self.direction_degrees:.1f}° {ref})"

    @property
    def coords_label(self) -> str:
        if not self.has_gps:
            return "No GPS data"
        lat_h = "N" if (self.latitude or 0) >= 0 else "S"
        lon_h = "E" if (self.longitude or 0) >= 0 else "W"
        return (f"{abs(self.latitude or 0):.6f}°{lat_h}, "
                f"{abs(self.longitude or 0):.6f}°{lon_h}")

    @property
    def datetime_label(self) -> str:
        if self.datetime_taken is None:
            return "Unknown"
        return self.datetime_taken.strftime("%d %b %Y  %H:%M:%S")

    @property
    def altitude_label(self) -> str:
        if self.altitude_m is None:
            return ""
        return f"{self.altitude_m:.1f} m ASL"
