"""The destination-neutral core: the domain model and the pipeline that fills it.

Only :mod:`living_ink.core.document` exists so far. It holds the types that
travel between the pipeline and a destination, and it imports nothing from the
rest of the package on purpose — a destination may read it without reaching
back into the pipeline.
"""
