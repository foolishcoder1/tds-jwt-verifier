# TDS GA-2: JWT Verification Service

A FastAPI service that validates RS256 JWTs per OAuth 2.0 / OIDC spec.

## Endpoint

`POST /verify`

### Request
```json
{"token": "<JWT string>"}
```

### Response (valid)
```json
{"valid": true, "email": "...", "sub": "...", "aud": "..."}
```

### Response (invalid)
HTTP 401
```json
{"valid": false}
```

## Verification Rules
1. RS256 signature verified against IdP public key
2. Issuer must equal `https://idp.exam.local`
3. Audience must equal `tds-pv3c9y4x.apps.exam.local`
4. Token must not be expired

## Deploy

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```
