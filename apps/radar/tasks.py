"""Background tasks of the Radar."""

from background_task import background

#: Once a week: Meta counts distinct hashtags per account over seven days.
MARKET_HASHTAGS_INTERVAL_SECONDS = 7 * 24 * 60 * 60


@background(schedule=60)
def collect_market_hashtags():
    """Read the market hashtags' seeds on Instagram.

    Safe to run again: seeds are read in turn, and what is found replaces the
    previous reading of the same segment.
    """
    from .market_hashtags import collect

    return collect()
