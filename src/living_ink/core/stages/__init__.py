"""The stages a document passes through, one module each.

Every stage used to be a private method on ``SyncPipeline``, which is how a
1,800-line class with sixty methods came about: each stage could reach any
other stage's state through ``self``, so nothing ever had to declare what it
needed. A stage here is handed what it works on and nothing else.

``pipeline.py`` still owns the *order* — that sequence is the pipeline, and it
is fixed on purpose. What lives here is what each step does.

Being under ``core/`` means rule 4 in ``tests/test_layering.py`` applies: a
stage may not import ``pipeline`` back. That is not a formality — it is what
moved ``log()`` down into ``living_ink.logs``, because a stage that has to
import the pipeline to say one line to the user has not been extracted at all.
"""

from living_ink.core.stages.preprocess import prepare_pages, preprocess_image
from living_ink.core.stages.transcribe import Transcriber
from living_ink.core.stages.transcript import write_transcript

__all__ = ["Transcriber", "prepare_pages", "preprocess_image", "write_transcript"]
