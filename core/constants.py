"""
farmercf.core.constants — Verified Cloudflare permission IDs + anti-detection pools.

All values reverse-engineered from Cloudflare Dashboard API (June 2026).
Permission IDs verified working via GraphQL aiInferenceAdaptiveGroups query.
"""

# ── Cloudflare API ────────────────────────────────────────────
CF_API = "https://dash.cloudflare.com/api/v4"
CF_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"
FALLBACK_SITEKEY = "0x4AAAAAAAJel0iaAR3mgkjp"
STRATUS_COMMIT = "43768e5f0b36b3c6c3c5ed00afa10affa55b38db"

# ── Verified Permission Group IDs (5 permissions) ─────────────
# These are the CORRECT IDs, verified via Dashboard API June 2026.
# Old repo had only 3 (missing Analytics) — neuron tracking was impossible.
AI_GATEWAY_RUN = "644535f4ed854494a59cb289d634b257"
WORKERS_AI_READ = "a92d2450e05d4e7bb7d0a64968f83d11"
WORKERS_AI_WRITE = "bacc64e0f6c34fc0883a1223f938a104"
ANALYTICS_READ = "9c88f9c5bce24ce7af9a958ba9c504db"
ACCOUNT_ANALYTICS_READ = "b89a480218d04ceb98b4fe57ca29dc1f"

API_TOKEN_PERMISSIONS = [
    {"id": AI_GATEWAY_RUN},
    {"id": WORKERS_AI_READ},
    {"id": WORKERS_AI_WRITE},
    {"id": ANALYTICS_READ},
    {"id": ACCOUNT_ANALYTICS_READ},
]

# ── Default headers ────────────────────────────────────────────
HEADERS = {"x-cross-site-security": "dash"}
DEFAULT_TIMEOUT = 45

# ── Anti-Detection: TLS Fingerprint Pool ───────────────────────
# curl_cffi impersonation targets — random per registration
TLS_FINGERPRINTS = [
    "chrome131", "chrome124", "chrome120", "chrome119",
    "chrome116", "chrome110", "chrome107", "chrome104",
    "chrome101", "chrome100",
]

# ── Anti-Detection: Locale Pool ────────────────────────────────
# Each generates unique Accept-Language + locale combo
LOCALE_POOL = [
    ("en-US", "en-US,en;q=0.9"),
    ("en-GB", "en-GB,en;q=0.9"),
    ("en-AU", "en-AU,en;q=0.9"),
    ("en-CA", "en-CA,en;q=0.8"),
    ("en-SG", "en-SG,en;q=0.9"),
    ("en-IN", "en-IN,en;q=0.9"),
    ("en-ZA", "en-ZA,en;q=0.8"),
    ("en-NZ", "en-NZ,en;q=0.9"),
    ("en-IE", "en-IE,en;q=0.9"),
    ("en-PH", "en-PH,en;q=0.8"),
    ("en-MY", "en-MY,en;q=0.8"),
    ("en-NG", "en-NG,en;q=0.8"),
]

# ── Anti-Detection: Legal Stamp Countries ──────────────────────
LEGAL_COUNTRIES = [
    "us", "gb", "au", "ca", "sg", "in", "za",
    "nz", "ie", "ph", "my", "ng",
]

# ── Anti-Detection: Password Patterns ─────────────────────────
# Each generates a unique-looking password with different structure
PASSWORD_PATTERNS = [
    lambda s: s[:10] + str(__import__('random').randint(10, 99)) + "-Aa1!",
    lambda s: s[:8] + "!" + str(__import__('random').randint(100, 999)) + "Xy",
    lambda s: s[:12] + "#2" + str(__import__('random').randint(10, 99)),
    lambda s: s[:9] + "Zq1!" + str(__import__('random').randint(100, 999)),
    lambda s: s[:11] + str(__import__('random').randint(1, 9)) + "$Ab",
]

# ── 9router ────────────────────────────────────────────────────
ROUTER_DB = "~/.9router/db/data.sqlite"
ROUTER_CONNECTION_NAME = "CfcFarmer"
