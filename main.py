"""
FastAPI 12-Factor Config Precedence Service
--------------------------------------------

ELI15 explanation of what this code does:
==========================================

Think of config like a game of "last one wins":
  Layer 1 (defaults)         → lowest priority, always there as fallback
  Layer 2 (config.dev.yaml)  → overwrites defaults for dev environment
  Layer 3 (.env file)        → overwrites yaml values
  Layer 4 (OS env APP_*)     → overwrites .env values
  Layer 5 (?set= in the URL) → highest priority, overwrites EVERYTHING

Steps this code takes when a request comes in:
  1. Start with hardcoded defaults dict
  2. Load config.development.yaml → update the dict
  3. Load .env file → update the dict (with alias: NUM_WORKERS → workers)
  4. Scan os.environ for APP_* vars → update the dict
  5. Parse ?set=key=value from URL → update the dict
  6. Coerce types (port→int, workers→int, debug→bool, rest→str)
  7. Mask api_key as "****"
  8. Return the final JSON
"""

import os
import yaml                        # pip install pyyaml  — reads YAML files
from dotenv import dotenv_values   # pip install python-dotenv — reads .env files
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import List, Optional
import uvicorn

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — Hardcoded defaults (lowest priority)
# These are the "factory settings". Every key is defined here so we always
# have something to return even if no other layer exists.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "port":      8000,
    "workers":   1,
    "debug":     False,
    "log_level": "info",
    "api_key":   "default-secret-000",
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPER: coerce a raw string value into the right Python type
# ─────────────────────────────────────────────────────────────────────────────
def coerce(key: str, raw_value) -> object:
    """
    ELI15: "coerce" = force the value to be the right type.
    
    For example, the OS env gives us the string "8593" for port.
    We must convert it to the INTEGER 8593 before returning JSON.
    
    Rules:
      - port, workers  → int   (e.g. "8593" → 8593)
      - debug          → bool  ("true"/"1"/"yes"/"on" → True, everything else → False)
      - log_level, etc → str   (no conversion needed)
    """
    if key in ("port", "workers"):
        return int(raw_value)
    elif key == "debug":
        if isinstance(raw_value, bool):
            return raw_value                              # already a bool (from YAML)
        return str(raw_value).strip().lower() in ("true", "1", "yes", "on")
    else:
        return str(raw_value)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: apply a dict of raw overrides onto the config dict
# ─────────────────────────────────────────────────────────────────────────────
def apply_overrides(config: dict, overrides: dict) -> dict:
    """
    ELI15: for each key=value in 'overrides', update 'config' if the key is
    one we know about (from DEFAULTS). Unknown keys are silently ignored
    so random ?set= params don't pollute our response.
    """
    for k, v in overrides.items():
        # Normalize key to lowercase for case-insensitive matching
        k_lower = k.lower()
        if k_lower in DEFAULTS:
            config[k_lower] = coerce(k_lower, v)
    return config


# ─────────────────────────────────────────────────────────────────────────────
# Create the FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="12-Factor Config Service",
    description="Merges config from defaults → YAML → .env → OS env → CLI overrides",
    version="1.0.0",
)

# ─────────────────────────────────────────────────────────────────────────────
# CORS Middleware — allows the browser / grader website to call our API
# Without this, browsers block cross-origin requests (CORS policy).
# "Allow all origins" is fine for a graded assignment.
# ─────────────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # ← allow any website to call us
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/effective-config")
async def effective_config(
    set: Optional[List[str]] = Query(default=None)
    # ELI15: `set` is the ?set=key=value query param.
    # FastAPI lets us receive it as a list because the same param
    # can appear multiple times: ?set=port=9000&set=debug=true
    # → set = ["port=9000", "debug=true"]
):
    """
    Returns the fully-merged configuration as JSON.

    Priority (highest wins):
        defaults → config.development.yaml → .env → OS env (APP_*) → ?set= params
    """

    # ── STEP 1: Start with a fresh copy of defaults ──────────────────────────
    config = dict(DEFAULTS)         # dict() makes a copy so we don't mutate DEFAULTS

    # ── STEP 2: Load config.development.yaml and merge ───────────────────────
    # os.path.dirname(__file__) = the folder where this script lives
    yaml_path = os.path.join(os.path.dirname(__file__), "config.development.yaml")
    if os.path.exists(yaml_path):
        with open(yaml_path, "r") as f:
            yaml_data = yaml.safe_load(f) or {}     # safe_load = no code execution risk
        # Merge: only update keys we know about
        for k, v in yaml_data.items():
            k_lower = k.lower()
            if k_lower in config:
                config[k_lower] = coerce(k_lower, v)
    # After step 2 example state:
    #   config = {port:8000, workers:1, debug:True, log_level:"info", api_key:"key-qnpex9znv9"}

    # ── STEP 3: Load .env file and merge ─────────────────────────────────────
    # dotenv_values() reads the file but does NOT inject into os.environ.
    # We read it manually so we can apply precedence correctly.
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        dot_env = dotenv_values(env_path)           # returns a dict of {KEY: value}
        for raw_key, v in dot_env.items():
            k = raw_key.lower()

            # ── ALIAS: NUM_WORKERS in .env → maps to "workers" key ───────────
            # ELI15: The question says NUM_WORKERS is a nickname for workers.
            # So if .env has NUM_WORKERS=4, we treat it as workers=4.
            if k == "num_workers":
                config["workers"] = coerce("workers", v)
                continue

            # Strip the APP_ prefix if present (e.g. APP_LOG_LEVEL → log_level)
            # In the .env layer the keys are plain (APP_LOG_LEVEL), treat similarly
            stripped = k.removeprefix("app_")       # "app_log_level" → "log_level"
            if stripped in config:
                config[stripped] = coerce(stripped, v)
    # After step 3 example state:
    #   config = {..., log_level:"info", ...}  (APP_LOG_LEVEL=info, no change)

    # ── STEP 4: Read OS environment variables with APP_* prefix ──────────────
    # ELI15: os.environ is a dictionary of everything the OS has set.
    # We only care about keys that start with "APP_".
    # "APP_PORT=8593" → strip "APP_" → "port" → update config["port"] = 8593
    for raw_key, v in os.environ.items():
        if raw_key.upper().startswith("APP_"):
            # Strip the APP_ prefix and lowercase
            stripped = raw_key[4:].lower()           # "APP_PORT" → "port"

            # Handle alias in OS env too (APP_NUM_WORKERS → workers)
            if stripped == "num_workers":
                config["workers"] = coerce("workers", v)
                continue

            if stripped in config:
                config[stripped] = coerce(stripped, v)
    # After step 4 example state:
    #   config = {port:8593, workers:6, debug:True, log_level:"info", api_key:"key-qnpex9znv9"}

    # ── STEP 5: Apply ?set=key=value CLI overrides (HIGHEST priority) ─────────
    # ELI15: Each element in `set` looks like "port=9000" or "debug=true".
    # We split on the FIRST "=" only, so values with "=" in them still work.
    if set:
        for item in set:
            if "=" in item:
                k, _, v = item.partition("=")   # partition splits on FIRST "=" only
                apply_overrides(config, {k: v})

    # ── STEP 6: Mask the api_key ─────────────────────────────────────────────
    # ELI15: We NEVER send the real api_key to the browser for security.
    # Always replace it with "****".
    config["api_key"] = "****"

    # ── STEP 7: Return JSON ───────────────────────────────────────────────────
    return JSONResponse(content=config)


# ─────────────────────────────────────────────────────────────────────────────
# Health check (optional, useful for debugging on Render)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "12-Factor Config Service"}


# ─────────────────────────────────────────────────────────────────────────────
# Run locally for testing
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
