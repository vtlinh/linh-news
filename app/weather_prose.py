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
# ``{l}`` get filled with the high/low temperature (°C, integer). The
# words "Today", "Tomorrow", and "Tonight" are pre-wrapped in <b>…</b>
# so the renderer doesn't have to find and bold them at runtime.

PHRASES: dict[str, dict[str, list[str]]] = {
    "sunny": {
        "today": [
            "Sunshine carries <b>today</b>, climbing to {h}°C and slipping to {l}°C <b>tonight</b>.",
            "Sunny <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Plenty of sunshine <b>today</b>, peaking at {h}°C before cooling to {l}°C <b>tonight</b>.",
            "Look for sunshine <b>today</b>, a high of {h}°C and a low of {l}°C overnight.",
            "<b>Today</b> opens sunny, climbing to {h}°C and easing to {l}°C overnight.",
            "Sunny skies <b>today</b>, swinging from a low of {l}°C to a high of {h}°C.",
            "Bright sunshine <b>today</b>, with the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "Sun-drenched <b>today</b>, high {h}°C, low {l}°C.",
            "Expect sunshine <b>today</b>, reaching {h}°C before falling to {l}°C <b>tonight</b>.",
            "Clear and sunny <b>today</b>, peaking near {h}°C and cooling to {l}°C overnight.",
            "A sunny day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "The sun takes the stage <b>today</b>, lifting temperatures to {h}°C before letting them slip to {l}°C overnight.",
            "Sunshine all day <b>today</b>, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Sunny throughout <b>today</b>, climbing to {h}°C and falling to {l}°C overnight.",
            "<b>Today</b> brings a sunny high of {h}°C and an overnight low of {l}°C.",
            "Looking sunny <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Bright skies <b>today</b>, the high reaching {h}°C and the low settling at {l}°C overnight.",
            "Full sun <b>today</b>, with temperatures topping out at {h}°C and bottoming at {l}°C overnight.",
            "<b>Today</b> stays sunny, between {l}°C overnight and {h}°C in the afternoon.",
            "Sunshine and warmth <b>today</b>, peaking at {h}°C and easing to {l}°C <b>tonight</b>.",
        ],
        "tomorrow": [
            "Sunshine returns <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "Sunny <b>tomorrow</b>, climbing to {h}°C and falling to {l}°C overnight.",
            "Plenty of sun <b>tomorrow</b>, peaking near {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b> brings sunshine, a high of {h}°C and a low of {l}°C.",
            "Sunny skies <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "<b>Tomorrow</b> opens sunny, lifting to {h}°C before easing to {l}°C overnight.",
            "Look for sunshine <b>tomorrow</b>, with highs of {h}°C and lows around {l}°C.",
            "Sun-drenched <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect sunshine <b>tomorrow</b>, reaching {h}°C and dipping to {l}°C overnight.",
            "Clear and sunny <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "A sunny day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "<b>Tomorrow</b> takes the stage with sunshine, lifting temperatures to {h}°C before letting them slip to {l}°C overnight.",
            "Sunshine all day <b>tomorrow</b>, top of {h}°C, low of {l}°C overnight.",
            "Sunny throughout <b>tomorrow</b>, climbing to {h}°C and falling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: sunny, with a high of {h}°C and a low of {l}°C.",
            "Looking sunny <b>tomorrow</b>, a high of {h}°C and a low of {l}°C overnight.",
            "Bright skies <b>tomorrow</b>, the high reaching {h}°C and the low settling at {l}°C overnight.",
            "Full sun <b>tomorrow</b>, with temperatures topping out at {h}°C and bottoming at {l}°C overnight.",
            "<b>Tomorrow</b> stays sunny, between {l}°C overnight and {h}°C in the afternoon.",
            "Sunshine and warmth <b>tomorrow</b>, peaking at {h}°C and easing to {l}°C overnight.",
        ],
    },
    "mostly_sunny": {
        "today": [
            "Mostly sunny <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> stays mostly sunny, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Mostly sunny skies <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Plenty of sun with a few clouds <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Sunshine with passing clouds <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Mostly sunny <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks mostly sunny, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Sun-leaning skies <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Mostly sunny <b>today</b>, high {h}°C, low {l}°C.",
            "Expect mostly sunny weather <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings mostly sunny skies, a high of {h}°C and a low of {l}°C.",
            "Light cloud cover with plenty of sun <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Mostly sunny <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Sun with intermittent clouds <b>today</b>, swinging from {l}°C overnight to {h}°C.",
            "Mostly sunny throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: mostly sunny, with a high of {h}°C and a low of {l}°C overnight.",
            "Mostly sunny skies <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "A mostly sunny day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Looking mostly sunny <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Mostly sunny <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
        ],
        "tomorrow": [
            "Mostly sunny <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> stays mostly sunny, climbing to {h}°C and falling to {l}°C overnight.",
            "Mostly sunny skies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Plenty of sun with a few clouds <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Sunshine with passing clouds <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Mostly sunny <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks mostly sunny, top of {h}°C, low of {l}°C overnight.",
            "Sun-leaning skies <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Mostly sunny <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect mostly sunny weather <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings mostly sunny skies, a high of {h}°C and a low of {l}°C.",
            "Light cloud cover with plenty of sun <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Mostly sunny <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Sun with intermittent clouds <b>tomorrow</b>, swinging from {l}°C overnight to {h}°C.",
            "Mostly sunny throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: mostly sunny, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "A mostly sunny day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Looking mostly sunny <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Mostly sunny <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
        ],
    },
    "partly_cloudy": {
        "today": [
            "Partly cloudy <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> stays partly cloudy, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Mixed sun and clouds <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Partly cloudy skies <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Sun and clouds trade off <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Partly cloudy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks partly cloudy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Clouds drift in and out <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Partly cloudy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect partly cloudy skies <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings a partly cloudy high of {h}°C and an overnight low of {l}°C.",
            "Sun mixed with clouds <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Partly cloudy throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: partly cloudy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking partly cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A partly cloudy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Partly cloudy skies <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Sun pushing through clouds <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Partly cloudy <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Mixed skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Partly cloudy <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> stays partly cloudy, climbing to {h}°C and falling to {l}°C overnight.",
            "Mixed sun and clouds <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Partly cloudy skies <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Sun and clouds trade off <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Partly cloudy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks partly cloudy, top of {h}°C, low of {l}°C overnight.",
            "Clouds drift in and out <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Partly cloudy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect partly cloudy skies <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings a partly cloudy high of {h}°C and an overnight low of {l}°C.",
            "Sun mixed with clouds <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Partly cloudy throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: partly cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking partly cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A partly cloudy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Partly cloudy skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Sun pushing through clouds <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Partly cloudy <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Mixed skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "cloudy": {
        "today": [
            "Cloudy <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> stays cloudy, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Overcast skies <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Cloud cover holds <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Clouds linger <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Cloudy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks cloudy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Grey skies <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Cloudy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect cloudy skies <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings cloudy skies, a high of {h}°C and a low of {l}°C.",
            "Mostly cloudy <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Cloudy throughout <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: cloudy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking cloudy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A cloudy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Cloudy skies <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Heavy cloud cover <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Cloudy <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Overcast <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Cloudy <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> stays cloudy, climbing to {h}°C and falling to {l}°C overnight.",
            "Overcast skies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Cloud cover holds <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Clouds linger <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Cloudy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks cloudy, top of {h}°C, low of {l}°C overnight.",
            "Grey skies <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Cloudy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect cloudy skies <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings cloudy skies, a high of {h}°C and a low of {l}°C.",
            "Mostly cloudy <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Cloudy throughout <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: cloudy, with a high of {h}°C and a low of {l}°C.",
            "Looking cloudy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A cloudy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Cloudy skies <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Heavy cloud cover <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Cloudy <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Overcast <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "rain": {
        "today": [
            "Rain <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> turns wet, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Showers move through <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Rain throughout <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Wet weather <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Rainy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks rainy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Showers move in <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Rain <b>today</b>, high {h}°C, low {l}°C.",
            "Expect rain <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings rain, a high of {h}°C and a low of {l}°C.",
            "Steady rain <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Showery <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: rainy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking rainy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A wet day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Rain falls <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Showers persist <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Rain <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Wet skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Rain <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> turns wet, climbing to {h}°C and falling to {l}°C overnight.",
            "Showers move through <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Rain throughout <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Wet weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Rainy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks rainy, top of {h}°C, low of {l}°C overnight.",
            "Showers move in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Rain <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect rain <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings rain, a high of {h}°C and a low of {l}°C.",
            "Steady rain <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Showery <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: rainy, with a high of {h}°C and a low of {l}°C.",
            "Looking rainy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A wet day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Rain falls <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Showers persist <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Rain <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Wet skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "thunderstorm": {
        "today": [
            "Thunderstorms <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> turns stormy, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Storms rumble through <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Thunderstorms throughout <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Stormy weather <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Thundery <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks stormy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Storms move in <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Thunderstorms <b>today</b>, high {h}°C, low {l}°C.",
            "Expect thunderstorms <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings thunderstorms, a high of {h}°C and a low of {l}°C.",
            "Stormy skies <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Thunder rolls <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: stormy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking stormy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A stormy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Storms break <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Thunderstorms persist <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Thunderstorms <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Stormy skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Thunderstorms <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> turns stormy, climbing to {h}°C and falling to {l}°C overnight.",
            "Storms rumble through <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Thunderstorms throughout <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Stormy weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Thundery <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks stormy, top of {h}°C, low of {l}°C overnight.",
            "Storms move in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Thunderstorms <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect thunderstorms <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings thunderstorms, a high of {h}°C and a low of {l}°C.",
            "Stormy skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Thunder rolls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: stormy, with a high of {h}°C and a low of {l}°C.",
            "Looking stormy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A stormy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Storms break <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Thunderstorms persist <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Thunderstorms <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Stormy skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "snow": {
        "today": [
            "Snow <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> turns snowy, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Snow falls <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Snow throughout <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Wintry weather <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Snowy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks snowy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Snow moves in <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Snow <b>today</b>, high {h}°C, low {l}°C.",
            "Expect snow <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings snow, a high of {h}°C and a low of {l}°C.",
            "Steady snow <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Snow flies <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: snowy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking snowy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A snowy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Snowfall <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Snow persists <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Snow <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Wintry skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Snow <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> turns snowy, climbing to {h}°C and falling to {l}°C overnight.",
            "Snow falls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Snow throughout <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Wintry weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Snowy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks snowy, top of {h}°C, low of {l}°C overnight.",
            "Snow moves in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Snow <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect snow <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings snow, a high of {h}°C and a low of {l}°C.",
            "Steady snow <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Snow flies <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: snowy, with a high of {h}°C and a low of {l}°C.",
            "Looking snowy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A snowy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Snowfall <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Snow persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Snow <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Wintry skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "fog": {
        "today": [
            "Fog <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> stays foggy, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "Fog settles in <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Fog throughout <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Hazy weather <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Foggy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks foggy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Fog rolls in <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Fog <b>today</b>, high {h}°C, low {l}°C.",
            "Expect fog <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings fog, a high of {h}°C and a low of {l}°C.",
            "Misty skies <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Fog hangs over <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: foggy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking foggy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A foggy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Fog blankets <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Fog persists <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Fog <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Hazy skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Fog <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> stays foggy, climbing to {h}°C and falling to {l}°C overnight.",
            "Fog settles in <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Fog throughout <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Hazy weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Foggy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks foggy, top of {h}°C, low of {l}°C overnight.",
            "Fog rolls in <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Fog <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect fog <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings fog, a high of {h}°C and a low of {l}°C.",
            "Misty skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Fog hangs over <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: foggy, with a high of {h}°C and a low of {l}°C.",
            "Looking foggy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A foggy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Fog blankets <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Fog persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Fog <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Hazy skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
    "windy": {
        "today": [
            "Windy <b>today</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Today</b> turns blustery, climbing to {h}°C and easing to {l}°C <b>tonight</b>.",
            "A breezy day <b>today</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Wind throughout <b>today</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Gusty weather <b>today</b>, high of {h}°C and low of {l}°C overnight.",
            "Windy <b>today</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Today</b> looks windy, top of {h}°C, low of {l}°C <b>tonight</b>.",
            "Wind picks up <b>today</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Windy <b>today</b>, high {h}°C, low {l}°C.",
            "Expect wind <b>today</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Today</b> brings wind, a high of {h}°C and a low of {l}°C.",
            "Blustery skies <b>today</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Wind howls <b>today</b>, peaking at {h}°C and cooling to {l}°C <b>tonight</b>.",
            "<b>Today</b>'s outlook: windy, with a high of {h}°C and a low of {l}°C overnight.",
            "Looking windy <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "A windy day ahead <b>today</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Gusts sweep through <b>today</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Wind persists <b>today</b>, with a high of {h}°C and a low of {l}°C overnight.",
            "Windy <b>today</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Breezy skies <b>today</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
        "tomorrow": [
            "Windy <b>tomorrow</b>, with a high of {h}°C and an overnight low of {l}°C.",
            "<b>Tomorrow</b> turns blustery, climbing to {h}°C and falling to {l}°C overnight.",
            "A breezy day <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "Wind throughout <b>tomorrow</b>, between {l}°C overnight and {h}°C in the afternoon.",
            "Gusty weather <b>tomorrow</b>, high of {h}°C and low of {l}°C.",
            "Windy <b>tomorrow</b>, the mercury reaching {h}°C and slipping to {l}°C overnight.",
            "<b>Tomorrow</b> looks windy, top of {h}°C, low of {l}°C overnight.",
            "Wind picks up <b>tomorrow</b>, climbing to {h}°C before falling to {l}°C overnight.",
            "Windy <b>tomorrow</b>, high {h}°C, low {l}°C.",
            "Expect wind <b>tomorrow</b>, peaking near {h}°C and dipping to {l}°C overnight.",
            "<b>Tomorrow</b> brings wind, a high of {h}°C and a low of {l}°C.",
            "Blustery skies <b>tomorrow</b>, lifting to {h}°C and easing to {l}°C overnight.",
            "Wind howls <b>tomorrow</b>, peaking at {h}°C and cooling to {l}°C overnight.",
            "<b>Tomorrow</b>'s outlook: windy, with a high of {h}°C and a low of {l}°C.",
            "Looking windy <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "A windy day ahead <b>tomorrow</b> — {h}°C at its warmest, {l}°C at its coolest.",
            "Gusts sweep through <b>tomorrow</b>, climbing to {h}°C before slipping to {l}°C overnight.",
            "Wind persists <b>tomorrow</b>, with a high of {h}°C and a low of {l}°C.",
            "Windy <b>tomorrow</b>, the day reaching {h}°C and the night settling at {l}°C.",
            "Breezy skies <b>tomorrow</b>, swinging between {l}°C overnight and {h}°C in the afternoon.",
        ],
    },
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

    Alerts are appended as ``⚠ {alert}`` segments after the prose, matching
    the suffix style used in ``app.weather.build_weather_strip``."""
    if rng is None:
        rng = random

    parts: list[str] = []

    today_h = forecast.get("today_h")
    today_l = forecast.get("today_l")
    if today_h is not None and today_l is not None:
        bkt = classify_bucket(forecast.get("today_short", ""))
        text = _pick_one(s, "today", bkt, rng)
        if text:
            parts.append(text.format(h=today_h, l=today_l))

    tomorrow_h = forecast.get("tomorrow_h")
    tomorrow_l = forecast.get("tomorrow_l")
    if tomorrow_h is not None and tomorrow_l is not None:
        bkt = classify_bucket(forecast.get("tomorrow_short", ""))
        text = _pick_one(s, "tomorrow", bkt, rng)
        if text:
            parts.append(text.format(h=tomorrow_h, l=tomorrow_l))

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
    "PERIODS",
    "PHRASES",
    "all_seed_rows",
    "count_seed_rows",
    "render_prose_html",
]
