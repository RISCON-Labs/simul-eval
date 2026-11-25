#!/usr/bin/env python3
"""Verify the SIMUL public pack. Python 3.11+ on Linux or macOS, standard library only.

    python3 -B -I scripts/verify_simul_pack.py                         # verify the files in data/
    python3 -B -I scripts/verify_simul_pack.py --shards DIR            # also verify the full shards in DIR
    python3 -B -I scripts/verify_simul_pack.py --require-blind-clean   # exit 1 while any agent-visible cue remains
    python3 -B -I scripts/verify_simul_pack.py --emit-agent-projection all   # print the agent-side view of the sample

It prints a JSON receipt and exits 0 when every check passes, 1 otherwise. The receipt holds no clock time and no
machine path, so the same files always give the same bytes.

The receipt keeps four questions apart, because a pass on one says nothing about the others:

  archive_integrity_status     Layer 1. The bytes are the published ones: digests, strict JSONL, the card schema of
                               protocol simul-stage1-v1, and the fixed 20 x 5 = 100 sample rule.
  blind_readiness              Layer 2. A lexical screen of every card for wording that tells how the case ended, in
                               the evaluator field and in the text an agent would read. The published stage-1 archive
                               does carry such wording, so this reads "requires_curation_before_judged_run".
  record_consistency           The run, roster and model records in the status file agree with each other.
  empirical_judge_separation,  What the public record shows about a run that actually happened. The pack holds no
  empirical_blinding           turn log and no model input, so both read "not_verified_from_public_record".

A passing receipt therefore means: the archive is intact and the records are consistent. It does not mean the cards
are ready for a blind run, that any run was blind, or that any judge was separate from the speakers.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import errno
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

VERSION = "2.0.0"
RECEIPT_SCHEMA_VERSION = "simul-eval-receipt-v2"
ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------------------------------- protocol contract
# The field contract of protocol simul-stage1-v1 is fixed here, not read from any file. The schema file, the card
# index and the sample manifest are checked against these constants; a file that disagrees fails, it does not
# redefine the contract. The agent projection and every text inspection use AGENT_FIELDS from here.
PROTOCOL_VERSION = "simul-stage1-v1"
IDENTITY_FIELDS = ("card_id", "edition")
AGENT_FIELDS = ("channel", "opener", "participants", "situation", "options")
EVALUATOR_FIELDS = ("invariants",)
WITHHELD_FIELDS = ("reference_outcome", "refutation_condition")
CARD_FIELDS = IDENTITY_FIELDS + AGENT_FIELDS + EVALUATOR_FIELDS

# The sample policy of this protocol version. A manifest that states anything else fails.
EXPECTED_SHARDS = 20
CARDS_PER_SHARD = 5
EXPECTED_SAMPLE_COUNT = 100
SAMPLE_RULE_ID = "first_5_by_card_id_asc_per_shard"

CARD_ID_PATTERN = "^[0-9a-f]{12}$"
EDITION_PATTERN = "^(0[1-9]|[1-9][0-9]|100)$"

# The card schema of this protocol version. schema/stage1-card.schema.json must state exactly these constraints;
# cards are always validated against this copy, so an edited schema file cannot loosen the check.
CANONICAL_CARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(CARD_FIELDS),
    "properties": {
        "card_id": {"type": "string", "pattern": CARD_ID_PATTERN},
        "edition": {"type": "string", "pattern": EDITION_PATTERN},
        "channel": {"type": "string", "minLength": 1},
        "opener": {"type": "string", "minLength": 1},
        "participants": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "situation": {"type": "string", "minLength": 1},
        "options": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "invariants": {"type": ["array", "null"], "minItems": 1, "items": {"type": "string", "minLength": 1}},
    },
    "x-simul-protocol": PROTOCOL_VERSION,
    "x-simul-visibility": {
        "identity": list(IDENTITY_FIELDS),
        "agent": list(AGENT_FIELDS),
        "evaluator": list(EVALUATOR_FIELDS),
    },
    "x-simul-withheld": list(WITHHELD_FIELDS),
}

FILES = {
    "schema": "schema/stage1-card.schema.json",
    "index": "data/cards-s1-index.json",
    "sample": "data/sample-cards-s1.jsonl",
    "manifest": "data/sample-manifest.json",
    "status": "data/simul-status.json",
    "org": "data/s1-org.json",
}

# Keys that must never appear in a published card, at any depth: the two withheld fields, and common names for the
# same thing, refused so a renamed field cannot slip through.
FORBIDDEN_KEYS = frozenset(WITHHELD_FIELDS) | frozenset({
    "outcome", "actual_outcome", "real_outcome", "ground_truth", "answer", "correct_option", "chosen_option",
    "decision", "label", "verdict", "judge", "judgement", "judgment", "resolution", "source", "source_id",
    "provenance",
})
# The name of a withheld field, or a labelled outcome line, in the text an agent would read.
WITHHELD_MARKERS = re.compile(
    r"reference[ _-]outcome|refutation[ _-]condition|ground_truth|chosen_option|correct_option"
    r"|\[\s*outcome\s*\]|^\s*(?:reference |actual |real |final )?outcome\s*:",
    re.IGNORECASE | re.MULTILINE,
)

# Layer 2 lexicon. Each entry is (cue class, pattern). The screen is lexical: a hit is a candidate a curator must
# read, not a proven leak, and a card without a hit is not cleared. The classes below were chosen from wording found
# in the published cards, where a situation narrates how the source conversation went on or ended.
_CONVERSATION = r"(?:thread|exchange|conversation|discussion|chat|meeting|back-and-forth|excerpt)"
_DECISION = r"(?:decision|ruling|resolution|conclusion|commitment|verdict)"
_NARRATION = (
    # "The thread ends without a call", "The exchange ends with her colleague agreeing", "The discussion stalls"
    ("conversation_end", r"\b" + _CONVERSATION + r"\s+(?:ends?|ended|closes|closed|concludes|concluded|stalls|stalled"
                         r"|stops|stopped|trails\s+off|breaks\s+off|dies\s+out)\b"),
    # "without a decision", "without a settled answer", "with no decision taken", "No decision is recorded"
    ("no_decision_reached", r"\bwithout\s+(?:a|an|any)\s+(?:\w+\s+){0,2}" + _DECISION + r"\b"
                            r"|\bwithout\s+(?:a|an|any)\s+(?:final|settled|firm|clear|agreed)\s+(?:call|answer|change)\b"
                            r"|\bwithout\s+(?:a|any)\s+call\s+being\s+made\b"
                            r"|\bno\s+(?:\w+\s+)?(?:" + _DECISION + r"|call)\s+(?:is|was|has\s+been|had\s+been|gets|got)"
                            r"\s+(?:\w+\s+)?(?:made|taken|reached|recorded|given)\b"
                            r"|\bwith\s+no\s+" + _DECISION + r"\s+(?:made|taken|reached|recorded)\b"),
    # "The question is left open", "the matter stays unresolved"
    ("left_undecided", r"\b(?:question|matter|issue|point|decision|request|choice)\s+(?:is|was|stays|stayed|remains"
                       r"|remained|is\s+left|was\s+left)\s+(?:open|unresolved|undecided|unsettled|unanswered)\b"),
    # "drops it for now", "nobody in the excerpt settles which way the quote goes"
    ("source_narration", r"\b(?:drops|dropped)\s+(?:it\s+for\s+now|the\s+(?:matter|idea|question|request|issue))\b"
                         r"|\bin\s+the\s+excerpt\b|\bthe\s+excerpt\s+(?:ends|shows|stops)\b"),
)
# "Leila has already said the promotion would wait until next year": a ruling reported inside the situation.
_PRIOR_RULING = (
    ("prior_ruling_stated", r"\b(?:has|have|had)\s+already\s+(?:said|told|decided|agreed|confirmed|ruled|chosen"
                            r"|approved|declined|rejected|refused|settled|opted|committed|signed\s+off)\b"),
)


def _compile(entries):
    return tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in entries)


# The situation is screened for every class. An option is a proposed action, and "has already agreed" inside one is
# the reason offered for it, so options are screened for the narration classes only.
SITUATION_CUE_PATTERNS = _compile(_NARRATION + _PRIOR_RULING)
OPTION_CUE_PATTERNS = _compile(_NARRATION)
# The evaluator field is always stripped from an agent input. The count below says how often it would matter.
EVALUATOR_CUE_PATTERN = re.compile(
    r"\b(?:settled|settles|agreed|agrees|decided|decides|waived|enforced|approved|rejected|refused|declined|deferred"
    r"|postponed|chosen|chose|closed|closes|resolved|reversed|overruled|granted|denied|accepted|dropped|withdrawn"
    r"|cancell?ed|confirmed|upheld|conceded|escalated|stalls|stalled|undecided|unresolved|outcome|ruling|ruled"
    r"|left\s+open|no\s+decision|not\s+settled)\b",
    re.IGNORECASE,
)

SHA256_RE = re.compile(r"[0-9a-f]{64}")
SHA1_RE = re.compile(r"[0-9a-f]{40}")
EDITION_RE = re.compile(EDITION_PATTERN)
SHARD_RE = re.compile(r"/data/cards/s1/((?:0[1-9]|[1-9][0-9]|100)\.jsonl)")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STATUSES = ("active", "planned")
RECORDED_JUDGE_STATUSES = ("verified", "not_verified")
NOT_VERIFIED = "not_verified_from_public_record"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_JSON_DEPTH = 32


class PackError(Exception):
    """A file that cannot be read as the format it claims, or a card that breaks the protocol contract."""


class ProjectionError(PackError):
    """A card that cannot be turned into an agent input under the protocol contract."""


# ---------------------------------------------------------------------------------------------------- reading
def safe_relative_parts(rel_path):
    """Split a pack-relative path, refusing anything that could leave the root."""
    if not isinstance(rel_path, str) or not rel_path:
        raise PackError("path is empty")
    if "\x00" in rel_path or "\\" in rel_path or rel_path.startswith("/"):
        raise PackError(f"{rel_path!r}: not a relative POSIX path")
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackError(f"{rel_path!r}: path steps outside the root")
    return parts


def read_regular_file_bytes(root, rel_path, label=None, root_label="pack root"):
    """Read one regular file under `root`, once, without following any symbolic link.

    The root and every directory on the way are opened one step at a time relative to the directory before, with
    O_NOFOLLOW, so a link at any component is refused, and the last step must be a regular file. The bytes returned
    are the only read of the file: callers hash and parse this same value, so the digest in the receipt is the digest
    of what was parsed. Directories above the root belong to the caller and are not inspected.
    """
    label = label or rel_path
    parts = safe_relative_parts(rel_path)
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd):
        raise PackError(f"{label}: this platform cannot open files without following links")
    common = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    dir_flags = common | os.O_DIRECTORY
    file_flags = common | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    handles = []
    try:
        try:
            handles.append(os.open(os.fspath(Path(root)), dir_flags))
        except (OSError, ValueError):
            raise PackError(f"{root_label}: not a real directory (missing, not a directory, or a symbolic link)") from None
        for name in parts[:-1]:
            try:
                handles.append(os.open(name, dir_flags, dir_fd=handles[-1]))
            except (OSError, ValueError):
                raise PackError(f"{label}: directory {name!r} is missing or is a symbolic link") from None
        try:
            handle = os.open(parts[-1], file_flags, dir_fd=handles[-1])
        except (OSError, ValueError) as ex:
            if getattr(ex, "errno", None) in (errno.ELOOP, errno.EMLINK):
                raise PackError(f"{label}: is a symbolic link") from None
            raise PackError(f"{label}: cannot be read") from None
        handles.append(handle)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise PackError(f"{label}: not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise PackError(f"{label}: larger than {MAX_FILE_BYTES} bytes")
        chunks, total = [], 0
        while True:
            try:
                chunk = os.read(handle, 1 << 20)
            except OSError:
                raise PackError(f"{label}: cannot be read") from None
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise PackError(f"{label}: larger than {MAX_FILE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        for handle in reversed(handles):
            try:
                os.close(handle)
            except OSError:
                pass


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise PackError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _no_constants(name):
    raise PackError(f"non-finite number {name}")


def json_depth(value):
    """Nesting depth of a parsed JSON value, counted without recursion."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            deepest = max(deepest, depth)
            stack += [(item, depth + 1) for item in node.values()]
        elif isinstance(node, list):
            deepest = max(deepest, depth)
            stack += [(item, depth + 1) for item in node]
    return deepest


def parse_json(text):
    """Strict JSON: duplicate keys, NaN/Infinity, runaway nesting and unparseable numbers are refused."""
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except json.JSONDecodeError as ex:
        raise PackError(f"not JSON ({ex.msg} at line {ex.lineno} column {ex.colno})") from None
    except RecursionError:
        raise PackError("not JSON (nested too deeply)") from None
    except ValueError:
        raise PackError("not JSON (a number cannot be read)") from None
    if json_depth(value) > MAX_JSON_DEPTH:
        raise PackError(f"nested deeper than {MAX_JSON_DEPTH} levels")
    return value


def parse_json_bytes(data, label):
    """Parse the bytes already read for `label`. The caller hashes the same bytes."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PackError(f"{label}: not UTF-8") from None
    try:
        return parse_json(text)
    except PackError as ex:
        raise PackError(f"{label}: {ex}") from None


def read_jsonl(data, what):
    """Return (records, errors); records are (line number, object, line text). Every line must be one JSON object."""
    errors, records = [], []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [f"{what}: not UTF-8"]
    if not text:
        return [], [f"{what}: empty file"]
    if not text.endswith("\n"):
        errors.append(f"{what}: last line has no newline")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            errors.append(f"{what} line {number}: blank line")
            continue
        try:
            record = parse_json(line)
        except PackError as ex:
            errors.append(f"{what} line {number}: {ex}")
            continue
        if not isinstance(record, dict):
            errors.append(f"{what} line {number}: not a JSON object")
            continue
        records.append((number, record, line))
    return records, errors


# ---------------------------------------------------------------------------------------------------- shapes
def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}
# Only the two patterns of the protocol are ever compiled. No pattern is taken from a file.
_PATTERNS = {CARD_ID_PATTERN: re.compile(CARD_ID_PATTERN), EDITION_PATTERN: re.compile(EDITION_PATTERN)}


def validate(value, schema, where):
    """Validate a value against one of the schemas written in this file. `schema` is never read from a file."""
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(_TYPES[t](value) for t in types):
            return [f"{where}: expected {' or '.join(types)}, found {kind(value)}"]
    errors = []
    if "enum" in schema and not any(value == item and type(value) is type(item) for item in schema["enum"]):
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            errors.append(f"{where}: empty")
        if "pattern" in schema and not _PATTERNS[schema["pattern"]].search(value):
            errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: {value} is below {schema['minimum']}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: fewer than {schema['minItems']} item(s)")
        if "items" in schema:
            for k, item in enumerate(value):
                errors += validate(item, schema["items"], f"{where}[{k}]")
    elif isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required field {name!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for name, item in value.items():
            if name in properties:
                errors += validate(item, properties[name], f"{where}.{name}")
            elif extra is False:
                errors.append(f"{where}: field {name!r} is not in the schema")
            elif isinstance(extra, dict):
                errors += validate(item, extra, f"{where}.{name}")
    return errors


def _obj(required=None, optional=None, extra=None):
    required, optional = required or {}, optional or {}
    schema = {"type": "object", "required": list(required), "properties": {**required, **optional}}
    if extra is not None:
        schema["additionalProperties"] = extra
    return schema


def _arr(items, least=0):
    return {"type": "array", "minItems": least, "items": items}


_STR = {"type": "string", "minLength": 1}
_COUNT = {"type": "integer", "minimum": 1}
_ROUTE = _obj({"route": _STR, "model": _STR, "model_id": _STR, "family": _STR})

# The parts of each file this verifier reads, with their types. A file is checked against its shape before any rule
# that reads it runs, so a value of the wrong type is reported as an error and never reaches a lookup.
INDEX_SHAPE = _obj({
    "name": _STR,
    "updated": _STR,
    "roster_revision": _STR,
    "source_revision": _STR,
    "source_revision_note": _STR,
    "description": _STR,
    "fields": _obj(extra=_STR),
    "not_included": _obj(extra=_STR),
    "total": _COUNT,
    "editions": _arr(_obj({"edition": _STR, "org": _STR, "file": _STR, "cards": _COUNT, "sha256": _STR,
                           "owner_at_source": _STR}), 1),
})
MANIFEST_SHAPE = _obj({
    "name": _STR,
    "protocol_version": _STR,
    "policy": _STR,
    "rule": _STR,
    "shards": _COUNT,
    "per_shard": _COUNT,
    "total": _COUNT,
    "source_index_sha256": _STR,
    "sample_sha256": _STR,
    "cards": _obj(extra=_arr(_STR)),
})
ORG_SHAPE = _obj({
    "branch": _STR,
    "headcount": _COUNT,
    "company": _obj({"name": _STR}, {"industry": _STR, "stage": _STR}),
    "people": _arr(_obj({"id": _STR, "route": _STR}, {"name": _STR, "title": _STR, "reports_to": {"type": ["string", "null"]}}), 1),
    "channels": _arr(_obj({"id": _STR, "name": _STR, "members": {"type": ["string", "array"], "items": _STR}}, {"type": _STR}), 1),
}, {"note": _STR})
STATUS_SHAPE = _obj({
    "updated": _STR,
    "office_url": _STR,
    "office_edition": _STR,
    "pipeline": _arr(_obj({
        "id": _STR, "status": _STR, "name": _STR, "name_ko": _STR,
        "desc": _STR, "desc_ko": _STR, "status_label": _STR, "status_label_ko": _STR,
    }), 1),
    "branches": _arr(_obj({"id": _STR, "size": _COUNT, "status": _STR}, {
        "name": _STR, "name_ko": _STR, "desc": _STR, "desc_ko": _STR,
        "status_label": _STR, "status_label_ko": _STR,
    }), 1),
    "editions": _arr(_obj({"no": _STR, "status": _STR, "branch": _STR, "size": _COUNT}, {
        "domain_id": _STR, "domain": _STR, "product": _STR, "status_label": _STR, "url": _STR,
    }), 1),
    "s1_company": _obj({
        "name": _STR, "stage": _STR, "channels": _arr(_STR, 1),
        "roles": _arr(_obj({"title": _STR, "count": _COUNT}), 1), "models": _STR,
    }),
    "runs": _arr(_obj({
        "id": _STR, "date": _STR, "branch": _STR, "edition": _STR, "channel": _STR, "channel_id": _STR,
        "turns": _COUNT, "seconds": _COUNT, "routing_policy": _STR, "roster_snapshot": _STR,
        "agent_inputs": _STR, "agent_inputs_ko": _STR,
        "judge": _STR, "judge_ko": _STR,
        "matched_reference": _STR, "matched_reference_ko": _STR,
        "note": _STR,
        "judge_family_check": _obj({"status": _STR}, {"note": _STR, "note_ko": _STR}),
    }, {
        "test_only": {"type": "boolean"},
        "forcing_condition": {"type": ["string", "null"]},
        "observed": _obj(optional={
            "turn_log": _STR, "turn_log_ko": _STR,
            "judge_model_id": {"type": ["string", "null"], "minLength": 1},
            "speaker_model_ids": _arr(_STR),
        }),
    }), 1),
    "roster_snapshots": _arr(_obj({
        "id": _STR, "edition": _STR, "org": _STR, "branch": _STR, "date": _STR, "git_blob": _STR,
        "routes": _arr(_ROUTE, 1),
    }, {"source_revision": _STR, "file": _STR, "note": _STR, "note_ko": _STR}), 1),
    "routing_policies": _arr(_obj({"id": _STR, "rule": _STR}, {
        "desc": _STR, "desc_ko": _STR, "used_by": _arr(_STR), "superseded_by": _STR, "introduced": _STR,
    }), 1),
    "model_routes": _arr(_ROUTE, 1),
    "model_families": _obj(extra=_obj({"name": _STR, "maker": _STR})),
    "research": _obj({
        "method": _arr(_STR, 1), "method_ko": _arr(_STR, 1),
        "limits": _arr(_STR), "limits_ko": _arr(_STR),
        "generated_limits": _arr(_STR), "generated_limits_ko": _arr(_STR),
        "downloads": _arr(_obj({"id": _STR, "href": _STR, "label": _STR, "label_ko": _STR})),
        "code": _STR, "code_ko": _STR,
        "generated_method_judge_clause": _STR, "generated_method_judge_clause_ko": _STR,
    }, {"generated_note": _STR}),
    "principles": _arr(_obj({"name": _STR, "name_ko": _STR, "desc": _STR, "desc_ko": _STR}, {"id": _STR}), 1),
}, {
    "judge_models": {"type": ["object", "null"], "additionalProperties": _obj({"family": _STR}, {"model": _STR})},
})


# ---------------------------------------------------------------------------------------------------- schema file
_ANNOTATIONS = ("$schema", "$id", "title", "description")
_REQUIREMENTS_KEY = "x-simul-blind-run-requirements"
_UNORDERED = ("required", "type")


def _constraints(node, where, problems):
    """A schema node with its annotations removed and its unordered lists sorted, ready to compare."""
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in _ANNOTATIONS:
            if not isinstance(value, str):
                problems.append(f"{where}.{key}: expected string, found {kind(value)}")
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: _constraints(sub, f"{where}.properties.{name}", problems) for name, sub in value.items()}
        elif key == "items":
            out[key] = _constraints(value, f"{where}.items", problems)
        elif key in _UNORDERED and isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[key] = sorted(value)
        else:
            out[key] = value
    return out


def json_diff(found, expected, where):
    """Where two JSON values differ. Types are compared first, so no value is used as another type."""
    if kind(found) != kind(expected):
        return [f"{where}: expected {kind(expected)}, found {kind(found)}"]
    if isinstance(expected, dict):
        out = []
        for key in sorted(set(found) | set(expected)):
            if key not in found:
                out.append(f"{where}.{key}: missing")
            elif key not in expected:
                out.append(f"{where}.{key}: not part of protocol {PROTOCOL_VERSION}")
            else:
                out += json_diff(found[key], expected[key], f"{where}.{key}")
        return out
    if isinstance(expected, list):
        if len(found) != len(expected):
            return [f"{where}: expected {json.dumps(expected)}, found a list of {len(found)}"]
        out = []
        for k, (a, b) in enumerate(zip(found, expected)):
            out += json_diff(a, b, f"{where}[{k}]")
        return out
    if found != expected:
        return [f"{where}: expected {json.dumps(expected)}, found {json.dumps(found, ensure_ascii=False)[:80]}"]
    return []


def _exact_names(found, expected, where):
    if not isinstance(found, list) or not all(isinstance(name, str) for name in found):
        return [f"{where}: must be a list of field names, found {kind(found)}"]
    if len(set(found)) != len(found):
        return [f"{where}: a field name is repeated"]
    if set(found) != set(expected):
        return [f"{where}: must be exactly {sorted(expected)}, found {sorted(found)}"]
    return []


def check_schema_contract(schema, index):
    """The schema file and the card index must state the field contract fixed in this file, exactly."""
    if not isinstance(schema, dict):
        return [f"schema: expected object, found {kind(schema)}"]
    errors = []
    if schema.get("x-simul-protocol") != PROTOCOL_VERSION:
        errors.append(f"schema: x-simul-protocol must be {PROTOCOL_VERSION!r}")
    visibility = schema.get("x-simul-visibility")
    groups = (("identity", IDENTITY_FIELDS), ("agent", AGENT_FIELDS), ("evaluator", EVALUATOR_FIELDS))
    if not isinstance(visibility, dict):
        errors.append(f"schema: x-simul-visibility: expected object, found {kind(visibility)}")
    else:
        for group, expected in groups:
            errors += _exact_names(visibility.get(group), expected, f"schema: x-simul-visibility.{group}")
        unknown = sorted(set(visibility) - {group for group, _ in groups})
        if unknown:
            errors.append(f"schema: x-simul-visibility has unknown group(s) {unknown}")
        agent = visibility.get("agent")
        if isinstance(agent, list) and any(name in EVALUATOR_FIELDS for name in agent if isinstance(name, str)):
            errors.append("schema: invariants must not be agent-visible")
    errors += _exact_names(schema.get("x-simul-withheld"), WITHHELD_FIELDS, "schema: x-simul-withheld")
    errors += _exact_names(schema.get("required"), CARD_FIELDS, "schema: required")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        withheld = sorted(name for name in properties if name.casefold() in FORBIDDEN_KEYS)
        if withheld:
            errors.append(f"schema: withheld field(s) {withheld} are listed as card fields")
    requirements = schema.get(_REQUIREMENTS_KEY)
    if not isinstance(requirements, list) or not requirements or not all(isinstance(r, str) and r.strip() for r in requirements):
        errors.append(f"schema: {_REQUIREMENTS_KEY} must list the steps required before a judged blind run")
    # Everything else: the constraints must be the canonical ones, key for key.
    handled = ("x-simul-protocol", "x-simul-visibility", "x-simul-withheld", "required", _REQUIREMENTS_KEY)
    found = _constraints(schema, "schema", errors)
    expected = _constraints(CANONICAL_CARD_SCHEMA, "schema", [])
    errors += json_diff({k: v for k, v in found.items() if k not in handled},
                        {k: v for k, v in expected.items() if k not in handled}, "schema")
    # The card index describes the same fields.
    if not isinstance(index, dict):
        errors.append("card index: not readable, so its field list cannot be compared")
    else:
        fields, not_included = index.get("fields"), index.get("not_included")
        if not isinstance(fields, dict) or set(fields) != set(CARD_FIELDS):
            errors.append(f"card index: fields must name exactly {sorted(CARD_FIELDS)}")
        if not isinstance(not_included, dict) or set(not_included) != set(WITHHELD_FIELDS):
            errors.append(f"card index: not_included must name exactly {sorted(WITHHELD_FIELDS)}")
    return errors


# ---------------------------------------------------------------------------------------------------- cards
def forbidden_keys_in(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}{key}")
            found += forbidden_keys_in(item, f"{path}{key}.")
    elif isinstance(value, list):
        for k, item in enumerate(value):
            found += forbidden_keys_in(item, f"{path}{k}.")
    return found


def project_for_agent(card):
    """The only view of a card an agent may be given: the five AGENT_FIELDS, in that order, and nothing else.

    `invariants` (evaluator-only) and the identity fields are left out. A card that carries a withheld key at any
    depth, a field outside the protocol, or lacks an agent field is refused with ProjectionError: it is not repaired.
    The projection does not curate text. A situation that tells how the case ended is still in the result; see
    audit_blind_readiness.
    """
    if not isinstance(card, dict):
        raise ProjectionError(f"card: expected object, found {kind(card)}")
    withheld = forbidden_keys_in(card)
    if withheld:
        raise ProjectionError(f"card carries withheld key(s) {sorted(withheld)}")
    unknown = sorted(str(name) for name in card if name not in CARD_FIELDS)
    if unknown:
        raise ProjectionError(f"card carries field(s) outside protocol {PROTOCOL_VERSION}: {unknown}")
    missing = [name for name in AGENT_FIELDS if name not in card]
    if missing:
        raise ProjectionError(f"card lacks agent field(s) {missing}")
    return {name: copy.deepcopy(card[name]) for name in AGENT_FIELDS}


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def agent_text(projection):
    return "\n".join(text for name in AGENT_FIELDS for text in _strings(projection.get(name)))


def agent_cues(texts, patterns=SITUATION_CUE_PATTERNS):
    """Cue classes found in agent-visible text, with the first wording matched for each class.

    `texts` is one string or a list of strings. Each string is screened on its own, so wording is never matched
    across the boundary between two options.
    """
    found = []
    for name, pattern in patterns:
        for text in _strings(texts):
            match = pattern.search(text)
            if match:
                found.append({"class": name, "text": " ".join(match.group(0).split())})
                break
    return found


def projection_line(projection):
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":")) + "\n"


def check_cards(records, what):
    """Schema validation, withheld-field absence and card_id uniqueness for a list of (label, record)."""
    schema_errors, withheld_errors, id_errors = [], [], []
    seen = {}
    for label, record in records:
        where = f"{what} {label}"
        schema_errors += validate(record, CANONICAL_CARD_SCHEMA, where)
        for key in forbidden_keys_in(record):
            withheld_errors.append(f"{where}: forbidden field {key!r}")
        visible = "\n".join(text for name in AGENT_FIELDS for text in _strings(record.get(name)))
        marker = WITHHELD_MARKERS.search(visible)
        if marker:
            withheld_errors.append(f"{where}: agent-visible text contains {marker.group(0)!r}")
        card_id = record.get("card_id")
        if isinstance(card_id, str):
            if card_id in seen:
                id_errors.append(f"{where}: card_id {card_id} repeats {seen[card_id]}")
            else:
                seen[card_id] = label
    return schema_errors, withheld_errors, id_errors


def audit_blind_readiness(records, what, detail):
    """Layer 2. Project every card for an agent and screen both sides for wording that tells how the case ended.

    Returns (block, projection_errors). `detail` adds the matched wording per flagged card; it is left out for the
    full shards to keep the receipt short.
    """
    projection_errors, situation_flags, option_flags, matches = [], [], [], []
    evaluator_cues = stripped = 0
    all_digest, clear_digest = hashlib.sha256(), hashlib.sha256()
    clear = 0
    for label, record in records:
        card_id = record.get("card_id") if isinstance(record, dict) else None
        name = card_id if isinstance(card_id, str) else f"{what} {label}"
        try:
            projection = project_for_agent(record)
        except ProjectionError as ex:
            projection_errors.append(f"{what} {label}: {ex}")
            continue
        stripped += not any(field in projection for field in EVALUATOR_FIELDS + IDENTITY_FIELDS)
        if any(EVALUATOR_CUE_PATTERN.search(text) for field in EVALUATOR_FIELDS for text in _strings(record.get(field))):
            evaluator_cues += 1
        in_situation = agent_cues(projection["situation"])
        in_options = agent_cues(projection["options"], OPTION_CUE_PATTERNS)
        line = projection_line(projection).encode("utf-8")
        all_digest.update(line)
        if in_situation:
            situation_flags.append(name)
        if in_options:
            option_flags.append(name)
        if in_situation or in_options:
            if detail:
                matches.append({"card_id": name, "where": f"{what} {label}", "situation": in_situation, "options": in_options})
        else:
            clear += 1
            clear_digest.update(line)
    audited = len(records) - len(projection_errors)
    found_any = bool(evaluator_cues or situation_flags or option_flags or projection_errors)
    block = {
        "status": "requires_curation_before_judged_run" if found_any else "no_lexical_cue_found_review_still_required",
        "cards_audited": audited,
        "evaluator_field_stripped_in_projection": audited > 0 and stripped == audited,
        "evaluator_resolution_cue_count": evaluator_cues,
        "agent_visible_situation_cue_count": len(situation_flags),
        "agent_visible_option_cue_count": len(option_flags),
        "flagged_situation_card_ids": sorted(situation_flags),
        "flagged_option_card_ids": sorted(option_flags),
        "agent_projection": {
            "fields": list(AGENT_FIELDS),
            "all_cards": {"cards": audited, "sha256": all_digest.hexdigest()},
            "excluding_flagged_cards": {"cards": clear, "sha256": clear_digest.hexdigest()},
        },
    }
    if detail:
        block["flagged_agent_visible_matches"] = sorted(matches, key=lambda m: m["card_id"])
    return block, projection_errors


def card_observations(records):
    """Known irregular records. They pass the schema; a reader should know about them."""
    cards = [record for _, record in records if isinstance(record, dict)]

    def items(record, key):
        return record[key] if isinstance(record.get(key), list) else []

    found = {
        "single_option": [r for r in cards if len(items(r, "options")) == 1],
        "empty_option_text": [r for r in cards if any(isinstance(o, str) and not o.strip() for o in items(r, "options"))],
        "null_invariants": [r for r in cards if "invariants" in r and r["invariants"] is None],
        "opener_not_in_participants": [r for r in cards if isinstance(r.get("participants"), list)
                                       and not any(r.get("opener") == p for p in r["participants"])],
        "repeated_participant": [r for r in cards if len({json.dumps(p, sort_keys=True) for p in items(r, "participants")})
                                 != len(items(r, "participants"))],
    }
    return {
        "cards": len(cards),
        "counts": {name: len(rows) for name, rows in found.items()},
        "card_ids": {name: sorted(str(r.get("card_id")) for r in rows) for name, rows in found.items()},
    }


def check_index(index):
    errors = validate(index, INDEX_SHAPE, "card index")
    if errors:
        return errors, {}
    editions = index["editions"]
    for key in ("edition", "org", "file", "sha256"):
        if len({e[key] for e in editions}) != len(editions):
            errors.append(f"card index: {key} is repeated")
    total = 0
    for e in editions:
        slot = e["edition"]
        if not EDITION_RE.fullmatch(slot):
            errors.append(f"card index: bad edition number {slot!r}")
        name = SHARD_RE.fullmatch(e["file"])
        if not name or name.group(1) != f"{slot}.jsonl":
            errors.append(f"card index: edition {slot!r} names file {e['file']!r}")
        if not SHA256_RE.fullmatch(e["sha256"]):
            errors.append(f"card index: edition {slot!r} has no SHA-256")
        total += e["cards"]
    if index["total"] != total:
        errors.append(f"card index: total says {index['total']!r}, the shard counts add up to {total}")
    slots = [e["edition"] for e in editions]
    if slots != sorted(slots, key=lambda s: (int(s) if s.isdigit() and s.isascii() else -1, s)):
        errors.append("card index: editions are not in ascending order")
    return errors, {"shards": len(editions), "organizations": len({e["org"] for e in editions}), "cards_total": total}


def select_sample(lines_by_edition):
    """The v1 sample rule: per shard, in index order, the first CARDS_PER_SHARD records by ascending card_id."""
    out = []
    for _, lines in lines_by_edition:
        out += sorted(lines, key=lambda pair: pair[0])[:CARDS_PER_SHARD]
    return out


def check_sample_policy(records, index, manifest, index_bytes, sample_bytes):
    """The manifest must state the fixed v1 policy, and the sample must be exactly what it lists."""
    errors = validate(manifest, MANIFEST_SHAPE, "sample manifest")
    if errors:
        return errors
    fixed = (("protocol_version", PROTOCOL_VERSION), ("policy", SAMPLE_RULE_ID), ("shards", EXPECTED_SHARDS),
             ("per_shard", CARDS_PER_SHARD), ("total", EXPECTED_SAMPLE_COUNT))
    for key, expected in fixed:
        if manifest[key] != expected:
            errors.append(f"sample manifest: {key} is {manifest[key]!r}, protocol {PROTOCOL_VERSION} fixes it at {expected!r}")
    cards = manifest["cards"]
    if len(cards) != EXPECTED_SHARDS:
        errors.append(f"sample manifest: lists {len(cards)} shard(s), the policy fixes {EXPECTED_SHARDS}")
    for edition, ids in cards.items():
        if len(ids) != CARDS_PER_SHARD or ids != sorted(set(ids)):
            errors.append(f"sample manifest: edition {edition} does not list {CARDS_PER_SHARD} distinct card ids in ascending order")
    if not isinstance(index, dict) or validate(index, INDEX_SHAPE, "card index"):
        errors.append("sample manifest: the card index is not readable, so the sample cannot be bound to it")
    else:
        editions = [e["edition"] for e in index["editions"]]
        if len(editions) != EXPECTED_SHARDS:
            errors.append(f"sample: the card index lists {len(editions)} shard(s), the policy fixes {EXPECTED_SHARDS}")
        if list(cards) != editions:
            errors.append("sample manifest: editions differ from the card index, or are in another order")
    if manifest["source_index_sha256"] != hashlib.sha256(index_bytes).hexdigest():
        errors.append("sample manifest: source_index_sha256 is not the digest of data/cards-s1-index.json")
    if manifest["sample_sha256"] != hashlib.sha256(sample_bytes).hexdigest():
        errors.append("sample manifest: sample_sha256 is not the digest of data/sample-cards-s1.jsonl")
    expected = [(edition, card_id) for edition in cards for card_id in cards[edition]]
    actual = [(record.get("edition"), record.get("card_id")) for _, record in records]
    if actual != expected:
        errors.append("sample: the cards are not the ones the manifest lists, in its order")
    if len(records) != EXPECTED_SAMPLE_COUNT:
        errors.append(f"sample: {len(records)} cards, the policy fixes {EXPECTED_SAMPLE_COUNT}")
    return errors


# ---------------------------------------------------------------------------------------------------- status
def git_blob_sha1(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def valid_date(text):
    if not DATE_RE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def check_org(org, status):
    errors = validate(org, ORG_SHAPE, "s1-org")
    if errors:
        return errors, {}
    people, channels = org["people"], org["channels"]
    ids = [p["id"] for p in people]
    if len(set(ids)) != len(ids):
        errors.append("s1-org: a member id is repeated")
    if org["headcount"] != len(people):
        errors.append(f"s1-org: headcount says {org['headcount']!r}, {len(people)} people are listed")
    if status is None:
        errors.append("s1-org: the status file is not usable, so branch size and model routes cannot be compared")
    else:
        branch = next((b for b in status["branches"] if b["id"] == org["branch"]), None)
        if branch is None or branch["size"] != len(people):
            errors.append("s1-org: headcount does not match its branch in simul-status.json")
        routes = {r["route"] for r in status["model_routes"]}
        for p in people:
            if p["route"] not in routes:
                errors.append(f"s1-org: {p['id']} has an unknown model route {p['route']!r}")
    for p in people:
        boss = p.get("reports_to")
        if boss is not None and boss not in ids:
            errors.append(f"s1-org: {p['id']} reports to an unknown member")
    if len({c["id"] for c in channels}) != len(channels):
        errors.append("s1-org: a channel id is repeated")
    for c in channels:
        members = c["members"]
        if isinstance(members, str):
            if members != "all":
                errors.append(f"s1-org: channel {c['id']!r} has members {members!r}; only the word 'all' or a list is allowed")
        elif not members or not set(members) <= set(ids):
            errors.append(f"s1-org: channel {c['id']!r} lists an unknown member, or nobody")
    return errors, {"people": len(people), "channels": len(channels)}


def check_status(status, index_summary):
    """Edition, branch and routing-policy records, and the card figures the status file states."""
    errors = []
    by_branch = {}
    for b in status["branches"]:
        if b["id"] in by_branch:
            errors.append(f"status: branch id {b['id']!r} is repeated")
        by_branch[b["id"]] = b
        if b["status"] not in STATUSES:
            errors.append(f"status: branch {b['id']!r} has status {b['status']!r}")
    numbers, seen = [], set()
    for e in status["editions"]:
        no = e["no"]
        if not EDITION_RE.fullmatch(no):
            errors.append(f"status: bad edition number {no!r}")
            continue
        if no in seen:
            errors.append(f"status: edition {no} is repeated")
        seen.add(no)
        numbers.append(int(no))
        if e["status"] not in STATUSES:
            errors.append(f"status: edition {no} has status {e['status']!r}")
        branch = by_branch.get(e["branch"])
        if branch is None:
            errors.append(f"status: edition {no} names unknown branch {e['branch']!r}")
        elif e["size"] != branch["size"]:
            errors.append(f"status: edition {no} has size {e['size']!r}, its branch has {branch['size']!r}")
    if numbers != sorted(numbers):
        errors.append("status: editions are not in ascending order")
    policy_ids = [p["id"] for p in status["routing_policies"]]
    if len(set(policy_ids)) != len(policy_ids):
        errors.append("status: a routing policy id is repeated")
    run_ids = [r["id"] for r in status["runs"]]
    if len(set(run_ids)) != len(run_ids):
        errors.append("status: a run id is repeated")
    if not index_summary:
        errors.append("status: the card index is not usable, so the card figures cannot be compared")
    else:
        total, orgs = index_summary["cards_total"], index_summary["organizations"]
        labels = [d["label"] for d in status["research"]["downloads"] if d["id"] == "cards-s1"]
        if len(labels) != 1 or f"{total:,} in {orgs} synthetic companies" not in labels[0]:
            errors.append(f"status: the cards download label does not say {total:,} in {orgs} synthetic companies")
    editions, branches, runs = status["editions"], status["branches"], status["runs"]
    summary = {
        "editions": len(editions),
        "editions_active": sum(1 for e in editions if e["status"] == "active"),
        "editions_planned": sum(1 for e in editions if e["status"] == "planned"),
        "branches": len(branches),
        "branches_active": sum(1 for b in branches if b["status"] == "active"),
        "runs": len(runs),
        "test_only_runs": sum(1 for r in runs if r.get("test_only") is True),
    }
    return errors, summary


def check_model_families(status):
    """One model id has one family, wherever it is declared. Returns (errors, {model_id: family})."""
    errors, table, origin = [], {}, {}
    families = status["model_families"]
    declared = [("model_routes", r["model_id"], r["family"]) for r in status["model_routes"]]
    for snapshot in status["roster_snapshots"]:
        declared += [(f"roster snapshot {snapshot['id']}", r["model_id"], r["family"]) for r in snapshot["routes"]]
        routes = [r["route"] for r in snapshot["routes"]]
        if len(set(routes)) != len(routes):
            errors.append(f"status: roster snapshot {snapshot['id']} lists a route twice")
    for model_id, entry in (status.get("judge_models") or {}).items():
        declared.append(("judge_models", model_id, entry["family"]))
    routes = [r["route"] for r in status["model_routes"]]
    if len(set(routes)) != len(routes):
        errors.append("status: model_routes lists a route twice")
    for where, model_id, family in declared:
        if family not in families:
            errors.append(f"status: {where} places model {model_id!r} in family {family!r}, which is not in model_families")
        if model_id in table and table[model_id] != family:
            errors.append(f"status: model {model_id!r} is declared in family {table[model_id]!r} by {origin[model_id]} "
                          f"and in family {family!r} by {where}")
        else:
            table.setdefault(model_id, family)
            origin.setdefault(model_id, where)
    return errors, table


def check_run_references(status, org, org_bytes, index=None):
    """Every reference a run makes must resolve, whether or not the run names a judge.

    Returns (errors, {run id: speakers or None}). `speakers` is the set of routes, models and families open to the
    run's channel under its routing policy, taken from the roster snapshot of the run.
    """
    errors, speakers_by_run = [], {}
    by_edition = {e["no"]: e for e in status["editions"]}
    branches = {b["id"] for b in status["branches"]}
    policies = {p["id"]: p for p in status["routing_policies"]}
    idx_by_edition = {e["edition"]: e for e in index["editions"]} if index else {}
    idx_by_org = {e["org"]: e for e in index["editions"]} if index else {}
    snapshots = {}
    for s in status["roster_snapshots"]:
        if s["id"] in snapshots:
            errors.append(f"status: roster snapshot id {s['id']!r} is repeated")
        snapshots[s["id"]] = s
        if not SHA1_RE.fullmatch(s["git_blob"]):
            errors.append(f"status: roster snapshot {s['id']} has no git blob id")
        if not valid_date(s["date"]):
            errors.append(f"status: roster snapshot {s['id']} has a bad date {s['date']!r}")
    org_blob = git_blob_sha1(org_bytes) if org_bytes is not None else None
    for run in status["runs"]:
        rid, found = run["id"], []
        if not valid_date(run["date"]):
            found.append(f"run {rid}: date {run['date']!r} is not a calendar date (YYYY-MM-DD)")
        edition = by_edition.get(run["edition"])
        if edition is None:
            found.append(f"run {rid}: names unknown edition {run['edition']!r}")
        elif run["branch"] != edition["branch"]:
            found.append(f"run {rid}: is on branch {run['branch']!r}, its edition is on {edition['branch']!r}")
        if run["branch"] not in branches:
            found.append(f"run {rid}: names unknown branch {run['branch']!r}")
        policy = policies.get(run["routing_policy"])
        if policy is None:
            found.append(f"run {rid}: names unknown routing policy {run['routing_policy']!r}")
        listed = [p["id"] for p in status["routing_policies"] if rid in p.get("used_by", [])]
        if listed != [run["routing_policy"]]:
            found.append(f"run {rid}: must be listed in used_by by its own routing policy only (listed by {listed})")
        snapshot = snapshots.get(run["roster_snapshot"])
        if snapshot is None:
            found.append(f"run {rid}: roster snapshot {run['roster_snapshot']!r} is not in the status file")
        else:
            for key in ("edition", "branch", "date"):
                if snapshot[key] != run[key]:
                    found.append(f"run {rid}: {key} is {run[key]!r}, its roster snapshot {snapshot['id']} says {snapshot[key]!r}")
            idx_ed = idx_by_edition.get(run["edition"])
            if idx_ed is not None and snapshot["org"] != idx_ed["org"]:
                found.append(f"run {rid}: edition {run['edition']!r} belongs to org {idx_ed['org']!r} in the card index, "
                             f"not roster snapshot org {snapshot['org']!r}")
            idx_org = idx_by_org.get(snapshot["org"])
            if idx_org is not None and idx_org["edition"] != run["edition"]:
                found.append(f"run {rid}: roster snapshot org {snapshot['org']!r} belongs to edition {idx_org['edition']!r} "
                             f"in the card index, not {run['edition']!r}")
            if org is not None:
                org_slug = re.sub(r"[^a-z0-9]+", "-", org["company"]["name"].lower()).strip("-")
                if org_slug != snapshot["org"]:
                    found.append(f"run {rid}: s1-org company {org['company']['name']!r} does not match roster snapshot org {snapshot['org']!r}")
                if run["branch"] == "S1" and status["s1_company"]["name"] != org["company"]["name"]:
                    found.append(f"run {rid}: s1_company {status['s1_company']['name']!r} differs from s1-org company {org['company']['name']!r}")
        speakers = None
        if org is None:
            found.append(f"run {rid}: data/s1-org.json is not usable, so the channel and its members cannot be checked")
        elif snapshot is not None:
            if snapshot["git_blob"] != org_blob:
                found.append(f"run {rid}: data/s1-org.json is not the roster snapshot {snapshot['id']} (git blob differs)")
            if org["branch"] != run["branch"]:
                found.append(f"run {rid}: is on branch {run['branch']!r}, the roster is for branch {org['branch']!r}")
            people = {p["id"]: p for p in org["people"]}
            channel = next((c for c in org["channels"] if c["id"] == run["channel_id"]), None)
            members = []
            if channel is None or channel["name"] != run["channel"]:
                found.append(f"run {rid}: channel {run['channel_id']!r} is not in the roster under the name {run['channel']!r}")
            else:
                members = list(people) if channel["members"] == "all" else list(channel["members"])
                if isinstance(channel["members"], str) and channel["members"] != "all":
                    members = []
                if not members or any(m not in people for m in members):
                    found.append(f"run {rid}: channel {run['channel_id']!r} has an unknown member, or nobody")
                    members = []
            table = {r["route"]: r for r in snapshot["routes"]}
            rule = policy["rule"] if policy is not None else None
            if policy is not None and rule not in ("role", "listener"):
                found.append(f"run {rid}: routing rule {rule!r} is not known")
            if members and rule in ("role", "listener"):
                used = {people[m]["route"] for m in members}
                if rule == "listener" and "default" in used:
                    used = {"default"}
                if not used <= set(table):
                    found.append(f"run {rid}: a member route is not in the snapshot's route table")
                else:
                    speakers = {
                        "families": sorted({table[r]["family"] for r in used}),
                        "routes": sorted(used),
                        "models": sorted({table[r]["model_id"] for r in used}),
                    }
                    for model in run.get("observed", {}).get("speaker_model_ids", []):
                        if model not in speakers["models"]:
                            found.append(f"run {rid}: observed speaker {model!r} is not a route open to channel {run['channel']}")
        errors += found
        speakers_by_run[rid] = None if found else speakers
    return errors, speakers_by_run


def check_judges(status, speakers_by_run, family_of):
    """What each run's record declares about its judge, against what the status file says about it.

    record_state is a statement about the record only:
      no_judge_model_in_record         the record names no judge model
      judge_family_distinct_in_record  the declared judge family is outside every declared speaker family
      judge_shares_speaker_family      the declared judge family is also a speaker family (fails)
      invalid                          the judge's family, or the run's speakers, cannot be established (fails)
    None of these is evidence about the run itself: see empirical_judge_separation in the receipt.
    """
    errors, rows = [], []
    for run in status["runs"]:
        rid = run["id"]
        speakers = speakers_by_run.get(rid)
        judge = run.get("observed", {}).get("judge_model_id")
        if judge is None:
            state = "no_judge_model_in_record"
        elif judge not in family_of or family_of[judge] not in status["model_families"]:
            state = "invalid"
            errors.append(f"run {rid}: judge model {judge!r} has no known family in judge_models")
        elif speakers is None:
            state = "invalid"
            errors.append(f"run {rid}: speaker families cannot be established, so the judge cannot be checked")
        elif family_of[judge] in speakers["families"]:
            state = "judge_shares_speaker_family"
            errors.append(f"run {rid}: judge family {family_of[judge]} is also a speaker family in channel {run['channel']}")
        else:
            state = "judge_family_distinct_in_record"
        recorded = run["judge_family_check"]["status"]
        supported = {"no_judge_model_in_record": "not_verified", "judge_family_distinct_in_record": "verified"}.get(state)
        if recorded not in RECORDED_JUDGE_STATUSES:
            errors.append(f"run {rid}: judge_family_check.status is {recorded!r}")
        elif supported is not None and recorded != supported:
            errors.append(f"run {rid}: judge_family_check says {recorded}, the record supports {supported}")
        rows.append({
            "run": rid,
            "judge_model_id": judge,
            "speaker_families": speakers["families"] if speakers else None,
            "recorded_status": recorded,
            "record_state": state,
        })
    undeclared = [row["run"] for row in rows if row["record_state"] != "judge_family_distinct_in_record"]
    if undeclared and "not verified" not in " ".join(status["research"]["generated_limits"]):
        errors.append("status: a run is not verified but research.generated_limits does not say so")
    return errors, rows


# ---------------------------------------------------------------------------------------------------- full shards
def check_shards(shard_dir, index, sample_bytes):
    """Optional: verify the full shards against the index, then re-derive the sample from them."""
    errors, digests, lines_by_edition, all_records = [], [], [], []
    for e in index["editions"]:
        match = SHARD_RE.fullmatch(e["file"])
        if not match:
            continue
        name = match.group(1)
        try:
            data = read_regular_file_bytes(shard_dir, name, label=f"shards/{name}", root_label="shards directory")
        except PackError as ex:
            errors.append(str(ex))
            continue
        digest = hashlib.sha256(data).hexdigest()
        digests.append({"path": f"shards/{name}", "bytes": len(data), "sha256": digest})
        if digest != e["sha256"]:
            errors.append(f"shards: {name} has SHA-256 {digest}, the index says {e['sha256']}")
        records, read_errors = read_jsonl(data, name)
        errors += read_errors
        if len(records) != e["cards"]:
            errors.append(f"shards: {name} holds {len(records)} cards, the index says {e['cards']}")
        keyed = []
        for line, record, text in records:
            if record.get("edition") != e["edition"]:
                errors.append(f"shards: {name} line {line} belongs to edition {record.get('edition')!r}")
            card_id = record.get("card_id")
            keyed.append((card_id if isinstance(card_id, str) else "", text))
        lines_by_edition.append((e["edition"], keyed))
        all_records += [(f"{name}:{line}", record) for line, record, _ in records]
    schema_errors, withheld_errors, id_errors = check_cards(all_records, "shards")
    errors += schema_errors + withheld_errors + id_errors
    derived = "".join(text + "\n" for _, text in select_sample(lines_by_edition))
    if derived.encode("utf-8") != sample_bytes:
        errors.append("shards: the sample is not what the sample rule gives for these shards")
    if len(all_records) != index["total"]:
        errors.append(f"shards: {len(all_records)} cards in all, the index says {index['total']!r}")
    return errors, digests, all_records


# ---------------------------------------------------------------------------------------------------- receipt
CHECK_TITLES = {
    "archive.files_readable": "Every pack file is a regular file inside the pack, read once, and parses as strict JSON",
    "archive.schema_contract": "Schema file and card index state the fixed field contract of the protocol, exactly",
    "archive.index_consistent": "Card index: unique shards, well-formed digests, total equals the sum of shard counts",
    "archive.sample_jsonl": "Sample: every line is one JSON object, no blank or malformed line",
    "archive.sample_schema": "Sample: every card has exactly the protocol's fields, with the right types",
    "archive.sample_card_id_unique": "Sample: card_id is unique",
    "archive.sample_withheld_absent": "Sample: no withheld field at any depth, and no withheld-field name in agent-visible text",
    "archive.sample_policy": "Sample: the fixed 20 x 5 = 100 policy, exactly the cards the manifest lists, bound to this card index",
    "archive.shards_full": "Full shards: digests, counts, schema, withheld fields, global card_id uniqueness; the sample re-derives",
    "blind_readiness.projection_contract": "Every card projects to exactly the five agent fields; the evaluator field is stripped",
    "blind_readiness.require_clean": "No agent-visible text carries a post-decision cue (requested with --require-blind-clean)",
    "record_consistency.status_counts": "Status: edition, branch and routing-policy records are unique and refer to each other",
    "record_consistency.roster": "Roster: headcount, reporting lines, channels and model routes resolve",
    "record_consistency.model_families": "Status: each model id has one family across model_routes, roster_snapshots and judge_models",
    "record_consistency.run_references": "Status: each run's date, edition, branch, roster snapshot, channel and members resolve",
    "record_consistency.judge_family": "Status: each run's judge-family status says what its record supports",
    "verifier.internal_error": "The verifier finished without an unexpected error",
}


def _add(checks, check_id, errors):
    checks.append({
        "id": check_id,
        "layer": check_id.split(".", 1)[0],
        "title": CHECK_TITLES[check_id],
        "result": "fail" if errors else "pass",
        "errors": list(errors),
    })


def _layer_status(checks, layer):
    results = [c["result"] for c in checks if c["layer"] == layer]
    if not results:
        return "not_evaluated"
    return "pass" if all(r == "pass" for r in results) else "fail"


def _assemble(checks, **parts):
    overall = "pass" if checks and all(c["result"] == "pass" for c in checks) else "fail"
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tool": "verify_simul_pack",
        "version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "overall_status": overall,
        "overall_status_scope": "archive_integrity and record_consistency checks, plus blind_readiness.require_clean "
                                "when it was requested. It is not a statement that the cards are ready for a blind run.",
        "archive_integrity_status": _layer_status(checks, "archive"),
        "record_consistency": _layer_status(checks, "record_consistency"),
        "checks": checks,
        "errors": [{"check": c["id"], "message": message} for c in checks for message in c["errors"]],
    }
    receipt.update(parts)
    return receipt


def failure_receipt(check_id, message):
    """A receipt for a run that could not be completed. Same top-level keys as any other receipt."""
    checks = []
    _add(checks, check_id, [message])
    return _assemble(checks)


def verify(root=ROOT, shards=None, require_blind_clean=False):
    checks, artifacts = [], []
    raw, loaded, load_errors = {}, {}, []
    for key, rel in FILES.items():
        try:
            raw[key] = read_regular_file_bytes(root, rel)
        except PackError as ex:
            load_errors.append(str(ex))
            continue
        artifacts.append({"path": rel, "bytes": len(raw[key]), "sha256": hashlib.sha256(raw[key]).hexdigest()})
        if rel.endswith(".json"):
            try:
                value = parse_json_bytes(raw[key], rel)
                if not isinstance(value, dict):
                    raise PackError(f"{rel}: expected object, found {kind(value)}")
                loaded[key] = value
            except PackError as ex:
                load_errors.append(str(ex))
    _add(checks, "archive.files_readable", load_errors)

    schema, index, manifest = loaded.get("schema"), loaded.get("index"), loaded.get("manifest")
    status, org = loaded.get("status"), loaded.get("org")
    summary, observations = {}, {}

    # ---- Layer 1: archive integrity
    _add(checks, "archive.schema_contract",
         check_schema_contract(schema, index) if schema is not None else ["schema: not readable"])
    index_errors, index_summary = check_index(index) if index is not None else (["card index: not readable"], {})
    _add(checks, "archive.index_consistent", index_errors)
    index_ok = index is not None and not index_errors
    if index_summary:
        summary["card_index"] = index_summary

    sample_records, sample_audit, projection_errors = [], None, []
    if "sample" in raw:
        records, read_errors = read_jsonl(raw["sample"], "sample")
        sample_records = [(f"line {line}", record) for line, record, _ in records]
        _add(checks, "archive.sample_jsonl", read_errors)
        schema_errors, withheld_errors, id_errors = check_cards(sample_records, "sample")
        if index_ok:
            editions = {e["edition"] for e in index["editions"]}
            schema_errors += [f"sample {label}: edition {record.get('edition')!r} is not in the card index"
                              for label, record in sample_records
                              if not isinstance(record.get("edition"), str) or record["edition"] not in editions]
        _add(checks, "archive.sample_schema", schema_errors)
        _add(checks, "archive.sample_card_id_unique", id_errors)
        _add(checks, "archive.sample_withheld_absent", withheld_errors)
        _add(checks, "archive.sample_policy",
             check_sample_policy(sample_records, index, manifest, raw.get("index", b""), raw.get("sample", b""))
             if manifest is not None else ["sample manifest: not readable"])
        observations["sample"] = card_observations(sample_records)
        sample_audit, projection_errors = audit_blind_readiness(sample_records, "sample", detail=True)
    else:
        for check_id in ("archive.sample_jsonl", "archive.sample_schema", "archive.sample_card_id_unique",
                         "archive.sample_withheld_absent", "archive.sample_policy"):
            _add(checks, check_id, ["sample: not readable"])

    shard_audit = None
    if shards is not None:
        if index_ok and "sample" in raw:
            shard_errors, shard_digests, shard_records = check_shards(shards, index, raw["sample"])
            artifacts += shard_digests
            observations["shards"] = card_observations(shard_records)
            shard_audit, shard_projection_errors = audit_blind_readiness(shard_records, "shards", detail=False)
            projection_errors += shard_projection_errors
        else:
            shard_errors = ["shards: not evaluated, because the card index or the sample is not usable"]
        _add(checks, "archive.shards_full", shard_errors)

    # ---- Layer 2: blind readiness
    if sample_audit is None:
        projection_errors = projection_errors + ["sample: not readable, so no card was projected"]
    _add(checks, "blind_readiness.projection_contract", projection_errors)
    blind = {
        "status": "not_evaluated",
        "method": "Lexical screen of the evaluator field and of the agent-visible situation and options. A hit is a "
                  "candidate for curation, not a proven leak; a card without a hit is not cleared. Semantic review "
                  "of every card used in a judged run is still required.",
        "required_before_judged_run": [
            "Build every agent input with project_for_agent: the five agent fields only, invariants stripped.",
            "Rewrite to the moment before the decision, or exclude, every card listed in flagged_situation_card_ids "
            "and flagged_option_card_ids, then review the remaining cards.",
            "Keep the published bytes unchanged and record the digest of the curated agent projection that was used.",
        ],
    }
    if sample_audit is not None:
        blind.update(sample_audit)
        blind["sample_cards_audited"] = blind.pop("cards_audited")
        if shard_audit is not None:
            blind["shards"] = shard_audit
            if shard_audit["status"].startswith("requires"):
                blind["status"] = shard_audit["status"]
    if require_blind_clean:
        dirty = []
        for name, block in (("sample", sample_audit), ("shards", shard_audit)):
            if block is None:
                if name == "sample" or shards is not None:
                    dirty.append(f"{name}: not audited, so it cannot be called clean")
                continue
            count = len(set(block["flagged_situation_card_ids"]) | set(block["flagged_option_card_ids"]))
            if count:
                dirty.append(f"{name}: {count} card(s) carry a post-decision cue in agent-visible text "
                             f"({block['agent_visible_situation_cue_count']} in situation, "
                             f"{block['agent_visible_option_cue_count']} in options)")
        _add(checks, "blind_readiness.require_clean", dirty + projection_errors)

    # ---- Record consistency
    status_ok = False
    if status is None:
        _add(checks, "record_consistency.status_counts", ["status: not readable"])
    else:
        shape_errors = validate(status, STATUS_SHAPE, "status")
        if shape_errors:
            _add(checks, "record_consistency.status_counts", shape_errors)
        else:
            status_ok = True
            status_errors, status_summary = check_status(status, index_summary if index_ok else {})
            _add(checks, "record_consistency.status_counts", status_errors)
            summary["status"] = status_summary
    org_errors, org_summary = (check_org(org, status if status_ok else None) if org is not None
                               else (["s1-org: not readable"], {}))
    org_ok = org is not None and not validate(org, ORG_SHAPE, "s1-org")
    _add(checks, "record_consistency.roster", org_errors)
    if org_summary:
        summary["roster"] = org_summary
    judge_rows = None
    if status_ok:
        family_errors, family_of = check_model_families(status)
        _add(checks, "record_consistency.model_families", family_errors)
        run_errors, speakers_by_run = check_run_references(status, org if org_ok else None, raw.get("org"), index if index_ok else None)
        _add(checks, "record_consistency.run_references", run_errors)
        judge_errors, judge_rows = check_judges(status, speakers_by_run, family_of)
        _add(checks, "record_consistency.judge_family", judge_errors)
    else:
        for check_id in ("record_consistency.model_families", "record_consistency.run_references",
                         "record_consistency.judge_family"):
            _add(checks, check_id, ["status: not usable, so this rule was not evaluated"])

    # ---- What the public record shows about runs that happened
    runs = len(judge_rows) if judge_rows is not None else 0
    if judge_rows is None:
        judge_separation = {"status": "not_evaluated", "verified_runs": 0, "not_verified_runs": 0}
        blinding = {"status": "not_evaluated", "verified_runs": 0, "not_verified_runs": 0}
    else:
        judge_separation = {
            "status": NOT_VERIFIED,
            "verified_runs": 0,
            "not_verified_runs": runs,
            "record_declares_distinct_judge_family_runs": sum(
                1 for row in judge_rows if row["record_state"] == "judge_family_distinct_in_record"),
            "basis": "The pack holds no turn log and no judge transcript. A judge family named in the status file is a "
                     "self-declared record; agreement inside that record is reported under record_consistency and is "
                     "not evidence of which model judged a run.",
            "runs": judge_rows,
        }
        blinding = {
            "status": NOT_VERIFIED,
            "verified_runs": 0,
            "not_verified_runs": runs,
            "basis": "The pack holds no per-turn model input. Whether an agent in a run received only the five agent "
                     "fields, and whether the situation it received told how the case ended, cannot be established "
                     "from the public record.",
        }

    return _assemble(
        checks,
        blind_readiness=blind,
        empirical_judge_separation=judge_separation,
        empirical_blinding=blinding,
        summary=summary,
        observations=observations,
        sample_policy={"rule_id": SAMPLE_RULE_ID, "shards": EXPECTED_SHARDS, "per_shard": CARDS_PER_SHARD,
                       "total": EXPECTED_SAMPLE_COUNT},
        field_contract={"identity": list(IDENTITY_FIELDS), "agent": list(AGENT_FIELDS),
                        "evaluator": list(EVALUATOR_FIELDS), "withheld": list(WITHHELD_FIELDS)},
        artifacts=sorted(artifacts, key=lambda a: a["path"]),
        not_checked=[
            "Whether a sentence an agent sees gives the outcome away in words the lexicon does not list.",
            "The reference outcomes and refutation conditions: they are withheld and are not in this pack.",
            "Model behaviour: this pack holds no model transcript, so no run is re-scored here.",
            "Whether the one recorded test run was blind, and which model judged it.",
        ] + ([] if shards is not None else ["The full shards: pass --shards DIR to check them against the card index."]),
    )


def render(receipt):
    return json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def emit_agent_projection(root, which):
    """The agent-side view of the sample as JSONL: `all` cards, or only those the lexical screen did not flag."""
    records, errors = read_jsonl(read_regular_file_bytes(root, FILES["sample"]), "sample")
    if errors:
        raise PackError(errors[0])
    out = []
    for line, record, _ in records:
        projection = project_for_agent(record)
        if which == "unflagged" and (agent_cues(projection["situation"])
                                     or agent_cues(projection["options"], OPTION_CUE_PATTERNS)):
            continue
        out.append(projection_line(projection))
    return "".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the SIMUL public pack and print a JSON receipt.")
    parser.add_argument("--root", type=Path, default=ROOT, help="pack root (default: the repository this script is in)")
    parser.add_argument("--shards", type=Path, default=None, help="directory holding the full NN.jsonl shards")
    parser.add_argument("--require-blind-clean", action="store_true",
                        help="fail (exit 1) while any card carries a post-decision cue in agent-visible text")
    parser.add_argument("--emit-agent-projection", choices=("all", "unflagged"), default=None,
                        help="print the agent projection of the sample as JSONL instead of a receipt")
    args = parser.parse_args(argv)
    try:
        receipt = verify(args.root, args.shards, args.require_blind_clean)
        if args.emit_agent_projection:
            if receipt["overall_status"] != "pass":
                sys.stdout.write(render(receipt))
                return 1
            sys.stdout.write(emit_agent_projection(args.root, args.emit_agent_projection))
            return 0
    except PackError as ex:
        receipt = failure_receipt("archive.files_readable", str(ex))
    except Exception as ex:  # never a traceback: an unexpected error is still a structured failure
        receipt = failure_receipt("verifier.internal_error", f"unexpected {type(ex).__name__}; please report it")
    sys.stdout.write(render(receipt))
    return 0 if receipt["overall_status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
