"""
EXIF extraction, weather fetching, and duplicate detection.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple
import logging

import piexif
import requests
from PIL import Image, ImageOps
import imagehash

from models import PhotoData, WeatherData

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WMO weather code → human description
# ---------------------------------------------------------------------------
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


# ---------------------------------------------------------------------------
# GPS / EXIF helpers
# ---------------------------------------------------------------------------

def _rat_to_float(val) -> Optional[float]:
    """Convert piexif rational (numerator, denominator) to float."""
    try:
        if isinstance(val, (list, tuple)) and len(val) == 2:
            num, den = val
            return float(num) / float(den) if den != 0 else None
        return float(val)
    except Exception:
        return None


def _dms_to_dd(dms) -> Optional[float]:
    """Convert DMS rational triplet to decimal degrees."""
    try:
        d = _rat_to_float(dms[0])
        m = _rat_to_float(dms[1])
        s = _rat_to_float(dms[2])
        if None in (d, m, s):
            return None
        return d + m / 60.0 + s / 3600.0
    except Exception:
        return None


def _parse_exif_datetime(raw: bytes) -> Optional[datetime]:
    """Parse EXIF datetime bytes 'YYYY:MM:DD HH:MM:SS'."""
    try:
        s = raw.decode("ascii", errors="ignore").strip("\x00").strip()
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def extract_photo_data(filepath: str) -> PhotoData:
    """Extract all available metadata from a photo file."""
    photo = PhotoData(
        file_path=filepath,
        filename=os.path.basename(filepath),
    )

    try:
        img = Image.open(filepath)
        # Respect EXIF orientation so thumbnails display correctly
        img = ImageOps.exif_transpose(img)

        raw_exif = img.info.get("exif", b"")
        if not raw_exif:
            return photo

        exif = piexif.load(raw_exif)
    except Exception as e:
        log.warning("Could not read EXIF from %s: %s", filepath, e)
        return photo

    # --- Camera make / model ---
    zeroth = exif.get("0th", {})
    try:
        photo.make = zeroth.get(piexif.ImageIFD.Make, b"").decode("ascii", errors="ignore").strip("\x00").strip()
    except Exception:
        pass
    try:
        photo.model = zeroth.get(piexif.ImageIFD.Model, b"").decode("ascii", errors="ignore").strip("\x00").strip()
    except Exception:
        pass

    # --- Datetime ---
    exif_ifd = exif.get("Exif", {})
    raw_dt = (
        exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
        or exif_ifd.get(piexif.ExifIFD.DateTimeDigitized)
        or zeroth.get(piexif.ImageIFD.DateTime)
    )
    if raw_dt:
        photo.datetime_taken = _parse_exif_datetime(raw_dt)

    # --- GPS ---
    gps = exif.get("GPS", {})
    if gps:
        lat_raw = gps.get(piexif.GPSIFD.GPSLatitude)
        lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N")
        lon_raw = gps.get(piexif.GPSIFD.GPSLongitude)
        lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E")

        lat = _dms_to_dd(lat_raw) if lat_raw else None
        lon = _dms_to_dd(lon_raw) if lon_raw else None

        if lat is not None and lon is not None:
            if isinstance(lat_ref, bytes):
                lat_ref = lat_ref.decode("ascii", errors="ignore").strip("\x00")
            if isinstance(lon_ref, bytes):
                lon_ref = lon_ref.decode("ascii", errors="ignore").strip("\x00")

            photo.latitude = -lat if str(lat_ref).upper().startswith("S") else lat
            photo.longitude = -lon if str(lon_ref).upper().startswith("W") else lon
            photo.has_gps = True

        # Altitude
        alt_raw = gps.get(piexif.GPSIFD.GPSAltitude)
        if alt_raw:
            alt = _rat_to_float(alt_raw)
            alt_ref = gps.get(piexif.GPSIFD.GPSAltitudeRef, 0)
            if alt is not None:
                photo.altitude_m = -alt if alt_ref == 1 else alt

        # Direction (bearing camera was pointing)
        dir_raw = gps.get(piexif.GPSIFD.GPSImgDirection)
        if dir_raw:
            direction = _rat_to_float(dir_raw)
            if direction is not None:
                photo.direction_degrees = direction % 360
                photo.has_direction = True
                dir_ref = gps.get(piexif.GPSIFD.GPSImgDirectionRef, b"T")
                if isinstance(dir_ref, bytes):
                    dir_ref = dir_ref.decode("ascii", errors="ignore").strip("\x00")
                photo.direction_ref = str(dir_ref).upper() or "T"

    return photo


# ---------------------------------------------------------------------------
# Perceptual hash for duplicate detection
# ---------------------------------------------------------------------------

DUPLICATE_THRESHOLD = 8    # hamming distance — exact dup = 0, very similar < 8
SIMILAR_THRESHOLD = 15     # warn as "similar" but not flagged as duplicate


def compute_phash(filepath: str) -> Optional[str]:
    try:
        img = Image.open(filepath)
        return str(imagehash.phash(img))
    except Exception as e:
        log.warning("phash failed for %s: %s", filepath, e)
        return None


def detect_duplicates(photos: List[PhotoData]) -> None:
    """
    In-place: mark photos[i].is_duplicate / similar_to based on phash comparison.
    Earlier photos in the list are treated as originals.
    """
    hashes: List[Tuple[str, imagehash.ImageHash]] = []

    for photo in photos:
        if photo.phash is None:
            continue
        try:
            h = imagehash.hex_to_hash(photo.phash)
        except Exception:
            continue

        for prev_name, prev_h in hashes:
            dist = h - prev_h
            if dist <= DUPLICATE_THRESHOLD:
                photo.is_duplicate = True
                photo.similar_to = prev_name
                break
            elif dist <= SIMILAR_THRESHOLD:
                if not photo.similar_to:
                    photo.similar_to = prev_name  # flagged similar, not duplicate
                break

        if not photo.is_duplicate:
            hashes.append((photo.filename, h))


# ---------------------------------------------------------------------------
# Weather fetching (Open-Meteo — free, no API key)
# ---------------------------------------------------------------------------

def _wmo_description(code: int) -> str:
    return WMO_CODES.get(int(code), f"Weather code {code}")


def fetch_weather(lat: float, lon: float, dt: Optional[datetime]) -> Optional[WeatherData]:
    """
    Fetch weather conditions at the given lat/lon and approximate datetime.
    Uses Open-Meteo archive API for historical data, forecast API for recent.
    Returns None on failure.
    """
    if dt is None:
        # Use current conditions
        return _fetch_current_weather(lat, lon)

    # Make dt timezone-aware (UTC) if naive
    if dt.tzinfo is None:
        dt_aware = dt.replace(tzinfo=timezone.utc)
    else:
        dt_aware = dt

    now = datetime.now(timezone.utc)
    age_days = (now - dt_aware).total_seconds() / 86400

    if age_days < 0.3:
        # Very recent — use current conditions
        return _fetch_current_weather(lat, lon)
    elif age_days <= 7:
        return _fetch_recent_weather(lat, lon, dt_aware)
    else:
        return _fetch_archive_weather(lat, lon, dt_aware)


def _fetch_current_weather(lat: float, lon: float) -> Optional[WeatherData]:
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,precipitation,weathercode,windspeed_10m,winddirection_10m",
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("current", {})
        return WeatherData(
            temperature_c=data.get("temperature_2m"),
            description=_wmo_description(data.get("weathercode", 0)),
            wind_speed_kmh=data.get("windspeed_10m"),
            wind_direction_deg=data.get("winddirection_10m"),
            precipitation_mm=data.get("precipitation"),
        )
    except Exception as e:
        log.warning("Current weather fetch failed: %s", e)
        return None


def _fetch_recent_weather(lat: float, lon: float, dt: datetime) -> Optional[WeatherData]:
    """Use forecast API with hourly data for dates within the past ~7 days."""
    try:
        date_str = dt.strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation,weathercode,windspeed_10m,winddirection_10m",
            "start_date": date_str,
            "end_date": date_str,
            "past_days": 7,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return _extract_hourly(r.json(), dt)
    except Exception as e:
        log.warning("Recent weather fetch failed: %s", e)
        return None


def _fetch_archive_weather(lat: float, lon: float, dt: datetime) -> Optional[WeatherData]:
    """Use archive API for dates older than ~7 days."""
    try:
        date_str = dt.strftime("%Y-%m-%d")
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_str,
            "end_date": date_str,
            "hourly": "temperature_2m,precipitation,weathercode,windspeed_10m,winddirection_10m",
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return _extract_hourly(r.json(), dt)
    except Exception as e:
        log.warning("Archive weather fetch failed: %s", e)
        return None


def _extract_hourly(data: dict, dt: datetime) -> Optional[WeatherData]:
    """Find the hourly slot closest to dt and return WeatherData."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return None

    target_str = dt.strftime("%Y-%m-%dT%H:00")
    # find closest hour
    idx = 0
    best = None
    for i, t in enumerate(times):
        if best is None or abs(_hours_diff(t, target_str)) < abs(_hours_diff(times[idx], target_str)):
            idx = i
            best = t

    def _get(key):
        vals = hourly.get(key, [])
        return vals[idx] if idx < len(vals) else None

    return WeatherData(
        temperature_c=_get("temperature_2m"),
        description=_wmo_description(_get("weathercode") or 0),
        wind_speed_kmh=_get("windspeed_10m"),
        wind_direction_deg=_get("winddirection_10m"),
        precipitation_mm=_get("precipitation"),
    )


def _hours_diff(t1: str, t2: str) -> float:
    try:
        fmt = "%Y-%m-%dT%H:%M" if ":" in t1[11:] else "%Y-%m-%dT%H"
        a = datetime.strptime(t1[:16], "%Y-%m-%dT%H:%M")
        b = datetime.strptime(t2[:16], "%Y-%m-%dT%H:%M")
        return (a - b).total_seconds() / 3600
    except Exception:
        return 999
