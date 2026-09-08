"""Version capability profiles.

DSpace's `bin/dspace` CLI has been remarkably stable across major versions
(import/export/metadata-import/metadata-export/filter-media/itemupdate/
structure-builder all exist from 5.x through 8.x with the same flags), but
a few things genuinely differ and matter for how tools behave:

  - Whether `import` needs to run as root (assetstore permission layout).
  - Whether the REST API actually works and should be preferred over SSH+CLI
    for read operations (DSpace 7/8 REST is a first-class citizen; DSpace <7
    REST is legacy/often disabled, and at MGIMO's 6.3 instance it is broken
    entirely -- redirects to a dead port).
  - Exact filter-media plugin names (media-filters.cfg plugin.named.* keys
    can be customized per instance, so these are defaults, not guarantees --
    dspace_ping/list tools should let an operator override via config).

Sources consulted (2026-07): LYRASIS DSpace 6.x/7.x "Command Line
Operations", "Importing and Exporting Items via Simple Archive Format",
"Batch Metadata Editing", and "Importing Community and Collection
Hierarchy" wiki pages. Where the wiki didn't have live detail (itemupdate,
filter-media flags), this falls back to hands-on-verified behavior from
operating open.mgimo.ru (DSpace 6.3) -- see the MGIMO memory files.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VersionProfile:
    major: int
    label: str

    # -- assetstore / execution model --
    import_needs_root: bool  # DSpace <7 classic assetstore: yes. 7/8 w/ S3 or
                              # container deployments: usually no, but SSH-based
                              # bare-metal 7/8 installs commonly still run as the
                              # tomcat/dspace service user, not root -- configurable.
    needs_chown_after_root_write: bool

    # -- REST --
    rest_generally_available: bool  # true for 7.x/8.x by default
    rest_base_path: str  # e.g. "/server/api" for 7/8

    # -- collection/community creation --
    # "cli"  -> bin/dspace structure-builder -f <xml> -o <out.xml> -e <admin>
    # "rest" -> POST /server/api/core/collections (7/8 only)
    # "sql"  -> direct SQL against metadatavalue/collection/handle tables
    #           (fallback; needed historically at MGIMO where structure-builder
    #           was not used and handle/policy minting was done by hand -- kept
    #           available for instances where structure-builder is disabled,
    #           patched out, or the operator prefers not to grant the CLI
    #           write access it needs)
    collection_create_via: tuple[str, ...]  # ordered preference

    # -- logo / bitstream injection --
    # "cli"  -> no first-class CLI for setting a collection logo pre-7; use REST
    #           on 7/8 (PUT .../collections/{id}/logo), else raw DB+assetstore.
    logo_via: tuple[str, ...]

    # -- filter-media plugin names (defaults; overridable per profile) --
    text_extractor_plugin: str = "PDF Text Extractor"
    thumbnail_plugin: str = "PDFBox JPEG Thumbnail"

    # -- itemupdate (bitstream add/delete without touching metadata) --
    itemupdate_supported: bool = True

    # -- assetstore path scheme (classic DSpaceBitStoreService default) --
    assetstore_scheme_notes: str = (
        "path = <root>/<store_number>/<id[0:2]>/<id[2:4]>/<id[4:6]>/<internal_id>"
        " when directoryLevels=3, digitsPerLevel=2 (also seen as "
        "<root>/<id[0:2]>/<id[2:4]>/<id[4:6]>/<internal_id> when store_number"
        " folder is implicit -- verify against dspace.cfg / assetstore.cfg on"
        " the target instance, do not assume)"
    )

    known_deltas: tuple[str, ...] = field(default_factory=tuple)


V5 = VersionProfile(
    major=5,
    label="5.x",
    import_needs_root=True,
    needs_chown_after_root_write=True,
    rest_generally_available=False,
    rest_base_path="/rest",
    collection_create_via=("cli", "sql"),
    logo_via=("sql",),
    known_deltas=(
        "Legacy REST API (/rest) exists but is read-mostly and frequently "
        "disabled; do not rely on it.",
        "No collection logo REST/CLI endpoint -- DB+assetstore injection is "
        "the only path.",
    ),
)

V6 = VersionProfile(
    major=6,
    label="6.x",
    import_needs_root=True,
    needs_chown_after_root_write=True,
    rest_generally_available=False,
    rest_base_path="/rest",
    collection_create_via=("cli", "sql"),
    logo_via=("sql",),
    known_deltas=(
        "structure-builder (bin/dspace structure-builder -f in.xml -o out.xml "
        "-e admin@x) can create communities/collections from XML and is "
        "present in 6.x -- try this before falling back to SQL. (Historically "
        "the MGIMO 6.3 instance's tooling used SQL directly without trying "
        "structure-builder first; both paths are kept here.)",
        "Legacy REST API (/rest) commonly broken/disabled in bare-metal "
        "installs behind a reverse proxy -- confirm with dspace_ping before "
        "ever choosing the rest_base_path fallback.",
        "`dspace import` must run as root when the assetstore is a local "
        "classic bitstore owned by the Tomcat user; a root-owned write means "
        "a `chown -R <tomcat_user>:<tomcat_user> <assetstore_root>` is "
        "required afterward or Tomcat cannot read/filter the new files.",
        "filter-media's -m flag is a MAXIMUM ITEM COUNT, not a batch/commit "
        "size -- omit it or loop to process an entire collection.",
        "PDFBox JPEG Thumbnail filter needs "
        "-Djavax.accessibility.assistive_technologies= (empty) in JAVA_OPTS "
        "or it crashes on headless hosts (AWT/AtkWrapper).",
    ),
)

V7 = VersionProfile(
    major=7,
    label="7.x",
    import_needs_root=False,  # typically runs as the dspace/tomcat service account
    needs_chown_after_root_write=False,
    rest_generally_available=True,
    rest_base_path="/server/api",
    collection_create_via=("rest", "cli", "sql"),
    logo_via=("rest", "sql"),
    known_deltas=(
        "REST API (/server/api) is the primary interface; CLI remains "
        "available for the same operations (import/export/metadata-*/"
        "filter-media/itemupdate/structure-builder).",
        "Legacy log-based statistics commands are removed; Solr statistics "
        "only.",
        "Community/collection creation and logo upload have REST endpoints "
        "(POST /server/api/core/collections, PUT .../logo) -- prefer these "
        "over CLI/SQL when rest_base is configured and reachable.",
    ),
)

V8 = VersionProfile(
    major=8,
    label="8.x",
    import_needs_root=False,
    needs_chown_after_root_write=False,
    rest_generally_available=True,
    rest_base_path="/server/api",
    collection_create_via=("rest", "cli", "sql"),
    logo_via=("rest", "sql"),
    known_deltas=(
        "Same CLI/REST surface as 7.x as far as this server relies on; "
        "re-verify against release notes for the specific 8.x point release "
        "before assuming parity on newer endpoints.",
    ),
)

_BY_MAJOR: dict[int, VersionProfile] = {5: V5, 6: V6, 7: V7, 8: V8}


def resolve(version_string: str) -> VersionProfile:
    """Map a version string like '6.3' or '7' to its capability profile.
    Unknown/future majors fall back to the closest known (>=7 -> V8 behavior,
    else V6) with a note that this is a guess.
    """
    try:
        major = int(version_string.strip().split(".")[0])
    except (ValueError, IndexError):
        major = 6
    if major in _BY_MAJOR:
        return _BY_MAJOR[major]
    if major > max(_BY_MAJOR):
        return V8
    if major < min(_BY_MAJOR):
        return V5
    return V6
