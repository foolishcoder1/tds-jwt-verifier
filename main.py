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
from fastapi import FastAPI, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import jwt                          # PyJWT — for Q2 JWT verification
from jwt import PyJWTError
import uvicorn

# ─────────────────────────────────────────────────────────────────────────────
# Q2 — JWT VERIFICATION CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2okOHspNjgA+2rTLbeuY
cxiP/hG8C6Sb9iwg3yiLAA4HCnpITcbWCSelbvbYGuc3EbNy4xFyf5Cbj5DHJMID
EkryOgyd2giIIIBOUBj8S63uGcnRpOBh9NFatfNwheKuzsPuVNldu6A9cNteNpXc
WyJjG2axVfmq7i6SuKr1JoWYG7xTTAvKPujSl4OtsQfO3h5NepzdfXpr28oNnzfW
ed+zclR6BcmNNo/WVfJ4xyCLSf0BCOgdTgW6PdaChd1l9VDetJZVEgC5tkyvXsfI
SI6iyrYbKR0NEBSqq4XkadEjsCs4F1RncsS4LlgniT7GlkL9Mce3b0wGLs9/7ZIX
dQIDAQAB
-----END PUBLIC KEY-----"""

EXPECTED_ISSUER   = "https://idp.exam.local"
EXPECTED_AUDIENCE = "tds-pv3c9y4x.apps.exam.local"


# Q2 — Request body model
class TokenRequest(BaseModel):
    token: str


# ─────────────────────────────────────────────────────────────────────────────
# Q5 — ANALYTICS ENDPOINT CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# ELI15: This is like a secret password the grader must say to use our endpoint.
# We check the incoming header "X-API-Key" against this value.
ANALYTICS_API_KEY = "ak_a8c840l49bohshnhu7co4z9o"

# ELI15: Your registered email for this course — returned in every response.
STUDENT_EMAIL = "23f2004185@ds.study.iitm.ac.in"


# Q5 — Pydantic models for the request body
# ELI15: Pydantic is like a "shape checker". It makes sure the incoming
# JSON has exactly the fields we expect, with the right types.
class AnalyticsEvent(BaseModel):
    user: str          # e.g. "alice"
    amount: float      # e.g. 42.5  (can be negative or zero — we handle that)
    ts: int            # Unix timestamp, e.g. 1700000000 — we don't use this but must accept it

class AnalyticsRequest(BaseModel):
    events: List[AnalyticsEvent]

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
# Q2 — JWT VERIFICATION ENDPOINT
# POST /verify  — validates RS256 JWTs and returns claims or 401
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/verify")
async def verify_token(body: TokenRequest):
    """
    Q2: Accepts a JWT and verifies it using the RS256 public key.
    Returns 200 + claims if valid, 401 if anything fails.
    """
    try:
        payload = jwt.decode(
            body.token,
            PUBLIC_KEY,
            algorithms=["RS256"],
            issuer=EXPECTED_ISSUER,
            audience=EXPECTED_AUDIENCE,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        return JSONResponse(
            status_code=200,
            content={
                "valid": True,
                "email": payload.get("email", ""),
                "sub":   payload.get("sub", ""),
                "aud":   payload.get("aud", ""),
            },
        )
    except PyJWTError:
        return JSONResponse(status_code=401, content={"valid": False})


# ─────────────────────────────────────────────────────────────────────────────
# Q5 — ANALYTICS ENDPOINT
# POST /analytics
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/analytics")
async def analytics(
    body: AnalyticsRequest,
    x_api_key: Optional[str] = Header(default=None),
    # ELI15: FastAPI reads the "X-Api-Key" header automatically.
    # The param name x_api_key maps to the HTTP header "X-API-Key"
    # (FastAPI converts underscores ↔ hyphens and is case-insensitive).
):
    """
    Q5: Authenticates via X-API-Key header, then aggregates event data.

    ELI15 walkthrough:
      1. Check if the header is present and matches our secret key.
         If not → return 401 (Unauthorized).
      2. Count all events → total_events
      3. Collect unique user names → unique_users
      4. Sum up amounts > 0 only → revenue
      5. Find which user has the highest positive-amount total → top_user
      6. Return everything as JSON.
    """

    # ── STEP 1: API Key Authentication ───────────────────────────────────────
    # ELI15: Like checking a wristband at an event.
    # If no wristband (None) OR wrong wristband → send them away (401).
    if x_api_key != ANALYTICS_API_KEY:
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized: missing or invalid API key"}
        )

    # ── STEP 2: total_events ─────────────────────────────────────────────────
    # ELI15: Just count how many items are in the events list.
    total_events = len(body.events)

    # ── STEP 3: unique_users ─────────────────────────────────────────────────
    # ELI15: Put all user names into a "set" (a set automatically
    # removes duplicates, like a bag that rejects duplicates).
    # Then count how many are in the set.
    all_users = set(event.user for event in body.events)
    unique_users = len(all_users)

    # ── STEP 4: revenue ──────────────────────────────────────────────────────
    # ELI15: Add up all amounts BUT only if amount > 0.
    # Zero and negative amounts are ignored (like refunds or errors).
    revenue = sum(event.amount for event in body.events if event.amount > 0)

    # ── STEP 5: top_user ─────────────────────────────────────────────────────
    # ELI15: For each user, add up all their POSITIVE amounts.
    # Then find which user's total is the biggest.
    #
    # Example:
    #   alice:  42.5 + 10.0 = 52.5
    #   bob:   -5.0 (negative, ignored) + 100.0 = 100.0
    #   charlie: 30.0
    # → top_user = "bob" (100.0 is highest)
    user_revenue: dict = {}
    for event in body.events:
        if event.amount > 0:
            # If user not seen before, start at 0; then add this amount
            user_revenue[event.user] = user_revenue.get(event.user, 0.0) + event.amount

    # max() with key= picks the user whose value (total revenue) is largest
    # ELI15: Like picking the winner of a race — max() finds the fastest.
    if user_revenue:
        top_user = max(user_revenue, key=lambda u: user_revenue[u])
    else:
        top_user = ""  # edge case: no events had positive amounts

    # ── STEP 6: Return JSON response ─────────────────────────────────────────
    return JSONResponse(
        status_code=200,
        content={
            "email":        STUDENT_EMAIL,
            "total_events": total_events,
            "unique_users": unique_users,
            "revenue":      round(revenue, 2),   # round to 2 decimal places
            "top_user":     top_user,
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Health check (optional, useful for debugging on Render)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "TDS GA-2 Service (Q2 + Q3 + Q5)"}


# ─────────────────────────────────────────────────────────────────────────────
# Run locally for testing
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
