"""
agentic_water_monitor_groq.py

Same agent loop as the Claude and Ollama versions, but running on Groq's
free, hosted API instead of a paid API or a locally-installed model.

Why this exists (vs. Ollama): Groq requires NO local install at all — no
GPU, no driver, no download. It's a plain HTTPS call to Groq's servers,
which host open-weight models (Llama 3.3, GPT-OSS, etc.) on fast hardware.
This makes it the version that actually works from a GitHub Actions
runner or any other machine with zero setup, unlike Ollama which needs
its own installed service running locally.

Prerequisites
-------------
1. Create a free account at https://console.groq.com (no card required)
2. Generate an API key from the console
3. Add it to your .env: GROQ_API_KEY=your_key_here

Usage
-----
    python agentic_water_monitor_groq.py \
        --location "Lake Mead, NV/AZ" \
        --bbox -114.75 36.00 -114.30 36.25 \
        --goal "Compare water extent between 2015 and 2023, decide if the change is significant, and explain a likely driver."
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time

import requests
from dotenv import load_dotenv

import extract_eo_data as eo
import compute_water_change as wc
import storage_utils as storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"  # confirmed available on this account; use --model to try openai/gpt-oss-120b for higher quality
MAX_TURNS = 16  # cloud/snow avoidance can legitimately need several retries per time period

# ---------------------------------------------------------------------------
# Tool definitions - OpenAI-compatible format (same shape Groq expects).
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
                "folder using NDWI, automatically excluding cloud/snow-covered pixels. "
                "Call after fetch_scene, passing the folder it returned. The result "
                "includes cloud_snow_masked_pct — if this is high (e.g. above ~30%), "
                "the result may be unreliable and you should consider fetching a "
                "different, clearer date instead of relying on it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string"},
                    "green_band": {
                        "type": "string",
                        "description": "Zero-padded HLS band code for Green, e.g. 'B03'. Default: B03.",
                    },
                    "nir_band": {
                        "type": "string",
                        "description": "Zero-padded HLS band code for Near-Infrared, e.g. 'B05'. Default: B05.",
                    },
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
                "investigation — no more tools will be called after this. "
                "You must reference the exact folder paths and dates you used "
                "from your earlier fetch_scene/compute_water_extent calls, so "
                "the result and both images can be permanently saved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "2-4 plain-English sentences: finding, magnitude, likely driver.",
                    },
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "before_folder": {"type": "string", "description": "Folder path from the earlier (older-date) fetch_scene call."},
                    "after_folder": {"type": "string", "description": "Folder path from the later (newer-date) fetch_scene call."},
                    "before_date": {"type": "string", "description": "YYYY-MM-DD used for the earlier fetch."},
                    "after_date": {"type": "string", "description": "YYYY-MM-DD used for the later fetch."},
                    "before_value": {"type": "number", "description": "water_area_km2 result for the earlier scene."},
                    "after_value": {"type": "number", "description": "water_area_km2 result for the later scene."},
                },
                "required": [
                    "summary", "confidence", "before_folder", "after_folder",
                    "before_date", "after_date", "before_value", "after_value",
                ],
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
            max_results=1,  # a single granule avoids multi-tile mixing when the AOI
                             # straddles a UTM zone boundary (see compute_water_extent errors)
        )
        if not granules:
            return {"error": "No granules found for that area/date range. Try a wider date range."}
        folder = tempfile.mkdtemp(prefix="vaanai_scene_")
        _created_folders.append(folder)
        eo.download_granules(granules, folder)
        return {"folder": folder, "granule_count": len(granules)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc) or repr(exc) or "Unknown error during fetch (see logs above)."}


def tool_compute_water_extent(args: dict) -> dict:
    folder = args["folder"]
    green_band = args.get("green_band", "B03")
    nir_band = args.get("nir_band", "B05")
    try:
        bad_mask, bad_pct = wc.read_bad_pixel_mask(folder)
        ndwi = wc.compute_ndwi(
            wc.find_band_file(folder, green_band),
            wc.find_band_file(folder, nir_band),
            bad_pixel_mask=bad_mask,
        )
        result = {
            "water_area_km2": round(wc.water_area_km2(ndwi), 2),
            "cloud_snow_masked_pct": round(bad_pct * 100, 1),
        }
        if bad_pct > 0.3:
            result["warning"] = (
                f"{bad_pct*100:.0f}% of this scene is cloud/snow-covered and was excluded from the "
                "calculation. The remaining result may be unreliable — consider fetching a different, "
                "clearer date instead of trusting this value if the percentage is high."
            )
        return result
    except ValueError as exc:
        return {"error": str(exc) or "Band lookup failed (see logs above)."}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc) or repr(exc) or "Unknown error computing water extent (see logs above)."}


TOOL_FUNCTIONS = {
    "fetch_scene": tool_fetch_scene,
    "compute_water_extent": tool_compute_water_extent,
}

_created_folders: list[str] = []  # tracks every temp folder this run creates, for cleanup at the end


def cleanup_temp_folders() -> None:
    """Deletes every temp folder this run created. Harmless on GitHub Actions
    (the whole machine is destroyed anyway) but keeps a local machine's disk
    clean across many weekly runs."""
    import shutil

    for folder in _created_folders:
        shutil.rmtree(folder, ignore_errors=True)
    log.info("Cleaned up %d temp folder(s).", len(_created_folders))


VALID_TOOL_NAMES = {"fetch_scene", "compute_water_extent", "finalize_report"}


def _try_recover_corrupted_tool_call(response) -> dict | None:
    """GPT-OSS on Groq occasionally leaks an internal formatting token (e.g.
    'finalize_report<|channel|>commentary') into the tool call's *name* field
    — a known harmony-format parsing issue. The arguments it generated are
    usually still fully correct and complete; only the name is corrupted.
    Rather than discard a fully-computed result (which, for this pipeline,
    means repeating expensive multi-minute NASA downloads), try to recover
    the intended call directly from the error body. Returns None if recovery
    isn't possible, so the caller can fall back to a normal retry."""
    try:
        error_body = response.json().get("error", {})
        failed_generation = error_body.get("failed_generation")
        if not failed_generation:
            return None

        parsed = json.loads(failed_generation)
        raw_name = parsed.get("name", "")
        clean_name = raw_name.split("<|channel|>")[0].split("<|")[0].strip()

        if clean_name not in VALID_TOOL_NAMES:
            return None

        log.warning("Recovered a corrupted tool call name: '%s' -> '%s'", raw_name, clean_name)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "recovered_call_1",
                                "type": "function",
                                "function": {
                                    "name": clean_name,
                                    "arguments": json.dumps(parsed.get("arguments", {})),
                                },
                            }
                        ],
                    }
                }
            ]
        }
    except Exception:  # noqa: BLE001 - recovery is best-effort; fall back on any parse issue
        return None


def call_groq(messages: list, api_key: str, max_retries: int = 3) -> dict:
    """Calls Groq, automatically waiting and retrying on free-tier rate limits (429),
    and on the GPT-OSS-specific 'output_parse_failed' error (a known Groq issue where
    the model's internal reasoning leaks into the response and breaks tool-call
    parsing) — both are common on the free tier and worth handling gracefully."""
    for attempt in range(max_retries + 1):
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                # GPT-OSS-specific settings (per Groq docs) that keep the model's
                # internal reasoning out of the response body, which otherwise
                # sometimes breaks structured tool-call parsing:
                "reasoning_format": "hidden",
                "reasoning_effort": "low",
            },
            timeout=60,
        )

        if response.status_code == 429 and attempt < max_retries:
            wait_seconds = 10 * (attempt + 1)  # simple backoff: 10s, 20s, 30s
            log.warning(
                "Rate limited (attempt %d/%d). Waiting %ds before retrying...",
                attempt + 1, max_retries, wait_seconds,
            )
            time.sleep(wait_seconds)
            continue

        is_known_groq_glitch = response.status_code == 400 and (
            "tool_use_failed" in response.text or "output_parse_failed" in response.text
        )

        if is_known_groq_glitch:
            recovered = _try_recover_corrupted_tool_call(response)
            if recovered is not None:
                return recovered

            # Recovery wasn't possible (output was too corrupted/truncated to parse
            # at all, not just mislabeled) — this is a known intermittent GPT-OSS
            # generation glitch on Groq, and a plain retry usually produces clean
            # output the second time, since it isn't a deterministic/repeatable failure.
            if attempt < max_retries:
                log.warning(
                    "Model produced corrupted/unparseable tool output (attempt %d/%d) "
                    "— retrying, this is a known intermittent GPT-OSS/Groq issue.",
                    attempt + 1, max_retries,
                )
                time.sleep(3)
                continue

        if not response.ok:
            log.error("Groq API error %s: %s", response.status_code, response.text)
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Exceeded max retries after repeated rate limiting / parse failures.")


def persist_result(final_args: dict, location: str) -> None:
    """Generates before/after preview images, uploads them to Supabase Storage,
    and inserts one row into the `insights` table — called once finalize_report
    is invoked. Keeps the model's own reported values as the source of truth
    for what to save, since it's the one that knows which folders/dates it used."""
    import tempfile as _tempfile

    before_dir = _tempfile.mkdtemp(prefix="vaanai_preview_")
    after_dir = _tempfile.mkdtemp(prefix="vaanai_preview_")
    _created_folders.extend([before_dir, after_dir])
    before_png = os.path.join(before_dir, "before.png")
    after_png = os.path.join(after_dir, "after.png")

    storage.generate_preview_png(final_args["before_folder"], before_png)
    storage.generate_preview_png(final_args["after_folder"], after_png)

    before_url = storage.upload_image(before_png)
    after_url = storage.upload_image(after_png)

    before_value = final_args["before_value"]
    after_value = final_args["after_value"]
    change_pct = ((after_value - before_value) / before_value * 100) if before_value else 0.0

    storage.save_insight(
        {
            "location": location,
            "before_date": final_args["before_date"],
            "after_date": final_args["after_date"],
            "before_value": before_value,
            "after_value": after_value,
            "change_pct": round(change_pct, 2),
            "summary": final_args["summary"],
            "confidence": final_args["confidence"],
            "before_image_url": before_url,
            "after_image_url": after_url,
        }
    )


def _handle_finalize(tool_args: dict, location: str) -> None:
    print("\n=== FINAL REPORT ===")
    print(f"Confidence: {tool_args.get('confidence', 'unknown')}")
    print(tool_args.get("summary", ""))
    try:
        persist_result(tool_args, location=location)
        print("(Saved image pair + result to Supabase.)")
    except Exception as exc:  # noqa: BLE001 - don't let a save failure hide the report itself
        log.error("Failed to persist insight to Supabase: %s", exc)
        print(f"(Warning: could not save to Supabase — {exc})")


def _force_finalize(messages: list, api_key: str, computed: dict, folder_dates: dict, location: str) -> bool:
    """Fallback for when the model has already computed enough evidence but
    stopped without calling finalize_report (e.g. it wrote a prose summary
    instead). Rather than lose a fully correct, already-computed result,
    nudge it once more with tool_choice FORCED to finalize_report specifically.
    Returns True if a report was successfully forced and saved."""
    # Only consider folders that were actually computed, not just fetched —
    # a folder can be fetched and then abandoned (e.g. the agent moved on to
    # try a different date) without ever being analyzed, and picking that as
    # "after" would silently use a result that doesn't exist.
    candidates = {f: d for f, d in folder_dates.items() if f in computed}
    if len(candidates) < 2:
        return False

    ordered = sorted(candidates.items(), key=lambda kv: kv[1]["start_date"])
    before_folder, before_meta = ordered[0]
    after_folder, after_meta = ordered[-1]

    log.warning("Model stopped without calling finalize_report despite having enough evidence — forcing it.")
    messages.append(
        {
            "role": "user",
            "content": (
                "You already computed these results:\n"
                f"- {before_meta['start_date']} to {before_meta['end_date']}: "
                f"folder={before_folder}, water_area_km2={computed[before_folder]}\n"
                f"- {after_meta['start_date']} to {after_meta['end_date']}: "
                f"folder={after_folder}, water_area_km2={computed[after_folder]}\n\n"
                "Call finalize_report now using these exact folders and values."
            ),
        }
    )

    response = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "finalize_report"}},
            "reasoning_format": "hidden",
            "reasoning_effort": "low",
        },
        timeout=60,
    )
    if not response.ok:
        log.error("Forced finalize call failed: %s", response.text)
        return False

    message = response.json()["choices"][0]["message"]
    for call in message.get("tool_calls") or []:
        if call["function"]["name"] == "finalize_report":
            tool_args = json.loads(call["function"]["arguments"])
            _handle_finalize(tool_args, location)
            return True
    return False


def run_agent(goal: str, api_key: str, location: str) -> None:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful research analyst investigating satellite data. "
                "Use the available tools to gather evidence before concluding. "
                "If a computed result has a high cloud_snow_masked_pct or comes with a "
                "warning, don't trust it — fetch a different date range instead of "
                "concluding from a heavily clouded scene. However, budget your attempts: "
                "try at most 2-3 scenes per time period before accepting the clearest one "
                "you've found so far and moving on to the other period — don't exhaust all "
                "your attempts repeatedly retrying a single period while neglecting the other. "
                "You need at least one usable result from EACH period to conclude anything."
                "Once you have computed both before and after values from clear scenes, "
                "you MUST call finalize_report as a tool call — do not just write your "
                "conclusion as text."
            ),
        },
        {"role": "user", "content": goal},
    ]

    computed: dict = {}       # folder -> water_area_km2
    folder_dates: dict = {}   # folder -> {"start_date":..., "end_date":...}

    for turn in range(1, MAX_TURNS + 1):
        log.info("--- Turn %d: asking the model what to do next ---", turn)
        response = call_groq(messages, api_key)
        message = response["choices"][0]["message"]
        messages.append(message)

        if message.get("content"):
            print(f"\n[Model]: {message['content'].strip()}")

        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            if _force_finalize(messages, api_key, computed, folder_dates, location):
                return
            log.info("No tool call made, and not enough tracked evidence to force a finalize; ending.")
            return

        stop = False
        for call in tool_calls:
            name = call["function"]["name"]
            tool_args = json.loads(call["function"]["arguments"])
            call_id = call["id"]

            log.info("Model called tool: %s(%s)", name, tool_args)

            if name == "finalize_report":
                _handle_finalize(tool_args, location)
                stop = True
                break

            func = TOOL_FUNCTIONS.get(name)
            result = func(tool_args) if func else {"error": f"Unknown tool '{name}'"}
            log.info("Tool result: %s", result)

            # Track evidence ourselves so we can force a conclusion later if the
            # model computes everything it needs but forgets the final tool call.
            if name == "fetch_scene" and "folder" in result:
                folder_dates[result["folder"]] = {
                    "start_date": tool_args.get("start_date"),
                    "end_date": tool_args.get("end_date"),
                }
            if name == "compute_water_extent" and "water_area_km2" in result:
                computed[tool_args["folder"]] = result["water_area_km2"]

            # Groq/OpenAI format requires tool_call_id to match the call being answered.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result),
                }
            )

        if stop:
            return

    if _force_finalize(messages, api_key, computed, folder_dates, location):
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
    parser.add_argument("--model", default=MODEL, help=f"Groq model to use (default: {MODEL})")
    return parser.parse_args()


def main() -> None:
    global MODEL
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log.error("GROQ_API_KEY not set in .env. Get a free key at https://console.groq.com")
        sys.exit(1)

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

    try:
        run_agent(goal_prompt, api_key, location=args.location)
    finally:
        cleanup_temp_folders()


if __name__ == "__main__":
    main()
