"""Where the caches and the state database live, resolved one way.

A second way to locate ``state.db`` is a second answer, so these accessors are
the only ones.
"""

from pathlib import Path


def state_db_path() -> Path:
    """Return where the state database lives, without creating it.

    Returns:
        Path to ``state.db`` inside the data directory. May not exist.
    """
    from living_ink import state
    from living_ink.pipeline import DATA_DIR

    return DATA_DIR / state.DB_FILENAME


def transcript_cache():
    """Return the transcription cache the configured settings describe.

    Built from the resolved settings rather than defaults so that ``info``
    reports on the same cache a sync would use, including a disabled one.

    Returns:
        A :class:`living_ink.cache.TranscriptCache`, whose directory may not
        exist yet. Reading the cache must not create it.
    """
    from living_ink.cache import TranscriptCache
    from living_ink.pipeline import TRANSCRIPT_CACHE_DIR, get_default_config
    from living_ink.settings import Settings

    settings = Settings.resolve(get_default_config())
    return TranscriptCache(
        TRANSCRIPT_CACHE_DIR,
        enabled=settings.transcript_cache,
        max_age_days=settings.cache_max_age_days,
    )


def render_cache():
    """Return the render cache the configured settings describe.

    Returns:
        A :class:`living_ink.cache.RenderCache`, whose directory may not exist
        yet. Reading the cache must not create it.
    """
    from living_ink.cache import RenderCache
    from living_ink.pipeline import RENDER_CACHE_DIR, get_default_config
    from living_ink.settings import Settings

    settings = Settings.resolve(get_default_config())
    return RenderCache(
        RENDER_CACHE_DIR,
        enabled=settings.render_cache,
        max_age_days=settings.cache_max_age_days,
    )


def all_caches():
    """Return every cache ``living-ink info`` reports on, in printing order.

    Returns:
        A list of :class:`living_ink.cache.FileCache` instances.
    """
    return [transcript_cache(), render_cache()]
