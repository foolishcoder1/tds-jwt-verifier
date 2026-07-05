# ELI15 Guide: Middleware Stack (Rate-Limit + CORS + Request Context)

This assignment asks us to build three layers of "middleware" for a `/ping` endpoint.

### What is Middleware?
Imagine a nightclub. The **endpoint** is the dance floor (where the actual fun happens). 
The **middleware** is the set of bouncers at the door. 
Every person (request) who wants to enter must pass through the bouncers first. If a bouncer doesn't like something, they can reject the person immediately, without the dance floor ever knowing.

In FastAPI, middlewares wrap around the endpoints. We need three specific "bouncers":
1. **The ID Tagger** (Request Context)
2. **The Counter** (Rate Limiter)
3. **The List Checker** (CORS Policy)

---

## Step 1: The Request Context Middleware (The ID Tagger)

**The Rule:** If a request comes in with an `X-Request-ID` header, use it. Otherwise, create a new unique one. Attach it to the response.

**How it works in the code:**
When a request arrives at `/ping`:
1. We look at the headers for `X-Request-ID`.
2. If it's missing, we use `uuid.uuid4()` to generate a random string like `123e4567-e89b-12d3...`.
3. We temporarily store it in `request.state.ping_request_id` (think of this as slapping a sticky note on the visitor's back so the endpoint can read it later).
4. We let the request continue to the endpoint.
5. As they leave with the response, we slap the ID onto the response headers too.

---

## Step 2: The Rate Limiter (The Counter)

**The Rule:** A client can only make 8 requests every 10 seconds. We identify clients using the `X-Client-Id` header.

**How it works in the code:**
We use a dictionary called `ping_clients` to keep track of when each client visited.
1. When Bob (`X-Client-Id: bob123`) arrives, we check his history.
2. We look at the current time (`time.time()`).
3. We erase any visits older than 10 seconds (because the window is 10s).
4. We count how many visits remain. If it's 8 or more, the bouncer says "Too Many Requests" (HTTP 429) and blocks Bob.
5. If it's under 8, we add the current time to Bob's history and let him in.

---

## Step 3: The CORS Policy (The List Checker)

**The Rule:** Browsers block requests made from a different website (origin) unless the server explicitly permits it using the `Access-Control-Allow-Origin` (ACAO) header. The grader needs us to allow *only* `https://app-3w0fj5.example.com` and the exam page itself.

**How it works in the code:**
Our app already has a global CORS rule allowing *everyone* (`*`). This is a problem because the assignment says **no wildcards allowed for this endpoint**.

We solve this using a custom middleware just for `/ping`:
1. We read the `Origin` header from the request.
2. We check if the origin is exactly `https://app-3w0fj5.example.com` or if it belongs to the grader (`iitm.ac.in`).
3. If it's a preflight request (`OPTIONS`), we reply immediately with a 204 No Content status and the correct CORS headers if allowed.
4. If it's a normal request (`GET`), we let it process. On its way out, if it was an allowed origin, we explicitly set the ACAO header to that specific origin.
5. If it wasn't allowed, we actually **delete** the ACAO header just in case the global middleware tried to add it.

---

## Step 4: The `/ping` Endpoint

Now that the bouncers have done all the heavy lifting, the actual endpoint is very simple.

```python
@app.get("/ping")
async def ping_endpoint(request: Request):
    # Read the sticky note the first middleware left on the request
    req_id = getattr(request.state, "ping_request_id", "")
    
    return {
        "email": STUDENT_EMAIL,
        "request_id": req_id
    }
```

## How to Test This Locally Before Deploying

You can use `curl` to simulate the grader's tests:

**Test 1: Request with an ID**
```bash
curl -i http://localhost:8000/ping -H "X-Request-ID: my-custom-id"
```
*Expected: The response body and headers should both show `my-custom-id`.*

**Test 2: Rate Limiting**
Run this 9 times quickly (within 10 seconds):
```bash
curl -i http://localhost:8000/ping -H "X-Client-Id: spammer99"
```
*Expected: The 9th request should return `429 Too Many Requests`.*

**Test 3: CORS Preflight**
```bash
curl -i -X OPTIONS http://localhost:8000/ping -H "Origin: https://app-3w0fj5.example.com"
```
*Expected: `Access-Control-Allow-Origin: https://app-3w0fj5.example.com` is present in the headers.*

### Final Step
The code is already injected into `main.py` without breaking your previous answers. 
Commit and push your code to your repository, which will automatically trigger your deployment on Render/Heroku. Wait for the deploy to finish, then paste your base URL into the exam portal!
