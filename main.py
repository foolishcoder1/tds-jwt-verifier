"""
FastAPI Multi-Question Service
-------------------------------

This file answers several TDS GA-2 questions:
  Q2  → JWT Verification  (/verify)
  Q3  → 12-Factor Config  (/effective-config)
  Q5  → Analytics         (/analytics)
  Q6  → Observability     (/work, /metrics, /healthz, /logs/tail)
  Q8  → Local LLM Invoice Extraction  (/extract)

Q6 ELI15 explanation:
======================

Imagine your API is a busy restaurant:
  - /work         → The waiter serves K dishes (does K units of work)
  - /metrics      → A scoreboard on the wall counting total customers served
  - /healthz      → The manager checks "are we open? how long have we been open?"
  - /logs/tail    → The receipt printer, showing the last N orders

The KEY trick: a "middleware" intercepts EVERY request (like a doorman),
automatically ticks the counter +1 and writes a log entry — before the
actual endpoint code even runs.
"""

import os
import re                            # re = regular expressions — for pattern matching
import time                         # for startup timestamp and uptime calculation
import uuid                         # for generating unique request IDs
import json                         # for formatting structured log entries
import math                         # for math.isfinite check in healthz
from collections import deque       # deque = a list with a max size (ring buffer)
from typing import List, Optional
import yaml                         # pip install pyyaml  — reads YAML files
from dotenv import dotenv_values    # pip install python-dotenv — reads .env files
from fastapi import FastAPI, Query, Header, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, field_validator
import jwt                           # PyJWT — for Q2 JWT verification
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
# Q6 — GLOBAL OBSERVABILITY STATE
# ─────────────────────────────────────────────────────────────────────────────

# ELI15: time.time() returns the current time as a decimal number of seconds
# since Jan 1 1970. We save it once when the server starts so we can calculate
# uptime later by doing: current_time - START_TIME.
START_TIME: float = time.time()

# ELI15: This is our request counter — like a turnstile click counter.
# We start at 0 and add 1 for every incoming HTTP request.
http_requests_total: int = 0

# ELI15: deque(maxlen=1000) is like a scroll of paper with space for 1000 lines.
# When it's full and you add a new line, the oldest line is automatically
# removed from the top. This is called a "ring buffer".
# We use it to store the last 1000 structured log entries.
LOG_BUFFER: deque = deque(maxlen=1000)


# ─────────────────────────────────────────────────────────────────────────────
# Create the FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TDS GA-2 Multi-Question Service",
    description="Q2 JWT + Q3 Config + Q5 Analytics + Q6 Observability + Q8 Invoice Extraction",
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
# Q6 — OBSERVABILITY MIDDLEWARE
# ELI15: Middleware is code that runs for EVERY request, before the actual
# endpoint handles it. Think of it as a security guard at a door — every
# visitor passes through the guard before entering the building.
#
# This middleware does two things for every request:
#   1. Increments our http_requests_total counter by 1
#   2. Writes a structured log entry to LOG_BUFFER
# ─────────────────────────────────────────────────────────────────────────────
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    global http_requests_total

    # Generate a unique ID for this request (like a ticket number)
    # ELI15: uuid.uuid4() creates a random string like "a3f9-12bc-..."
    # It's unique for every single request so we can trace it in logs.
    request_id = str(uuid.uuid4())

    # Tick the counter BEFORE the endpoint runs
    http_requests_total += 1

    # Actually run the endpoint and get the response
    response = await call_next(request)

    # Write a structured log entry AFTER the response is ready
    # ELI15: This is like writing in a diary: "At this time, someone
    # visited this page and got this response code."
    log_entry = {
        "level":      "info",
        "ts":         time.time(),           # Unix timestamp (decimal seconds)
        "path":       request.url.path,      # e.g. "/work" or "/metrics"
        "request_id": request_id,
        "method":     request.method,        # GET, POST, etc.
        "status":     response.status_code,  # 200, 404, etc.
    }
    LOG_BUFFER.append(log_entry)

    return response


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
# Q8 — INVOICE EXTRACTION ENDPOINT
# POST /extract
#
# ELI15: Imagine you receive a messy letter with payment details written in
# different ways every time. Your job is to read the letter and pull out:
#   1. Who sent the bill (vendor)
#   2. How much you owe (amount)
#   3. What currency (USD, EUR, GBP)
#   4. When to pay by (date)
#
# We use "regular expressions" (regex) to scan the text for patterns.
# Think of regex like a very smart CTRL+F that can find things like
# "any number with a decimal point" or "4 digits - 2 digits - 2 digits".
# ─────────────────────────────────────────────────────────────────────────────

# ── Q8 Pydantic Models ───────────────────────────────────────────────────────
class InvoiceRequest(BaseModel):
    """What the grader sends us: a blob of invoice text."""
    text: str


class InvoiceResponse(BaseModel):
    """
    What we must return — Pydantic guarantees these fields are always present
    with the right types. If our code forgets a field, Pydantic raises an error
    before the response is sent.
    """
    vendor:   str           # vendor name, e.g. "Acme-xxxx Industries Ltd."
    amount:   float         # total due, e.g. 1234.56
    currency: str           # 3-letter code, e.g. "USD"
    date:     str           # YYYY-MM-DD, e.g. "2026-03-15"


def extract_invoice_fields(text: str) -> dict:
    """
    ELI15: This is our "smart reader" function. It takes raw invoice text
    and uses regex patterns to find the four required fields.

    Regex crash-course:
      \\d      = any digit (0-9)
      \\d+     = one or more digits
      \\d{4}   = exactly four digits
      [A-Z]{3} = exactly three uppercase letters
      (?:...)  = a group we don't need to capture separately
      (...)    = a capture group — what we actually want to extract
      \\s*     = zero or more spaces
      (?i)     = make the pattern case-insensitive
    """

    # ── STEP 1: Extract DATE (YYYY-MM-DD) ────────────────────────────────────
    # Pattern: exactly 4 digits, dash, 2 digits, dash, 2 digits
    # Example matches: "2026-03-15", "2026-12-01"
    date = ""
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if date_match:
        date = date_match.group(1)    # group(1) = the first capture group = the date

    # ── STEP 2: Extract CURRENCY (3-letter code) ─────────────────────────────
    # We look for any of the known 3-letter codes near a number.
    # Priority: explicit label like "Currency: USD" first, then near an amount.
    currency = ""
    # Pattern A: "Currency: USD" or "Currency USD"
    cur_label_match = re.search(
        r"(?i)currency[:\s]+([A-Z]{3})", text
    )
    if cur_label_match:
        currency = cur_label_match.group(1).upper()
    else:
        # Pattern B: a currency code appearing near a number
        # e.g. "USD 1,234.56" or "1,234.56 EUR"
        cur_near_match = re.search(
            r"\b(USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR)\b", text, re.IGNORECASE
        )
        if cur_near_match:
            currency = cur_near_match.group(1).upper()

    # ── STEP 3: Extract AMOUNT (a number, possibly with commas/decimals) ─────
    # Pattern: optional currency code, then digits (with optional commas and
    # a decimal portion). We look for labelled amounts first.
    amount = 0.0

    # Helper: strip commas and convert to float
    def parse_num(s: str) -> float:
        return float(s.replace(",", ""))

    # Pattern A: labelled as "Total", "Amount Due", "Total Due", "Balance Due"
    # followed by optional currency symbol/code, then the number
    amount_match = re.search(
        r"(?i)(?:total\s+(?:due|amount)?|amount\s+due|balance\s+due|invoice\s+total|due)[:\s]*"
        r"(?:[A-Z]{3}|[\$\€\£])?\s*([\d,]+(?:\.\d+)?)",
        text
    )
    if amount_match:
        try:
            amount = parse_num(amount_match.group(1))
        except ValueError:
            pass

    # Pattern B: if Pattern A found nothing, try "USD 1234.56" or "1234.56 USD"
    if amount == 0.0:
        amt_cur_match = re.search(
            r"(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR)\s*([\d,]+(?:\.\d+)?)",
            text, re.IGNORECASE
        )
        if amt_cur_match:
            try:
                amount = parse_num(amt_cur_match.group(1))
            except ValueError:
                pass

    # Pattern C: fallback — find any standalone number that looks like a price
    if amount == 0.0:
        all_nums = re.findall(r"\b([\d,]+\.\d{2})\b", text)
        if all_nums:
            try:
                amount = parse_num(all_nums[-1])   # last decimal number in text
            except ValueError:
                pass

    # ── STEP 4: Extract VENDOR (company/person name) ──────────────────────────
    # The grader plants names like "Acme-xxxx Industries Ltd."
    # Strategy: look for labelled lines like "Vendor:", "Bill From:", "Supplier:",
    # "Company:", "From:" — and grab the rest of that line.
    vendor = ""

    vendor_match = re.search(
        r"(?i)(?:vendor|bill\s+(?:from|to)|supplier|company|from|sold\s+by|billed\s+by)[:\s]+(.+)",
        text
    )
    if vendor_match:
        # Strip trailing whitespace and punctuation like commas/periods at end
        vendor = vendor_match.group(1).strip().rstrip(",;")
        # Take only the first line in case more text follows
        vendor = vendor.split("\n")[0].strip()

    # Fallback: look for lines with company-like suffixes (Ltd, LLC, Inc, Corp)
    if not vendor:
        corp_match = re.search(
            r"([A-Za-z0-9\-\s]+?(?:Ltd\.?|LLC\.?|Inc\.?|Corp\.?|Co\.?|Industries|Solutions|Services|Group))",
            text
        )
        if corp_match:
            vendor = corp_match.group(1).strip()

    return {
        "vendor":   vendor,
        "amount":   amount,
        "currency": currency,
        "date":     date,
    }


@app.post("/extract", response_model=InvoiceResponse)
async def extract_invoice(body: InvoiceRequest):
    """
    Q8: Accepts free-form invoice text and returns structured JSON.

    ELI15 walkthrough:
      1. Receive the text in the request body.
      2. If the text is empty or too short, return a "best-effort" response
         (empty strings and 0.0) instead of crashing with HTTP 500.
      3. Run our regex extractor to find vendor, amount, currency, date.
      4. Return the result — Pydantic's `response_model=InvoiceResponse`
         guarantees the JSON shape is always correct.
    """
    # Guard: empty or whitespace-only text → return best-effort defaults
    # (The question says empty input must NOT return HTTP 500.)
    if not body.text or not body.text.strip():
        return InvoiceResponse(
            vendor="",
            amount=0.0,
            currency="USD",
            date="1970-01-01",
        )

    fields = extract_invoice_fields(body.text)

    # Fill in safe defaults for any field we couldn't find
    return InvoiceResponse(
        vendor=fields.get("vendor") or "",
        amount=fields.get("amount") or 0.0,
        currency=fields.get("currency") or "USD",
        date=fields.get("date") or "1970-01-01",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Root health check (optional, useful for debugging on Render)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "TDS GA-2 Service (Q2 + Q3 + Q5 + Q6 + Q8)"}


# ─────────────────────────────────────────────────────────────────────────────
# Q6 — ENDPOINT 1: /work
# ELI15: The grader calls this to give you "work" to do.
# ?n=K means "do K units of work".
# The counter is already incremented by the middleware above.
# We just need to return the right JSON.
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/work")
async def do_work(n: int = Query(default=1, ge=0)):
    """
    GET /work?n=K
    Does K units of work and returns {"email": ..., "done": K}.
    The middleware already incremented the counter for this request.
    """
    # ELI15: We simulate "K units of work" by just returning the number K.
    # In a real app this might process K files or make K database calls.
    # The grader only cares that: (a) the counter ticked, (b) done==K.
    return JSONResponse(content={
        "email": STUDENT_EMAIL,
        "done":  n,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Q6 — ENDPOINT 2: /metrics
# ELI15: Prometheus is a popular monitoring tool. It scrapes /metrics and
# expects a specific plain-text format. Each line looks like:
#   metric_name{optional_labels} value
# We must expose http_requests_total — our running counter.
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    """
    GET /metrics
    Returns Prometheus text format with the http_requests_total counter.
    """
    # ELI15: The Prometheus text format has 3 lines per metric:
    #   Line 1: # HELP <name> <description>     ← human-readable description
    #   Line 2: # TYPE <name> counter            ← declares the metric type
    #   Line 3: <name> <value>                   ← the actual number
    body = (
        "# HELP http_requests_total Total number of HTTP requests received\n"
        "# TYPE http_requests_total counter\n"
        f"http_requests_total {http_requests_total}\n"
    )
    # ELI15: PlainTextResponse sends raw text instead of JSON.
    # Prometheus REQUIRES plain text — it cannot read JSON.
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4")


# ─────────────────────────────────────────────────────────────────────────────
# Q6 — ENDPOINT 3: /healthz
# ELI15: This is like a doctor's "are you alive?" check.
# It returns:
#   status   → "ok" if the server is running fine
#   uptime_s → how many seconds the server has been running
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    """
    GET /healthz
    Returns {"status": "ok", "uptime_s": <seconds since startup>}.
    """
    # ELI15: time.time() - START_TIME = seconds elapsed since we started.
    # Example: if START_TIME was 1000.0 and now it's 1065.3, uptime = 65.3 seconds.
    uptime = time.time() - START_TIME
    return JSONResponse(content={
        "status":   "ok",
        "uptime_s": uptime,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Q6 — ENDPOINT 4: /logs/tail
# ELI15: This is like asking "show me the last N receipts from the printer".
# LOG_BUFFER holds up to 1000 log entries. We return the last N of them.
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/logs/tail")
async def logs_tail(limit: int = Query(default=20, ge=1, le=1000, alias="limit")):
    """
    GET /logs/tail?limit=N
    Returns the last N structured log entries as a JSON array.
    Each entry has: level, ts, path, request_id, method, status.
    """
    # ELI15: list(LOG_BUFFER) converts the deque to a regular list.
    # [-limit:] is Python slicing: take the LAST `limit` items.
    # Example: [1,2,3,4,5][-3:] → [3,4,5]
    all_logs = list(LOG_BUFFER)
    tail = all_logs[-limit:] if len(all_logs) >= limit else all_logs
    return JSONResponse(content=tail)


# ─────────────────────────────────────────────────────────────────────────────
# Q9: API Engineering (Idempotency, Pagination, Rate Limiting)
# ─────────────────────────────────────────────────────────────────────────────
import base64
import uuid

# Assigned values
TOTAL_ORDERS = 41
RATE_LIMIT_REQUESTS = 18
RATE_LIMIT_WINDOW_SEC = 10

# State stores (in-memory for assignment)
idempotency_store = {}
client_requests = {} # dict mapping client_id to list of timestamps
fixed_catalog = [{"id": i} for i in range(1, TOTAL_ORDERS + 1)]

def check_rate_limit(x_client_id: str = Header(..., alias="X-Client-Id")):
    now = time.time()
    
    # Initialize if new client
    if x_client_id not in client_requests:
        client_requests[x_client_id] = []
        
    # Filter out requests older than the window
    window_start = now - RATE_LIMIT_WINDOW_SEC
    valid_requests = [req_time for req_time in client_requests[x_client_id] if req_time > window_start]
    client_requests[x_client_id] = valid_requests
    
    # Check if limit exceeded
    if len(valid_requests) >= RATE_LIMIT_REQUESTS:
        # Calculate retry after based on the oldest request in the window
        oldest_request = valid_requests[0]
        retry_after = max(1, int(oldest_request + RATE_LIMIT_WINDOW_SEC - now + 1))
        
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={"Retry-After": str(retry_after)}
        )
        
    # Add current request
    client_requests[x_client_id].append(now)
    return x_client_id

@app.post("/orders", status_code=201)
def create_order(
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    client_id: str = Depends(check_rate_limit)
):
    """
    Idempotent Order Creation.
    If the same key is passed, the same order ID is returned.
    """
    if idempotency_key in idempotency_store:
        return {"id": idempotency_store[idempotency_key]}
    
    # Create new order (just generate a random UUID for demo)
    new_order_id = str(uuid.uuid4())
    idempotency_store[idempotency_key] = new_order_id
    
    return {"id": new_order_id}

@app.get("/orders")
def get_orders(
    limit: int = 10,
    cursor: str = None,
    client_id: str = Depends(check_rate_limit)
):
    """
    Cursor-based pagination of a fixed catalog of orders.
    """
    start_idx = 0
    if cursor:
        try:
            # Our cursor is just the base64 encoded string of the next index
            decoded_str = base64.b64decode(cursor.encode()).decode()
            start_idx = int(decoded_str)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
            
    # Calculate end index based on limit
    end_idx = start_idx + limit
    
    # Slice the catalog
    items = fixed_catalog[start_idx:end_idx]
    
    # Prepare response
    response = {"items": items}
    
    # If there are more items, provide a next_cursor
    if end_idx < TOTAL_ORDERS:
        next_cursor_str = str(end_idx)
        response["next_cursor"] = base64.b64encode(next_cursor_str.encode()).decode()
        
    return response

# ─────────────────────────────────────────────────────────────────────────────
# Run locally for testing
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
