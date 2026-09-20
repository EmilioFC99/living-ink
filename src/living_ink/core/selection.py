"""One classifier, one candidate set: what a run would do, decided once.

Everything that narrows a sync used to be spread across three places that were
free to disagree — a discovery filter in the pipeline, a comparison in
:mod:`living_ink.state`, and a second listing pass in the CLI that answered
``sync --preview``. A preview that predicts something other than what the run
does is worse than no preview at all, in a feature whose entire purpose is
"tell me what will happen". So :func:`select` is the only function that
decides, and the preview and the run call it with the same arguments.

Three things it is careful about, each of which was a defect:

**A document is pending for more than one reason.** ``version`` answers "did it
change on the tablet". It cannot answer "would we produce different output from
the same document", which is what happens when the user edits a prompt, changes
AI provider, moves their vault, or upgrades a renderer. That half is the
*recipe* (:mod:`living_ink.core.recipe`), recorded beside the version. Nor can
it answer "is the note still there" — a user who deletes a note in their vault
gets it back, because :meth:`Destination.published_exists` is asked.

**"Not selected" is not one outcome.** A document can be out of scope, in the
trash, genuinely unchanged, or *pending but past the limit*. The last one used
to be reported as ``unchanged``, so a run with the default limit of 1 and ten
pending notebooks told the user nine were up to date. :attr:`Selection.deferred`
is what makes that impossible to say.

**Pending-at-zero-destinations is a real answer.** It used to be spelled as an
empty list in a dict and read back with ``or``, which is indistinguishable from
a missing key — so a document computed as needing no destination was published
to all of them. Here the answer rides on the candidate as a tuple, and an empty
tuple never reaches the publish stage because it is not a candidate.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from living_ink.core.listing import (
    document_name,
    document_path,
    document_version,
    get_document_type,
    get_notebook_path,
    get_val,
    is_trashed,
    matches_notebook_target,
)
from living_ink.core.recipe import document_recipe
from living_ink.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - annotations only; core/ takes no plugin dependency
    from living_ink.destinations.base import Destination
    from living_ink.state import StateStore

logger = logging.getLogger(__name__)

#: Why a document was left out. Phrased for the summary table, because that is
#: where they are read: "why was my notebook not picked up" is the question.
TRASHED = "in the trash"
WRONG_TYPE = "type not enabled"
EXCLUDED = "in an excluded folder"
OUTSIDE_PATH = "outside the selected path"
NO_MATCHING_TAG = "no matching tag"
NOT_TARGETED = "not the requested notebook"
UNCHANGED = "unchanged"


@dataclass(frozen=True)
class SelectionCriteria:
    """Everything that narrows a run. Built once, from flags plus settings.

    Attributes:
        source_path: Case-insensitive substring match on the document's full
            path, title included. A fragment means "the ones called roughly
            this", so it is wrapped in implicit wildcards rather than compared
            for equality; matching the *full* path is what makes
            ``--source-path "Work/"`` a folder filter without a second flag.
        source_regex: Case-sensitive regex searched against that same full
            path. Case-sensitive because a regex user who wants otherwise
            writes ``(?i)``, and forcing a flag onto someone's pattern is
            worse than making them state it. Mutually exclusive with
            :attr:`source_path` — two filters over one field silently
            intersect, and a user who passes both means one of them.
        exclude: Folder names never synced — ``Templates`` and ``Quick
            sheets`` by default. Matched against each segment of the folder
            path, so excluding ``Templates`` excludes what is under it too.
            Trash is not in here: it is excluded unconditionally.
        target: A notebook named on the command line, by id, title or path.
            Narrows to that document and nothing else.
        tags: Match any of these. Empty means no tag filter. Reading tags costs
            a round trip per document, so the filter is applied last.
        types: Source names to include. **Replaces** the configured set when
            any type flag is given, rather than adding to it.
        limit: Maximum documents to process, or None for unlimited.
        force: Republish regardless of what the comparison concluded.
    """

    source_path: Optional[str] = None
    source_regex: Optional[str] = None
    exclude: FrozenSet[str] = frozenset()
    target: Optional[str] = None
    tags: FrozenSet[str] = frozenset()
    types: FrozenSet[str] = frozenset()
    limit: Optional[int] = None
    force: bool = False

    def __post_init__(self) -> None:
        """Reject a pair of filters that cannot both have been meant.

        Raises:
            ValueError: If both a path and a regex were given, or the regex
                does not compile. Both are user input, and failing here means
                failing before the tablet is contacted.
        """
        if self.source_path and self.source_regex:
            raise ValueError("use --source-path or --source-regex, not both")
        if self.source_regex:
            try:
                re.compile(self.source_regex)
            except re.error as exc:
                raise ValueError(f"invalid path regex {self.source_regex!r}: {exc}") from exc


@dataclass(frozen=True)
class Candidate:
    """One document a run will work on, with the facts the stages need.

    Attributes:
        item: The transport's own handle on the document. Opaque here; the
            download stage needs the object, not a copy of its fields.
        doc_id: reMarkable document id.
        name: Display title.
        folder: Folder path, ``" / "``-joined, empty at the root.
        source: Registered source name — ``"notebook"``, ``"pdf"``, ``"epub"``.
        version: What the tablet says the content is, for change detection.
        pending: The destinations that owe this document a publish, as a
            tuple, never a falsy stand-in for "everywhere". A candidate always
            has at least one.
        recipes: The recipe digest per destination ``state_key``, computed to
            make the decision and kept so the publish stage does not recompute
            it — and so a forced run records the *correct* recipe rather than
            an empty one that makes the next ordinary run republish everything.
    """

    item: Any
    doc_id: str
    name: str
    folder: str
    source: str
    version: str
    pending: Tuple["Destination", ...]
    recipes: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Selection:
    """The result of applying criteria to the tablet's listing.

    Attributes:
        to_process: Documents that will be synced, in listing order.
        skipped: One ``(candidate_or_item, reason)`` per excluded document. The
            reason is the real one.
        deferred: Documents that were pending and fell outside ``limit``. A
            distinct outcome, not a lie about being unchanged.
        orphans: Document ids with publications but absent from the listing —
            what a notebook deleted on the tablet looks like.
    """

    to_process: Tuple[Candidate, ...] = ()
    skipped: Tuple[Tuple[Any, str], ...] = ()
    deferred: Tuple[Candidate, ...] = ()
    orphans: Tuple[str, ...] = ()

    @property
    def pending_total(self) -> int:
        """Documents that need work, whether or not the limit lets them run."""
        return len(self.to_process) + len(self.deferred)

    @property
    def considered(self) -> int:
        """Documents this run actually judged.

        Everything the classifier reached, so a document ruled out before it —
        trashed, the wrong type, in another folder — does not inflate the count
        of what the run looked at. A skipped entry is a
        :class:`Candidate` exactly when it got that far.
        """
        return self.pending_total + sum(
            1 for item, _ in self.skipped if isinstance(item, Candidate)
        )


def select(
    listing: Sequence[Any],
    criteria: SelectionCriteria,
    store: "StateStore",
    destinations: Sequence["Destination"],
    *,
    settings: Settings,
    client: Any = None,
) -> Selection:
    """Decide what a run would do. The only function that decides this.

    Called by the sync run and by the preview that predicts it, with the same
    arguments. That shared call is the only thing guaranteeing the two agree;
    do not give the preview a lighter query because it needs less, because the
    two will drift the first time the rule changes.

    Args:
        listing: Everything the transport reported, documents and folders.
        criteria: What narrows the run.
        store: Where publications are recorded.
        destinations: The enabled destinations, already preflighted.
        settings: The run's resolved settings, for the recipe.
        client: The transport, for the authoritative document type. Pass it
            whenever there is one — without it the type is guessed from the
            title, and a PDF that does not say ``.pdf`` gets the notebook
            renderer's version baked into a digest that is wrong but stable,
            so a genuine PDF renderer bump would never flip it pending.

    Returns:
        The selection, with every excluded document accounted for.
    """
    id_map = {get_val(item, "ID"): item for item in listing}
    recipes = _RecipeCache(destinations, settings)

    candidates: List[Candidate] = []
    skipped: List[Tuple[Any, str]] = []

    for item in listing:
        if get_val(item, "Type") != "DocumentType" or not document_name(item):
            # A folder is not a document that was skipped; it is not a
            # document. Reporting it would bury the real skips.
            continue

        reason = _out_of_scope(item, id_map, criteria, client)
        if reason:
            skipped.append((item, reason))
            continue

        candidate = _classify(item, id_map, criteria, store, destinations, recipes, client)
        if candidate.pending:
            candidates.append(candidate)
        else:
            skipped.append((candidate, UNCHANGED))

    if criteria.tags:
        candidates, dropped = _keep_tagged(candidates, criteria.tags, client)
        skipped.extend(dropped)

    limit = criteria.limit
    if limit is not None and limit > 0:
        to_process, deferred = candidates[:limit], candidates[limit:]
    else:
        to_process, deferred = candidates, []

    return Selection(
        to_process=tuple(to_process),
        skipped=tuple(skipped),
        deferred=tuple(deferred),
        orphans=_orphans(store, id_map),
    )


def _out_of_scope(
    item: Any,
    id_map: Dict[str, Any],
    criteria: SelectionCriteria,
    client: Any,
) -> Optional[str]:
    """Return why this document is not a candidate, or None if it is.

    Ordered cheapest first, and the tag filter is not here at all: reading tags
    costs a round trip per document, so it runs once over the survivors.

    Args:
        item: A document from the listing.
        id_map: Every listed item by id, for resolving the folder path.
        criteria: What narrows the run.
        client: The transport, or None.

    Returns:
        One of the reason constants, or None.
    """
    if is_trashed(item, id_map):
        return TRASHED

    if criteria.target:
        if not matches_notebook_target(item, criteria.target, id_map):
            return NOT_TARGETED
        # A targeted run is the user naming one document. It overrides the
        # type and folder filters, which exist to narrow a sweep.
        return None

    folder = get_notebook_path(item, id_map)
    if _is_excluded(folder, criteria.exclude):
        return EXCLUDED

    # The full path, not the folder: a user filtering on "diary" means the
    # notebook called that as readily as the folder holding it, and the two
    # filters below are documented against the path the flag is named for.
    if criteria.source_path or criteria.source_regex:
        path = document_path(item, id_map)
        if criteria.source_path and criteria.source_path.lower() not in path.lower():
            return OUTSIDE_PATH
        if criteria.source_regex and not re.search(criteria.source_regex, path):
            return OUTSIDE_PATH

    if criteria.types and get_document_type(item, client) not in criteria.types:
        return WRONG_TYPE

    return None


def _is_excluded(folder: str, exclude: FrozenSet[str]) -> bool:
    """Whether a folder path sits under any of the excluded names.

    Matched per segment rather than as a substring: ``Templates`` must not
    also exclude a folder called ``My Templates Archive``, and excluding a
    folder has to exclude everything beneath it.

    Args:
        folder: The document's folder path, ``" / "``-joined.
        exclude: The excluded folder names, however the user cased them.

    Returns:
        True if the document is in or under an excluded folder.
    """
    if not exclude or not folder:
        return False
    wanted = {name.strip().lower() for name in exclude if name.strip()}
    return any(segment.strip().lower() in wanted for segment in folder.split(" / "))


def _classify(
    item: Any,
    id_map: Dict[str, Any],
    criteria: SelectionCriteria,
    store: "StateStore",
    destinations: Sequence["Destination"],
    recipes: "_RecipeCache",
    client: Any,
) -> Candidate:
    """Work out which destinations owe this document a publish.

    ``--force`` overrides the result, it does not skip the comparison: the
    recipe still has to be computed, or a forced run would record an empty one
    and make the next ordinary run republish everything again.

    Args:
        item: A document from the listing.
        id_map: Every listed item by id.
        criteria: What narrows the run.
        store: Where publications are recorded.
        destinations: The enabled destinations.
        recipes: Memoised recipe digests.
        client: The transport, or None.

    Returns:
        The candidate, whose ``pending`` is empty when nothing owes it a
        publish.
    """
    doc_id = get_val(item, "ID")
    version = document_version(item)
    source = get_document_type(item, client)

    pending: List["Destination"] = []
    digests: Dict[str, str] = {}
    for dest in destinations:
        digest = recipes.for_(source, dest)
        digests[dest.state_key] = digest
        if criteria.force or _owes_a_publish(store, doc_id, version, digest, dest):
            pending.append(dest)

    return Candidate(
        item=item,
        doc_id=doc_id,
        name=document_name(item),
        folder=get_notebook_path(item, id_map),
        source=source,
        version=version,
        pending=tuple(pending),
        recipes=digests,
    )


def _owes_a_publish(
    store: "StateStore",
    doc_id: str,
    version: str,
    recipe: str,
    dest: "Destination",
) -> bool:
    """Decide whether one destination is behind on one document.

    Args:
        store: Where publications are recorded.
        doc_id: reMarkable document id.
        version: What the tablet says the content is now.
        recipe: What this run would produce from it.
        dest: The destination being asked about.

    Returns:
        True if the note is missing, out of date, incomplete, or would be
        produced differently now.
    """
    row = store.get_publication(doc_id, dest.state_key)
    if row is None:
        return True
    if row["version"] != version:
        return True
    if (row["recipe"] or "") != recipe:
        return True
    if row["pages_failed"]:
        # A partial publish is a publish, so the row exists and the version
        # matches — without this the gaps would be permanent. The pages that
        # did come back are served from the transcript cache, so retrying a
        # 200-page notebook for three rate-limited pages costs three calls.
        return True
    return not _still_there(dest, doc_id, row)


def _still_there(dest: "Destination", doc_id: str, row: Any) -> bool:
    """Ask a destination whether the note it reported writing is still there.

    Args:
        dest: The destination to ask.
        doc_id: reMarkable document id.
        row: The publication row, for the coordinates it recorded.

    Returns:
        Whether the note is still where it was left. True when the destination
        raises: a broken check must not republish the whole library.
    """
    from living_ink.core.document import PublishContext

    try:
        return dest.published_exists(
            PublishContext(
                doc_id=doc_id,
                existing_external_id=row["external_id"],
                existing_target=row["target"],
            )
        )
    except Exception:
        logger.debug("%s could not check whether its note exists", dest.state_key, exc_info=True)
        return True


def _keep_tagged(
    candidates: Sequence[Candidate],
    wanted: FrozenSet[str],
    client: Any,
) -> Tuple[List[Candidate], List[Tuple[Any, str]]]:
    """Drop candidates carrying none of the requested tags.

    Last, and only when asked for: reading a document's tags is a full SSH
    handshake or one to two HTTPS round trips *each*, so on a 200-notebook
    tablet an eagerly applied tag filter is 200 sequential round trips before a
    single page renders.

    Args:
        candidates: The documents that survived every cheaper filter.
        wanted: Tags to match, any one of which is enough.
        client: The transport. Without one no tag can be read, so nothing is
            dropped — a filter that cannot run must not silently exclude
            everything.

    Returns:
        ``(kept, dropped)``, the second in ``(candidate, reason)`` form.
    """
    if client is None:
        return list(candidates), []

    lowered = {tag.lower() for tag in wanted}
    kept: List[Candidate] = []
    dropped: List[Tuple[Any, str]] = []
    for candidate in candidates:
        try:
            tags = {str(tag).lower() for tag in (client.get_tags(candidate.item) or [])}
        except Exception:
            # A transport that cannot report tags must not turn the filter
            # into a library-wide exclusion.
            logger.debug("could not read tags for %s", candidate.doc_id, exc_info=True)
            kept.append(candidate)
            continue
        if tags & lowered:
            kept.append(candidate)
        else:
            dropped.append((candidate, NO_MATCHING_TAG))
    return kept, dropped


def _orphans(store: "StateStore", id_map: Dict[str, Any]) -> Tuple[str, ...]:
    """Find documents that were published but are no longer on the tablet.

    Args:
        store: Where publications are recorded.
        id_map: Every listed item by id.

    Returns:
        Their document ids, in the order the store reports them.
    """
    return tuple(doc_id for doc_id in store.all_publications() if doc_id not in id_map)


class _RecipeCache:
    """One recipe per (source type, destination), not one per document.

    A run computes at most *types × destinations* distinct digests — three, in
    1.0 — and every one of them involves reading two prompt files. Computing
    them per document would read those files twice per notebook.
    """

    def __init__(self, destinations: Sequence["Destination"], settings: Settings) -> None:
        """Args:
        destinations: The enabled destinations.
        settings: The run's resolved settings.
        """
        self._destinations = {dest.state_key: dest for dest in destinations}
        self._settings = settings
        self._cache: Dict[Tuple[str, str], str] = {}

    def for_(self, source_name: str, dest: "Destination") -> str:
        """Return the recipe for one source type at one destination.

        Args:
            source_name: A registered source name.
            dest: The destination.

        Returns:
            The digest.
        """
        from living_ink.sources import source_for_name

        key = (source_name, dest.state_key)
        if key not in self._cache:
            self._cache[key] = document_recipe(source_for_name(source_name), dest, self._settings)
        return self._cache[key]
