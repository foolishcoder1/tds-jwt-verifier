"""
FastAPI JWT Verification Service
---------------------------------
POST /verify endpoint that validates RS256 JWTs issued by our mock IdP.

ELI15 breakdown:
- We receive a JWT (a fancy signed string) in the request body
- We use the IdP's PUBLIC KEY to check: is this signature genuine?
- We also check: right issuer? right audience? not expired?
- If all checks pass → 200 {"valid": true, ...claims}
- If anything fails → 401 {"valid": false}
"""

from fastapi import FastAPI
from pydantic import BaseModel
import jwt  # PyJWT library handles all the heavy lifting
from jwt import PyJWTError
import uvicorn

# ──────────────────────────────────────────────
# 1. The public key the IdP gave us.
#    This is like the "official stamp" we use to
#    verify the wristband is real.
# ──────────────────────────────────────────────
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2okOHspNjgA+2rTLbeuY
cxiP/hG8C6Sb9iwg3yiLAA4HCnpITcbWCSelbvbYGuc3EbNy4xFyf5Cbj5DHJMID
EkryOgyd2giIIIBOUBj8S63uGcnRpOBh9NFatfNwheKuzsPuVNldu6A9cNteNpXc
WyJjG2axVfmq7i6SuKr1JoWYG7xTTAvKPujSl4OtsQfO3h5NepzdfXpr28oNnzfW
ed+zclR6BcmNNo/WVfJ4xyCLSf0BCOgdTgW6PdaChd1l9VDetJZVEgC5tkyvXsfI
SI6iyrYbKR0NEBSqq4XkadEjsCs4F1RncsS4LlgniT7GlkL9Mce3b0wGLs9/7ZIX
dQIDAQAB
-----END PUBLIC KEY-----"""

# ──────────────────────────────────────────────
# 2. Things the token MUST claim to be trusted.
#    If the token says "I'm from Google" but we
#    expect "https://idp.exam.local", reject it.
# ──────────────────────────────────────────────
EXPECTED_ISSUER   = "https://idp.exam.local"
EXPECTED_AUDIENCE = "tds-pv3c9y4x.apps.exam.local"

# ──────────────────────────────────────────────
# 3. Create the FastAPI app
# ──────────────────────────────────────────────
app = FastAPI(
    title="JWT Verification Service",
    description="RS256 JWT verifier for TDS GA-2",
    version="1.0.0",
)

# ──────────────────────────────────────────────
# 4. Define what the request body looks like
#    Pydantic ensures we always get {"token": "..."}
# ──────────────────────────────────────────────
class TokenRequest(BaseModel):
    token: str

# ──────────────────────────────────────────────
# 5. The main endpoint
# ──────────────────────────────────────────────
from fastapi.responses import JSONResponse

@app.post("/verify")
async def verify_token(body: TokenRequest):
    """
    Accepts a JWT and verifies it.

    Steps:
    1. Decode the token using the RS256 public key
    2. PyJWT automatically checks: signature, exp, iss, aud
    3. If decode succeeds → valid token → return 200 with claims
    4. If decode raises any error → invalid token → return 401
    """
    try:
        # PyJWT does ALL the work here:
        # - Verifies the RS256 signature with our PUBLIC_KEY
        # - Rejects if exp is in the past
        # - Rejects if iss != EXPECTED_ISSUER
        # - Rejects if aud != EXPECTED_AUDIENCE
        payload = jwt.decode(
            body.token,
            PUBLIC_KEY,
            algorithms=["RS256"],
            issuer=EXPECTED_ISSUER,
            audience=EXPECTED_AUDIENCE,
            # options: do NOT skip any checks
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )

        # If we reach here, the token is 100% valid ✅
        # Extract the claims the grader expects us to echo back
        return JSONResponse(
            status_code=200,
            content={
                "valid": True,
                "email": payload.get("email", ""),
                "sub":   payload.get("sub", ""),
                "aud":   payload.get("aud", ""),
            },
        )

    except PyJWTError as e:
        # Token failed verification for any reason ❌
        # (expired, bad sig, wrong iss, wrong aud, malformed…)
        return JSONResponse(
            status_code=401,
            content={"valid": False},
        )

# ──────────────────────────────────────────────
# 6. Health-check root endpoint (optional but helpful)
# ──────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "JWT Verification Service"}

# ──────────────────────────────────────────────
# 7. Run locally for testing
# ──────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
