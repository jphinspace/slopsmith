"""Plugin discovery and loading system."""

import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

log = logging.getLogger("slopsmith.plugins")


PLUGINS_DIR = Path(__file__).parent
LOADED_PLUGINS = []
# Guards all mutations of and snapshots from LOADED_PLUGINS so the
# background plugin-loader thread and the event-loop request handlers
# never race on the list.
PLUGINS_LOCK = threading.RLock()

# Persistent pip install location (survives container restarts)
_PIP_TARGET = Path(os.environ.get("CONFIG_DIR", "/config")) / "pip_packages"


def _safe_plugin_id_for_module_name(plugin_id: str) -> str:
    """Bijectively encode a plugin_id for safe use as part of a Python
    module name.

    Plugin ids are opaque manifest values that can take reverse-DNS
    forms (`com.example.foo`) or contain other characters that
    Python's import machinery interprets specially — most
    importantly `.`, which it treats as a package boundary.

    The encoding is **bijective** so distinct plugin_ids always map
    to distinct encoded strings (otherwise two installed plugins
    could share a cache-key prefix and reintroduce the cross-plugin
    collision this PR is fixing). To make `_<hex>_` sequences in
    the output ONLY appear as a result of intentional escapes, the
    underscore is encoded first:

      `_` → `_5f_`   (hex of `_`)
      `.` → `_2e_`   (hex of `.`, applied after the `_` pass)

    With this scheme:
      `foo`            → `foo`
      `foo_bar`        → `foo_5f_bar`
      `foo.bar`        → `foo_2e_bar`
      `foo_2e_bar`     → `foo_5f_2e_5f_bar`  (distinct from `foo.bar`)
      `com.example.x`  → `com_2e_example_2e_x`

    Spotted across multiple Copilot review rounds on PR #105.
    """
    return plugin_id.replace("_", "_5f_").replace(".", "_2e_")


def _load_plugin_sibling(plugin_id: str, plugin_dir: Path, name: str):
    """Load a sibling module from a plugin's directory under a namespaced
    module name (`plugin_<plugin_id>.<name>`, with plugin_id
    bijectively encoded by `_safe_plugin_id_for_module_name` —
    `_` -> `_5f_`, `.` -> `_2e_`). Both single-file siblings
    (`extractor.py`) and package-form siblings (`extractor/__init__.py`)
    are supported; package form wins when both exist (matches CPython's
    import precedence). Mirrors the routes-loading pattern in
    `load_plugins()` and shares its `sys.modules` cache, so two plugins
    that each ship `extractor.py` get distinct cached modules instead
    of stomping each other through `sys.path`. See slopsmith#33."""
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError(
            f"load_sibling: plugin_id must be a non-empty string, got {plugin_id!r}"
        )
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or "." in name
        or name.endswith(".py")
    ):
        # Reject path traversal, the redundant `.py` suffix, and any
        # `.` (the separator between id and name in the cache key).
        raise ValueError(
            f"plugin {plugin_id!r}: load_sibling expects a bare module name, got {name!r}"
        )
    safe_plugin_id = _safe_plugin_id_for_module_name(plugin_id)
    parent_name = f"plugin_{safe_plugin_id}"
    module_name = f"{parent_name}.{name}"

    # Pre-check that the sibling actually exists before we hand off
    # to importlib.import_module — its ModuleNotFoundError is less
    # specific than the message we want to surface (which lists both
    # probed paths so a confused author sees "I checked here AND
    # here").
    file_path = plugin_dir / f"{name}.py"
    pkg_init = plugin_dir / name / "__init__.py"
    if not file_path.is_file() and not pkg_init.is_file():
        raise ImportError(
            f"plugin {plugin_id!r}: no sibling module {name!r} at "
            f"{file_path} or {pkg_init}"
        )

    # Register a synthetic parent package so the standard import
    # machinery can find this plugin's siblings via the parent's
    # `__path__`. The parent points at the plugin's directory; this
    # is what relative imports between siblings consult. It does NOT
    # undermine the namespace isolation, because:
    #   • bare `import sibling` still goes through sys.path (the
    #     transition fallback for plugins that haven't migrated)
    #   • `import plugin_<id>.sibling` lands in the namespaced
    #     sys.modules entry — same key load_sibling produces
    # `setdefault` is atomic under the GIL so two threads racing to
    # create the parent can't overwrite each other's registration.
    # Spotted by codex/Copilot reviews on PRs for slopsmith#33.
    import types
    new_parent = types.ModuleType(parent_name)
    new_parent.__path__ = [str(plugin_dir)]
    sys.modules.setdefault(parent_name, new_parent)

    # Delegate the actual load to importlib.import_module. It uses
    # Python's per-module import lock, so concurrent callers — via
    # load_sibling, relative imports inside another sibling
    # (`from . import extractor`), or an explicit
    # `importlib.import_module('plugin_<id>.<name>')` from anywhere
    # — all serialize through the SAME lock. A rolled-our-own lock
    # could only coordinate load_sibling callers; the standard lock
    # plugs cross-API races where the half-initialized module would
    # otherwise leak. Python's standard finder walks the parent's
    # `__path__`, picks package over file when both exist (matching
    # CPython precedence), exposes the child as an attribute on the
    # parent post-load (`setattr(parent, name, child)`), and cleans
    # up sys.modules on exec failure — all the things this helper
    # used to do by hand. Spotted by Copilot review on PR #105
    # round 5.
    return importlib.import_module(module_name)


def _warn_on_module_collisions(plugin_specs):
    """Scan top-level importable modules across all plugins about to
    be loaded. Print a warning for any module name shipped by 2+
    plugins, since bare `import <name>` from those plugins will hit
    the sys.path-based cache and cross-load (slopsmith#33).

    Both top-level `.py` files AND top-level packages (directories
    containing `__init__.py`) are scanned — the same collision
    pattern applies to either, e.g. one plugin's `extractor.py` vs
    another plugin's `extractor/__init__.py` both produce a shared
    `sys.modules['extractor']` entry. Spotted by codex review on
    PR for slopsmith#33.

    `routes.py` itself is excluded because the loader already
    namespaces it as `plugin_{id}_routes`. Top-level dunder files
    (like a hypothetical bare `__main__.py`) are excluded too.

    `plugin_specs` is a list of `(plugin_id, plugin_dir)` tuples for
    plugins the loader has decided to load (post-dedup).
    """
    # Map: module_name -> {plugin_id: set_of_kinds}.
    # Using a per-plugin nested dict deduplicates the case where ONE
    # plugin ships both `extractor.py` and `extractor/__init__.py`
    # — that intra-plugin layout is supported by load_sibling
    # (package form wins, matching CPython precedence) and shouldn't
    # trip a cross-plugin collision warning. Spotted by codex review
    # on PR for slopsmith#33.
    by_name: dict[str, dict[str, set[str]]] = {}
    for plugin_id, plugin_dir in plugin_specs:
        try:
            for child in plugin_dir.iterdir():
                module_name = None
                kind = None
                if child.is_file() and child.suffix == ".py":
                    if child.name == "routes.py" or child.name.startswith("__"):
                        continue
                    module_name = child.stem
                    kind = "module"
                elif child.is_dir() and (child / "__init__.py").is_file():
                    if child.name.startswith("__"):
                        continue
                    module_name = child.name
                    kind = "package"
                if module_name is None:
                    continue
                by_name.setdefault(module_name, {}).setdefault(plugin_id, set()).add(kind)
        except OSError:
            # Unreadable plugin dir — the per-plugin load below will
            # surface the error in a more useful place; don't warn here.
            continue
    for name, by_plugin in by_name.items():
        # Count distinct plugin ids — only fire when MULTIPLE plugins
        # ship the same module name. A single plugin shipping the
        # name in multiple forms is fine.
        if len(by_plugin) < 2:
            continue
        ids_quoted = ", ".join(f"'{pid}'" for pid in sorted(by_plugin))
        # Aggregate kinds across all plugins to label the warning.
        kinds = {k for kind_set in by_plugin.values() for k in kind_set}
        kind_label = "module/package" if len(kinds) > 1 else next(iter(kinds))
        log.warning(
            "Module-name collision: %r (%s) is shipped by %d plugins (%s). "
            "Bare `import %s` may load the wrong file. "
            "Migrate to context['load_sibling']('%s') — see CLAUDE.md (slopsmith#33).",
            name, kind_label, len(by_plugin), ids_quoted, name, name,
        )


def _is_safe_tour_manifest_filename(val) -> bool:
    """Return True only for non-empty relative tour manifest filenames.

    The filename must not be absolute, must not contain backslashes, must
    not contain any ``..`` path segment, and must not be a bare ``.`` (which
    would resolve to the plugin directory itself) so ``has_tour`` only
    advertises files that are eligible to be served by the route handler.
    """
    if not isinstance(val, str) or not val:
        return False
    if "\\" in val or os.path.isabs(val):
        return False
    p = Path(val)
    if ".." in p.parts:
        return False
    # Reject "." and any path whose final component is "." (e.g. "./").
    return p.name != '.'


def _is_valid_tour_manifest(val) -> bool:
    """Return True only when the tour manifest field is a usable string
    filename or a dict.  A dict without a ``file`` key is valid — the route
    handler defaults it to ``"tour.json"``.  A dict that explicitly sets
    ``file`` must use a non-empty safe relative string (``file: null``,
    ``file: 1``, ``file: "../x.json"``, absolute paths, etc. are rejected).
    Empty strings, non-string scalars (e.g. ``true``, ``1``), and dicts with
    an explicitly invalid ``file`` value are treated as absent.
    """
    if isinstance(val, str):
        return _is_safe_tour_manifest_filename(val)
    if isinstance(val, dict):
        if "file" not in val:
            return True  # no file key → defaults to "tour.json" in the route
        return _is_safe_tour_manifest_filename(val["file"])
    return False


def _normalize_export_paths(settings_field, plugin_id: str) -> list[str]:
    """Validate and normalize a plugin's `settings.server_files` manifest
    list into clean POSIX-style relpaths suitable for the settings
    export/import bundle (slopsmith#113).

    Each entry must be a non-empty string with no absolute prefix and
    no `..` segment. A trailing `/` denotes a directory (recurse on
    export). Invalid entries are dropped with a `[Plugin]` warning so
    a bad manifest can't smuggle a path-traversal opportunity into
    the importer's allowlist.

    Returns a list of normalized strings. Returns `[]` when the
    manifest doesn't declare any exportable server files (the common
    case — most plugins keep state purely in localStorage).
    """
    if not isinstance(settings_field, dict):
        return []
    raw = settings_field.get("server_files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning(
            "Plugin %r: settings.server_files must be a list, got %s; ignoring",
            plugin_id, type(raw).__name__,
        )
        return []

    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            log.warning(
                "Plugin %r: dropping non-string / empty server_files entry %r",
                plugin_id, entry,
            )
            continue
        # Loader rules mirror what `_validate_relpath` enforces at import
        # time, so any entry that passes here is guaranteed to round-trip
        # through export and back through import. Surfacing whitespace /
        # `.` / dotfile entries as warnings beats silently producing a
        # bundle that the same server later refuses to ingest.
        if entry != entry.strip():
            log.warning(
                "Plugin %r: dropping server_files entry with leading/trailing whitespace %r",
                plugin_id, entry,
            )
            continue
        # Reject absolute paths, drive letters, and any backslash-
        # separated form before splitting — the importer treats the
        # allowlist as POSIX strings, so accepting `foo\bar` here would
        # let a malicious manifest sidestep traversal detection on
        # platforms whose `Path` accepts both separators.
        if "\\" in entry:
            log.warning(
                "Plugin %r: server_files entry must use POSIX separators, dropping %r",
                plugin_id, entry,
            )
            continue
        # Strip a single trailing slash for the traversal check, then
        # re-attach it so the export walker can still detect "this is
        # a directory" from the normalized form.
        is_dir = entry.endswith("/")
        body = entry.rstrip("/")
        if not body:
            log.warning("Plugin %r: dropping empty server_files entry", plugin_id)
            continue
        parts = body.split("/")
        if (
            body.startswith("/")
            or (len(body) >= 2 and body[1] == ":")  # Windows drive letter
            or any(part in ("", ".", "..") for part in parts)
            or parts[0].startswith(".")
        ):
            log.warning(
                "Plugin %r: dropping unsafe server_files entry %r "
                "(absolute / traversal / dotfile / empty segment)",
                plugin_id, entry,
            )
            continue
        cleaned.append(body + ("/" if is_dir else ""))
    return cleaned


def _normalize_diagnostics_paths(diagnostics_field, plugin_id: str) -> list[str]:
    """Validate and normalize a plugin's `diagnostics.server_files`
    manifest list. Mirrors `_normalize_export_paths` semantics — the
    diagnostics export reads files using the same allowlist rules so
    every entry that passes here is safe for the bundle assembler to
    open without re-validating. Returns `[]` when the manifest doesn't
    declare any diagnostic files.
    """
    if not isinstance(diagnostics_field, dict):
        return []
    raw = diagnostics_field.get("server_files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning(
            "Plugin %r: diagnostics.server_files must be a list, got %s; ignoring",
            plugin_id, type(raw).__name__,
        )
        return []
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            log.warning(
                "Plugin %r: dropping non-string / empty diagnostics.server_files entry %r",
                plugin_id, entry,
            )
            continue
        if entry != entry.strip():
            log.warning(
                "Plugin %r: dropping diagnostics.server_files entry with leading/trailing whitespace %r",
                plugin_id, entry,
            )
            continue
        if "\\" in entry:
            log.warning(
                "Plugin %r: diagnostics.server_files entry must use POSIX separators, dropping %r",
                plugin_id, entry,
            )
            continue
        is_dir = entry.endswith("/")
        body = entry.rstrip("/")
        if not body:
            log.warning("Plugin %r: dropping empty diagnostics.server_files entry", plugin_id)
            continue
        parts = body.split("/")
        if (
            body.startswith("/")
            or (len(body) >= 2 and body[1] == ":")
            or any(part in ("", ".", "..") for part in parts)
            or parts[0].startswith(".")
        ):
            log.warning(
                "Plugin %r: dropping unsafe diagnostics.server_files entry %r "
                "(absolute / traversal / dotfile / empty segment)",
                plugin_id, entry,
            )
            continue
        cleaned.append(body + ("/" if is_dir else ""))
    return cleaned


def _parse_diagnostics_callable(diagnostics_field, plugin_id: str) -> str | None:
    """Validate `diagnostics.callable` shape (`"<module>:<function>"`)
    and return the literal spec string. Resolution happens lazily when
    the export endpoint actually needs the callable, so a missing
    sibling at load time doesn't fail plugin registration.
    """
    if not isinstance(diagnostics_field, dict):
        return None
    spec = diagnostics_field.get("callable")
    if spec is None:
        return None
    if not isinstance(spec, str) or ":" not in spec:
        log.warning(
            "Plugin %r: diagnostics.callable must be a string of the form "
            "'<module>:<function>', got %r; ignoring",
            plugin_id, spec,
        )
        return None
    module_name, _, fn_name = spec.partition(":")
    if not module_name or not fn_name:
        log.warning(
            "Plugin %r: diagnostics.callable %r is missing module or function name; ignoring",
            plugin_id, spec,
        )
        return None
    # Validate module_name matches load_sibling() constraints: bare name,
    # no dots, no slashes, no .py suffix.  A malformed spec would only
    # surface as an error at export time; reject it here consistently.
    if (
        "/" in module_name
        or "\\" in module_name
        or "." in module_name
        or module_name.endswith(".py")
    ):
        log.warning(
            "Plugin %r: diagnostics.callable module %r must be a bare "
            "module name (no dots, slashes, or .py suffix); ignoring",
            plugin_id, module_name,
        )
        return None
    return spec


def _install_requirements(plugin_dir: Path, plugin_id: str):
    """Install plugin requirements.txt to a persistent location."""
    req_file = plugin_dir / "requirements.txt"
    if not req_file.exists():
        return True

    _PIP_TARGET.mkdir(parents=True, exist_ok=True)
    pip_target = str(_PIP_TARGET)

    # Add to sys.path if not already there
    if pip_target not in sys.path:
        sys.path.insert(0, pip_target)

    # Check if already installed (marker file). Use a deterministic
    # digest — Python's built-in hash() is randomised per process
    # (PYTHONHASHSEED), so the marker would never match on restart and
    # pip would re-resolve every plugin's requirements on every boot.
    marker = _PIP_TARGET / f".installed_{plugin_id}"
    req_hash = hashlib.sha256(req_file.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text().strip() == req_hash:
        return True  # Already installed, same requirements

    log.info("Installing requirements for plugin %r (this can take a while for large deps)...", plugin_id)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--target", pip_target,
             "--quiet",
             "-r", str(req_file)],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode == 0:
            marker.write_text(req_hash)
            log.info("Requirements installed for plugin %r", plugin_id)
            return True
        else:
            err_lower = result.stderr.lower() if result.stderr else ""
            if "read-only" in err_lower or "permission denied" in err_lower:
                log.warning(
                    "Plugin %r: optional dependencies not installed — "
                    "functionality may be limited. Install dependencies manually "
                    "or configure an external service if available.",
                    plugin_id,
                )
            else:
                log.warning("Plugin %r: failed to install requirements: %s", plugin_id, result.stderr[:300])
            return False
    except Exception as e:
        err_lower = str(e).lower()
        if "read-only" in err_lower or "permission denied" in err_lower:
            log.warning(
                "Plugin %r: optional dependencies not installed — "
                "functionality may be limited. Install dependencies manually "
                "or configure an external service if available.",
                plugin_id,
            )
        else:
            log.warning("Plugin %r: error installing requirements: %s", plugin_id, e)
        return False


def load_plugins(app: FastAPI, context: dict, progress_cb=None, route_setup_fn=None):
    """Discover and load all plugins from built-in and user directories.

    progress_cb, when provided, receives structured progress events:
    {
      "phase": "<phase-id>",
      "message": "<human text>",
      "plugin_id": "<id or ''>",
      "loaded": <int>,
      "total": <int>,
      "error": "<optional error text>"
    }

    route_setup_fn, when provided, is called instead of directly invoking
    `routes_module.setup(app, ctx)`.  Callers that load plugins from a
    background thread can pass a hook that marshals the call back to the
    main thread (e.g. via loop.call_soon_threadsafe) to keep FastAPI/
    Starlette router mutation on the event-loop thread.

    Signature: route_setup_fn(fn: Callable[[], None]) -> None
    where `fn` is a zero-argument callable that performs the setup call.
    """

    def _emit_progress(phase: str, message: str, plugin_id: str = "", loaded: int = 0,
                       total: int = 0, error: str | None = None,
                       clear_error: bool = False):
        if not progress_cb:
            return
        try:
            event: dict = {
                "phase": phase,
                "message": message,
                "plugin_id": plugin_id,
                "loaded": loaded,
                "total": total,
            }
            # Include the error key only when meaningful:
            # - A non-null error string sets/updates the error field.
            # - clear_error=True sends an explicit null to clear a
            #   previously-reported error (e.g. bundled failure cleared
            #   by a successful user-copy fallback). Downstream handlers
            #   must check `"error" in event`, not `event.get("error") is
            #   not None`, to receive the clear signal.
            # - No error kwarg → key is omitted; downstream preserves
            #   any previously-reported error across non-error events.
            if error is not None:
                event["error"] = error
            elif clear_error:
                event["error"] = None
            progress_cb(event)
        except Exception:
            # Progress reporting must never break plugin startup.
            pass

    # Collect plugin directories — user plugins first so they override built-in
    plugin_dirs = []
    user_plugins_dir = os.environ.get("SLOPSMITH_PLUGINS_DIR")
    if user_plugins_dir:
        user_path = Path(user_plugins_dir)
        if user_path.is_dir() and user_path != PLUGINS_DIR:
            plugin_dirs.append(user_path)
    if PLUGINS_DIR.is_dir():
        plugin_dirs.append(PLUGINS_DIR)

    if not plugin_dirs:
        _emit_progress("plugins-complete", "No plugin directories found", loaded=0, total=0)
        return

    # Add persistent pip target to sys.path
    pip_target = str(_PIP_TARGET)
    if _PIP_TARGET.exists() and pip_target not in sys.path:
        sys.path.insert(0, pip_target)

    loaded_ids = set()
    # id → (plugin_id, plugin_dir, manifest) for the *kept* copy of each
    # plugin id. Used by the duplicate-skip path to log a useful
    # "user copy at X overriding bundled core plugin at Y" message
    # instead of a generic "skipping duplicate" line. Mirrors loaded_ids
    # in lifetime; both are local to this discovery pass.
    loaded_specs_by_id: dict[str, tuple] = {}
    # Maps plugin_id → evicted user spec (plugin_id, plugin_dir, manifest).
    # Populated when a bundled plugin evicts a user-installed copy. Used as
    # a fallback: if the bundled copy later fails to load its routes, the
    # user copy is restored so the server remains functional.
    _pending_evictions: dict[str, tuple] = {}
    # Maps plugin_id → set of sys.modules keys that were NEW during the
    # failed bundled route load. Bundled routes may import helpers under
    # bare names (e.g. `import helper`); these survive the namespaced
    # _parent_pkg cleanup and would resolve to bundled code if the fallback
    # plugin also uses bare imports. Purging them gives the fallback a
    # clean import slate (Thread 1, review-4226783807).
    _pending_eviction_stale_modules: dict[str, set] = {}

    def _is_bundled(pdir: Path, mf: dict) -> bool:
        """Return True iff pdir is the real in-tree bundled core plugin.

        Requires ALL THREE of:
        - Located directly in PLUGINS_DIR (pdir.parent == PLUGINS_DIR)
        - Manifest carries ``"bundled": true``
        - Directory name matches the plugin id (pdir.name == mf.get("id"))

        The directory-name check distinguishes the real in-tree copy from a
        verbatim user copy placed in plugins/ under a different folder name
        but still carrying ``"bundled": true`` from the source manifest.
        Neither the directory location alone nor the manifest field alone is
        sufficient — a user plugin cloned into plugins/ would pass the first
        check, and a user plugin could forge the second. The name check ties
        the directory to the specific plugin id so only the canonical
        ``plugins/<id>/`` location passes all three.
        """
        return (
            pdir.parent == PLUGINS_DIR
            and bool(mf.get("bundled"))
            and pdir.name == mf.get("id")
        )

    # Two-pass discovery so we can warn about cross-plugin module-name
    # collisions BEFORE any plugin's setup runs (slopsmith#33). The
    # first pass collects (plugin_id, plugin_dir, manifest) tuples in
    # load order; the second pass actually executes each plugin's
    # setup with a per-plugin context.
    plugin_load_specs = []
    for plugins_base_dir in plugin_dirs:
        for plugin_dir in sorted(plugins_base_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Failed to read plugin manifest %s: %s", manifest_path, e)
                continue
            plugin_id = manifest.get("id")
            if plugin_id is None:
                # No `id` key at all — silently skip (existing
                # behavior; manifests without an id were never
                # meant to be valid).
                continue
            # Type-check BEFORE the empty check: falsy non-string
            # values (`{"id": 0}`, `{"id": []}`) should produce the
            # explicit "must be a string" warning, not be silently
            # dropped. Spotted by Copilot review on PR #105 round 4.
            if not isinstance(plugin_id, str):
                log.warning(
                    "Skipping %s: 'id' must be a string, got %s (%r)",
                    manifest_path, type(plugin_id).__name__, plugin_id,
                )
                continue
            if not plugin_id:
                # Empty-string id — silently skip (matches the
                # original `if not plugin_id: continue` semantics
                # for empty strings).
                continue
            if plugin_id in loaded_ids:
                # Duplicate id — pick a winner. Bundled plugins always win.
                # `loaded_specs_by_id` records the already-seen copy; this
                # is the new candidate. Use specific log messages so it's
                # obvious which copy wins and why.
                kept = loaded_specs_by_id.get(plugin_id)
                # Bundled-ness requires ALL THREE: the in-tree PLUGINS_DIR
                # location, the manifest's ``"bundled": true`` flag, AND
                # the directory name matching the manifest id. See the
                # ``_is_bundled`` helper defined above for the full contract.
                this_is_bundled = _is_bundled(plugin_dir, manifest)
                kept_is_bundled = _is_bundled(kept[1], kept[2]) if kept else False
                if this_is_bundled and not kept_is_bundled:
                    # The incoming copy is the canonical bundled plugin; the
                    # already-kept copy is user-installed (SLOPSMITH_PLUGINS_DIR
                    # or cloned directly into plugins/). Bundled always wins —
                    # evict the user copy and fall through to register the
                    # bundled version instead.
                    #
                    # Store the evicted spec as a potential fallback: if the
                    # bundled copy later fails to load its routes, the server
                    # restores this user copy so it keeps working.
                    _pending_evictions[plugin_id] = kept
                    log.warning(
                        "User-installed copy of bundled plugin %r at %s ignored; "
                        "using bundled version at %s.",
                        plugin_id, kept[1] if kept else "(unknown)", plugin_dir,
                    )
                    # Replace the user copy's slot in-place so the bundled
                    # copy inherits the same discovery position.  Removing and
                    # re-appending would shift the bundled entry to the end of
                    # plugin_load_specs, changing /api/plugins order and the
                    # frontend playSong wrapper chain.
                    _user_slot = next(
                        (i for i, s in enumerate(plugin_load_specs) if s[0] == plugin_id),
                        None,
                    )
                    _bundled_spec = (plugin_id, plugin_dir, manifest)
                    if _user_slot is not None:
                        plugin_load_specs[_user_slot] = _bundled_spec
                    else:
                        plugin_load_specs.append(_bundled_spec)
                    loaded_specs_by_id[plugin_id] = _bundled_spec
                    continue  # loaded_ids already contains plugin_id
                elif this_is_bundled and kept_is_bundled:
                    # Two bundled plugins share an id — shouldn't happen in a
                    # well-maintained tree, but emit a clear warning so it
                    # doesn't pass silently.
                    log.warning(
                        "Skipping duplicate bundled plugin %r at %s (already registered from %s)",
                        plugin_id, plugin_dir, kept[1] if kept else "(unknown)",
                    )
                    continue
                elif kept_is_bundled:
                    # A non-bundled (user) copy encountered after an already-kept
                    # bundled copy. Bundled always wins — discard the user copy.
                    # Store as a potential fallback: if the bundled copy later
                    # fails to load its routes, the server restores this user copy
                    # so it keeps working. Only the first user copy encountered is
                    # kept as the fallback (subsequent duplicates are dropped).
                    if plugin_id not in _pending_evictions:
                        _pending_evictions[plugin_id] = (plugin_id, plugin_dir, manifest)
                    log.warning(
                        "User-installed copy of bundled plugin %r at %s ignored; "
                        "using bundled version at %s.",
                        plugin_id, plugin_dir, kept[1] if kept else "(unknown)",
                    )
                    continue
                else:
                    log.warning("Skipping duplicate plugin %r at %s", plugin_id, plugin_dir)
                    continue
            loaded_ids.add(plugin_id)
            plugin_load_specs.append((plugin_id, plugin_dir, manifest))
            loaded_specs_by_id[plugin_id] = (plugin_id, plugin_dir, manifest)

    # Warn before loading so authors see the message even if a colliding
    # plugin's setup itself blows up later in the loop.
    _warn_on_module_collisions(
        [(plugin_id, plugin_dir) for plugin_id, plugin_dir, _ in plugin_load_specs]
    )

    _emit_progress(
        "plugins-discovered",
        f"Discovered {len(plugin_load_specs)} plugin(s)",
        loaded=0,
        total=len(plugin_load_specs),
    )

    # Accumulate into a local list and publish atomically once all
    # plugins are loaded, so concurrent readers never see a partially
    # populated LOADED_PLUGINS.
    _loaded_batch: list = []
    # Track plugin_ids whose routes.setup() raised an exception, so we
    # can fall back to evicted user copies for those plugin_ids below.
    _route_failed_ids: set[str] = set()
    # Track plugin_ids whose bundled setup() timed out while already
    # running (mid-flight). For those, activating the fallback is unsafe
    # because the original setup() may still be mutating the router
    # concurrently — fallback routes would mount on top of partial bundled
    # routes, producing duplicate or conflicting endpoints.
    _route_mid_flight_ids: set[str] = set()

    for idx, (plugin_id, plugin_dir, manifest) in enumerate(plugin_load_specs):
        _emit_progress(
            "plugin-start",
            f"Loading plugin '{plugin_id}'",
            plugin_id=plugin_id,
            loaded=idx,
            total=len(plugin_load_specs),
        )

        # Install plugin requirements if present
        _emit_progress(
            "plugin-requirements",
            f"Installing requirements for '{plugin_id}' (if needed)",
            plugin_id=plugin_id,
            loaded=idx,
            total=len(plugin_load_specs),
        )
        req_ok = _install_requirements(plugin_dir, plugin_id)
        if not req_ok:
            _emit_progress(
                "plugin-error",
                f"Failed to install requirements for '{plugin_id}'",
                plugin_id=plugin_id,
                loaded=idx,
                total=len(plugin_load_specs),
                error="Requirements installation failed; check server logs for details",
            )

        # Add plugin directory to sys.path so the plugin's bare
        # `import sibling` keeps working during the slopsmith#33
        # transition. New plugins should prefer
        # `context['load_sibling']('sibling')` instead — see
        # CLAUDE.md / Plugin System / Backend routes.
        plugin_dir_str = str(plugin_dir)
        if plugin_dir_str not in sys.path:
            sys.path.insert(0, plugin_dir_str)

        # Build a per-plugin context: dict-copy the shared mapping
        # so plugin A re-binding `ctx['x']` doesn't leak into plugin
        # B's view, then add a `load_sibling` closure scoped to THIS
        # plugin's id + dir. (Note: the COPY is shallow — values
        # stored in context are still the same objects across
        # plugins, so e.g. `ctx['meta_db']` mutations are still
        # observable everywhere by design.) The helper namespaces
        # sibling modules as `plugin_<id>.<name>` (with plugin_id
        # bijectively encoded by _safe_plugin_id_for_module_name:
        # `_` -> `_5f_`, `.` -> `_2e_`) so two plugins shipping the
        # same filename get distinct cached modules. See
        # slopsmith#33.
        plugin_context = dict(context)
        plugin_context["load_sibling"] = (
            lambda name, _pid=plugin_id, _pdir=plugin_dir:
                _load_plugin_sibling(_pid, _pdir, name)
        )
        plugin_context["log"] = logging.getLogger(f"slopsmith.plugin.{plugin_id}")

        # Load routes using importlib to avoid module name collisions
        routes_file = manifest.get("routes")
        if routes_file:
            _emit_progress(
                "plugin-routes",
                f"Loading routes for '{plugin_id}'",
                plugin_id=plugin_id,
                loaded=idx,
                total=len(plugin_load_specs),
            )
            # Capture the current route count so we can detect whether
            # setup() registered any handlers before raising. FastAPI has
            # no route-removal API, so partial registration is permanent.
            _routes_before = len(getattr(app, "routes", []))
            try:
                # Escape `.` in plugin_id the same way load_sibling
                # does. Without it, a plugin id like
                # `com.example.foo` would land at
                # `plugin_com.example.foo_routes` — which Python
                # parses as a dotted module path, sets
                # `__package__` to `plugin_com.example`, and breaks
                # any relative imports inside routes.py. Spotted by
                # Copilot review on PR #105 round 2.
                module_name = f"plugin_{_safe_plugin_id_for_module_name(plugin_id)}_routes"
                spec = importlib.util.spec_from_file_location(
                    module_name, str(plugin_dir / routes_file))
                routes_module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = routes_module
                spec.loader.exec_module(routes_module)
                if hasattr(routes_module, "setup"):
                    if route_setup_fn is not None:
                        # Bind routes_module and plugin_context by value so
                        # the callable is safe regardless of when/how
                        # route_setup_fn dispatches it — avoids late-binding
                        # closure bugs if the caller defers execution.
                        _fn = lambda rm=routes_module, ctx=plugin_context: rm.setup(app, ctx)
                        _fn._plugin_id = plugin_id
                        route_setup_fn(_fn)
                    else:
                        routes_module.setup(app, plugin_context)
                    log.info("Loaded routes for plugin %r", plugin_id)
            except Exception as e:
                log.exception("Failed to load routes for plugin %r", plugin_id)
                _route_failed_ids.add(plugin_id)
                # If this was a mid-flight timeout, mark the plugin so the
                # fallback block skips it — the original setup() may still be
                # running and registering routes concurrently; mounting a
                # fallback on top would produce duplicate/conflicting endpoints.
                if getattr(e, "setup_mid_flight", False):
                    _route_mid_flight_ids.add(plugin_id)
                # Detect partial route registration: if setup() mounted any
                # handlers before raising, those routes stay permanently (no
                # FastAPI deregistration API). Warn loudly so maintainers can
                # identify conflicting endpoints in the server log.
                _routes_after = len(getattr(app, "routes", []))
                if _routes_after > _routes_before:
                    log.warning(
                        "Plugin %r registered %d route(s) before its setup() raised; "
                        "these handlers cannot be removed and may conflict with any fallback.",
                        plugin_id, _routes_after - _routes_before,
                    )
                # Compute bare-import modules added during the failed load.
                # IMPORTANT: Filter strictly to modules whose __file__ lives
                # inside this plugin's directory — the naive set-diff would
                # capture every module imported by any concurrent thread
                # (metadata scan, stdlib lazy imports, etc.) between the
                # snapshot and the failure, causing the fallback block to
                # delete unrelated entries from sys.modules.
                # Purge them NOW (not deferred) so subsequent plugins in the
                # main loop don't accidentally resolve this plugin's helpers
                # when they do bare `import helper`.  The fallback block's
                # per-key pop() is a harmless no-op when the keys are already
                # absent.
                if plugin_id in _pending_evictions:
                    _plugin_dir_prefix = str(plugin_dir) + os.sep
                    _stale = set()
                    # Scan ALL cached modules (not only those newly added since
                    # the snapshot) for any whose __file__ lives inside this
                    # plugin's directory.  A previous load_plugins() call (test
                    # reload, dev restart) may have left same-named helpers from
                    # the bundled copy in sys.modules before this run's
                    # snapshot was taken; diffing against the snapshot alone
                    # would miss those and let the fallback copy resolve the
                    # old bundled code on repeated loads.
                    for _k, _mod in list(sys.modules.items()):
                        _mf = getattr(_mod, "__file__", None)
                        if _mf and str(_mf).startswith(_plugin_dir_prefix):
                            _stale.add(_k)
                    _pending_eviction_stale_modules[plugin_id] = _stale
                    # Purge immediately to prevent module leakage into later plugins.
                    for _k in _stale:
                        sys.modules.pop(_k, None)
                _emit_progress(
                    "plugin-error",
                    f"Failed loading routes for '{plugin_id}'",
                    plugin_id=plugin_id,
                    loaded=idx,
                    total=len(plugin_load_specs),
                    error=str(e),
                )

        _loaded_batch.append({
            "id": plugin_id,
            "name": manifest.get("name", plugin_id),
            "nav": manifest.get("nav"),
            # `type` is an optional manifest hint for the frontend —
            # e.g. "visualization" lets the highway viz picker know
            # this plugin offers a renderer. Absent → no declared
            # role; plugin is still loaded and scripts run, it just
            # doesn't show up in role-specific UIs. See slopsmith#36.
            "type": manifest.get("type"),
            # Uses the same _is_bundled() helper as the duplicate-skip path.
            # See the helper's docstring for the three-part check that prevents
            # user-installed plugins (cloned into plugins/ or carrying a forged
            # "bundled": true manifest field) from being misidentified as core.
            # Surfaced in /api/plugins so the plugin-list UI can render a
            # 'Bundled' badge next to the plugin name in the settings collapsible.
            "bundled": _is_bundled(plugin_dir, manifest),
            "has_screen": bool(manifest.get("screen")),
            "has_script": bool(manifest.get("script")),
            "has_settings": bool(manifest.get("settings")),
            "has_tour": _is_valid_tour_manifest(manifest.get("tour")),
            # Normalized list of relpaths under CONFIG_DIR that this
            # plugin opts in to settings export/import. Empty for
            # plugins that don't declare `settings.server_files`. See
            # slopsmith#113.
            "_export_paths": _normalize_export_paths(manifest.get("settings"), plugin_id),
            # Diagnostics opt-in (slopsmith#166): same allowlist semantics
            # as `_export_paths` but for the troubleshooting bundle.
            "_diagnostics_paths": _normalize_diagnostics_paths(manifest.get("diagnostics"), plugin_id),
            "_diagnostics_callable_spec": _parse_diagnostics_callable(manifest.get("diagnostics"), plugin_id),
            "_load_sibling": plugin_context["load_sibling"],
            "_dir": plugin_dir,
            "_manifest": manifest,
        })
        log.info("Registered plugin %r (%s)", plugin_id, manifest.get("name", ""))
        _emit_progress(
            "plugin-registered",
            f"Registered plugin '{plugin_id}'",
            plugin_id=plugin_id,
            loaded=idx + 1,
            total=len(plugin_load_specs),
        )

    # If any bundled plugin failed to load its routes AND it evicted a
    # user-installed copy during discovery, fall back to that user copy so
    # the server remains functional. A bad bundled release should never
    # leave the plugin completely broken when a working user copy exists.
    #
    # NOTE on partial-registration: if the bundled setup() managed to register
    # some FastAPI routes before raising, those handlers stay permanently (no
    # route-removal API). The partial-registration warning above names the
    # count; the fallback copy's routes then mount alongside them, so duplicate
    # or conflicting endpoints are possible. This is an accepted limitation;
    # the primary mitigation is thorough testing of bundled releases.
    #
    # NOTE on timeout race: in async mode the bundled setup() runs on the
    # event-loop thread via route_setup_fn. If it times out (>60 s) while
    # setup() has ALREADY STARTED executing, `_route_mid_flight_ids` is set
    # for that plugin and the fallback is skipped — the original setup() may
    # still be mutating the router concurrently and mounting a second set of
    # routes on top would produce duplicate/conflicting endpoints. The
    # mid-flight case is detected by the `setup_mid_flight` attribute on the
    # TimeoutError re-raised by _route_setup_on_main (server.py).
    # If the timeout fires BEFORE _do() has started, the _cancelled flag
    # in _route_setup_on_main prevents the queued callback from executing,
    # making the fallback safe in that case.
    for evicted_id, evicted_spec in _pending_evictions.items():
        if evicted_id not in _route_failed_ids:
            continue
        if evicted_id in _route_mid_flight_ids:
            log.warning(
                "Skipping fallback for %r: bundled setup() timed out while already "
                "executing; the router may have partial routes from the bundled copy. "
                "Restart the server to recover.",
                evicted_id,
            )
            # Remove the broken bundled entry too — its routes are in an
            # unknown state and it should not appear in /api/plugins.
            _loaded_batch[:] = [e for e in _loaded_batch if e["id"] != evicted_id]
            continue
        _ev_id, ev_dir, ev_manifest = evicted_spec
        log.warning(
            "Bundled plugin %r failed to load routes; "
            "falling back to user-installed copy at %s.",
            evicted_id, ev_dir,
        )
        # Remove the broken bundled entry from the batch, recording its
        # original position so the fallback is inserted there instead of
        # being appended at the end (appending would change the order of
        # plugins in /api/plugins and the frontend playSong wrapper chain).
        _bundled_orig_idx = next(
            (i for i, e in enumerate(_loaded_batch) if e["id"] == evicted_id),
            len(_loaded_batch),
        )
        _loaded_batch[:] = [e for e in _loaded_batch if e["id"] != evicted_id]
        # Ensure the fallback directory is at the FRONT of sys.path so
        # its modules take priority over any bundled copy still present.
        # Simply inserting when absent is not enough: on repeated
        # load_plugins() calls (tests, dev reloads) the user-copy dir may
        # already be in sys.path but behind the bundled dir from an earlier
        # run, letting bare imports in the fallback still resolve bundled
        # files. Always remove-then-reinsert to guarantee front-of-path.
        ev_dir_str = str(ev_dir)
        if ev_dir_str in sys.path:
            sys.path.remove(ev_dir_str)
        sys.path.insert(0, ev_dir_str)
        ev_context = dict(context)
        ev_context["load_sibling"] = (
            lambda name, _pid=evicted_id, _pdir=ev_dir:
                _load_plugin_sibling(_pid, _pdir, name)
        )
        ev_context["log"] = logging.getLogger(f"slopsmith.plugin.{evicted_id}")
        # Install the fallback copy's requirements. It was evicted before
        # the main load loop ran, so _install_requirements was never called
        # for it. A user copy that depends on extra packages would otherwise
        # fail with an import error even when those packages can be installed.
        # Mirror the main load-loop contract: _install_requirements returning
        # False is *non-fatal* (read-only filesystem, optional dep, etc.) —
        # we emit a plugin-error and continue loading, exactly as the main
        # loop does. Treating it as fatal here would break the fallback for
        # those same tolerated cases and leave the bundled-failure error
        # unresolved.
        ev_req_ok = _install_requirements(ev_dir, evicted_id)
        if not ev_req_ok:
            _emit_progress(
                "plugin-error",
                f"Failed to install requirements for fallback copy of '{evicted_id}'",
                plugin_id=evicted_id,
                loaded=len(_loaded_batch),
                total=len(plugin_load_specs),
                error="Requirements installation failed for fallback copy; check server logs",
            )
        # Purge any sibling modules the failed bundled copy may have loaded.
        # They are cached under the same namespace as what the fallback would use.
        # The parent package is `plugin_{safe_id}`, sibling modules are
        # `plugin_{safe_id}.{name}` (from load_sibling), and the routes module is
        # `plugin_{safe_id}_routes` (note the underscore). Clearing all three
        # patterns ensures the fallback gets a clean slate and doesn't accidentally
        # resolve bundled helper code that is still cached in sys.modules.
        _safe_eid = _safe_plugin_id_for_module_name(evicted_id)
        _parent_pkg = f"plugin_{_safe_eid}"
        # The routes module is registered under exactly `{_parent_pkg}_routes`
        # (underscore, not dot — it is NOT a sub-package of _parent_pkg).
        # Using startswith(f"{_parent_pkg}_") would incorrectly match
        # "plugin_a_5f_b_routes" (routes for plugin "a_b") when evicting
        # plugin "a", because "plugin_a_5f_b_routes".startswith("plugin_a_")
        # is True. Match the routes entry exactly instead.
        _stale_sibling_keys = [
            k for k in list(sys.modules)
            if k == _parent_pkg
            or k.startswith(f"{_parent_pkg}.")
            or k == f"{_parent_pkg}_routes"
        ]
        for _k in _stale_sibling_keys:
            del sys.modules[_k]
        # Also purge bare-import modules the failed bundled copy may have added
        # to sys.modules. These are NOT covered by the namespaced purge above;
        # a bundled plugin that does `import helper` (bare import via sys.path)
        # would otherwise leave a stale `helper` module in sys.modules that
        # the fallback copy could accidentally resolve instead of its own file.
        for _k in _pending_eviction_stale_modules.get(evicted_id, set()):
            sys.modules.pop(_k, None)
        # Re-load the fallback's routes using the same module-name slot so
        # it naturally replaces the previously-failed bundled module.
        ev_routes_file = ev_manifest.get("routes")
        # If the user copy has no routes file it cannot restore the bundled
        # plugin's backend endpoints (the route failure is the very reason we
        # are in this fallback path). Treat that as a failed recovery so the
        # bundled-failure error is NOT cleared from startup-status.
        fallback_routes_ok = bool(ev_routes_file)
        if ev_routes_file:
            # Capture route count before fallback setup() to detect partial
            # registration — same permanent-mount limitation as the main loop.
            _fallback_routes_before = len(getattr(app, "routes", []))
            try:
                ev_module_name = f"plugin_{_safe_plugin_id_for_module_name(evicted_id)}_routes"
                ev_spec = importlib.util.spec_from_file_location(
                    ev_module_name, str(ev_dir / ev_routes_file))
                ev_routes_module = importlib.util.module_from_spec(ev_spec)
                sys.modules[ev_module_name] = ev_routes_module
                ev_spec.loader.exec_module(ev_routes_module)
                if hasattr(ev_routes_module, "setup"):
                    if route_setup_fn is not None:
                        _fn = lambda rm=ev_routes_module, ctx=ev_context, a=app: rm.setup(a, ctx)
                        _fn._plugin_id = evicted_id
                        route_setup_fn(_fn)
                    else:
                        ev_routes_module.setup(app, ev_context)
                log.info("Loaded routes for fallback copy of plugin %r", evicted_id)
            except Exception:
                log.exception(
                    "Fallback user-installed copy of %r also failed to load routes; "
                    "plugin unavailable (not registered).", evicted_id,
                )
                # Emit a plugin-error so startup-status reflects the
                # fallback's failure as the root cause, not the earlier
                # bundled-copy error. Without this the status stays on the
                # stale bundled error even though that's no longer the
                # active failure.
                _emit_progress(
                    "plugin-error",
                    f"Fallback copy of plugin '{evicted_id}' also failed to load routes",
                    plugin_id=evicted_id,
                    loaded=len(_loaded_batch),
                    total=len(plugin_load_specs),
                    error=(
                        f"Both bundled and user-installed copies of '{evicted_id}' "
                        "failed to load routes; plugin unavailable — check server logs"
                    ),
                )
                # Warn on partial registration in the fallback path too.
                _fallback_routes_after = len(getattr(app, "routes", []))
                if _fallback_routes_after > _fallback_routes_before:
                    log.warning(
                        "Fallback copy of %r registered %d route(s) before its setup() raised; "
                        "these handlers cannot be removed.",
                        evicted_id, _fallback_routes_after - _fallback_routes_before,
                    )
                fallback_routes_ok = False
        if fallback_routes_ok:
            _loaded_batch.insert(_bundled_orig_idx, {
                "id": evicted_id,
                "name": ev_manifest.get("name", evicted_id),
                "nav": ev_manifest.get("nav"),
                "type": ev_manifest.get("type"),
                "bundled": False,  # user copy, not bundled
                # Marks this entry as an emergency user-copy fallback: the
                # bundled routes failed, so the evicted user-installed copy
                # was loaded instead.  Surfaced in /api/plugins and the
                # settings UI so users know the bundled build is broken.
                "fallback": True,
                "has_screen": bool(ev_manifest.get("screen")),
                "has_script": bool(ev_manifest.get("script")),
                "has_settings": bool(ev_manifest.get("settings")),
                "has_tour": _is_valid_tour_manifest(ev_manifest.get("tour")),
                "_export_paths": _normalize_export_paths(ev_manifest.get("settings"), evicted_id),
                "_diagnostics_paths": _normalize_diagnostics_paths(ev_manifest.get("diagnostics"), evicted_id),
                "_diagnostics_callable_spec": _parse_diagnostics_callable(ev_manifest.get("diagnostics"), evicted_id),
                "_load_sibling": ev_context["load_sibling"],
                "_dir": ev_dir,
                "_manifest": ev_manifest,
            })
            log.info("Registered fallback user copy of plugin %r (%s)", evicted_id, ev_manifest.get("name", ""))
            # Emit a compensating progress event to clear the bundled-failure
            # error from startup-status. Without this, the final
            # `plugins-complete` status would still carry the error text from
            # the bundled failure even though the plugin is now active via the
            # fallback copy. Uses clear_error=True so the server handler
            # replaces the stale error with null rather than ignoring it.
            # Only send clear_error when req install also succeeded; if req
            # failed we emitted a plugin-error above and must not clear it —
            # the fallback copy is active but degraded (missing dependencies).
            _emit_progress(
                "plugin-registered",
                f"Registered fallback copy of plugin '{evicted_id}'",
                plugin_id=evicted_id,
                loaded=len(_loaded_batch),
                total=len(plugin_load_specs),
                clear_error=ev_req_ok,
            )

    # Publish all plugins atomically so concurrent readers never observe
    # a partially-populated list during the loading window.  We clear
    # first so that repeated load_plugins() calls (tests, dev reloads,
    # future "reload plugins" feature) never accumulate duplicate entries
    # while keeping the list object identity stable.
    with PLUGINS_LOCK:
        LOADED_PLUGINS.clear()
        LOADED_PLUGINS.extend(_loaded_batch)

    _emit_progress(
        "plugins-complete",
        f"Loaded {len(_loaded_batch)} plugin(s)",
        loaded=len(_loaded_batch),
        total=len(plugin_load_specs),
    )


def _check_plugin_update(plugin_dir: Path) -> dict | None:
    """Check if a plugin's git repo has updates available."""
    git_dir = plugin_dir / ".git"
    if not git_dir.exists():
        return None
    try:
        # Fetch latest from remote (quick, refs only)
        subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=str(plugin_dir), capture_output=True, timeout=15,
        )
        # Compare local HEAD with remote tracking branch
        result = subprocess.run(
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=str(plugin_dir), capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        behind = int(result.stdout.strip())
        # Get current and remote commit hashes
        local = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(plugin_dir), capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "rev-parse", "--short", "@{u}"],
            cwd=str(plugin_dir), capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return {"behind": behind, "local": local, "remote": remote}
    except Exception:
        return None


def register_plugin_api(app: FastAPI):
    """Register the plugin discovery API endpoints."""

    @app.get("/api/plugins")
    def list_plugins():
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        return [
            {
                "id": p["id"],
                "name": p["name"],
                # Surface the manifest's `version` field (free-form
                # semver string) so diagnostics bundles + the future
                # plugin marketplace can identify exactly which build
                # is loaded. None when the plugin omits the field.
                "version": (p.get("_manifest") or {}).get("version"),
                "nav": p["nav"],
                # type is None for plugins without the manifest hint —
                # frontend filters like "give me all type=visualization"
                # work via identity comparison; absent is treated as
                # "no declared role".
                "type": p.get("type"),
                # `bundled` is reserved metadata flagging plugins that
                # ship with the default container image (slopsmith#160).
                # Surfaced in /api/plugins so the plugin-list UI can
                # render a "Bundled" badge (lock icon) next to the
                # plugin name in the settings collapsible.
                "bundled": p.get("bundled", False),
                # `fallback` is True only for user-installed copies that
                # are active because the bundled plugin's routes failed.
                # Surfaced in /api/plugins so the settings UI can show
                # a warning badge, letting users know the bundled build
                # is broken and they are running an older user copy.
                "fallback": p.get("fallback", False),
                "has_screen": p["has_screen"],
                "has_script": p["has_script"],
                "has_settings": p["has_settings"],
                "has_tour": p.get("has_tour", False),
            }
            for p in snapshot
        ]

    @app.get("/api/plugins/updates")
    def check_updates():
        """Check all plugins for available git updates."""
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        updates = {}
        for p in snapshot:
            info = _check_plugin_update(p["_dir"])
            if info and info["behind"] > 0:
                updates[p["id"]] = {
                    "name": p["name"],
                    "behind": info["behind"],
                    "local": info["local"],
                    "remote": info["remote"],
                }
        return {"updates": updates}

    @app.post("/api/plugins/{plugin_id}/update")
    def update_plugin(plugin_id: str):
        """Pull latest changes for a plugin. Stashes local edits first."""
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        for p in snapshot:
            if p["id"] == plugin_id:
                git_dir = p["_dir"] / ".git"
                if not git_dir.exists():
                    return {"error": "Not a git repository"}
                cwd = str(p["_dir"])
                try:
                    # Stash any local modifications so pull doesn't fail
                    subprocess.run(
                        ["git", "stash", "--quiet"],
                        cwd=cwd, capture_output=True, timeout=10,
                    )
                    result = subprocess.run(
                        ["git", "pull", "--ff-only"],
                        cwd=cwd, capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode != 0:
                        # Restore stash on failure
                        subprocess.run(
                            ["git", "stash", "pop", "--quiet"],
                            cwd=cwd, capture_output=True, timeout=10,
                        )
                        return {"error": result.stderr[:500]}
                    return {"ok": True, "message": result.stdout.strip()}
                except Exception as e:
                    return {"error": str(e)}
        return {"error": "Plugin not found"}

    @app.get("/api/plugins/{plugin_id}/screen.html")
    def plugin_screen_html(plugin_id: str):
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        for p in snapshot:
            if p["id"] == plugin_id:
                screen_file = p["_dir"] / p["_manifest"].get("screen", "screen.html")
                if screen_file.exists():
                    return HTMLResponse(screen_file.read_text(encoding="utf-8"))
        return HTMLResponse("", status_code=404)

    @app.get("/api/plugins/{plugin_id}/screen.js")
    def plugin_screen_js(plugin_id: str):
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        for p in snapshot:
            if p["id"] == plugin_id:
                script_file = p["_dir"] / p["_manifest"].get("script", "screen.js")
                if script_file.exists():
                    return Response(script_file.read_text(encoding="utf-8"), media_type="application/javascript")
        return Response("", status_code=404)

    @app.get("/api/plugins/{plugin_id}/settings.html")
    def plugin_settings_html(plugin_id: str):
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        for p in snapshot:
            if p["id"] == plugin_id:
                settings = p["_manifest"].get("settings", {})
                settings_file = p["_dir"] / (settings.get("html", "settings.html") if isinstance(settings, dict) else "settings.html")
                if settings_file.exists():
                    return HTMLResponse(settings_file.read_text(encoding="utf-8"))
        return HTMLResponse("", status_code=404)

    @app.get("/api/plugins/{plugin_id}/tour.json")
    def plugin_tour_json(plugin_id: str):
        with PLUGINS_LOCK:
            snapshot = list(LOADED_PLUGINS)
        for p in snapshot:
            if p["id"] == plugin_id:
                tour_val = p["_manifest"].get("tour")
                if not _is_valid_tour_manifest(tour_val):
                    break
                if isinstance(tour_val, str):
                    tour_filename = tour_val
                elif isinstance(tour_val, dict):
                    tour_filename = tour_val.get("file", "tour.json")
                else:
                    break  # shouldn't reach here; _is_valid_tour_manifest guards above
                # Quick pre-filter for obvious bad paths. This is not the
                # authoritative security boundary — the resolve+relative_to
                # check below is — but catching simple cases early produces
                # a cleaner log message before the filesystem calls.
                if (
                    not isinstance(tour_filename, str)
                    or not tour_filename
                    or ".." in tour_filename.split("/")
                    or tour_filename.startswith("/")
                    or "\\" in tour_filename
                ):
                    log.warning("Plugin %r: invalid tour path rejected: %r", plugin_id, tour_filename)
                    break
                tour_file = (p["_dir"] / tour_filename).resolve()
                plugin_dir = p["_dir"].resolve()
                # Ensure resolved path stays inside the plugin directory
                try:
                    tour_file.relative_to(plugin_dir)
                except ValueError:
                    log.warning("Plugin %r: tour path escapes plugin dir: %r", plugin_id, tour_filename)
                    break
                if tour_file.is_file():
                    return Response(tour_file.read_text(encoding="utf-8"), media_type="application/json")
                break
        return Response("{}", status_code=404, media_type="application/json")
