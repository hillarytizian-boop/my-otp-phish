"""
memeterm - Meme Agents AI Trading Terminal (Binance Spot edition).

A local-only, paper-first trading terminal layered on top of the existing
`meme_agents.py` research bot. Nothing in this package touches the Solana
pipeline; the two systems share conventions, not state.

Security posture, in one paragraph: the HTTP server binds to loopback by
default, state-changing requests need a same-origin CSRF token, API keys live
in a 0600 file outside the repository, and no endpoint ever returns a secret.
Loopback is *not* an encryption boundary: malware or a compromised device can
still read that file, which is why the recommended key is spot-only with
withdrawals disabled.
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
