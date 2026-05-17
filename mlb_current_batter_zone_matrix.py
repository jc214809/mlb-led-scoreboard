#!/usr/bin/env python3
"""
Render the current batter's pitch locations from the MLB live feed onto a 64x64 LED matrix.

Behavior:
- Polls the live feed for one gamePk
- Tries a `fields=` filter to reduce payload size, falls back to full feed if needed
- Tracks only the CURRENT batter's pitches
- Resets automatically when batter.id or atBatIndex changes
- Draws the strike zone and one dot per pitch on a 64x64 matrix
- If rgbmatrix is not installed, saves a PNG preview instead

Designed for Raspberry Pi + hzeller/rpi-rgb-led-matrix.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw

# =========================
# USER SETTINGS
# =========================
GAME_PK = 831897
POLL_SECONDS = 2.0
REQUEST_TIMEOUT = 10
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREVIEW_PATH = os.path.join(BASE_DIR, "strikezone_preview.png")
LAST_RESPONSE_PATH = os.path.join(BASE_DIR, "last_live_response.json")

# Matrix / panel settings
MATRIX_WIDTH = 64
MATRIX_HEIGHT = 64
CHAIN_LENGTH = 1
PARALLEL = 1
HARDWARE_MAPPING = "adafruit-hat"  # common for Adafruit bonnet/HAT; change if needed
BRIGHTNESS = 60
GPIO_SLOWDOWN = 2

# Plot area inside the 64x64 panel
LEFT_PAD = 10
RIGHT_PAD = 10
TOP_PAD = 6
BOTTOM_PAD = 6

# Horizontal strike-zone assumption used for pX normalization.
# pX is in feet at the front of the plate.
ZONE_LEFT_FT = -0.83
ZONE_RIGHT_FT = 0.83
# Display range for plotting pitches (feet). Keep wider than strike zone
# so out-of-zone locations render outside the box.
PLOT_PX_MIN = -1.5
PLOT_PX_MAX = 1.5
PLOT_PZ_MIN = 0.5
PLOT_PZ_MAX = 4.5

# Conservative projection (we'll use it only if it returns data).
FIELDS = "gamePk,gameData,liveData"

LIVE_URL = f"https://statsapi.mlb.com/api/v1.1/game/{GAME_PK}/feed/live"


@dataclass
class PitchDot:
    pitch_number: int
    description: str
    call: str
    pitch_type: str
    is_ball: bool
    is_strike: bool
    zone: Optional[int]
    px: float
    pz: float
    zone_top: float
    zone_bottom: float


@dataclass
class CurrentBatterState:
    batter_id: Optional[int] = None
    batter_name: str = ""
    at_bat_index: Optional[int] = None
    inning: Optional[int] = None
    half_inning: str = ""
    balls: int = 0
    strikes: int = 0
    outs: int = 0
    pitches: List[PitchDot] = field(default_factory=list)
    seen_pitch_numbers: set = field(default_factory=set)

    def reset(self, batter_id: int, batter_name: str, at_bat_index: Optional[int]) -> None:
        self.batter_id = batter_id
        self.batter_name = batter_name
        self.at_bat_index = at_bat_index
        self.pitches.clear()
        self.seen_pitch_numbers.clear()

    def add_pitch(self, pitch: PitchDot) -> bool:
        if pitch.pitch_number in self.seen_pitch_numbers:
            return False
        self.seen_pitch_numbers.add(pitch.pitch_number)
        self.pitches.append(pitch)
        return True


class MatrixRenderer:
    def __init__(self) -> None:
        self.matrix = None
        self.mode = "preview"
        self._init_matrix_if_available()

    def _init_matrix_if_available(self) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions  # type: ignore
        except Exception:
            print("rgbmatrix not available; using PNG preview mode.")
            return

        options = RGBMatrixOptions()
        options.rows = MATRIX_HEIGHT
        options.cols = MATRIX_WIDTH
        options.chain_length = CHAIN_LENGTH
        options.parallel = PARALLEL
        options.hardware_mapping = HARDWARE_MAPPING
        options.brightness = BRIGHTNESS
        options.gpio_slowdown = GPIO_SLOWDOWN

        self.matrix = RGBMatrix(options=options)
        self.mode = "matrix"
        print("rgbmatrix loaded; rendering to LED matrix.")

    def render(self, state: CurrentBatterState) -> None:
        image = self._build_image(state)

        if self.mode == "matrix" and self.matrix is not None:
            self.matrix.SetImage(image.convert("RGB"))
        else:
            image.save(PREVIEW_PATH)

    def _build_image(self, state: CurrentBatterState) -> Image.Image:
        img = Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Plot frame
        plot_left = LEFT_PAD
        plot_top = TOP_PAD
        plot_right = MATRIX_WIDTH - RIGHT_PAD - 1
        plot_bottom = MATRIX_HEIGHT - BOTTOM_PAD - 1

        # Outer plot box
        draw.rectangle([plot_left, plot_top, plot_right, plot_bottom], outline=(40, 40, 40))

        # Header/status markers
        self._draw_status(draw, state)

        # Determine latest known strike zone from current batter pitches.
        latest = state.pitches[-1] if state.pitches else None
        if latest is not None:
            zone_top = latest.zone_top
            zone_bottom = latest.zone_bottom
        else:
            zone_top = 3.5
            zone_bottom = 1.5

        # Strike zone box (centered horizontally in plot area)
        zone_left_px = self._map_px_to_x(ZONE_LEFT_FT, plot_left, plot_right)
        zone_right_px = self._map_px_to_x(ZONE_RIGHT_FT, plot_left, plot_right)
        zone_top_px = self._map_pz_to_y(zone_top, plot_top, plot_bottom)
        zone_bottom_px = self._map_pz_to_y(zone_bottom, plot_top, plot_bottom)
        draw.rectangle([zone_left_px, zone_top_px, zone_right_px, zone_bottom_px], outline=(255, 255, 255))

        # Rule-of-thirds grid inside strike zone
        third_w = (zone_right_px - zone_left_px) / 3.0
        third_h = (zone_bottom_px - zone_top_px) / 3.0
        for i in (1, 2):
            x = zone_left_px + int(round(third_w * i))
            y = zone_top_px + int(round(third_h * i))
            draw.line([x, zone_top_px, x, zone_bottom_px], fill=(90, 90, 90))
            draw.line([zone_left_px, y, zone_right_px, y], fill=(90, 90, 90))

        # Plot pitches for current batter.
        for idx, pitch in enumerate(state.pitches, start=1):
            x = self._map_px_to_x(pitch.px, plot_left, plot_right)
            y = self._map_pz_to_y(pitch.pz, plot_top, plot_bottom)
            color = self._pitch_color(pitch)
            self._draw_dot(draw, x, y, color)
            self._draw_pitch_index(draw, x, y, idx)

        return img

    def _draw_status(self, draw: ImageDraw.ImageDraw, state: CurrentBatterState) -> None:
        # Tiny ball/strike/out indicators along top edge.
        bx = 1
        for i in range(3):
            fill = (0, 180, 0) if i < state.balls else (20, 40, 20)
            draw.rectangle([bx, 1, bx + 3, 4], fill=fill)
            bx += 5

        sx = 20
        for i in range(2):
            fill = (220, 160, 0) if i < state.strikes else (45, 35, 10)
            draw.rectangle([sx, 1, sx + 3, 4], fill=fill)
            sx += 5

        ox = 32
        for i in range(2):
            fill = (180, 0, 0) if i < state.outs else (40, 10, 10)
            draw.rectangle([ox, 1, ox + 3, 4], fill=fill)
            ox += 5

    @staticmethod
    def _pitch_color(pitch: PitchDot) -> Tuple[int, int, int]:
        if pitch.is_ball:
            return (0, 200, 0)
        if pitch.is_strike:
            return (220, 40, 40)
        return (0, 160, 255)

    @staticmethod
    def _draw_dot(draw: ImageDraw.ImageDraw, x: int, y: int, color: Tuple[int, int, int]) -> None:
        r = 2
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    @staticmethod
    def _draw_pitch_index(draw: ImageDraw.ImageDraw, x: int, y: int, idx: int) -> None:
        # Small index tick beside dot. Avoid font dependency.
        mark_x = min(MATRIX_WIDTH - 1, x + 3)
        mark_y = max(0, y - 3)
        h = min(3, idx)
        draw.line([mark_x, mark_y, mark_x, mark_y + h], fill=(255, 255, 255))

    @staticmethod
    def _map_px_to_x(px: float, left: int, right: int) -> int:
        px = max(PLOT_PX_MIN, min(PLOT_PX_MAX, px))
        pct = (px - PLOT_PX_MIN) / (PLOT_PX_MAX - PLOT_PX_MIN)
        return left + int(round(pct * (right - left)))

    @staticmethod
    def _map_pz_to_y(pz: float, top: int, bottom: int) -> int:
        pz = max(PLOT_PZ_MIN, min(PLOT_PZ_MAX, pz))
        # Higher pZ should render higher on the matrix (smaller y)
        pct = (pz - PLOT_PZ_MIN) / (PLOT_PZ_MAX - PLOT_PZ_MIN)
        return bottom - int(round(pct * (bottom - top)))


def fetch_live_data() -> Dict[str, Any]:
    # Try with fields first; if it yields empty shells, retry without fields.
    resp = requests.get(
        LIVE_URL,
        params={"fields": FIELDS},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not (data.get("gameData") or data.get("liveData")):
        print("Fields filter returned empty data; retrying without fields.")
        resp = requests.get(
            LIVE_URL,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    try:
        with open(LAST_RESPONSE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"Failed to write {LAST_RESPONSE_PATH}: {exc}")
    return data


def extract_current_play(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return data.get("liveData", {}).get("plays", {}).get("currentPlay")


def extract_pitch_dots(current_play: Dict[str, Any]) -> List[PitchDot]:
    dots: List[PitchDot] = []
    for event in current_play.get("playEvents", []):
        if not event.get("isPitch"):
            continue

        pitch_data = event.get("pitchData") or {}
        coords = pitch_data.get("coordinates") or {}
        details = event.get("details") or {}

        px = coords.get("pX")
        pz = coords.get("pZ")
        zone_top = pitch_data.get("strikeZoneTop")
        zone_bottom = pitch_data.get("strikeZoneBottom")
        pitch_number = event.get("pitchNumber", event.get("index"))

        if px is None or pz is None or zone_top is None or zone_bottom is None or pitch_number is None:
            continue

        dots.append(
            PitchDot(
                pitch_number=int(pitch_number),
                description=details.get("description", ""),
                call=(details.get("call") or {}).get("description", ""),
                pitch_type=(details.get("type") or {}).get("description", ""),
                is_ball=bool(details.get("isBall", False)),
                is_strike=bool(details.get("isStrike", False)),
                zone=pitch_data.get("zone"),
                px=float(px),
                pz=float(pz),
                zone_top=float(zone_top),
                zone_bottom=float(zone_bottom),
            )
        )
    return dots


def update_state_from_feed(state: CurrentBatterState, data: Dict[str, Any]) -> bool:
    status = (data.get("gameData") or {}).get("status") or {}
    datetime_info = (data.get("gameData") or {}).get("datetime") or {}
    abstract_state = status.get("abstractGameState", "Unknown")
    detailed_state = status.get("detailedState", "Unknown")
    coded_state = status.get("codedGameState", "Unknown")
    start_time = datetime_info.get("dateTime", "Unknown")

    current_play = extract_current_play(data)
    if not current_play:
        print(
            "No currentPlay found in live feed. "
            f"Status: {abstract_state} / {detailed_state} (code={coded_state}), "
            f"startTime={start_time}"
        )
        return False

    about = current_play.get("about") or {}
    matchup = current_play.get("matchup") or {}
    batter = matchup.get("batter") or {}
    count = current_play.get("count") or {}

    batter_id = batter.get("id")
    batter_name = batter.get("fullName", "Unknown Batter")
    at_bat_index = about.get("atBatIndex")

    if batter_id is None:
        print("No batter id in currentPlay.")
        return False

    if state.batter_id != batter_id or state.at_bat_index != at_bat_index:
        state.reset(batter_id=batter_id, batter_name=batter_name, at_bat_index=at_bat_index)
        print(f"\nNew batter: {batter_name} ({batter_id}) | atBatIndex={at_bat_index}")

    state.inning = about.get("inning")
    state.half_inning = about.get("halfInning", "")
    state.balls = int(count.get("balls", 0))
    state.strikes = int(count.get("strikes", 0))
    state.outs = int(count.get("outs", 0))

    for dot in extract_pitch_dots(current_play):
        if state.add_pitch(dot):
            print(
                f"Pitch {dot.pitch_number}: {dot.pitch_type or 'Unknown'} | "
                f"{dot.call or dot.description} | "
                f"pX={dot.px:.3f}, pZ={dot.pz:.3f}, "
                f"zoneTop={dot.zone_top:.3f}, zoneBottom={dot.zone_bottom:.3f}, "
                f"zone={dot.zone}"
            )

    return True


def is_game_over(data: Dict[str, Any]) -> bool:
    status = (data.get("gameData") or {}).get("status") or {}
    abstract_state = (status.get("abstractGameState") or "").upper()
    detailed_state = (status.get("detailedState") or "").upper()
    coded_state = (status.get("codedGameState") or "").upper()
    return abstract_state == "FINAL" or detailed_state == "FINAL" or coded_state in {"F", "O", "DI"}


def main() -> int:
    print(f"Starting strike-zone tracker for gamePk={GAME_PK}")
    print(f"Live URL: {LIVE_URL}?fields=...")
    renderer = MatrixRenderer()
    state = CurrentBatterState()

    while True:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] Polling live feed...")
            data = fetch_live_data()
            if is_game_over(data):
                status = (data.get("gameData") or {}).get("status") or {}
                print(
                    f"[{ts}] Game over: "
                    f"{status.get('abstractGameState','Unknown')} / "
                    f"{status.get('detailedState','Unknown')} "
                    f"(code={status.get('codedGameState','Unknown')}). Stopping."
                )
                return 0
            ok = update_state_from_feed(state, data)
            if ok:
                renderer.render(state)
                print(
                    f"[{ts}] State: batter={state.batter_name or 'Unknown'} "
                    f"id={state.batter_id} atBatIndex={state.at_bat_index} "
                    f"inning={state.inning} {state.half_inning} "
                    f"count={state.balls}-{state.strikes} outs={state.outs} "
                    f"pitches={len(state.pitches)}"
                )
            else:
                # If no live data / current play yet, still emit a blank preview.
                renderer.render(state)
        except requests.HTTPError as exc:
            print(f"HTTP error: {exc}")
        except requests.RequestException as exc:
            print(f"Network error: {exc}")
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            print(f"Unexpected error: {exc}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
