#!/usr/bin/env python3
"""Rebuild data/sample-cards-s1.jsonl and data/sample-manifest.json from the full shards. Standard library only.

    python3 -B -I scripts/make_sample.py --shards DIR

DIR holds the NN.jsonl shards named in data/cards-s1-index.json (download them from the addresses in that file).
The rule is the one fixed in verify_simul_pack.py for protocol simul-stage1-v1, first_5_by_card_id_asc_per_shard:
for each of the 20 shards, in index order, sort the records by card_id and keep the first 5, copied byte for byte.
A shard whose SHA-256 differs from the index, or that is not a regular file, stops the build.
"""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("verify_simul_pack", Path(__file__).resolve().parent / "verify_simul_pack.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def build(root, shards):
    """Return (sample bytes, manifest) for the shards in `shards`, or raise PackError."""
    index_bytes = v.read_regular_file_bytes(root, v.FILES["index"])
    index = v.parse_json_bytes(index_bytes, v.FILES["index"])
    errors, _ = v.check_index(index) if isinstance(index, dict) else (["card index: not a JSON object"], {})
    if errors:
        raise v.PackError(errors[0])
    if len(index["editions"]) != v.EXPECTED_SHARDS:
        raise v.PackError(f"the card index lists {len(index['editions'])} shard(s), the policy fixes {v.EXPECTED_SHARDS}")
    lines_by_edition = []
    for entry in index["editions"]:
        name = v.SHARD_RE.fullmatch(entry["file"]).group(1)
        data = v.read_regular_file_bytes(shards, name, label=f"shards/{name}", root_label="shards directory")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise v.PackError(f"shards/{name}: SHA-256 differs from the card index")
        records, read_errors = v.read_jsonl(data, name)
        if read_errors:
            raise v.PackError(read_errors[0])
        if any(not isinstance(record.get("card_id"), str) for _, record, _ in records):
            raise v.PackError(f"shards/{name}: a card has no card_id")
        lines_by_edition.append((entry["edition"], [(record["card_id"], text) for _, record, text in records]))
    listed, picked = {}, []
    for edition, lines in lines_by_edition:
        first = v.select_sample([(edition, lines)])
        if len(first) != v.CARDS_PER_SHARD:
            raise v.PackError(f"edition {edition}: fewer than {v.CARDS_PER_SHARD} cards")
        listed[edition] = [card_id for card_id, _ in first]
        picked += [text for _, text in first]
    sample_bytes = ("\n".join(picked) + "\n").encode("utf-8")
    manifest = {
        "name": "SIMUL stage-1 case cards: 100-card sample",
        "protocol_version": v.PROTOCOL_VERSION,
        "policy": v.SAMPLE_RULE_ID,
        "rule": "For each shard listed in cards-s1-index.json, in index order: sort its records by card_id "
                f"(ascending, as strings) and keep the first {v.CARDS_PER_SHARD}. Each line is copied byte for byte from the shard.",
        "shards": v.EXPECTED_SHARDS,
        "per_shard": v.CARDS_PER_SHARD,
        "total": len(picked),
        "source_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "cards": listed,
    }
    return sample_bytes, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rebuild the 100-card sample and its manifest from the full shards.")
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if the committed files differ")
    args = parser.parse_args(argv)
    try:
        sample, manifest = build(args.root, args.shards)
        manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if args.check:
            same = (v.read_regular_file_bytes(args.root, v.FILES["sample"]) == sample
                    and v.read_regular_file_bytes(args.root, v.FILES["manifest"]) == manifest_bytes)
            print("sample and manifest match the shards" if same else "sample or manifest differ from the shards")
            return 0 if same else 1
    except v.PackError as ex:
        print(f"make_sample: {ex}", file=sys.stderr)
        return 1
    (args.root / v.FILES["sample"]).write_bytes(sample)
    (args.root / v.FILES["manifest"]).write_bytes(manifest_bytes)
    print(f"wrote {manifest['total']} cards from {len(manifest['cards'])} shards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
