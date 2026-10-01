"""
agentic_water_monitor.py

A real reference implementation of an "agentic" pipeline, as opposed to a
fixed script. The difference from compute_water_change.py is structural:

    compute_water_change.py:  YOU decide the order of operations in Python.
                              Claude is called once, at the end, to phrase
                              a sentence. It cannot ask for more data.

    agentic_water_monitor.py: CLAUDE decides the order of operations. Your
                              code just exposes tools (fetch data, compute
                              water extent, conclude) and a loop that keeps
                              asking Claude "what do you want to do next?"
                              until Claude itself calls finalize_report.

Concretely, Claude can now:
  - decide it needs a scene for a date range you never specified
  - fetch a THIRD scene on its own if the first two look ambiguous
  - decide the change isn't significant and end the investigation quickly
  - decide it needs more evidence before concluding, and keep going

None of that branching is written in this file's control flow — it emerges
from the loop just repeatedly asking Claude for its next action.

Usage
-----
    python agentic_water_monitor.py \
        --location "Lake Mead, NV/AZ" \
        --bbox -114.75 36.00 -114.30 36.25 \
        --goal "Compare water extent between 2015 and 2023, decide if the change is significant, and explain a likely driver."

Requires the same .env as the other scripts (EARTHDATA_USERNAME,
EARTHDATA_PASSWORD, ANTHROPIC_API_KEY) plus everything already installed
for extract_eo_data.py and compute_water_change.py.
"""

import argparse
import json
import logging
import os
import sys
import tempfile

import requests
from dotenv import load_dotenv

# Reuse the plumbing you already have — the agent's "tools" are thin
# wrappers around these, not new logic.
import extract_eo_data as eo
import compute_water_change as wc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TURNS = 8  # safety cap so a confused agent can't loop forever and burn tokens/quota


# ---------------------------------------------------------------------------
# Tool definitions — this is the "menu" of actions Claude is allowed to take.
# Claude only ever sees these descriptions + schemas; it never sees this
# file's Python code.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "fetch_scene",
        "description": (
            "Search NASA's HLS (Harmonized Landsat Sentinel-2) archive for scenes "
            "over a bounding box and date range, download up to 3 granules, and "
            "return a local folder path. Use this whenever you need imagery for a "
            "new date range you don't already have downloaded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_name": {
                    "type": "string",
                    "description": "HLS dataset short_name, e.g. HLSL30 (Landsat) or HLSS30 (Sentinel-2).",
                    "default": "HLSL30",
                },
                "min_lon": {"type": "number"},
                "min_lat": {"type": "number"},
                "max_lon": {"type": "number"},
                "max_lat": {"type": "number"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["min_lon", "min_lat", "max_lon", "max_lat", "start_date", "end_date"],
        },
    },
    {
        "name": "compute_water_extent",
        "description": (
            "Compute water surface area (km^2) for a previously fetched scene folder "
            "using NDWI. Call this after fetch_scene, passing the folder path it returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder path returned by fetch_scene."},
                "green_band": {"type": "string", "default": "B03"},
                "nir_band": {"type": "string", "default": "B05"},
            },
            "required": ["folder"],
        },
    },
    {
        "name": "finalize_report",
        "description": (
            "Call this ONLY when you're confident you have enough evidence to conclude. "
            "This ends the investigation — no more tools will be called after this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-4 plain-English sentences: the finding, its magnitude, and a likely driver.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Your confidence in this conclusion given the evidence gathered.",
                },
            },
            "required": ["summary", "confidence"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution — the actual Python behind each tool name above.
# Each function returns a plain dict; errors are returned as data (not
# raised) so Claude can see the failure and decide how to react to it.
# ---------------------------------------------------------------------------

def tool_fetch_scene(input: dict) -> dict:
    bbox = (input["min_lon"], input["min_lat"], input["max_lon"], input["max_lat"])
    short_name = input.get("short_name", "HLSL30")
    try:
        granules = eo.search_granules(
            short_name=short_name,
            bbox=bbox,
            start_date=input["start_date"],
            end_date=input["end_date"],
            max_results=3,
        )
        if not granules:
            return {"error": "No granules found for that area/date range. Try a wider date range."}

        folder = tempfile.mkdtemp(prefix="vaanai_scene_")
        eo.download_granules(granules, folder)
        return {"folder": folder, "granule_count": len(granules)}
    except Exception as exc:  # noqa: BLE001 - surface any failure back to the agent as data
        return {"error": str(exc)}


def tool_compute_water_extent(input: dict) -> dict:
    folder = input["folder"]
    green_band = input.get("green_band", "B03")
    nir_band = input.get("nir_band", "B05")
    try:
        ndwi = wc.compute_ndwi(
            wc.find_band_file(folder, green_band),
            wc.find_band_file(folder, nir_band),
        )
        area = wc.water_area_km2(ndwi)
        return {"water_area_km2": round(area, 2)}
    except SystemExit:
        # find_band_file calls sys.exit on failure in the original script;
        # convert that into a normal error result instead of killing the agent.
        return {"error": f"Could not find band {green_band}/{nir_band} in {folder}."}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


TOOL_FUNCTIONS = {
    "fetch_scene": tool_fetch_scene,
    "compute_water_extent": tool_compute_water_extent,
}


# ---------------------------------------------------------------------------
# The agent loop itself — this is the entire "agentic" mechanism.
# ---------------------------------------------------------------------------

def call_claude(messages: list, api_key: str) -> dict:
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "tools": TOOLS,
            "messages": messages,
        },
        timeout=60,
    )
    if not response.ok:
        # Anthropic's error body explains exactly what's wrong with the request —
        # surface it instead of letting raise_for_status() hide it.
        log.error("Anthropic API error %s: %s", response.status_code, response.text)
    response.raise_for_status()
    return response.json()


def run_agent(goal: str, api_key: str) -> None:
    messages = [{"role": "user", "content": goal}]

    for turn in range(1, MAX_TURNS + 1):
        log.info("--- Turn %d: asking Claude what to do next ---", turn)
        response = call_claude(messages, api_key)
        content = response["content"]
        messages.append({"role": "assistant", "content": content})

        # Print any reasoning/commentary Claude produced alongside tool calls —
        # this is what makes the decision-making visible instead of a black box.
        for block in content:
            if block["type"] == "text" and block["text"].strip():
                print(f"\n[Claude]: {block['text'].strip()}")

        tool_use_blocks = [b for b in content if b["type"] == "tool_use"]

        if not tool_use_blocks:
            # Claude replied with only text and called no tool — treat as done.
            log.info("No tool call made; ending.")
            return

        tool_results = []
        stop = False

        for block in tool_use_blocks:
            name = block["name"]
            tool_input = block["input"]
            log.info("Claude called tool: %s(%s)", name, tool_input)

            if name == "finalize_report":
                print("\n=== FINAL REPORT ===")
                print(f"Confidence: {tool_input['confidence']}")
                print(tool_input["summary"])
                stop = True
                break

            func = TOOL_FUNCTIONS.get(name)
            if func is None:
                result = {"error": f"Unknown tool '{name}'"}
            else:
                result = func(tool_input)

            log.info("Tool result: %s", result)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result),
                }
            )

        if stop:
            return

        # Feed the tool results back so Claude can decide its NEXT move.
        messages.append({"role": "user", "content": tool_results})

    log.warning("Hit MAX_TURNS (%d) without a final report. Ending.", MAX_TURNS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--location", required=True, help="Human-readable name, for context in the prompt.")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), required=True)
    parser.add_argument(
        "--goal",
        default="Investigate how water extent has changed over time in this area and explain why.",
        help="The open-ended instruction given to the agent — deliberately not a fixed step-by-step recipe.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set in .env — required for the agent loop.")
        sys.exit(1)

    eo.authenticate()  # still needs NASA credentials to actually fetch data

    args = parse_args()
    min_lon, min_lat, max_lon, max_lat = args.bbox

    goal_prompt = (
        f"Location: {args.location}\n"
        f"Bounding box: min_lon={min_lon}, min_lat={min_lat}, max_lon={max_lon}, max_lat={max_lat}\n\n"
        f"Task: {args.goal}\n\n"
        "You have tools to fetch satellite scenes for date ranges you choose and to compute "
        "water extent from them. Decide which date ranges to compare, fetch what you need, "
        "compute the numbers, and only call finalize_report once you're confident in a conclusion. "
        "If two scenes give an ambiguous or surprising result, consider fetching a third data point "
        "before concluding."
    )

    run_agent(goal_prompt, api_key)


if __name__ == "__main__":
    main()
