"""Pitch zone renderer for displaying current batter pitch locations.

This renderer was cleaned up to avoid writing any files to disk for
debugging. All diagnostics are emitted via logging instead of creating
heartbeat/proof/debug JSON files.

It preserves the existing coordinate mapping and drawing behavior so it
continues to work with the project's layout/graphics APIs.
"""
from typing import Dict, Any, List, Optional, Tuple

from driver import graphics
import logging
import time

logger = logging.getLogger(__name__)

# Conservative plotting ranges (same as the standalone script)
PLOT_PX_MIN = -1.5
PLOT_PX_MAX = 1.5
PLOT_PZ_MIN = 0.5
PLOT_PZ_MAX = 4.5


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _map_px_to_x(px: float, left: int, width: int) -> int:
    px = _clamp(px, PLOT_PX_MIN, PLOT_PX_MAX)
    pct = (px - PLOT_PX_MIN) / (PLOT_PX_MAX - PLOT_PX_MIN)
    return left + int(round(pct * (width - 1)))


def _map_pz_to_y(pz: float, top: int, height: int) -> int:
    pz = _clamp(pz, PLOT_PZ_MIN, PLOT_PZ_MAX)
    pct = (pz - PLOT_PZ_MIN) / (PLOT_PZ_MAX - PLOT_PZ_MIN)
    # higher pZ -> smaller y (upwards)
    return top + (height - 1) - int(round(pct * (height - 1)))


def _small_dot(canvas, x: int, y: int, color: Tuple[int, int, int]) -> None:
    # draw a 3x3 filled square as a simple dot
    for yy in range(y - 1, y + 2):
        graphics.DrawLine(canvas, x - 1, yy, x + 1, yy, color)


def _draw_rect_outline(canvas, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]) -> None:
    graphics.DrawLine(canvas, x, y, x + w - 1, y, color)
    graphics.DrawLine(canvas, x, y + h - 1, x + w - 1, y + h - 1, color)
    graphics.DrawLine(canvas, x, y, x, y + h - 1, color)
    graphics.DrawLine(canvas, x + w - 1, y, x + w - 1, y + h - 1, color)


def render_pitch_zone(canvas, layout, colors, game) -> None:
    """Render the current batter pitch locations into the layout area.

    Expects layout.coords("pitchzone") to return a dict with keys x,y,width,height,enabled.
    """
    # Get layout coords
    try:
        coords = layout.coords("pitchzone")
    except Exception as e:
        logger.debug("Pitch zone layout not configured or error retrieving coords: %s", e)
        logger.debug("coords lookup context: %r", {"timestamp": time.time(), "game_id": getattr(game, "game_id", None)})
        return

    if not isinstance(coords, dict) or not coords.get("enabled", False):
        logger.debug("Pitch zone disabled or coords invalid; game_id=%s coords=%r", getattr(game, "game_id", None), coords)
        return

    # Colors
    zone_color = colors.graphics_color("pitchzone.zone")
    pitch_ball_color = colors.graphics_color("pitchzone.pitch.ball")
    pitch_strike_color = colors.graphics_color("pitchzone.pitch.strike")
    pitch_other_color = colors.graphics_color("pitchzone.pitch.other")

    left = coords.get("x", 0)
    top = coords.get("y", 0)
    width = coords.get("width", 16)
    height = coords.get("height", 16)

    # Draw outline of plot area
    _draw_rect_outline(canvas, left, top, width, height, zone_color)

    # Read live data from the game object
    live = (getattr(game, "_current_data", None) or {}).get("liveData") or {}
    try:
        live_game_id = (getattr(game, "_current_data", None) or {}).get("gameData", {}).get("game", {}).get("id")
    except Exception:
        live_game_id = None

    current_play = live.get("plays", {}).get("currentPlay")
    logger.debug("render_pitch_zone: current_play present=%s game_id=%s", bool(current_play), getattr(game, "game_id", None))

    # Helper to collect pitch events that contain coordinates
    def collect_from_events(ev_list: List[Dict[str, Any]], collector: List[tuple]) -> None:
        for ev in ev_list:
            if not ev.get("isPitch"):
                continue
            pd = ev.get("pitchData") or {}
            coords_pd = pd.get("coordinates") or {}
            px = coords_pd.get("pX")
            pz = coords_pd.get("pZ")
            if px is None or pz is None:
                continue
            collector.append((ev, pd))

    pitches_with_coords: List[tuple] = []
    all_plays = (live.get("plays", {}) or {}).get("allPlays") or []

    # Prefer finding the at-bat by atBatIndex in allPlays
    target_atbat = None
    try:
        target_atbat = (current_play or {}).get("about", {}).get("atBatIndex")
    except Exception:
        target_atbat = None

    if target_atbat is not None and all_plays:
        found_play = None
        for play in reversed(all_plays):
            about = play.get("about") or {}
            if about.get("atBatIndex") == target_atbat:
                found_play = play
                break
        if found_play:
            collect_from_events(found_play.get("playEvents") or [], pitches_with_coords)

    # If none found yet, try currentPlay events
    if not pitches_with_coords and current_play:
        events = current_play.get("playEvents") or []
        # Log a small sample for debugging
        try:
            sample = []
            for ev in events[:6]:
                pd = ev.get("pitchData") or {}
                coords_pd = pd.get("coordinates") or {}
                sample.append({
                    "isPitch": bool(ev.get("isPitch")),
                    "pX": coords_pd.get("pX"),
                    "pZ": coords_pd.get("pZ"),
                })
            logger.debug("playEvents sample: %r", sample)
        except Exception:
            logger.debug("Failed to prepare playEvents sample")

        collect_from_events(events, pitches_with_coords)

    # Fallback: scan recent allPlays
    if not pitches_with_coords and all_plays:
        for idx, play in enumerate(reversed(all_plays)):
            collect_from_events(play.get("playEvents") or [], pitches_with_coords)
            if pitches_with_coords:
                # Log which play provided the pitchData and a small sample for inspection
                try:
                    ev, pd = pitches_with_coords[0]
                    about = (play.get("about") or {})
                    sample = {
                        "atBatIndex": about.get("atBatIndex"),
                        "play_id": about.get("id"),
                        "pX": (pd.get("coordinates") or {}).get("pX"),
                        "pZ": (pd.get("coordinates") or {}).get("pZ"),
                        "startSpeed": pd.get("startSpeed"),
                        "strikeZoneTop": pd.get("strikeZoneTop"),
                        "strikeZoneBottom": pd.get("strikeZoneBottom"),
                        "pitchType": (ev.get("details") or {}).get("type"),
                        "description": (ev.get("details") or {}).get("description"),
                    }
                    logger.debug("pitchzone: collected %d pitches from allPlays at reversed index=%d about=%r sample=%r", len(pitches_with_coords), idx, about, sample)
                except Exception:
                    logger.exception("pitchzone: failed to build sample log for allPlays-derived pitch")
                break

    logger.debug("Collected %d pitches with coords", len(pitches_with_coords))

    # Determine strike zone (first event with strikeZoneTop/bottom)
    zone_top = None
    zone_bottom = None
    for ev, pd in pitches_with_coords:
        zt = pd.get("strikeZoneTop")
        zb = pd.get("strikeZoneBottom")
        if zt is not None and zb is not None:
            try:
                zone_top = float(zt)
                zone_bottom = float(zb)
                break
            except Exception:
                continue

    if zone_top is None or zone_bottom is None:
        zone_top = 3.5
        zone_bottom = 1.5
        logger.debug("Using default strike zone: top=%s bottom=%s", zone_top, zone_bottom)
    else:
        logger.debug("Using strike zone from event: top=%s bottom=%s", zone_top, zone_bottom)

    # Draw strike zone rectangle
    zone_left_ft = -0.83
    zone_right_ft = 0.83
    zx = _map_px_to_x(zone_left_ft, left, width)
    zx2 = _map_px_to_x(zone_right_ft, left, width)
    zy = _map_pz_to_y(zone_top, top, height)
    zy2 = _map_pz_to_y(zone_bottom, top, height)
    top_y = min(zy, zy2)
    rect_w = abs(zx2 - zx) or 1
    rect_h = abs(zy2 - zy) or 1
    _draw_rect_outline(canvas, zx, top_y, rect_w, rect_h, zone_color)

    # If no pitches to plot, draw a small indicator and exit
    if not pitches_with_coords:
        ox = left + 1
        oy = top + 1
        for yy in range(oy, oy + 3):
            graphics.DrawLine(canvas, ox, yy, ox + 2, yy, pitch_other_color)
        logger.debug("No pitch coords found; rendered overlay indicator")
        return

    # Plot each pitch
    for ev, pd in pitches_with_coords:
        coords_pd = pd.get("coordinates") or {}
        px = coords_pd.get("pX")
        pz = coords_pd.get("pZ")
        try:
            x = _map_px_to_x(float(px), left, width)
            y = _map_pz_to_y(float(pz), top, height)
        except Exception as e:
            logger.debug("Failed to map pitch coords px=%r pz=%r: %s", px, pz, e)
            continue

        details = ev.get("details") or {}
        is_ball = bool(details.get("isBall", False))
        is_strike = bool(details.get("isStrike", False))
        if is_ball:
            color = pitch_ball_color
        elif is_strike:
            color = pitch_strike_color
        else:
            color = pitch_other_color

        logger.debug("Plotting pitch pX=%s pZ=%s -> x=%d y=%d ball=%s strike=%s", px, pz, x, y, is_ball, is_strike)
        _small_dot(canvas, x, y, color)

    # Done
    return
