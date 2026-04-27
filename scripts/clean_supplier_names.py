"""
scripts/clean_supplier_names.py

One-shot migration: clean SEO marketing suffixes from `saved_suppliers.supplier_name`.

The ", Suppliers" / "Manufacturers and Factory, Suppliers" / "(ISO Certified)"
junk leaks in because the cleaner used to take the page title verbatim. Now
that `clean_supplier_name()` exists in modules/cleaner.py, run this once
against the live DB to retroactively clean the rows that were saved before
the fix.

USAGE:
    # Dry run (default — prints what WOULD change, doesn't write):
    python -m scripts.clean_supplier_names

    # Apply for real:
    python -m scripts.clean_supplier_names --apply

Safety nets:
    - Skip if cleaned length < 5 chars (avoid clearing names to junk)
    - Skip if cleaned name keeps < 30% of original length
    - Skip if cleaned name has fewer than 3 meaningful tokens (≥3 chars)
    - Skip if cleaned name ends in an SEO word (manufacturers / suppliers /
      factory / wholesale) — means the regex didn't fully strip the tail
    - Always log every CHANGE / SKIP / NOOP for inspection
"""

import argparse
import re
import sys
from pathlib import Path

# Make project root importable when running as `python -m scripts.xxx`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db import SessionLocal, init_db
from models import SavedSupplier
from modules.cleaner import clean_supplier_name


_MIN_LEN          = 5
_MIN_RETAIN_RATIO = 0.30   # cleaned must keep ≥30% of original length
_MIN_TOKENS       = 3      # cleaned must keep at least 3 meaningful tokens (≥3 chars)
_BAD_TAIL_TOKENS  = {"manufacturers", "manufacturer", "supplier", "suppliers",
                     "factory", "factories", "wholesale", "wholesalers"}


def _token_count(s: str) -> int:
    return len([t for t in re.findall(r"\w+", s) if len(t) >= 3])


def _ends_with_bad_tail(s: str) -> bool:
    toks = re.findall(r"\w+", s.lower())
    return bool(toks) and toks[-1] in _BAD_TAIL_TOKENS


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean SEO suffixes from saved supplier names.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually commit changes. Default is dry-run.")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()

    try:
        rows = db.query(SavedSupplier).all()
        n_total    = len(rows)
        n_changed  = 0
        n_skipped  = 0
        n_unchanged = 0

        print(f"\nScanning {n_total} saved supplier rows...")
        print(f"Mode: {'APPLY (writes)' if args.apply else 'DRY RUN (no writes)'}")
        print("-" * 80)

        for s in rows:
            old = s.supplier_name or ""
            new = clean_supplier_name(old)

            if new == old:
                n_unchanged += 1
                continue

            # Safety net 1: minimum length
            if len(new) < _MIN_LEN:
                print(f"[SKIP-too-short] id={s.id:4d}  {old!r}  →  {new!r}  (cleaned <{_MIN_LEN} chars)")
                n_skipped += 1
                continue

            # Safety net 2: retain at least 30% of original
            if len(old) > 0 and len(new) / len(old) < _MIN_RETAIN_RATIO:
                print(f"[SKIP-too-aggressive] id={s.id:4d}  {old!r}  →  {new!r}"
                      f"  (kept {len(new)/len(old):.0%}, need >={_MIN_RETAIN_RATIO:.0%})")
                n_skipped += 1
                continue

            # Safety net 3: at least N meaningful tokens
            if _token_count(new) < _MIN_TOKENS:
                print(f"[SKIP-too-few-tokens] id={s.id:4d}  {old!r}  →  {new!r}"
                      f"  ({_token_count(new)} tokens, need >={_MIN_TOKENS})")
                n_skipped += 1
                continue

            # Safety net 4: forbid SEO-word tail (manufacturers/suppliers/factory/wholesale)
            if _ends_with_bad_tail(new):
                print(f"[SKIP-bad-tail] id={s.id:4d}  {old!r}  →  {new!r}  (ends in SEO token)")
                n_skipped += 1
                continue

            print(f"[CLEAN]  id={s.id:4d}  {old!r}  →  {new!r}")
            n_changed += 1

            if args.apply:
                s.supplier_name = new

        if args.apply and n_changed > 0:
            db.commit()
            print(f"\nCommitted {n_changed} changes.")
        elif args.apply:
            print("\nNothing to commit.")
        else:
            print(f"\n(dry run — no DB writes; pass --apply to commit)")

        print("-" * 80)
        print(f"Summary: {n_changed} would change · {n_skipped} skipped (safety) · "
              f"{n_unchanged} unchanged · {n_total} total")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
