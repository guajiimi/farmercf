# farmercf — Auth Flow & Core Logic

Pure HTTP Cloudflare Workers AI account farmer. Zero browser dependency.

---

## Architecture

```
farmercf/
├── server.py              # FastAPI: POST /farm, GET /farm/result, GET /health
├── config.json            # Runtime config (keys, IMAP, proxy, domain)
├── accounts.json          # Output: farmed accounts
├── proxy.txt              # Optional proxy list
└── core/
    ├── __init__.py
    ├── farmer.py          # AccountFarmer — full auth pipeline
    ├── solverify.py       # CaptchaSolver — Solverify/Capsolver/2Captcha
    └── twocaptcha.py      # TwoCaptchaClient — alternative solver
```

## Full Auth Pipeline

```
                        ┌──────────────────────┐
                        │   POST /farm {count}  │
                        └──────────┬───────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │   For each account (batch)   │
                    └──────────────┬───────────────┘
                                   │
          ═══════════════════════════════════════════════
          ║           PHASE 1: CREATE ACCOUNT           ║
          ║      (retry loop, max 6 fresh sessions)     ║
          ═══════════════════════════════════════════════
                                   │
                    ┌──────────────▼───────────────┐
                    │  New AsyncSession            │
                    │  impersonate="chrome131"     │
                    │  + random proxy              │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
              ┌─1─▶ │  GET /api/v4/system/bootstrap│
              │     │  → security_token            │
              │     │  → ip_country                │
              │     └──────────────┬───────────────┘
              │                    │
              │     ┌──────────────▼───────────────┐
              │ ┌─2▶│  GET /api/v4/captcha/        │
              │ │   │  challenge?context=signup     │
              │ │   │  → sitekey                   │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │   ┌──────────────▼───────────────┐
              │ │   │  Solve Turnstile             │
              │ │   │  via CaptchaSolver           │
              │ │   │  (Capsolver/2Captcha/        │
              │ │   │   Solverify)                 │
              │ │   │  → cf_challenge_response     │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │   ┌──────────────▼───────────────┐
              │ │   │  POST /api/v4/user/create    │
              │ │   │  {email, password,           │
              │ │   │   security_token,            │
              │ │   │   cf_challenge_response,     │
              │ │   │   legal_stamp, ...}          │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │          ┌──────┴──────┐
              │ │     success         failed
              │ │        │              │
              │ │        │         retry ──┘
              │ │        │    (new session + proxy,
              │ │        │     sleep 4-10s)
              │ │        │
              │ └────────┘
              │
          ═══════════════════════════════════════════════
          ║        PHASE 2: VERIFY + CREATE TOKEN        ║
          ═══════════════════════════════════════════════
              │
              │     ┌──────────────────────────────┐
              └─5─▶ │  POST /api/v4/persistence/   │
                    │  user                        │
                    │  {emailVerificationRequest:   │
                    │   "welcome"}                 │
                    │  → triggers CF verify email  │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
              ┌─6─▶ │  IMAP poll (max 240s)        │
              │     │  search: (TO "email_addr")   │
              │     │  regex: /email-verification  │
              │     │    \?token=([A-Za-z0-9_-]+)/ │
              │     │  → verification_token        │
              │     │  (every 8s)                  │
              │     └──────────────┬───────────────┘
              │                    │
              │     ┌──────────────▼───────────────┐
              │ ┌─7▶│  PUT /api/v4/user/           │
              │ │   │  email-verification          │
              │ │   │  {token: verification_token}  │
              │ │   │  → email verified            │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │   ┌──────────────▼───────────────┐
              │ │   │  GET /api/v4/accounts        │
              │ │   │  ?per_page=100               │
              │ │   │  → account_id                │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │   ┌──────────────▼───────────────┐
              │ │   │  POST /api/v4/user/tokens    │
              │ │   │  {name: "workers-ai",        │
              │ │   │   policies: [{               │
              │ │   │     effect: "allow",         │
              │ │   │     resources: {             │
              │ │   │       account.{id}: "*"      │
              │ │   │     },                       │
              │ │   │     permission_groups: [     │
              │ │   │       acct_read,             │
              │ │   │       ai_read,               │
              │ │   │       ai_write               │
              │ │   │     ]                        │
              │ │   │   }]}                        │
              │ │   │  → api_token                 │
              │ │   │  → wait 12s propagation      │
              │ │   └──────────────┬───────────────┘
              │ │                  │
              │ │          ┌──────┴──────┐
              │ │    verified        not verified
              │ │       │                │
              │ │    has token       skip token
              │ │       │                │
              │ └───────┘                │
              │                          │
          ═══════════════════════════════════════════════
          ║       PHASE 3: VALIDATE + SAVE               ║
          ═══════════════════════════════════════════════
                                   │
                    ┌──────────────▼───────────────┐
                    │  POST /ai/run/@cf/meta/      │
                    │  llama-3.2-1b-instruct       │
                    │  Authorization: Bearer token  │
                    │  {prompt: "Say hi in one word"}│
                    │  → success = token works      │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │  Save to accounts.json        │
                    │  {email, password, user_id,   │
                    │   account_id, api_token,      │
                    │   status: "active"|"partial"} │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │  (Optional) Inject to 9router │
                    │  SQLite providerConnections   │
                    └──────────────────────────────┘
```

---

## Core Logic: `farm_one()`

### Phase 1 — Create Account

```python
for attempt in range(1, max_retries + 1):
    s = AsyncSession(impersonate="chrome131", headers=HEADERS)
    proxy = self._random_proxy()

    # 1. Bootstrap — get security_token + country
    boot = await get_json(s, f"{CF_API}/system/bootstrap", proxy=proxy)
    sec_token = boot["result"]["data"]["data"]["security_token"]
    country = boot["result"]["data"].get("ip_country", "id").lower()

    # 2. Get dynamic sitekey
    chal = await get_json(s, f"{CF_API}/captcha/challenge?context=signup", proxy=proxy)
    sitekey = chal["result"]["key"] if chal.get("success") else FALLBACK_SITEKEY

    # 3. Solve Turnstile via external provider
    token = await self._solver.solve_turnstile(
        sitekey=sitekey,
        page_url="https://dash.cloudflare.com/sign-up",
    )

    # 4. Create user
    resp = await post_json(s, f"{CF_API}/user/create", {
        "email": gen_email(domain, email_base),
        "password": gen_password(),
        "mrk_optin": True,
        "security_token": sec_token,
        "method": "Onboarding: New_v2",
        "locale": "en-US",
        "legal_stamp": make_legal_stamp(country),
        "cf_challenge_response": token,
        "opt_ins": {},
        "mrktCheckboxDisplayed": False,
        "hCaptchaDisplayed": False,
    }, extra={"Referer": "https://dash.cloudflare.com/"}, proxy=proxy)

    if resp.get("success"):
        break  # → Phase 2
    # else: new session + proxy, sleep 4-10s, retry
```

**Why `impersonate="chrome131"`?** Cloudflare validates TLS fingerprint (JA4). `curl_cffi` impersonates real Chrome TLS handshake — requests appear as genuine Chrome traffic.

**Retry strategy:** Fresh `AsyncSession` + random proxy per attempt. Sleep 4-10s between failures.

### Phase 2 — Verify Email + Create Token

```python
# 5. Trigger verification email
await post_json(session, f"{CF_API}/persistence/user",
                {"emailVerificationRequest": "welcome"})

# 6. IMAP poll — search (TO "email_addr"), not (FROM "cloudflare")
vtok = await poll_imap_verification(
    email_addr, imap_host, imap_port, imap_user, imap_pass,
    timeout=240  # configurable
)
# Regex: /email-verification\?token=([A-Za-z0-9_\-]+)

# 7. Confirm verification
await put_json(session, f"{CF_API}/user/email-verification", {"token": vtok})

# 8. Get account ID
accts = await get_json(session, f"{CF_API}/accounts?per_page=100")
account_id = accts["result"][0]["id"]

# 9. Create API token
tk = await post_json(session, f"{CF_API}/user/tokens", {
    "name": "workers-ai",
    "condition": {},
    "policies": [{
        "effect": "allow",
        "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
        "permission_groups": API_TOKEN_PERMISSIONS,  # 3 IDs
    }],
})
api_token = tk["result"]["value"]
await asyncio.sleep(12)  # propagation
```

### Phase 3 — Validate + Save

```python
# 10. Validate via Workers AI inference
url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/meta/llama-3.2-1b-instruct"
resp = await post_json(s, url,
    {"prompt": "Say hi in one word"},
    extra={"Authorization": f"Bearer {api_token}"})
# success → status = "active"

# 11. Save
acc = {
    "email": email_addr,
    "password": password,
    "user_id": created["id"],
    "account_id": account_id,
    "api_token": api_token,
    "status": "active" | "verified_no_token" | "created",
}
```

---

## Captcha Solving

### Supported Providers

| Provider | Task Type | Endpoint | Poll Interval | Timeout |
|----------|-----------|----------|---------------|---------|
| **Capsolver** | `AntiTurnstileTaskProxyLess` | `api.capsolver.com` | 3s | 135s |
| **Solverify** | `turnstile` | `solver.solverify.net` | 3s | 135s |
| **2Captcha** | `TurnstileTaskProxyless` | `api.2captcha.com` | 5s | 135s |

### API Pattern (all 3 providers)

```
1. POST /createTask
   {clientKey, task: {type, websiteURL, websiteKey}}
   → {taskId}

2. Poll POST /getTaskResult (every 3-5s)
   {clientKey, taskId}
   → {status: "completed"/"ready", solution: {token/value}}
```

### Capsolver Detail

```python
POST https://api.capsolver.com/createTask
{
    "clientKey": "CAP-xxx",
    "task": {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": "https://dash.cloudflare.com/sign-up",
        "websiteKey": "0x4AAAAAAAJel0iaAR3mgkjp"
    }
}
→ {"taskId": "xxx"}

POST https://api.capsolver.com/getTaskResult
{"clientKey": "CAP-xxx", "taskId": "xxx"}
→ {"status": "ready", "solution": {"token": "0.xxxxx..."}}
```

### 2Captcha Detail

```python
POST https://api.2captcha.com/createTask
{
    "clientKey": "xxx",
    "task": {
        "type": "TurnstileTaskProxyless",
        "websiteURL": "https://dash.cloudflare.com/sign-up",
        "websiteKey": "0x4AAAAAAAJel0iaAR3mgkjp"
    }
}
→ {"taskId": 12345}

POST https://api.2captcha.com/getTaskResult
{"clientKey": "xxx", "taskId": 12345}
→ {"status": "ready", "solution": {"token": "0.xxxxx..."}}
```

### Serialization

All solver calls are serialized globally (`asyncio.Semaphore(1)`) to prevent rate-limiting from captcha providers.

---

## Email Verification (IMAP)

```python
async def poll_imap_verification(email_addr, imap_host, imap_port,
                                  imap_user, imap_pass, timeout=240):
    seen = set()
    while elapsed < timeout:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")

        # Search TO the target address (not FROM cloudflare)
        status, messages = mail.search(None, f'(TO "{email_addr}")')

        for num in messages[0].split():
            if num in seen:
                continue
            seen.add(num)
            msg = email_lib.message_from_bytes(data[0][1])
            body = extract_text(msg)

            # Primary pattern
            link = re.search(r"/email-verification\?token=([A-Za-z0-9_\-]+)", body)
            if link:
                return link.group(1)

            # Fallback: any long token param
            tok = re.search(r"[?&]token=([A-Za-z0-9_\-]{40,})", body)
            if tok:
                return tok.group(1)

        await asyncio.sleep(8)
```

**Why `(TO "email")` not `(FROM "cloudflare")`?** Gmail plus-addressing (`base+suffix@gmail.com`) — searching by TO is more reliable and catches all variants.

---

## Token Permissions

Workers AI requires 3 permission group IDs:

```python
API_TOKEN_PERMISSIONS = [
    {"id": "644535f4ed854494a59cb289d634b257"},  # Account Read
    {"id": "a92d2450e05d4e7bb7d0a64968f83d11"},  # Workers AI Read
    {"id": "bacc64e0f6c34fc0883a1223f938a104"},  # Workers AI Write
]
```

Token scope: `com.cloudflare.api.account.{account_id}: *`

---

## 9router Injection

Optionally injects created tokens into local 9router SQLite DB:

```python
INSERT INTO providerConnections
    (id, provider, authType, name, priority, isActive, data, createdAt, updatedAt)
VALUES
    (uuid4(), "cloudflare-ai", "apikey",
     "CfcFarmer #{priority}", priority, 1,
     '{"apiKey": "token", "testStatus": "active",
       "providerSpecificData": {"accountId": "id"}}',
     now, now)
```

Retries: 5x with exponential backoff (0.5s → 8s). Uses `BEGIN IMMEDIATE` + `PRAGMA busy_timeout=30000`.

---

## Generated Fields

| Field | Format | Example |
|-------|--------|---------|
| `email` | `{random8alpha}{3digit}@{domain}` or `{base}+{suffix}@gmail.com` | `xkmpqrtu123@gmail.com` |
| `password` | `{10alpha}{2digit}-Aa1!` | `xKmPqRtYuI42-Aa1!` |
| `legal_stamp` | base64 of `ts:{ms_timestamp}/stratus_commit:{hash}/country:{cc}` | `dHM6MTcy...` |
| `security_token` | From bootstrap response | hex string |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Status + provider info |
| `POST` | `/farm` | Start farming `{count: N}` |
| `GET` | `/farm/result?id=` | Poll for results |

### Usage

```bash
# Start farming 5 accounts
curl -X POST http://localhost:8107/farm \
  -H "Content-Type: application/json" \
  -d '{"count": 5}'

# Poll result
curl http://localhost:8107/farm/result?id=<task_id>
```

---

## Configuration

```json
{
    "host": "0.0.0.0",
    "port": 8107,
    "captcha_provider": "capsolver",
    "capsolver_api_key": "CAP-xxx",
    "twocaptcha_api_key": "",
    "twocaptcha_timeout": 120,
    "farm_domain": "gmail.com",
    "farm_email_base": "yourbase",
    "farm_create_retries": 6,
    "proxy_file": "proxy.txt",
    "accounts_file": "accounts.json",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "imap_user": "your@gmail.com",
    "imap_pass": "your-app-password",
    "imap_timeout": 240,
    "inject_9router": false
}
```

---

## Proxy Format (`proxy.txt`)

```
host:port:user:pass
host:port
http://user:***@host:port
socks5://host:port
```

Lines starting with `#` are ignored.

---

## Output Schema (`accounts.json`)

```json
{
    "email": "xkmpqrtu123@gmail.com",
    "password": "xKmPqRtYuI42-Aa1!",
    "user_id": "abc123...",
    "account_id": "def456...",
    "created_at": "2026-07-10T12:00:00+00:00",
    "email_verified": true,
    "api_token": "v1.0.0...",
    "neurons_quota": 10000,
    "status": "active"
}
```

Status values:
- `active` — token verified via Workers AI inference
- `verified_no_token` — email verified, token creation failed
- `token_unverified` — token created but validation failed
- `created` — account created, email not verified
