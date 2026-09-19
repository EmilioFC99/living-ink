"""Fakes that exercise contracts the shipped code cannot reach.

Not scaffolding. Each one covers an abstraction with no real user today, which
is the condition under which a contract rots unnoticed: nothing fails, because
nothing runs it.

A fake implements the real contract, so changing that contract breaks the fake.
A ``MagicMock`` would return a Mock for a newly added method and keep passing.
"""

from typing import Any, Dict, List, Optional

from living_ink.core.document import Document, PublishContext, PublishResult
from living_ink.destinations.base import Destination, DestinationStatus
from living_ink.settings import Settings


class FakeApiDestination(Destination):
    """A destination that identifies notes by an id it mints itself.

    Covers the one loop nothing shipped covers end to end:
    :attr:`PublishResult.external_id` → the ``external_id`` column of
    ``publications`` → back into the next publish as ``existing_id``. Obsidian
    identifies notes by frontmatter and reports no id at all, and Apple Notes —
    the only destination that does — does not survive 1.0, so without this the
    round trip would be dead code the day that destination is deleted.

    Attributes:
        objects: Every id this destination has handed out, newest last.
        seen_existing_id: The id it was given on each publish, in order. A
            ``None`` here on a second publish is the failure this fake exists
            to catch.
        seen_documents: Every document it was handed, newest last.
    """

    # Set here rather than by @register_destination: registering mutates a
    # process-wide dict at import time, and a fake that appears in every other
    # test's registry is a fake that changes what those tests are testing.
    config_key = "fake_api"
    state_key = "FakeApiDestination"
    display_name = "Fake API"

    def __init__(self, folder: str = "inbox") -> None:
        """Start with no objects and no history.

        Args:
            folder: Where the fake pretends to file notes, so ``target`` is
                something other than the title.
        """
        self.folder = folder
        self.objects: List[str] = []
        self.seen_existing_id: List[Optional[str]] = []
        self.seen_existing_target: List[Optional[str]] = []
        self.seen_documents: List[Document] = []
        self.deleted: List[str] = []
        self.ready = True

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> "FakeApiDestination":
        """Build from a section that only has to name a folder."""
        return cls(folder=section.get("folder", "inbox"))

    def describe(self) -> str:
        """Name the fake and the folder it files into."""
        return f"Fake API (Folder: {self.folder})"

    def check(self) -> DestinationStatus:
        """Report the readiness a test set on :attr:`ready`."""
        if not self.ready:
            return DestinationStatus(
                ok=False, detail="The fake API is unreachable.", remedy="Set ready = True."
            )
        return DestinationStatus(ok=True, detail=f"Folder '{self.folder}'.")

    def publish(self, doc: Document, ctx: PublishContext) -> PublishResult:
        """Replace the object named by the recorded id, or mint a new one.

        Returns:
            The outcome, always carrying the id of the object that now holds
            this document.
        """
        self.seen_existing_id.append(ctx.existing_external_id)
        self.seen_existing_target.append(ctx.existing_target)
        self.seen_documents.append(doc)

        existing_id = ctx.existing_external_id
        if existing_id and existing_id in self.objects:
            object_id = existing_id
        else:
            object_id = f"obj-{len(self.objects) + 1}"
            self.objects.append(object_id)

        where = "/".join([self.folder, *doc.folder_path, doc.title])
        return PublishResult(ok=True, target=where, external_id=object_id, detail=where)

    def unpublish(self, ctx: PublishContext) -> PublishResult:
        """Delete exactly the object named, and refuse without a name.

        Returns:
            The outcome. Refusing is not an error — it is what a destination
            that cannot prove which object is the right one must do.
        """
        external_id = ctx.existing_external_id
        target = ctx.existing_target
        if not external_id or external_id not in self.objects:
            return PublishResult(ok=False, target=target, detail="No such object.")
        self.objects.remove(external_id)
        self.deleted.append(external_id)
        return PublishResult(ok=True, target=target, external_id=external_id, detail="Deleted.")
