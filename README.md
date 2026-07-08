# farmercf

Pure HTTP Cloudflare Workers AI account farmer.

## Flow
1. Bootstrap → get security_token
2. Challenge → get Turnstile sitekey
3. Solve Turnstile (Capsolver / 2Captcha createTask v2)
4. Create account → email verify via IMAP → create API token → validate

## Supported Providers
- **Capsolver** — `AntiTurnstileTaskProxyLess` (~5s)
- **2Captcha** — `TurnstileTaskProxyless` via createTask API v2 (~8-14s)
- **Solverify** — createTask/getTaskResult (~3-5s)

## Setup
```bash
pip install -r requirements.txt
cp config.example.json config.json  # edit with your keys
```

## Run
```bash
python server.py
```

## API
```bash
# Farm 1 account
curl -X POST http://localhost:8107/farm -H "Content-Type: application/json" -d '{"count": 1}'

# Check result
curl http://localhost:8107/farm/result?id=YOUR_TASK_ID

# Health check
curl http://localhost:8107/health
```

## Proxy
Residential proxy required for CF POST endpoints. Add proxies to `proxy.txt`:
```
http://user:***@host:port
socks5://user:pass@host:port
```
