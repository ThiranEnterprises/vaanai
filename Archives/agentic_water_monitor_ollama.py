"""
agentic_water_monitor_ollama.py

Identical agent loop to agentic_water_monitor.py, but running on a free,
local, open-source model via Ollama instead of the paid Claude API.

Same tools (fetch_scene, compute_water_extent, finalize_report), same
"observe -> decide -> act -> observe again" loop. Only the API call layer
and the tool-schema format change, since Ollama uses an OpenAI-style
function-calling format rather than Anthropic's.

Prerequisites
-------------
1. Install Ollama: https://ollama.com (Windows/Mac/Linux installer)
2. Pull a tool-calling-capable model (one-time, ~4-5 GB download):
       ollama pull qwen2.5:7b
3. Ollama runs a local server automatically after install
   (http://localhost:11434). No API key, no billing, no internet
   round-trip to Anthropic for the reasoning step — only the NASA data
   fetch still goes out to the internet.

Usage
-----
    python agentic_water_monitor_ollama.py \
        --location "Lake Mead, NV/AZ" \
        --bbox -114.75 36.00 -114.30 36.25 \
        --goal "Compare water extent between 2015 and 2023, decide if the change is significant, and explain a likely driver."

Honest expectation-setting: qwen2.5:7b is genuinely good at tool calling,
but it is noticeably less reliable than Claude at the *judgment* parts of
this loop — deciding when a result is "significant enough," when to dig
deeper vs. conclude, etc. Expect to tighten the prompts/goal wording more
than you would with Claude, and don't be surprised if it concludes too
early or too late a few times before you tune it.
"""

import argparse
import json
import logging
import sys
import tempfile

import requests

import extract_eo_data as eo
import compute_water_change as wc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"
MAX_TURNS = 8

# ---------------------------------------------------------------------------
# Tool definitions in Ollama/OpenAI-style function-calling format.
# Same three tools as the Claude version, just a different schema shape.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_scene",
            "description": (
                "Search NASA's HLS archive for satellite scenes over a bounding box "
                "and date range, download up to 3 granules, and return a local folder "
                "path. Use this whenever you need imagery for a new date range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "short_name": {"type": "string", "description": "HLSL30 (Landsat) or HLSS30 (Sentinel-2)."},
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
    },
    {
        "type": "function",
        "function": {
            "name": "compute_water_extent",
            "description": (
                "Compute water surface area (km^2) for a previously fetched scene "
                "folder using NDWI. Call after fetch_scene, passing the folder it returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string"},
                    "green_band": {"type": "string"},
                    "nir_band": {"type": "string"},
                },
                "required": ["folder"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_report",
            "description": (
                "Call this ONLY when confident enough to conclude. Ends the "
                "investigation — no more tools will be called after this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "2-4 plain-English sentences: finding, magnitude, likely driver.",
                    },
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["summary", "confidence"],
            },
        },
    },
]


def tool_fetch_scene(args: dict) -> dict:
    bbox = (args["min_lon"], args["min_lat"], args["max_lon"], args["max_lat"])
    short_name = args.get("short_name", "HLSL30")
    try:
        granules = eo.search_granules(
            short_name=short_name,
            bbox=bbox,
            start_date=args["start_date"],
            end_date=args["end_date"],
            max_results=3,
        )
        if not granules:
            return {"error": "No granules found for that area/date range. Try a wider date range."}
        folder = tempfile.mkdtemp(prefix="vaanai_scene_")
        eo.download_granules(granules, folder)
        return {"folder": folder, "granule_count": len(granules)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def tool_compute_water_extent(args: dict) -> dict:
    folder = args["folder"]
    green_band = args.get("green_band", "B03")
    nir_band = args.get("nir_band", "B05")
    try:
        ndwi = wc.compute_ndwi(
            wc.find_band_file(folder, green_band),
            wc.find_band_file(folder, nir_band),
        )
        return {"water_area_km2": round(wc.water_area_km2(ndwi), 2)}
    except SystemExit:
        return {"error": f"Could not find band {green_band}/{nir_band} in {folder}."}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


TOOL_FUNCTIONS = {
    "fetch_scene": tool_fetch_scene,
    "compute_water_extent": tool_compute_water_extent,
}


def call_ollama(messages: list) -> dict:
    """Calls the local Ollama server. Raises a clear error if Ollama isn't running."""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False},
            timeout=120,  # local inference on CPU can be slow — give it room
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        log.error(
            "Could not reach Ollama at %s. Is Ollama running? "
            "It should start automatically after installation, or run `ollama serve`.",
            OLLAMA_URL,
        )
        sys.exit(1)
    return response.json()


def run_agent(goal: str) -> None:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful research analyst investigating satellite data. "
                "Use the available tools to gather evidence before concluding. "
                "Only call finalize_report once you have enough evidence."
            ),
        },
        {"role": "user", "content": goal},
    ]

    for turn in range(1, MAX_TURNS + 1):
        log.info("--- Turn %d: asking the local model what to do next ---", turn)
        response = call_ollama(messages)
        message = response["message"]
        messages.append(message)

        if message.get("content", "").strip():
            print(f"\n[Model]: {message['content'].strip()}")

        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            log.info("No tool call made; ending.")
            return

        stop = False
        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"]["arguments"]
            # Ollama sometimes returns arguments as a dict already, sometimes as a JSON string.
            tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

            log.info("Model called tool: %s(%s)", name, tool_args)

            if name == "finalize_report":
                print("\n=== FINAL REPORT ===")
                print(f"Confidence: {tool_args.get('confidence', 'unknown')}")
                print(tool_args.get("summary", ""))
                stop = True
                break

            func = TOOL_FUNCTIONS.get(name)
            result = func(tool_args) if func else {"error": f"Unknown tool '{name}'"}
            log.info("Tool result: %s", result)

            # Feed the result back as a "tool" role message so the model sees it next turn.
            messages.append({"role": "tool", "content": json.dumps(result)})

        if stop:
            return

    log.warning("Hit MAX_TURNS (%d) without a final report. Ending.", MAX_TURNS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--location", required=True)
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), required=True)
    parser.add_argument(
        "--goal",
        default="Investigate how water extent has changed over time in this area and explain why.",
    )
    parser.add_argument("--model", default=MODEL, help=f"Ollama model to use (default: {MODEL})")
    return parser.parse_args()


def main() -> None:
    global MODEL
    args = parse_args()
    MODEL = args.model

    eo.authenticate()  # NASA credentials still needed for the data-fetching tool

    min_lon, min_lat, max_lon, max_lat = args.bbox
    goal_prompt = (
        f"Location: {args.location}\n"
        f"Bounding box: min_lon={min_lon}, min_lat={min_lat}, max_lon={max_lon}, max_lat={max_lat}\n\n"
        f"Task: {args.goal}\n\n"
        "You have tools to fetch satellite scenes for date ranges you choose and to compute "
        "water extent from them. Decide which date ranges to compare, fetch what you need, "
        "compute the numbers, and only call finalize_report once confident in a conclusion."
    )

    run_agent(goal_prompt)


if __name__ == "__main__":
    main()
