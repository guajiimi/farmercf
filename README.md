# farmercf

Pure HTTP Cloudflare Workers AI account farmer.

## Flow
1. Bootstrap → get security_token
2. Challenge → get Turnstile sitekey
3. Solve Turnstile (Capsolver / 2Captcha / Solverify)
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

### Farming
```bash
# Farm 1 account
curl -X POST http://localhost:8107/farm -H "Content-Type: application/json" -d '{"count": 1}'

# Check result
curl http://localhost:8107/farm/result?id=YOUR_TASK_ID

# Health check
curl http://localhost:8107/health
```

### Neuron Usage
```bash
# Get stored usage summary (all accounts, today)
curl http://localhost:8107/neuron-usage

# Get usage for specific date
curl http://localhost:8107/neuron-usage?date=2026-07-10

# Get usage for specific account
curl http://localhost:8107/neuron-usage?account_id=ACCOUNT_ID

# Live check all accounts (makes real inference, ~2-3s per account)
curl http://localhost:8107/neuron-usage/live
# Returns task_id, poll: curl http://localhost:8107/farm/result?id=TASK_ID

# Live check single account
curl "http://localhost:8107/neuron-usage/check?account_id=ID&api_token=TOKEN&email=EMAIL"
```

### Neuron Usage Output
```json
{
  "date": "2026-07-10",
  "total_accounts": 5,
  "total_neurons_used": 23400.5,
  "total_neurons_remaining": 26600.0,
  "total_quota": 50000,
  "total_pct_used": 46.8,
  "accounts": [
    {
      "account_id": "...",
      "email": "user@gmail.com",
      "neurons_used": 4521.5,
      "neurons_remaining": 5478.5,
      "requests": 87,
      "quota": 10000,
      "pct_used": 45.2
    }
  ]
}
```

## How Neuron Tracking Works

Every `/ai/run` response includes a `cf-ai-neurons` header showing exact neurons consumed:
```
cf-ai-neurons: 0.26
```

farmercf tracks this automatically:
1. **During farming** — `verify_token()` reads the header and saves to `neuron_usage.json`
2. **During usage** — NeuronTracker records per-call consumption
3. **On demand** — `/neuron-usage/live` makes a minimal inference to get fresh data

Quota: **10,000 neurons/day** per account (Cloudflare free tier, resets midnight UTC).

## Proxy
Residential proxy required for CF POST endpoints. Add proxies to `proxy.txt`:
```
http://user:***@host:port
socks5://user:pass@host:port
```

## Docs
- [AUTH_FLOW.md](docs/AUTH_FLOW.md) — Full auth pipeline deep-dive with core logic
