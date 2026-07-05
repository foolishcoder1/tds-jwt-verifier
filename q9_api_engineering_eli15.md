# API Engineering: The ELI15 Guide

Welcome! If you're new to backend engineering, some of the vocabulary (like "idempotency" or "cursor-based pagination") can sound intimidating. But don't worry—these are just fancy words for simple, practical rules that keep web services safe and fast. 

Let's break down the three requirements for this assignment using simple analogies (first principles), and then look at how we implemented them in code.

---

## 1. Idempotency (The "Don't Double Charge Me" Pattern)

### The First Principle
Imagine you are buying a concert ticket on your phone. You tap "Pay $100," but your internet connection drops. You're not sure if the payment went through, so you tap "Pay" again. 

If the server is "dumb," it might charge you twice and give you two tickets. But if the server is **idempotent**, it knows that both requests are part of the exact same action. It says: *"Oh, I already processed this payment. Here is the receipt from the first time."* 

**Idempotency** means that making the same request multiple times has the exact same effect as making it once.

### How We Implemented It
To make an endpoint idempotent, the client (the browser or app) sends a unique ID with the request, called an `Idempotency-Key`. 
When the server receives a request to create an order:
1. It checks its "memory" (a dictionary) to see if it has seen this `Idempotency-Key` before.
2. If **yes**, it just returns the exact same Order ID it generated the first time.
3. If **no**, it creates a new Order ID, saves it in the dictionary under that key, and returns it.

---

## 2. Cursor Pagination (The "Bookmarking" Pattern)

### The First Principle
Imagine reading a 1,000-page book. You don't try to read it all at once; you read a chunk, and then you put a bookmark in it so you know exactly where to start next time.

When an API has thousands of records (like orders), it shouldn't send them all at once. That would crash the server or the user's phone. Instead, it sends a small chunk (a "page") and gives you a **cursor** (a bookmark). 

When you want the next chunk, you give the API the cursor, and it says: *"Ah, you stopped at item #10. Here are items #11 through #20."*

### How We Implemented It
We have a total of 41 orders. 
1. When you ask for orders (e.g., `limit=10`), the API sends the first 10 orders.
2. Along with those 10 orders, it sends a `next_cursor`. In our code, we take the index where we stopped (e.g., `10`) and scramble it using `base64` encoding (so it looks like a random string of letters). 
3. When you send that `next_cursor` back to us, we decode it, realize we stopped at `10`, and send items 11 through 20. 
4. We stop sending a `next_cursor` when we reach the end of our list.

---

## 3. Rate Limiting (The "Bouncer" Pattern)

### The First Principle
Imagine a popular nightclub with a bouncer at the door. If one person tries to bring 50 friends inside all at once, the bouncer stops them because they would overcrowd the club. The bouncer tells them: *"You can only let 18 people in every 10 seconds. You have to wait."*

APIs need bouncers too. If a malicious user (or a badly written program) sends thousands of requests a second, it could crash the server. **Rate limiting** puts a cap on how many requests a specific user can make in a given time frame.

### How We Implemented It
The assignment requires a limit of **18 requests per 10 seconds** per client.
1. The server identifies who is making the request using the `X-Client-Id` header.
2. It looks at a list of timestamps tracking exactly when that specific client made recent requests.
3. It throws away any timestamps older than 10 seconds (because the window has passed).
4. If the client has 18 or more timestamps remaining, the server acts as the bouncer. It returns an HTTP Error `429 Too Many Requests` and includes a `Retry-After` header, telling the client exactly how many seconds to wait before trying again.

---

## Putting It All Together: The Code

Here is how all these concepts translate into Python using FastAPI. 

```python
import base64
import uuid
import time
from fastapi import FastAPI, Header, Depends, HTTPException

app = FastAPI()

# ─────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────
TOTAL_ORDERS = 41
RATE_LIMIT_REQUESTS = 18
RATE_LIMIT_WINDOW_SEC = 10

# State stores (in-memory for the assignment)
idempotency_store = {}
client_requests = {} 
fixed_catalog = [{"id": i} for i in range(1, TOTAL_ORDERS + 1)]


# ─────────────────────────────────────────
# The "Bouncer" (Rate Limiting Dependency)
# ─────────────────────────────────────────
def check_rate_limit(x_client_id: str = Header(..., alias="X-Client-Id")):
    now = time.time()
    
    if x_client_id not in client_requests:
        client_requests[x_client_id] = []
        
    # Throw away history older than 10 seconds
    window_start = now - RATE_LIMIT_WINDOW_SEC
    valid_requests = [req_time for req_time in client_requests[x_client_id] if req_time > window_start]
    client_requests[x_client_id] = valid_requests
    
    # If they hit the limit of 18 requests
    if len(valid_requests) >= RATE_LIMIT_REQUESTS:
        oldest_request = valid_requests[0]
        retry_after = max(1, int(oldest_request + RATE_LIMIT_WINDOW_SEC - now + 1))
        
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={"Retry-After": str(retry_after)}
        )
        
    # Log this new request timestamp
    client_requests[x_client_id].append(now)
    return x_client_id


# ─────────────────────────────────────────
# Idempotency (Order Creation)
# ─────────────────────────────────────────
@app.post("/orders", status_code=201)
def create_order(
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    client_id: str = Depends(check_rate_limit) # The bouncer runs first!
):
    # If we've seen this key before, don't create a new order
    if idempotency_key in idempotency_store:
        return {"id": idempotency_store[idempotency_key]}
    
    # Otherwise, create a new order
    new_order_id = str(uuid.uuid4())
    idempotency_store[idempotency_key] = new_order_id
    
    return {"id": new_order_id}


# ─────────────────────────────────────────
# Cursor Pagination (Listing Orders)
# ─────────────────────────────────────────
@app.get("/orders")
def get_orders(
    limit: int = 10,
    cursor: str = None,
    client_id: str = Depends(check_rate_limit) # The bouncer runs first!
):
    start_idx = 0
    if cursor:
        try:
            # Decode the base64 cursor to get our integer index back
            decoded_str = base64.b64decode(cursor.encode()).decode()
            start_idx = int(decoded_str)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
            
    end_idx = start_idx + limit
    items = fixed_catalog[start_idx:end_idx]
    
    response = {"items": items}
    
    # If we haven't reached the total (41), send a cursor for the next page
    if end_idx < TOTAL_ORDERS:
        next_cursor_str = str(end_idx)
        # Encode the index into base64 so it looks like an opaque string
        response["next_cursor"] = base64.b64encode(next_cursor_str.encode()).decode()
        
    return response
```
