"""What differs between the providers this app can translate with.

Everything provider-shaped lives here so the rest of the app can ask questions
instead of testing names: which models exist, which thinking levels a model
will accept, what currency its bill arrives in.

A vendor is chosen when the key is entered, because that is what the key is
for. Changing it therefore means entering a key again — the old one is for a
different service and is dropped rather than kept around.
"""

# Thinking levels, in the order the interface offers them. "off" means the
# model is asked not to think at all; the rest are the provider's own names.
EFFORTS = ["off", "low", "high", "max"]

VENDORS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "currency": "CNY",
        "symbol": "¥",
        "default_effort": "high",
        "models": {
            "deepseek-v4-flash": {"label": "DeepSeek V4 Flash",
                                  "hint": "model_hint_fast"},
            "deepseek-v4-pro": {"label": "DeepSeek V4 Pro",
                                "hint": "model_hint_quality"},
        },
        "efforts": EFFORTS,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "currency": "USD",
        "symbol": "$",
        # gpt-6-luna is a reasoning model whose effort can be turned down to
        # "none". Only that setting is offered: the others buy reasoning this
        # task does not use and bills for it as output tokens, which on one
        # measured run was the difference between ¥0.28 and ¥3.26.
        "default_effort": "off",
        "models": {
            "gpt-6-luna": {"label": "GPT-6 Luna", "hint": "model_hint_fast"},
        },
        # No longer offered, but past jobs ran on them and still need to be
        # attributed to this vendor — their bill is in dollars, not yuan.
        "retired": ["gpt-5.6-luna"],
        "efforts": ["off"],
    },
}

DEFAULT_VENDOR = "deepseek"


def vendor_of(model: str) -> str:
    """Which provider serves this model. Model names do not collide."""
    for name, vendor in VENDORS.items():
        if model in vendor["models"] or model in vendor.get("retired", ()):
            return name
    return DEFAULT_VENDOR


def models() -> dict:
    """Every model, flattened, for the places that only need the labels."""
    out = {}
    for vendor in VENDORS.values():
        out.update(vendor["models"])
    return out


def efforts_for(model: str) -> list:
    return VENDORS[vendor_of(model)]["efforts"]


def symbol_of(model: str) -> str:
    return VENDORS[vendor_of(model)]["symbol"]


def symbols() -> dict:
    """Currency symbol for every model a job may have run on, retired included."""
    return {m: v["symbol"] for v in VENDORS.values()
            for m in [*v["models"], *v.get("retired", ())]}
