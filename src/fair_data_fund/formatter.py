"""This module provides formatting functions for API output."""

from datetime import datetime
from fair_data_fund.convenience import value_or_none

def to_timestamp (epoch):
    """Transform a UNIX timestamp into a human-readable timestamp string."""
    if epoch is None:
        return None
    return datetime.strftime(datetime.fromtimestamp(epoch), "%Y-%m-%dT%H:%M:%SZ")

