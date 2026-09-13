"""What a model call costs, in USD, from the token counts the API reports.

The budget is enforced in dollars because that is the unit the client set it
in. A table here rather than a lookup against the vendor's price list because
the number that matters is the one that was true when the call was made, and
the table is versioned with the code.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.providers.llm import Usage

logger = logging.getLogger(__name__)

#: USD per million tokens: (input, output, cache read, cache write).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "extractive-v1": (0, 0, 0, 0),
}

_PER_MILLION = Decimal(1_000_000)
_SIX_DP = Decimal("0.000001")

#: Models already warned about, so an unpriced model produces one log line
#: rather than one per call.
_unpriced_warned: set[str] = set()


def _prices_for(model: str) -> tuple[float, float, float, float] | None:
    """The price row for a model, by exact name or by family.

    What is recorded is the model the API *answered with*, which for an alias
    is its dated release: ask for ``claude-opus-5`` and the response says
    ``claude-opus-5-20260401``. An exact-match table misses that, every call
    then costs zero, and the daily budget never fires -- which is the one
    failure this system must not have. So an unknown name falls back to the
    longest priced name it starts with.
    """
    prices = PRICES.get(model)
    if prices is not None:
        return prices
    families = [name for name in PRICES if model.startswith(name)]
    return PRICES[max(families, key=len)] if families else None


def cost_usd(model: str, usage: Usage) -> Decimal:
    """Cost of one call, quantised to six decimal places.

    An unknown model costs nothing and logs once. Zero is wrong, but it is
    visibly wrong on the usage page, whereas a guess would look like data.
    """
    prices = _prices_for(model)
    if prices is None:
        if model not in _unpriced_warned:
            _unpriced_warned.add(model)
            logger.warning("no price on record for model %r; recording its cost as 0", model)
        return Decimal(0)

    input_price, output_price, read_price, write_price = (Decimal(str(p)) for p in prices)
    total = (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + usage.cache_read_tokens * read_price
        + usage.cache_write_tokens * write_price
    ) / _PER_MILLION
    return total.quantize(_SIX_DP)
