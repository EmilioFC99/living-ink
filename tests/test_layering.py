"""The allowed import edges between packages.

One test per rule, as a list that starts small and grows with each package that
lands. It is the only thing that keeps ``destinations/`` from importing
``core/`` eighteen months from now, by which point the cycle it creates is
load-bearing and nobody remembers why it should not be there.

Module-level imports only: a deliberate function-level import is how a module
reaches across a layer for one value without taking the dependency, and the
rules below are about the dependency.
"""

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "living_ink"


def module_imports(path: Path) -> List[str]:
    """Return the ``living_ink`` modules a file imports at module level.

    Args:
        path: The ``.py`` file to read.

    Returns:
        Dotted module names, without the ``living_ink.`` prefix. An import
        inside a function or a ``TYPE_CHECKING`` block is not module level and
        is not returned.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("living_ink."):
                    found.append(alias.name[len("living_ink.") :])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "living_ink":
                found.extend(alias.name for alias in node.names)
            elif node.module.startswith("living_ink."):
                found.append(node.module[len("living_ink.") :])
    return found


def package_files(name: str) -> Iterator[Tuple[Path, List[str]]]:
    """Yield every module in a package with what it imports.

    Args:
        name: Package directory name under ``src/living_ink``.

    Yields:
        ``(path, imported_modules)`` pairs.
    """
    for path in sorted((PACKAGE_ROOT / name).rglob("*.py")):
        yield path, module_imports(path)


class TestConfigIsTheBottom:
    """Rule 1: ``config/`` imports nothing from the rest of the package.

    Two leaf helpers are excepted. ``safeio`` is how a credential is written
    atomically at 0600 and ``redact`` is how it is kept out of a log line;
    both are a handful of functions over the standard library and neither can
    import ``config`` back, which the second test below is what proves.
    """

    allowed_leaves = {"redact", "safeio"}

    def test_config_imports_no_sibling(self):
        offenders = {
            path.name: [
                name
                for name in imports
                if not name.startswith("config") and name not in self.allowed_leaves
            ]
            for path, imports in package_files("config")
        }
        assert {k: v for k, v in offenders.items() if v} == {}

    def test_the_excepted_leaves_cannot_import_config_back(self):
        for leaf in sorted(self.allowed_leaves):
            imports = module_imports(PACKAGE_ROOT / f"{leaf}.py")
            assert [name for name in imports if name.startswith("config")] == []


class TestDestinationsDoNotReachBack:
    """Rule 2: ``destinations/`` never imports the pipeline.

    The one exception is ``core.document``, which holds the types that travel
    between the two. It is a leaf — it imports nothing itself — so reading it
    is not a dependency on ``core/``, and the alternative is a destination
    that cannot name its own return type.
    """

    allowed_core_modules = {"core.document"}

    def test_destinations_import_only_the_domain_model_from_core(self):
        offenders = {
            path.name: [
                name
                for name in imports
                if name.split(".")[0] == "core" and name not in self.allowed_core_modules
            ]
            for path, imports in package_files("destinations")
        }
        assert {k: v for k, v in offenders.items() if v} == {}

    def test_destinations_import_no_command_layer(self):
        forbidden = {"cli", "ui", "scheduler", "pipeline", "setup_wizard"}
        offenders = {
            path.name: [name for name in imports if name.split(".")[0] in forbidden]
            for path, imports in package_files("destinations")
        }
        assert {k: v for k, v in offenders.items() if v} == {}


class TestSourcesDoNotReachBack:
    """Rule 3: ``sources/`` never imports the pipeline, and imports no renderer.

    A source describes a document format and knows how to turn its pages into
    images. It is called *by* the pipeline, so importing the pipeline back would
    be the cycle; and the pipeline resolves sources from the registry, so there
    is nothing there a renderer needs.

    ``extract`` is the harder case, and the reason it is not in the allowed set
    below. Every shipped renderer calls it — but only from inside a method, for
    two reasons. It would be a cycle: :func:`extract.extract_raw_document_from_zip`
    reads the registry to learn which suffixes count as an original document.
    And it is the §9.6 trap: about fifteen tests monkeypatch
    ``living_ink.extract.render_page_from_document_zip``, which only takes
    effect while the name is looked up on the module at call time. Hoisting
    these imports binds the function at import, the monkeypatch silently stops
    applying, **and the tests keep passing** against the real renderer.
    """

    def test_sources_import_no_command_layer(self):
        forbidden = {"cli", "ui", "scheduler", "pipeline", "setup_wizard", "destinations"}
        offenders = {
            path.name: [name for name in imports if name.split(".")[0] in forbidden]
            for path, imports in package_files("sources")
        }
        assert {k: v for k, v in offenders.items() if v} == {}

    def test_sources_import_no_renderer_at_module_level(self):
        offenders = {
            path.name: [name for name in imports if name.split(".")[0] in {"extract", "api"}]
            for path, imports in package_files("sources")
        }
        assert {k: v for k, v in offenders.items() if v} == {}


class TestCoreIsBelowThePlugins:
    """Rule 4: ``core/`` imports neither the command layer nor a plugin package.

    ``destinations/`` and ``sources/`` both read ``core/`` — that is what rules
    2 and 3 allow — so ``core/`` importing either of them back is the cycle
    those rules exist to prevent. It is also why ``core/recipe.py`` names
    ``Destination`` and ``SourceType`` only under ``TYPE_CHECKING``: what it
    needs from each is one declared attribute, not the module.
    """

    def test_core_imports_no_command_layer_and_no_plugin(self):
        forbidden = {
            "cli",
            "ui",
            "scheduler",
            "pipeline",
            "setup_wizard",
            "destinations",
            "sources",
        }
        offenders = {
            path.name: [name for name in imports if name.split(".")[0] in forbidden]
            for path, imports in package_files("core")
        }
        assert {k: v for k, v in offenders.items() if v} == {}


class TestTheDomainModelIsALeaf:
    """``core/document.py`` imports nothing from Living Ink at all.

    This is what makes the exception above safe: if the domain model ever grows
    an import, the exception stops being one and the cycle it was guarding
    against is back.
    """

    def test_document_imports_nothing_from_the_package(self):
        assert module_imports(PACKAGE_ROOT / "core" / "document.py") == []


class TestTheWorkspaceIsALeaf:
    """``core/temp.py`` imports nothing from Living Ink either.

    ``pipeline`` imports it, so an import back is a cycle. It is also what lets
    a stage, a source or a future renderer be handed a workspace without any of
    them taking a dependency on the pipeline that built it.
    """

    def test_temp_imports_nothing_from_the_package(self):
        assert module_imports(PACKAGE_ROOT / "core" / "temp.py") == []


class TestTheInteractiveLayerIsALeaf:
    """``ui.py`` imports nothing from Living Ink, and is the only home of the widgets.

    Both halves matter. A widget module that imported ``config`` or ``pipeline``
    could not be used by them — and ``cli/app.py`` asks it for the terminal test
    before dispatching anything, which is as close to the bottom of the stack as
    a module gets.

    The second half is the seam itself: ``questionary`` appears in exactly one
    module, so "how does this tool ask a question" has one answer, and a test
    can replace six functions instead of intercepting a library.
    """

    def test_ui_imports_nothing_from_the_package(self):
        assert module_imports(PACKAGE_ROOT / "ui.py") == []

    def test_questionary_is_imported_in_one_place(self):
        importers = sorted(
            path.relative_to(PACKAGE_ROOT).as_posix()
            for path in PACKAGE_ROOT.rglob("*.py")
            if "questionary" in path.read_text(encoding="utf-8")
        )
        assert importers == ["ui.py"]


def cli_self_imports(path: Path) -> List[Tuple[str, str]]:
    """Return the ``living_ink.cli`` names a file imports at module level.

    Args:
        path: The ``.py`` file to read.

    Returns:
        ``(module, name)`` pairs, where ``module`` is the dotted module the
        import came from and ``name`` is the thing taken out of it. A plain
        ``import living_ink.cli.foo`` yields ``("living_ink.cli.foo", "")``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: List[Tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.extend(
                (alias.name, "") for alias in node.names if alias.name.startswith("living_ink.cli")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "living_ink.cli" or node.module.startswith("living_ink.cli."):
                found.extend((node.module, alias.name) for alias in node.names)
    return found


class TestTheFrontEndIsTheTop:
    """Rule 5: nothing in the library imports ``cli/``, and ``cli/`` never imports itself.

    Two directions, one idea. Outward, the front end is the top of the stack:
    a library module that reaches into it has made the CLI a dependency of the
    thing the CLI exists to drive, and `python -m living_ink` is the single
    allowed edge. Inward, ``cli/__init__`` imports every submodule to re-export
    it, so a submodule importing *back* from ``living_ink.cli`` is a cycle that
    resolves only by accident of import order — it must name the submodule that
    defines what it wants.

    The re-export surface is for callers outside the package, and it is also
    why the seams the behaviour tests replace are imported as modules
    (``from living_ink.cli import caches as caches_api``): that is a submodule
    import, not a re-export, and reaching the function through it is what keeps
    a ``monkeypatch`` of ``living_ink.cli.caches.all_caches`` applying.
    """

    def test_no_library_module_imports_the_front_end(self):
        offenders = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if path.name == "__main__.py" or "cli" in path.relative_to(PACKAGE_ROOT).parts:
                continue
            named = [name for name in module_imports(path) if name.split(".")[0] == "cli"]
            if named:
                offenders[str(path.relative_to(PACKAGE_ROOT))] = named
        assert offenders == {}

    def test_a_cli_submodule_never_imports_the_package_itself(self):
        submodules = {
            path.stem if path.name != "__init__.py" else path.parent.name
            for path in (PACKAGE_ROOT / "cli").rglob("*.py")
        }
        offenders = {}
        for path, imported in (
            (path, cli_self_imports(path)) for path in sorted((PACKAGE_ROOT / "cli").rglob("*.py"))
        ):
            if path.name == "__init__.py" and path.parent.name == "cli":
                continue
            named = [
                f"{module}.{name}" if name else module
                for module, name in imported
                if module == "living_ink.cli" and name not in submodules
            ]
            if named:
                offenders[str(path.relative_to(PACKAGE_ROOT))] = named
        assert offenders == {}
