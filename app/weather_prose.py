"""Render the daily weather strip as newspaper-style prose, no LLM.

Twenty hand-written today-clauses and twenty tomorrow-clauses live in the
``weather_phrases`` table for each of nine condition buckets. At generation
time we pick one of each at random based on the NWS shortForecast for that
period, fill in the high/low temperature slots (Celsius), and concatenate.

The seed phrase library is also defined in this module so it can be loaded
by the Alembic migration that bootstraps the table and asserted against in
tests."""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.weather import bucket as classify_bucket

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

PERIODS = ("today", "tomorrow")
NIGHT_PERIOD = "night"
NIGHT_BUCKETS = ("thunderstorm", "snow")
BUCKETS = (
    "sunny",
    "mostly_sunny",
    "partly_cloudy",
    "cloudy",
    "rain",
    "thunderstorm",
    "snow",
    "fog",
    "windy",
)

# ─────────────────────── Seed phrase library ───────────────────────
#
# 9 buckets × 2 periods × 20 variants = 360 phrases. Slots ``{h}`` and
# ``{l}`` get filled with the high/low temperature in Celsius — both
# computed across the 7 AM–10 PM daytime window, so ``{l}`` is the
# *daytime* low, not the overnight low. Phrases avoid "tonight" /
# "overnight" wording for that reason; the separate severe-night clause
# (see :data:`NIGHT_PHRASES`) handles real overnight weather. The words
# "Today" and "Tomorrow" are pre-wrapped in <b>…</b> so the renderer
# doesn't have to find and bold them at runtime.

PHRASES: dict[str, dict[str, list[str]]] = {
    "sunny": {
        "today": [
            "Sunshine carries <b>today</b>, climbing to {h}°C and slipping to {l}°C.",
            "Sunny <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Plenty of sunshine <b>today</b>, peaking at {h}°C before cooling to {l}°C.",
            "Look for sunshine <b>today</b>, a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> opens sunny, climbing to {h}°C and easing to {l}°C.",
            "Sunny skies <b>today</b>, swinging from a low of {l}°C to a high of {h}°C.",
            "Bright sunshine <b>today</b>, with the mercury reaching {h}°C and slipping to {l}°C.",
            "Sun-drenched <b>today</b>, high {h}°C, low {l}°C.",
            "Expect sunshine <b>today</b>, reaching {h}°C before falling to {l}°C.",
            "Clear and sunny <b>today</b>, peaking near {h}°C and cooling to {l}°C.",
            "A sunny day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "The sun takes the stage <b>today</b>, lifting temperatures to {h}°C before letting them slip to {l}°C.",
            "Sunshine all day <b>today</b>, top of {h}°C, low of {l}°C.",
            "Sunny throughout <b>today</b>, climbing to {h}°C and falling to {l}°C.",
            "<b>Today</b> brings a sunny high of {h}°C and a low of {l}°C.",
            "Looking sunny <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Bright skies <b>today</b>, the high reaching {h}°C and the low settling at {l}°C.",
            "Full sun <b>today</b>, with temperatures topping out at {h}°C and bottoming at {l}°C.",
            "<b>Today</b> stays sunny, between {l}°C and {h}°C.",
            "Sunshine and warmth <b>today</b>, peaking at {h}°C and easing to {l}°C.",
        ],
        "tomorrow": [
            "Sunshine returns <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Sunny <b>tomorrow</b>, climbing to {h}°C and falling to {l}°C.",
            "Plenty of sun <b>tomorrow</b>, peaking near {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b> brings sunshine, a high of {h}°C and a low of {l}°C.",
            "Sunny skies <b>tomorrow</b>, between {l}°C and {h}°C.",
            "<b>Tomorrow</b> opens sunny, lifting to {h}°C before easing to {l}°C.",
            "Look for sunshine <b>tomorrow</b>, with highs of {h}°C and lows around {l}°C.",
            "Sun-drenched <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect sunshine <b>tomorrow</b>, reaching {h}°C and dipping to {l}°C.",
            "Clear and sunny <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "A sunny day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "<b>Tomorrow</b> takes the stage with sunshine, lifting temperatures to {h}°C before letting them slip to {l}°C.",
            "Sunshine all day <b>tomorrow</b>, top of {h}°C, low of {l}°C.",
            "Sunny throughout <b>tomorrow</b>, climbing to {h}°C and falling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: sunny, with a high of {h}°C and a low of {l}°C.",
            "Looking sunny <b>tomorrow</b>, a high of {h}°C and a low of {l}°C.",
            "Bright skies <b>tomorrow</b>, the high reaching {h}°C and the low settling at {l}°C.",
            "Full sun <b>tomorrow</b>, with temperatures topping out at {h}°C and bottoming at {l}°C.",
            "<b>Tomorrow</b> stays sunny, between {l}°C and {h}°C.",
            "Sunshine and warmth <b>tomorrow</b>, peaking at {h}°C and easing to {l}°C.",
        ],
    },
    "mostly_sunny": {
        "today": [
            "Mostly sunny <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> stays mostly sunny, climbing to {h}°C and easing to {l}°C.",
            "Mostly sunny skies <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Plenty of sun with a few clouds <b>today</b>, high of {h}°C and low of {l}°C.",
            "Sunshine with passing clouds <b>today</b>, between {l}°C and {h}°C.",
            "Mostly sunny <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks mostly sunny, top of {h}°C, low of {l}°C.",
            "Sun-leaning skies <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Mostly sunny <b>today</b>, high {h}°C, low {l}°C.",
            "Expect mostly sunny weather <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings mostly sunny skies, a high of {h}°C and a low of {l}°C.",
            "Light cloud cover with plenty of sun <b>today</b>, high of {h}°C and low of {l}°C.",
            "Mostly sunny <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Sun with intermittent clouds <b>today</b>, swinging from {l}°C to {h}°C.",
            "Mostly sunny throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: mostly sunny, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny skies <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "A mostly sunny day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Looking mostly sunny <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny <b>today</b>, reaching {h}°C with a low of {l}°C.",
        ],
        "tomorrow": [
            "Mostly sunny <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> stays mostly sunny, climbing to {h}°C and falling to {l}°C.",
            "Mostly sunny skies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Plenty of sun with a few clouds <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Sunshine with passing clouds <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Mostly sunny <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks mostly sunny, top of {h}°C, low of {l}°C.",
            "Sun-leaning skies <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Mostly sunny <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect mostly sunny weather <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings mostly sunny skies, a high of {h}°C and a low of {l}°C.",
            "Light cloud cover with plenty of sun <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Mostly sunny <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Sun with intermittent clouds <b>tomorrow</b>, swinging from {l}°C to {h}°C.",
            "Mostly sunny throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: mostly sunny, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "A mostly sunny day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Looking mostly sunny <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
        ],
    },
    "partly_cloudy": {
        "today": [
            "Partly cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> stays partly cloudy, climbing to {h}°C and easing to {l}°C.",
            "Mixed sun and clouds <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Partly cloudy skies <b>today</b>, between {l}°C and {h}°C.",
            "Sun and clouds trade off <b>today</b>, high of {h}°C and low of {l}°C.",
            "Partly cloudy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks partly cloudy, top of {h}°C, low of {l}°C.",
            "Clouds drift in and out <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Partly cloudy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect partly cloudy skies <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings a partly cloudy high of {h}°C and a low of {l}°C.",
            "Sun mixed with clouds <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Partly cloudy throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: partly cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking partly cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A partly cloudy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Partly cloudy skies <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Sun pushing through clouds <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Partly cloudy <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Mixed skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Partly cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> stays partly cloudy, climbing to {h}°C and falling to {l}°C.",
            "Mixed sun and clouds <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Partly cloudy skies <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Sun and clouds trade off <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Partly cloudy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks partly cloudy, top of {h}°C, low of {l}°C.",
            "Clouds drift in and out <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Partly cloudy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect partly cloudy skies <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings a partly cloudy high of {h}°C and a low of {l}°C.",
            "Sun mixed with clouds <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Partly cloudy throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: partly cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking partly cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A partly cloudy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Partly cloudy skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Sun pushing through clouds <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Partly cloudy <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Mixed skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "cloudy": {
        "today": [
            "Cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> stays cloudy, climbing to {h}°C and easing to {l}°C.",
            "Overcast skies <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Cloud cover holds <b>today</b>, between {l}°C and {h}°C.",
            "Clouds linger <b>today</b>, high of {h}°C and low of {l}°C.",
            "Cloudy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks cloudy, top of {h}°C, low of {l}°C.",
            "Grey skies <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Cloudy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect cloudy skies <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings cloudy skies, a high of {h}°C and a low of {l}°C.",
            "Mostly cloudy <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Cloudy throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A cloudy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Cloudy skies <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Heavy cloud cover <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Cloudy <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Overcast <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> stays cloudy, climbing to {h}°C and falling to {l}°C.",
            "Overcast skies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Cloud cover holds <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Clouds linger <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Cloudy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks cloudy, top of {h}°C, low of {l}°C.",
            "Grey skies <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Cloudy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect cloudy skies <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings cloudy skies, a high of {h}°C and a low of {l}°C.",
            "Mostly cloudy <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Cloudy throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A cloudy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Cloudy skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Heavy cloud cover <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Cloudy <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Overcast <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "rain": {
        "today": [
            "Rain <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> turns wet, climbing to {h}°C and easing to {l}°C.",
            "Showers move through <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Rain throughout <b>today</b>, between {l}°C and {h}°C.",
            "Wet weather <b>today</b>, high of {h}°C and low of {l}°C.",
            "Rainy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks rainy, top of {h}°C, low of {l}°C.",
            "Showers move in <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Rain <b>today</b>, high {h}°C, low {l}°C.",
            "Expect rain <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings rain, a high of {h}°C and a low of {l}°C.",
            "Steady rain <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Showery <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: rainy, with a high of {h}°C and a low of {l}°C.",
            "Looking rainy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A wet day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Rain falls <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Showers persist <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Rain <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Wet skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Rain <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> turns wet, climbing to {h}°C and falling to {l}°C.",
            "Showers move through <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Rain throughout <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Wet weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Rainy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks rainy, top of {h}°C, low of {l}°C.",
            "Showers move in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Rain <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect rain <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings rain, a high of {h}°C and a low of {l}°C.",
            "Steady rain <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Showery <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: rainy, with a high of {h}°C and a low of {l}°C.",
            "Looking rainy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A wet day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Rain falls <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Showers persist <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Rain <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Wet skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "thunderstorm": {
        "today": [
            "Thunderstorms <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> turns stormy, climbing to {h}°C and easing to {l}°C.",
            "Storms rumble through <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Thunderstorms throughout <b>today</b>, between {l}°C and {h}°C.",
            "Stormy weather <b>today</b>, high of {h}°C and low of {l}°C.",
            "Thundery <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks stormy, top of {h}°C, low of {l}°C.",
            "Storms move in <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Thunderstorms <b>today</b>, high {h}°C, low {l}°C.",
            "Expect thunderstorms <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings thunderstorms, a high of {h}°C and a low of {l}°C.",
            "Stormy skies <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Thunder rolls <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: stormy, with a high of {h}°C and a low of {l}°C.",
            "Looking stormy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A stormy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Storms break <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Thunderstorms persist <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Thunderstorms <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Stormy skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Thunderstorms <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> turns stormy, climbing to {h}°C and falling to {l}°C.",
            "Storms rumble through <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Thunderstorms throughout <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Stormy weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Thundery <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks stormy, top of {h}°C, low of {l}°C.",
            "Storms move in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Thunderstorms <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect thunderstorms <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings thunderstorms, a high of {h}°C and a low of {l}°C.",
            "Stormy skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Thunder rolls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: stormy, with a high of {h}°C and a low of {l}°C.",
            "Looking stormy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A stormy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Storms break <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Thunderstorms persist <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Thunderstorms <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Stormy skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "snow": {
        "today": [
            "Snow <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> turns snowy, climbing to {h}°C and easing to {l}°C.",
            "Snow falls <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Snow throughout <b>today</b>, between {l}°C and {h}°C.",
            "Wintry weather <b>today</b>, high of {h}°C and low of {l}°C.",
            "Snowy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks snowy, top of {h}°C, low of {l}°C.",
            "Snow moves in <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Snow <b>today</b>, high {h}°C, low {l}°C.",
            "Expect snow <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings snow, a high of {h}°C and a low of {l}°C.",
            "Steady snow <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Snow flies <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: snowy, with a high of {h}°C and a low of {l}°C.",
            "Looking snowy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A snowy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Snowfall <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Snow persists <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Snow <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Wintry skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Snow <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> turns snowy, climbing to {h}°C and falling to {l}°C.",
            "Snow falls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Snow throughout <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Wintry weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Snowy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks snowy, top of {h}°C, low of {l}°C.",
            "Snow moves in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Snow <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect snow <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings snow, a high of {h}°C and a low of {l}°C.",
            "Steady snow <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Snow flies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: snowy, with a high of {h}°C and a low of {l}°C.",
            "Looking snowy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A snowy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Snowfall <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Snow persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Snow <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Wintry skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "fog": {
        "today": [
            "Fog <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> stays foggy, climbing to {h}°C and easing to {l}°C.",
            "Fog settles in <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Fog throughout <b>today</b>, between {l}°C and {h}°C.",
            "Hazy weather <b>today</b>, high of {h}°C and low of {l}°C.",
            "Foggy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks foggy, top of {h}°C, low of {l}°C.",
            "Fog rolls in <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Fog <b>today</b>, high {h}°C, low {l}°C.",
            "Expect fog <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings fog, a high of {h}°C and a low of {l}°C.",
            "Misty skies <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Fog hangs over <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: foggy, with a high of {h}°C and a low of {l}°C.",
            "Looking foggy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A foggy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Fog blankets <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Fog persists <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Fog <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Hazy skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Fog <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> stays foggy, climbing to {h}°C and falling to {l}°C.",
            "Fog settles in <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Fog throughout <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Hazy weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Foggy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks foggy, top of {h}°C, low of {l}°C.",
            "Fog rolls in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Fog <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect fog <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings fog, a high of {h}°C and a low of {l}°C.",
            "Misty skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Fog hangs over <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: foggy, with a high of {h}°C and a low of {l}°C.",
            "Looking foggy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A foggy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Fog blankets <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Fog persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Fog <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Hazy skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
    "windy": {
        "today": [
            "Windy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Today</b> turns blustery, climbing to {h}°C and easing to {l}°C.",
            "A breezy day <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "Wind throughout <b>today</b>, between {l}°C and {h}°C.",
            "Gusty weather <b>today</b>, high of {h}°C and low of {l}°C.",
            "Windy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Today</b> looks windy, top of {h}°C, low of {l}°C.",
            "Wind picks up <b>today</b>, climbing to {h}°C before falling to {l}°C.",
            "Windy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect wind <b>today</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Today</b> brings wind, a high of {h}°C and a low of {l}°C.",
            "Blustery skies <b>today</b>, lifting to {h}°C and easing to {l}°C.",
            "Wind howls <b>today</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Today</b>'s outlook: windy, with a high of {h}°C and a low of {l}°C.",
            "Looking windy <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "A windy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Gusts sweep through <b>today</b>, climbing to {h}°C before slipping to {l}°C.",
            "Wind persists <b>today</b>, with a high of {h}°C and a low of {l}°C.",
            "Windy <b>today</b>, reaching {h}°C with a low of {l}°C.",
            "Breezy skies <b>today</b>, swinging between {l}°C and {h}°C.",
        ],
        "tomorrow": [
            "Windy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "<b>Tomorrow</b> turns blustery, climbing to {h}°C and falling to {l}°C.",
            "A breezy day <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "Wind throughout <b>tomorrow</b>, between {l}°C and {h}°C.",
            "Gusty weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Windy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C.",
            "<b>Tomorrow</b> looks windy, top of {h}°C, low of {l}°C.",
            "Wind picks up <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C.",
            "Windy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect wind <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C.",
            "<b>Tomorrow</b> brings wind, a high of {h}°C and a low of {l}°C.",
            "Blustery skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C.",
            "Wind howls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C.",
            "<b>Tomorrow</b>'s outlook: windy, with a high of {h}°C and a low of {l}°C.",
            "Looking windy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A windy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Gusts sweep through <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C.",
            "Wind persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Windy <b>tomorrow</b>, reaching {h}°C with a low of {l}°C.",
            "Breezy skies <b>tomorrow</b>, swinging between {l}°C and {h}°C.",
        ],
    },
}


# ─────────────────────── Night phrase library ───────────────────────
#
# Severe-overnight phrases. Appended after the today/tomorrow clause
# (and after its optional "; overnight low …" tail) when the 10 PM–7 AM
# window following that day contains thunderstorm or snow conditions.
# 20 variants per bucket × 2 buckets = 40 phrases. Each phrase stands
# on its own — no temperature slot, no period word — so it reads
# naturally appended to either today's or tomorrow's sentence.

NIGHT_PHRASES: dict[str, list[str]] = {
    "thunderstorm": [
        "Thunderstorms rumble through.",
        "Storms break out.",
        "Lightning lights the sky.",
        "Thunderstorms move in after dark.",
        "Storms develop.",
        "Thunder rolls.",
        "Stormy weather.",
        "Thunderstorms push through the overnight hours.",
        "Storms rumble after dark.",
        "Thunderstorms light up the.",
        "Late-night thunderstorms.",
        "Storms persist.",
        "Heavy thunderstorms after midnight.",
        "Thunder and lightning.",
        "Storms sweep through.",
        "Overnight thunderstorms.",
        "Bursts of thunder.",
        "Storms gather.",
        "Thunder echoes.",
        "Stormy skies.",
    ],
    "snow": [
        "Snow falls.",
        "Snowfall after dark.",
        "Snow piles up.",
        "Wintry weather.",
        "Snow flies through the overnight hours.",
        "Steady snow.",
        "Snow develops after midnight.",
        "Snow showers.",
        "Overnight snowfall.",
        "Snow blankets the area.",
        "Heavy snow.",
        "Snow persists.",
        "Late-night snow.",
        "Snow drifts in.",
        "Snowy skies.",
        "Snow sweeps in after dark.",
        "Snow continues.",
        "Overnight snow showers.",
        "Snowflakes fall.",
        "Snow sets in.",
    ],
}


def all_seed_rows() -> list[dict]:
    """Flatten ``PHRASES`` into the row shape expected by the migration's
    ``op.bulk_insert``: ``[{"period", "bucket", "text"}, …]``."""
    rows: list[dict] = []
    for bkt in BUCKETS:
        for period in PERIODS:
            for text in PHRASES[bkt][period]:
                rows.append({"period": period, "bucket": bkt, "text": text})
    return rows


def night_seed_rows() -> list[dict]:
    """Flatten ``NIGHT_PHRASES`` into the same row shape, with
    ``period="night"``."""
    rows: list[dict] = []
    for bkt in NIGHT_BUCKETS:
        for text in NIGHT_PHRASES[bkt]:
            rows.append({"period": NIGHT_PERIOD, "bucket": bkt, "text": text})
    return rows


# ─────────────────────────── Renderer ───────────────────────────


def _pick_one(s: Session, period: str, bkt: str, rng: random.Random) -> str | None:
    """Return one ``text`` for (period, bucket), or ``None`` if no row matches.

    Avoids ``func.random()`` so the call stays portable between Postgres and
    the SQLite test setup (SQLite has ``func.random()`` but Postgres uses
    ``random()`` natively; both work here, but pulling a small candidate set
    and picking in Python is simpler and lets tests inject a seeded RNG."""
    from app.db import WeatherPhrase

    rows = (
        s.execute(
            select(WeatherPhrase.text).where(
                WeatherPhrase.period == period,
                WeatherPhrase.bucket == bkt,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return rng.choice(rows)


# Overnight lows colder than this threshold (in Celsius, regardless of the
# user's display unit) earn a "; overnight low N°C" tail on the prose
# clause. Above the threshold the tail is omitted — readers can already
# infer "a normal cool overnight" from the day clause.
NIGHT_L_TAIL_C_THRESHOLD = 5


def _render_period(
    s: Session,
    period: str,
    forecast: dict,
    rng: random.Random,
    *,
    short_key: str,
    h_key: str,
    l_key: str,
    night_l_key: str,
    night_severe_key: str,
) -> str | None:
    """Render one period's clause (today or tomorrow) plus, if applicable,
    its overnight-low tail and severe-overnight night clause.

    Returns ``None`` when the day's high/low are missing — caller skips."""
    h = forecast.get(h_key)
    low = forecast.get(l_key)
    if h is None or low is None:
        return None
    bkt = classify_bucket(forecast.get(short_key, "") or "")
    text = _pick_one(s, period, bkt, rng)
    if not text:
        return None
    clause = text.format(h=h, l=low)

    night_l = forecast.get(night_l_key)
    if night_l is not None and int(night_l) <= NIGHT_L_TAIL_C_THRESHOLD:
        # Strip a trailing period so the tail reads as one sentence; restore
        # it after appending.
        body = clause.rstrip()
        if body.endswith("."):
            body = body[:-1]
        clause = f"{body}; overnight low {int(night_l)}°C."

    night_severe = forecast.get(night_severe_key)
    if night_severe in NIGHT_BUCKETS:
        night_text = _pick_one(s, NIGHT_PERIOD, night_severe, rng)
        if night_text:
            clause = f"{clause} {night_text}"

    return clause


def render_prose_html(
    forecast: dict,
    alerts: list[str],
    s: Session,
    *,
    rng: random.Random | None = None,
) -> str:
    """Render today + tomorrow weather as a single ``<div class="weather-prose">``.

    Returns ``""`` when neither today nor tomorrow has a usable H/L pair —
    callers should treat the empty string as "fall back to the legacy strip"
    so a missing-data day still renders something sensible.

    Each period clause may be followed by:

    1. ``; overnight low N°C`` — when ``{period}_night_l`` is at or below
       :data:`NIGHT_L_TAIL_C_THRESHOLD` (5°C, evaluated in Celsius regardless
       of the user's display unit; ``app.weather.convert_celsius_html`` does
       the final °C→°F swap downstream).
    2. A severe-overnight night phrase from :data:`NIGHT_PHRASES` — when
       ``{period}_night_severe`` is ``"thunderstorm"`` or ``"snow"``.

    Alerts are appended as ``⚠ {alert}`` segments after the prose, matching
    the suffix style used in ``app.weather.build_weather_strip``."""
    if rng is None:
        rng = random

    parts: list[str] = []

    today_clause = _render_period(
        s,
        "today",
        forecast,
        rng,
        short_key="today_short",
        h_key="today_h",
        l_key="today_l",
        night_l_key="today_night_l",
        night_severe_key="today_night_severe",
    )
    if today_clause:
        parts.append(today_clause)

    tomorrow_clause = _render_period(
        s,
        "tomorrow",
        forecast,
        rng,
        short_key="tomorrow_short",
        h_key="tomorrow_h",
        l_key="tomorrow_l",
        night_l_key="tomorrow_night_l",
        night_severe_key="tomorrow_night_severe",
    )
    if tomorrow_clause:
        parts.append(tomorrow_clause)

    if not parts:
        return ""

    for a in alerts or []:
        parts.append(f"⚠ {a}")

    inner = " ".join(parts)
    return f'<div class="weather-prose">{inner}</div>'


def count_seed_rows() -> int:
    """Sanity helper used by tests and migrations: total rows in the seed."""
    return sum(len(PHRASES[b][p]) for b in BUCKETS for p in PERIODS)


__all__ = [
    "BUCKETS",
    "NIGHT_BUCKETS",
    "NIGHT_L_TAIL_C_THRESHOLD",
    "NIGHT_PERIOD",
    "NIGHT_PHRASES",
    "PERIODS",
    "PHRASES",
    "all_seed_rows",
    "count_seed_rows",
    "night_seed_rows",
    "render_prose_html",
]
