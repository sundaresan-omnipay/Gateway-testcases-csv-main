"""
Index gateway test-case CSVs into Chroma Cloud DB.

Each CSV row becomes one document. On re-run the same rows are upserted
(idempotent), so only truly changed files need reprocessing.

Usage (called by GitHub Actions with repo-root-relative paths):
    python scripts/index_to_chroma.py \\
        adyen_direct_intergration/add_card.csv \\
        adyen_direct_intergration/wallet.csv

    # Reindex every CSV in the default gateway directory:
    python scripts/index_to_chroma.py --all

    # Reindex all CSVs under multiple directories (including spaces):
    python scripts/index_to_chroma.py --dirs "adyen_direct_intergration" "Internal Fund Transfer"

Required environment variables:
    CHROMA_API_KEY   - Chroma Cloud API key
    CHROMA_TENANT    - Chroma Cloud tenant name
    CHROMA_DATABASE  - Chroma Cloud database name

Optional:
    OPENAI_API_KEY          - Uses OpenAI embeddings.
                       Falls back to sentence-transformers if not set.
    CHROMA_EMBEDDING_MODEL  - OpenAI embedding model name (default: text-embedding-3-large).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

COLLECTION_NAME = "gateway_testcases"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"

# Repo root is one level above scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GATEWAY_DIR = _REPO_ROOT / "adyen_direct_intergration"


# ---------------------------------------------------------------------------
# Chroma + embedding helpers
# ---------------------------------------------------------------------------

def _get_chroma_client():
    api_key = os.environ.get("CHROMA_API_KEY")
    tenant = os.environ.get("CHROMA_TENANT")
    database = os.environ.get("CHROMA_DATABASE")

    missing = [k for k, v in [
        ("CHROMA_API_KEY", api_key),
        ("CHROMA_TENANT", tenant),
        ("CHROMA_DATABASE", database),
    ] if not v]
    if missing:
        raise SystemExit(
            f"Missing Chroma Cloud env vars: {', '.join(missing)}\n"
            "Set them as GitHub Actions secrets: CHROMA_API_KEY, CHROMA_TENANT, CHROMA_DATABASE"
        )

    try:
        import chromadb  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit(
            "Missing dependency: 'chromadb'. Install it with:\n"
            "  python3 -m pip install -r scripts/requirements-chroma.txt\n"
        ) from e

    # Many corporate/dev environments set HTTP(S)_PROXY which can break Chroma Cloud calls
    # (seen as httpx.ProxyError: 403 Forbidden). Ensure Chroma's host bypasses proxies.
    chroma_host = "api.trychroma.com"
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if chroma_host not in {h.strip() for h in no_proxy.split(",") if h.strip()}:
        updated = (no_proxy + "," if no_proxy.strip() else "") + chroma_host
        os.environ["NO_PROXY"] = updated
        os.environ["no_proxy"] = updated

    print(f"Connecting to Chroma Cloud (tenant={tenant!r}, database={database!r}) …")
    client = chromadb.HttpClient(
        host=chroma_host,
        ssl=True,
        tenant=tenant,
        database=database,
        headers={"x-chroma-token": api_key},
    )
    return client


def _get_embedding_function():
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
        model_name = os.environ.get("CHROMA_EMBEDDING_MODEL") or DEFAULT_OPENAI_EMBEDDING_MODEL
        print(f"Using OpenAI embeddings ({model_name})")
        return OpenAIEmbeddingFunction(
            api_key=openai_key,
            model_name=model_name,
        )

    print(
        "[warn] OPENAI_API_KEY not set — falling back to sentence-transformers embeddings.",
        file=sys.stderr,
    )
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    return DefaultEmbeddingFunction()


# ---------------------------------------------------------------------------
# CSV → Chroma document
# ---------------------------------------------------------------------------

def _is_empty_row(row: dict) -> bool:
    return all(v is None or str(v).strip() == "" for v in row.values())


def _row_to_doc(row: dict, file_stem: str, row_idx: int) -> tuple[str, str, dict]:
    """Return (chroma_id, document_text, metadata) for one CSV row."""
    tc_id = (row.get("Test Case #") or "").strip()
    scenario = (row.get("Scenario") or "").strip()
    precondition = (row.get("Precondition") or "").strip()
    test_steps = (row.get("Test steps") or "").strip()
    expected = (row.get("Expected Result") or "").strip()

    # Stable, human-readable Chroma ID
    if tc_id:
        chroma_id = f"{file_stem}__{tc_id}"
    else:
        chroma_id = f"{file_stem}__row{row_idx}"

    # Rich text used for semantic embedding
    parts: list[str] = []
    if scenario:
        parts.append(f"Scenario: {scenario}")
    if precondition:
        parts.append(f"Precondition: {precondition}")
    if test_steps:
        parts.append(f"Steps: {test_steps}")
    if expected:
        parts.append(f"Expected: {expected}")
    document = "\n".join(parts) or "No description"

    # Metadata stored alongside the vector (filterable)
    metadata: dict[str, str] = {
        "source_file": file_stem,
        "module": (row.get("Module") or "").strip(),
        "submodule": (row.get("Submodule") or "").strip(),
        "parent_id": (row.get("Parent ID") or "").strip(),
        "test_case_id": tc_id,
        "priority": (row.get("Priority") or "").strip(),
        "automation_status": (row.get("Automation Status") or "").strip(),
        "labels": (row.get("Labels") or "").strip(),
    }
    # Chroma metadata values must be str/int/float/bool — ensure str
    metadata = {k: str(v) for k, v in metadata.items()}

    return chroma_id, document, metadata


# ---------------------------------------------------------------------------
# Core indexing
# ---------------------------------------------------------------------------

_UPSERT_BATCH = 100


def index_csv(collection, csv_path: Path) -> int:
    """Parse one CSV file and upsert all rows. Returns the number of rows upserted."""
    file_stem = csv_path.stem
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    seen_ids: set[str] = set()

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for idx, row in enumerate(reader, start=1):
            if not row or _is_empty_row(row):
                continue
            chroma_id, document, metadata = _row_to_doc(row, file_stem, idx)
            # Some CSVs contain duplicate "Test Case #" values. Chroma requires unique ids
            # within a single upsert call, so disambiguate deterministically by row index.
            if chroma_id in seen_ids:
                chroma_id = f"{chroma_id}__row{idx}"
            seen_ids.add(chroma_id)
            ids.append(chroma_id)
            docs.append(document)
            metas.append(metadata)

    if not ids:
        print(f"  [skip] {csv_path.name}: no rows found")
        return 0

    for start in range(0, len(ids), _UPSERT_BATCH):
        collection.upsert(
            ids=ids[start : start + _UPSERT_BATCH],
            documents=docs[start : start + _UPSERT_BATCH],
            metadatas=metas[start : start + _UPSERT_BATCH],
        )

    return len(ids)

def _collect_csvs_from_dir(dir_path: Path) -> list[Path]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    return sorted(p for p in dir_path.glob("*.csv") if p.is_file())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index gateway test-case CSVs into Chroma Cloud."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="CSV file paths to index (repo-root-relative or absolute).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Reindex all CSVs in the gateway directory.",
    )
    parser.add_argument(
        "--dirs",
        nargs="+",
        help='Reindex all CSVs under these directories (relative to repo root or absolute). Example: --dirs "adyen_direct_intergration" "Internal Fund Transfer"',
    )
    parser.add_argument(
        "--gateway-dir",
        type=Path,
        default=_DEFAULT_GATEWAY_DIR,
        help=f"Gateway CSV directory (default: {_DEFAULT_GATEWAY_DIR})",
    )
    args = parser.parse_args()

    if not args.files and not args.all and not args.dirs:
        parser.error("Provide CSV file paths or pass --all / --dirs to reindex folders.")

    client = _get_chroma_client()
    ef = _get_embedding_function()

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"description": "Gateway test cases — adyen_direct_integration CSVs"},
    )
    print(f"Collection: {COLLECTION_NAME!r}\n")

    # Resolve the list of CSV files to process
    csv_paths: list[Path] = []
    if args.all:
        # Backwards-compatible: reindex only the gateway dir
        csv_paths = _collect_csvs_from_dir(args.gateway_dir)
        if not csv_paths:
            raise SystemExit(f"No CSV files found in: {args.gateway_dir}")
    elif args.dirs:
        for raw_dir in args.dirs:
            p = Path(raw_dir)
            if not p.is_absolute():
                p = (_REPO_ROOT / p)
            found = _collect_csvs_from_dir(p)
            if not found:
                print(f"[warn] No CSVs found (or not a dir): {raw_dir}", file=sys.stderr)
            csv_paths.extend(found)
    else:
        for raw in args.files:
            p = Path(raw)
            if p.exists():
                csv_paths.append(p.resolve())
            else:
                # File path might be relative to repo root or gateway dir
                alt = _REPO_ROOT / raw
                if alt.exists():
                    csv_paths.append(alt.resolve())
                else:
                    print(f"[warn] File not found, skipping: {raw}", file=sys.stderr)

    if not csv_paths:
        raise SystemExit("No valid CSV files to index.")

    # De-dup if multiple dirs overlap
    csv_paths = sorted({p.resolve() for p in csv_paths})

    print(f"Indexing {len(csv_paths)} file(s) …")
    total = 0
    for csv_path in csv_paths:
        print(f"  -> {csv_path.name}")
        count = index_csv(collection, csv_path)
        print(f"     {count} rows upserted")
        total += count

    print(f"\nDone — {total} rows indexed into Chroma Cloud collection {COLLECTION_NAME!r}.")


if __name__ == "__main__":
    main()
