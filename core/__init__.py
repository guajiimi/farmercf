"""farmercf.core — Cloudflare account farmer with anti-detection + multi-provider captcha solving."""

from .constants import API_TOKEN_PERMISSIONS
from .captcha import CaptchaSolver
from .farmer import AccountFarmer, FakeIdentity
from .neuron import NeuronTracker
from .email import poll_imap_verification

__all__ = [
    "AccountFarmer",
    "FakeIdentity",
    "CaptchaSolver",
    "NeuronTracker",
    "poll_imap_verification",
    "API_TOKEN_PERMISSIONS",
]
