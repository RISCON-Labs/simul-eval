"""Tests for scripts/verify_simul_pack.py. Standard library only.

    python3 -B -m unittest
    python3 -B -m unittest discover -s tests

Both commands find the same tests. Each negative test copies the pack into a temporary directory, breaks one thing,
and checks that the verifier fails on the check that owns that rule. The shipped pack must pass and must reproduce
expected-receipt.json.
"""
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # loading the scripts below must not leave compiled files in the repository

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "verify_simul_pack.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v = _load("verify_simul_pack", SCRIPT)
make_sample = _load("make_sample", REPO / "scripts" / "make_sample.py")

SAMPLE = "data/sample-cards-s1.jsonl"
STATUS = "data/simul-status.json"
ORG = "data/s1-org.json"
INDEX = "data/cards-s1-index.json"
MANIFEST = "data/sample-manifest.json"
SCHEMA = "schema/stage1-card.schema.json"
JSON_FILES = (SCHEMA, INDEX, MANIFEST, STATUS, ORG)
NESTED = "SIMUL_EVAL_NESTED_TEST_RUN"

# Endings of published situations that an audit found to tell how the case ended (shard file and line in brackets).
PUBLISHED_SITUATION_ENDINGS = {
    "sample line 54 [12.jsonl]": "Leila has already said the program's own standalone promotion would wait until next year.",
    "01.jsonl:272": "Amira responds with laughter. The exchange ends without a decision.",
    "08.jsonl:470": "Ahn Minh acknowledges and drops it for now. The thread ends without a call on when the work is assigned.",
    "13.jsonl:191": "She says she will talk it over later. The thread ends with her colleague agreeing that the cost looks heavy.",
}
CLEAN_SITUATION = ("A client has asked for a refund on a cancelled phase. The enclosures that should come back with "
                   "the request are missing. The account owner asks the lead how to handle it.")


class PackCase(unittest.TestCase):
    def setUp(self):
        if os.environ.get(NESTED):
            self.skipTest("nested discovery run: tests are counted, the outer run executes them")
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "pack"
        for folder in ("data", "schema"):
            shutil.copytree(REPO / folder, self.root / folder)

    def tearDown(self):
        self._tmp.cleanup()

    def reset(self):
        """A fresh copy of the pack, for tests that break several things one after another."""
        self.tearDown()
        self.setUp()

    # ---- helpers
    def lines(self):
        return (self.root / SAMPLE).read_text(encoding="utf-8").splitlines()

    def write_lines(self, lines, end="\n", sync_manifest=True):
        raw = ("\n".join(lines) + end).encode("utf-8")
        (self.root / SAMPLE).write_bytes(raw)
        if sync_manifest and (self.root / MANIFEST).is_file():
            try:
                manifest = self.read_json(MANIFEST)
                if isinstance(manifest, dict) and "sample_sha256" in manifest:
                    manifest["sample_sha256"] = hashlib.sha256(raw).hexdigest()
                    self.write_json(MANIFEST, manifest)
            except ValueError:
                pass

    def edit_card(self, number, change):
        lines = self.lines()
        card = json.loads(lines[number])
        change(card)
        lines[number] = json.dumps(card, ensure_ascii=False)
        self.write_lines(lines)

    def edit_cards(self, change):
        lines = []
        for line in self.lines():
            card = json.loads(line)
            change(card)
            lines.append(json.dumps(card, ensure_ascii=False))
        self.write_lines(lines)

    def read_json(self, rel):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def write_json(self, rel, data):
        (self.root / rel).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def edit_json(self, rel, change):
        data = self.read_json(rel)
        change(data)
        self.write_json(rel, data)

    def receipt(self, shards=None, **options):
        return v.verify(self.root, shards, **options)

    def failed(self, receipt):
        return {c["id"] for c in receipt["checks"] if c["result"] == "fail"}

    def errors(self, receipt, check_id):
        return next(c["errors"] for c in receipt["checks"] if c["id"] == check_id)

    def assert_fails(self, check_id, fragment, shards=None, **options):
        receipt = self.receipt(shards, **options)
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertIn(check_id, self.failed(receipt), receipt["errors"][:5])
        self.assertIn(fragment, "\n".join(self.errors(receipt, check_id)))
        self.assertNotIn("verifier.internal_error", self.failed(receipt))
        return receipt

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-I", str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8")


class HappyPath(PackCase):
    def test_shipped_pack_passes_every_check(self):
        receipt = self.receipt()
        self.assertEqual(receipt["overall_status"], "pass", receipt["errors"])
        self.assertEqual({c["id"] for c in receipt["checks"]}, {
            "archive.files_readable", "archive.schema_contract", "archive.index_consistent", "archive.sample_jsonl",
            "archive.sample_schema", "archive.sample_card_id_unique", "archive.sample_withheld_absent",
            "archive.sample_policy", "blind_readiness.projection_contract", "record_consistency.status_counts",
            "record_consistency.roster", "record_consistency.model_families", "record_consistency.run_references",
            "record_consistency.judge_family"})
        self.assertEqual(receipt["errors"], [])
        self.assertEqual(receipt["observations"]["sample"]["cards"], 100)

    def test_receipt_keeps_the_four_questions_apart(self):
        receipt = self.receipt()
        self.assertEqual(receipt["schema_version"], v.RECEIPT_SCHEMA_VERSION)
        self.assertEqual(receipt["protocol_version"], "simul-stage1-v1")
        self.assertEqual(receipt["archive_integrity_status"], "pass")
        self.assertEqual(receipt["record_consistency"], "pass")
        self.assertEqual(receipt["blind_readiness"]["status"], "requires_curation_before_judged_run")
        self.assertEqual(receipt["empirical_judge_separation"]["status"], "not_verified_from_public_record")
        self.assertEqual(receipt["empirical_judge_separation"]["verified_runs"], 0)
        self.assertEqual(receipt["empirical_judge_separation"]["not_verified_runs"], 1)
        self.assertEqual(receipt["empirical_blinding"]["status"], "not_verified_from_public_record")
        self.assertEqual(receipt["empirical_blinding"]["verified_runs"], 0)
        self.assertIn("not a statement that the cards are ready", receipt["overall_status_scope"])

    def test_summary_counts_come_from_the_files(self):
        receipt = self.receipt()
        status, index = self.read_json(STATUS), self.read_json(INDEX)
        self.assertEqual(receipt["summary"]["status"]["editions"], len(status["editions"]))
        self.assertEqual(receipt["summary"]["status"]["branches"], len(status["branches"]))
        self.assertEqual(receipt["summary"]["status"]["runs"], len(status["runs"]))
        self.assertEqual(receipt["summary"]["card_index"]["cards_total"], index["total"])
        self.assertEqual(receipt["summary"]["card_index"]["cards_total"], sum(e["cards"] for e in index["editions"]))
        self.assertEqual(receipt["summary"]["card_index"]["organizations"], len({e["org"] for e in index["editions"]}))

    def test_first_run_record_names_no_judge_and_nothing_is_verified(self):
        judges = self.receipt()["empirical_judge_separation"]
        row = judges["runs"][0]
        self.assertIsNone(row["judge_model_id"])
        self.assertEqual((row["recorded_status"], row["record_state"]), ("not_verified", "no_judge_model_in_record"))
        self.assertTrue(row["speaker_families"])
        self.assertEqual(judges["record_declares_distinct_judge_family_runs"], 0)

    def test_receipt_is_deterministic_and_lists_a_digest_for_every_file(self):
        first, second = v.render(self.receipt()), v.render(self.receipt())
        self.assertEqual(first, second)
        receipt = json.loads(first)
        self.assertEqual([a["path"] for a in receipt["artifacts"]], sorted(v.FILES.values()))
        for a in receipt["artifacts"]:
            data = (self.root / a["path"]).read_bytes()
            self.assertEqual((a["bytes"], a["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertNotIn(str(self.root), first, "the receipt must not hold a machine path")
        self.assertNotIn(str(self.base), first)

    def test_shipped_pack_reproduces_the_committed_receipt(self):
        self.assertEqual(v.render(v.verify(REPO)), (REPO / "expected-receipt.json").read_text(encoding="utf-8"))

    def test_command_line_prints_the_receipt_and_exits_zero(self):
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, (REPO / "expected-receipt.json").read_text(encoding="utf-8"))

    def test_main_exits_one_on_a_broken_pack(self):
        self.write_lines(self.lines()[:-1] + ["{not json"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = v.main(["--root", str(self.root)])
        self.assertEqual(code, 1)
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertEqual(receipt["archive_integrity_status"], "fail")


class ProtocolContract(PackCase):
    """The field contract is fixed in the verifier. A schema file can agree with it or fail; it cannot change it."""

    def test_contract_constants_are_the_protocol_v1_ones(self):
        self.assertEqual(v.PROTOCOL_VERSION, "simul-stage1-v1")
        self.assertEqual(v.IDENTITY_FIELDS, ("card_id", "edition"))
        self.assertEqual(v.AGENT_FIELDS, ("channel", "opener", "participants", "situation", "options"))
        self.assertEqual(v.EVALUATOR_FIELDS, ("invariants",))
        self.assertEqual(v.WITHHELD_FIELDS, ("reference_outcome", "refutation_condition"))
        self.assertEqual(self.receipt()["field_contract"], {
            "identity": ["card_id", "edition"], "agent": ["channel", "opener", "participants", "situation", "options"],
            "evaluator": ["invariants"], "withheld": ["reference_outcome", "refutation_condition"]})

    def test_shipped_schema_states_exactly_the_contract(self):
        schema = self.read_json(SCHEMA)
        self.assertEqual(schema["x-simul-protocol"], v.PROTOCOL_VERSION)
        self.assertEqual(tuple(schema["x-simul-visibility"]["agent"]), v.AGENT_FIELDS)
        self.assertEqual(tuple(schema["x-simul-visibility"]["evaluator"]), v.EVALUATOR_FIELDS)
        self.assertEqual(tuple(schema["x-simul-visibility"]["identity"]), v.IDENTITY_FIELDS)
        self.assertEqual(tuple(schema["x-simul-withheld"]), v.WITHHELD_FIELDS)
        text = " ".join(schema["x-simul-blind-run-requirements"]) + schema["description"]
        self.assertIn("invariants", text)
        self.assertIn("situation", text)

    def test_a_complete_but_different_partition_fails(self):
        """Every field still sits in exactly one group, so a disjointness check alone would pass this."""
        def change(schema):
            schema["x-simul-visibility"]["agent"].remove("situation")
            schema["x-simul-visibility"]["evaluator"].append("situation")
        self.edit_json(SCHEMA, change)
        receipt = self.assert_fails("archive.schema_contract", "x-simul-visibility.agent: must be exactly")
        self.assertIn("x-simul-visibility.evaluator: must be exactly", "\n".join(self.errors(receipt, "archive.schema_contract")))

    def test_moving_an_identity_field_to_the_agent_group_fails(self):
        def change(schema):
            schema["x-simul-visibility"]["identity"].remove("edition")
            schema["x-simul-visibility"]["agent"].append("edition")
        self.edit_json(SCHEMA, change)
        self.assert_fails("archive.schema_contract", "x-simul-visibility.identity: must be exactly")

    def test_schema_that_makes_invariants_agent_visible_fails(self):
        def change(schema):
            schema["x-simul-visibility"]["agent"].append("invariants")
            schema["x-simul-visibility"]["evaluator"] = []
        self.edit_json(SCHEMA, change)
        self.assert_fails("archive.schema_contract", "invariants must not be agent-visible")

    def test_agent_fields_are_inspected_whatever_the_schema_file_claims(self):
        """A schema that hides `situation` from the agent list fails, and the situation is still screened."""
        before = self.receipt()["blind_readiness"]
        self.edit_json(SCHEMA, lambda schema: schema["x-simul-visibility"].__setitem__("agent", ["channel"]))
        receipt = self.assert_fails("archive.schema_contract", "x-simul-visibility.agent: must be exactly")
        after = receipt["blind_readiness"]
        self.assertGreater(after["agent_visible_situation_cue_count"], 0)
        self.assertEqual(after["flagged_situation_card_ids"], before["flagged_situation_card_ids"])
        self.assertEqual(after["agent_projection"], before["agent_projection"])
        # and a withheld-field name planted in the situation is still caught
        self.edit_card(0, lambda card: card.__setitem__("situation", card["situation"] + " See reference_outcome."))
        self.assert_fails("archive.sample_withheld_absent", "agent-visible text contains")

    def test_cards_are_validated_against_the_fixed_schema_not_the_file(self):
        """Loosening the schema file fails the contract, and an empty situation still fails the sample."""
        self.edit_json(SCHEMA, lambda schema: schema["properties"]["situation"].pop("minLength"))
        self.edit_card(2, lambda card: card.__setitem__("situation", "   "))
        receipt = self.assert_fails("archive.schema_contract", "schema.properties.situation.minLength: missing")
        self.assertIn("archive.sample_schema", self.failed(receipt))

    def test_schema_that_admits_a_withheld_field_fails(self):
        def change(schema):
            schema["properties"]["reference_outcome"] = {"type": "string"}
            schema["required"].append("reference_outcome")
        self.edit_json(SCHEMA, change)
        self.assert_fails("archive.schema_contract", "withheld field(s) ['reference_outcome']")

    def test_schema_with_another_keyword_or_pattern_is_refused(self):
        cases = {
            "maxLength": (lambda s: s["properties"]["situation"].__setitem__("maxLength", 10), "maxLength: not part of protocol"),
            "pattern": (lambda s: s["properties"]["card_id"].__setitem__("pattern", "^.*$"), "schema.properties.card_id.pattern: expected"),
            "extra property": (lambda s: s["properties"].__setitem__("notes", {"type": "string"}), "schema.properties.notes: not part of protocol"),
            "additionalProperties": (lambda s: s.__setitem__("additionalProperties", True), "schema.additionalProperties: expected false"),
            "protocol": (lambda s: s.__setitem__("x-simul-protocol", "simul-stage1-v2"), "x-simul-protocol must be 'simul-stage1-v1'"),
            "withheld": (lambda s: s.__setitem__("x-simul-withheld", ["reference_outcome"]), "x-simul-withheld: must be exactly"),
            "unknown group": (lambda s: s["x-simul-visibility"].__setitem__("public", []), "unknown group(s) ['public']"),
            "requirements": (lambda s: s.pop("x-simul-blind-run-requirements"), "must list the steps required before a judged blind run"),
            "repeated name": (lambda s: s["x-simul-visibility"]["agent"].append("channel"), "a field name is repeated"),
        }
        for name, (change, fragment) in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.edit_json(SCHEMA, change)
                self.assert_fails("archive.schema_contract", fragment)

    def test_card_index_must_name_the_same_fields(self):
        self.edit_json(INDEX, lambda index: index["fields"].pop("opener"))
        self.assert_fails("archive.schema_contract", "card index: fields must name exactly")
        self.reset()
        self.edit_json(INDEX, lambda index: index["not_included"].pop("refutation_condition"))
        self.assert_fails("archive.schema_contract", "card index: not_included must name exactly")


class AgentProjection(unittest.TestCase):
    def card(self):
        return {"card_id": "0123456789ab", "edition": "01", "channel": "cust", "opener": "cs1",
                "participants": ["cs1", "cs-lead"], "situation": CLEAN_SITUATION,
                "options": ["Refund now", "Ask for the enclosures first"],
                "invariants": ["The condition was waived rather than enforced"]}

    def test_projection_holds_exactly_the_five_agent_fields_in_order(self):
        projection = v.project_for_agent(self.card())
        self.assertEqual(tuple(projection), ("channel", "opener", "participants", "situation", "options"))
        self.assertNotIn("invariants", projection)
        self.assertNotIn("card_id", projection)
        self.assertNotIn("waived", json.dumps(projection))

    def test_projection_is_a_copy(self):
        card = self.card()
        projection = v.project_for_agent(card)
        projection["options"].append("changed")
        self.assertEqual(len(card["options"]), 2)

    def test_null_invariants_still_projects(self):
        card = self.card()
        card["invariants"] = None
        self.assertEqual(set(v.project_for_agent(card)), set(v.AGENT_FIELDS))

    def test_withheld_keys_are_refused_at_any_depth_and_under_aliases(self):
        cases = {
            "top level": lambda c: c.__setitem__("reference_outcome", "refunded"),
            "refutation": lambda c: c.__setitem__("refutation_condition", "if the items come back"),
            "alias": lambda c: c.__setitem__("ground_truth", "refunded"),
            "case": lambda c: c.__setitem__("Reference_Outcome", "refunded"),
            "nested": lambda c: c.__setitem__("options", [{"text": "Refund now", "chosen_option": True}]),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                card = self.card()
                change(card)
                with self.assertRaises(v.ProjectionError) as caught:
                    v.project_for_agent(card)
                self.assertIn("withheld key", str(caught.exception))

    def test_unknown_missing_and_malformed_cards_are_refused(self):
        card = self.card()
        card["notes"] = "internal"
        with self.assertRaisesRegex(v.ProjectionError, "outside protocol"):
            v.project_for_agent(card)
        card = self.card()
        card.pop("situation")
        with self.assertRaisesRegex(v.ProjectionError, "lacks agent field"):
            v.project_for_agent(card)
        for value in (None, [], "card", 7, True):
            with self.subTest(value=value), self.assertRaisesRegex(v.ProjectionError, "expected object"):
                v.project_for_agent(value)


class BlindReadiness(PackCase):
    """Layer 2. The published archive carries post-decision wording; the receipt must say so and not pass it off."""

    def test_default_run_passes_the_archive_and_reports_curation_needed(self):
        receipt = self.receipt()
        blind = receipt["blind_readiness"]
        self.assertEqual(receipt["overall_status"], "pass")
        self.assertEqual(blind["status"], "requires_curation_before_judged_run")
        self.assertIs(blind["evaluator_field_stripped_in_projection"], True)
        self.assertEqual(blind["sample_cards_audited"], 100)
        self.assertGreater(blind["evaluator_resolution_cue_count"], 0)
        self.assertGreater(blind["agent_visible_situation_cue_count"], 0)
        self.assertEqual(blind["agent_visible_situation_cue_count"], len(blind["flagged_situation_card_ids"]))
        self.assertEqual(blind["flagged_situation_card_ids"], sorted(set(blind["flagged_situation_card_ids"])))
        self.assertEqual(len(blind["required_before_judged_run"]), 3)

    def test_sample_line_1_is_an_evaluator_cue_and_line_54_is_an_agent_visible_cue(self):
        lines = self.lines()
        first, line54 = json.loads(lines[0]), json.loads(lines[53])
        self.assertEqual((first["card_id"], line54["card_id"]), ("004fe71b9355", "009cbe6ab507"))
        self.assertTrue(any(v.EVALUATOR_CUE_PATTERN.search(text) for text in first["invariants"]))
        self.assertEqual(v.agent_cues(first["situation"]), [])
        self.assertEqual([cue["class"] for cue in v.agent_cues(line54["situation"])], ["prior_ruling_stated"])
        blind = self.receipt()["blind_readiness"]
        self.assertIn("009cbe6ab507", blind["flagged_situation_card_ids"])
        self.assertNotIn("004fe71b9355", blind["flagged_situation_card_ids"])
        match = next(m for m in blind["flagged_agent_visible_matches"] if m["card_id"] == "009cbe6ab507")
        self.assertEqual(match["where"], "sample line 54")
        self.assertEqual(match["situation"][0]["text"], "has already said")

    def test_published_post_decision_endings_are_caught(self):
        for where, ending in PUBLISHED_SITUATION_ENDINGS.items():
            with self.subTest(where=where):
                self.assertTrue(v.agent_cues(CLEAN_SITUATION + " " + ending), ending)

    def test_wording_is_not_matched_across_two_options(self):
        options = ["Refund the fee immediately without a final", "decision is for the lead"]
        self.assertEqual(v.agent_cues(options, v.OPTION_CUE_PATTERNS), [])
        self.assertTrue(v.agent_cues([" ".join(options)], v.OPTION_CUE_PATTERNS))

    def test_pre_decision_wording_is_not_flagged(self):
        for text in (CLEAN_SITUATION,
                     "Marcus has not agreed to anything yet and asks which way to go.",
                     "The call sits with the director, who has to settle it today.",
                     "The supplier is waiting for a reply and the meeting is at three."):
            with self.subTest(text=text):
                self.assertEqual(v.agent_cues(text), [])

    def test_require_blind_clean_fails_the_shipped_pack_with_exit_one(self):
        receipt = self.assert_fails("blind_readiness.require_clean", "carry a post-decision cue in agent-visible text",
                                    require_blind_clean=True)
        self.assertEqual(receipt["archive_integrity_status"], "pass", "the archive is intact; only the blind gate fails")
        self.assertEqual(receipt["record_consistency"], "pass")
        done = self.run_cli("--root", str(self.root), "--require-blind-clean")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["overall_status"], "fail")
        self.assertEqual(self.run_cli("--root", str(self.root)).returncode, 0, "without the flag the same pack exits 0")

    def test_one_planted_situation_cue_fails_require_blind_clean(self):
        self.edit_cards(lambda card: card.__setitem__("situation", CLEAN_SITUATION))
        self.assertEqual(self.receipt(require_blind_clean=True)["overall_status"], "pass")
        self.edit_card(7, lambda card: card.__setitem__("situation", CLEAN_SITUATION + " The thread ends without a decision."))
        receipt = self.assert_fails("blind_readiness.require_clean", "sample: 1 card(s)", require_blind_clean=True)
        self.assertEqual(receipt["blind_readiness"]["agent_visible_situation_cue_count"], 1)

    def test_a_cue_in_an_option_also_fails_require_blind_clean(self):
        self.edit_cards(lambda card: card.__setitem__("situation", CLEAN_SITUATION))
        self.edit_card(3, lambda card: card["options"].__setitem__(0, "Leave it; the thread ends without a decision"))
        receipt = self.assert_fails("blind_readiness.require_clean", "1 in options", require_blind_clean=True)
        self.assertEqual(len(receipt["blind_readiness"]["flagged_option_card_ids"]), 1)

    def test_curated_situations_pass_the_gate_but_evaluator_cues_still_require_stripping(self):
        self.edit_cards(lambda card: card.__setitem__("situation", CLEAN_SITUATION))
        receipt = self.receipt(require_blind_clean=True)
        self.assertEqual(receipt["overall_status"], "pass", receipt["errors"])
        blind = receipt["blind_readiness"]
        self.assertEqual(blind["agent_visible_situation_cue_count"], 0)
        self.assertEqual(blind["flagged_situation_card_ids"], [])
        self.assertGreater(blind["evaluator_resolution_cue_count"], 0)
        self.assertEqual(blind["status"], "requires_curation_before_judged_run")

    def test_no_cue_at_all_is_still_not_called_ready(self):
        def change(card):
            card["situation"], card["invariants"] = CLEAN_SITUATION, None
        self.edit_cards(change)
        blind = self.receipt()["blind_readiness"]
        self.assertEqual(blind["status"], "no_lexical_cue_found_review_still_required")
        self.assertEqual(blind["evaluator_resolution_cue_count"], 0)

    def test_agent_projection_digests_can_be_reproduced_from_the_command_line(self):
        blind = self.receipt()["blind_readiness"]
        for which, key in (("all", "all_cards"), ("unflagged", "excluding_flagged_cards")):
            with self.subTest(which=which):
                done = self.run_cli("--root", str(self.root), "--emit-agent-projection", which)
                self.assertEqual(done.returncode, 0, done.stderr)
                data = done.stdout.encode("utf-8")
                self.assertEqual(hashlib.sha256(data).hexdigest(), blind["agent_projection"][key]["sha256"])
                rows = [json.loads(line) for line in done.stdout.splitlines()]
                self.assertEqual(len(rows), blind["agent_projection"][key]["cards"])
                self.assertTrue(all(tuple(row) == v.AGENT_FIELDS for row in rows))
        flagged = len(set(blind["flagged_situation_card_ids"]) | set(blind["flagged_option_card_ids"]))
        self.assertEqual(blind["agent_projection"]["excluding_flagged_cards"]["cards"], 100 - flagged)

    def test_a_card_that_cannot_be_projected_fails_the_projection_contract(self):
        self.edit_card(0, lambda card: card.__setitem__("reference_outcome", "The refund was processed."))
        receipt = self.assert_fails("blind_readiness.projection_contract", "withheld key(s) ['reference_outcome']")
        self.assertEqual(receipt["blind_readiness"]["sample_cards_audited"], 99)


class SampleStructure(PackCase):
    def test_duplicate_card_id_fails(self):
        lines = self.lines()
        card = json.loads(lines[1])
        card["card_id"] = json.loads(lines[0])["card_id"]
        lines[1] = json.dumps(card, ensure_ascii=False)
        self.write_lines(lines)
        self.assert_fails("archive.sample_card_id_unique", "repeats line 1")

    def test_malformed_jsonl_line_fails(self):
        lines = self.lines()
        lines[4] = lines[4][:-1]  # drop the closing brace
        self.write_lines(lines)
        self.assert_fails("archive.sample_jsonl", "sample line 5: not JSON")

    def test_blank_line_fails(self):
        lines = self.lines()
        lines.insert(3, "")
        self.write_lines(lines)
        self.assert_fails("archive.sample_jsonl", "line 4: blank line")

    def test_line_that_is_not_an_object_fails(self):
        for text in ('["a", "list"]', "7", "true", "null", '"text"'):
            with self.subTest(line=text):
                self.reset()
                lines = self.lines()
                lines[0] = text
                self.write_lines(lines)
                self.assert_fails("archive.sample_jsonl", "line 1: not a JSON object")

    def test_duplicate_key_inside_a_card_fails(self):
        lines = self.lines()
        lines[0] = lines[0][:-1] + ', "situation": "a second situation"}'
        self.write_lines(lines)
        self.assert_fails("archive.sample_jsonl", "duplicate key 'situation'")

    def test_missing_final_newline_fails(self):
        self.write_lines(self.lines(), end="")
        self.assert_fails("archive.sample_jsonl", "last line has no newline")

    def test_empty_and_non_utf8_sample_fail(self):
        (self.root / SAMPLE).write_bytes(b"")
        self.assert_fails("archive.sample_jsonl", "empty file")
        (self.root / SAMPLE).write_bytes(b'{"card_id": "\xff\xfe"}\n')
        self.assert_fails("archive.sample_jsonl", "not UTF-8")

    def test_missing_required_field_fails(self):
        for field in v.CARD_FIELDS:
            with self.subTest(field=field):
                self.reset()
                self.edit_card(0, lambda card: card.pop(field))
                self.assert_fails("archive.sample_schema", f"missing required field '{field}'")

    def test_extra_field_fails(self):
        self.edit_card(0, lambda card: card.__setitem__("notes", "internal"))
        self.assert_fails("archive.sample_schema", "field 'notes' is not in the schema")

    def test_empty_values_fail(self):
        cases = {
            "situation": ("   ", "situation: empty"),
            "options": ([], "options: fewer than 1"),
            "participants": ([], "participants: fewer than 1"),
            "invariants": ([], "invariants: fewer than 1"),
            "channel": ("", "channel: empty"),
            "opener": ("", "opener: empty"),
        }
        for field, (value, fragment) in cases.items():
            with self.subTest(field=field):
                self.reset()
                self.edit_card(2, lambda card: card.__setitem__(field, value))
                self.assert_fails("archive.sample_schema", fragment)

    def test_wrong_types_and_patterns_fail(self):
        cases = {
            "card_id": ("NOT-HEX-0000", "does not match"),
            "edition": ("1", "does not match"),
            "options": ("one option as text", "expected array"),
            "participants": (["ceo", 7], "expected string"),
            "situation": (None, "expected string"),
            "invariants": ({"a": 1}, "expected array or null"),
            "channel": (["cust"], "expected string"),
            "opener": (True, "expected string"),
        }
        for field, (value, fragment) in cases.items():
            with self.subTest(field=field):
                self.reset()
                self.edit_card(0, lambda card: card.__setitem__(field, value))
                self.assert_fails("archive.sample_schema", fragment)

    def test_null_invariants_is_allowed_and_counted(self):
        self.edit_card(0, lambda card: card.__setitem__("invariants", None))
        receipt = self.receipt()
        self.assertNotIn("archive.sample_schema", self.failed(receipt))
        self.assertEqual(receipt["observations"]["sample"]["counts"]["null_invariants"], 1)

    def test_edition_outside_the_card_index_fails(self):
        self.edit_card(0, lambda card: card.__setitem__("edition", "99"))
        self.assert_fails("archive.sample_schema", "edition '99' is not in the card index")


class WithheldFields(PackCase):
    def test_withheld_field_on_a_card_fails(self):
        for key in v.WITHHELD_FIELDS:
            with self.subTest(key=key):
                self.reset()
                self.edit_card(0, lambda card: card.__setitem__(key, "The refund was processed without the missing items."))
                receipt = self.assert_fails("archive.sample_withheld_absent", f"forbidden field '{key}'")
                self.assertIn("archive.sample_schema", self.failed(receipt), "an extra field also breaks the schema")

    def test_renamed_or_nested_outcome_field_fails(self):
        self.edit_card(0, lambda card: card.__setitem__("options", [{"text": "Refund now", "Chosen_Option": True}, "Wait"]))
        self.assert_fails("archive.sample_withheld_absent", "Chosen_Option")

    def test_outcome_label_in_agent_text_fails(self):
        cases = {
            "situation": lambda card: card.__setitem__("situation", card["situation"] + "\nReference outcome: the refund was processed."),
            "option": lambda card: card["options"].__setitem__(0, card["options"][0] + " [OUTCOME]"),
            "opener": lambda card: card.__setitem__("opener", "see refutation_condition"),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.edit_card(0, change)
                self.assert_fails("archive.sample_withheld_absent", "agent-visible text contains")

    def test_ordinary_phrases_are_not_markers(self):
        self.edit_card(0, lambda card: card.__setitem__("situation", card["situation"] + " Nobody knows what actually happened, or the correct answer to give; the cost cap moves the outcome: higher is slower."))
        self.assertNotIn("archive.sample_withheld_absent", self.failed(self.receipt()))

    def test_marker_in_the_evaluator_field_is_not_an_agent_side_failure(self):
        self.edit_card(0, lambda card: card["invariants"].append("Outcome: settled at working level"))
        self.assertNotIn("archive.sample_withheld_absent", self.failed(self.receipt()))


class SamplePolicy(PackCase):
    """Protocol v1 fixes 20 shards x 5 cards = 100 and one rule id. The manifest cannot restate them."""

    def test_policy_constants(self):
        self.assertEqual((v.EXPECTED_SHARDS, v.CARDS_PER_SHARD, v.EXPECTED_SAMPLE_COUNT), (20, 5, 100))
        self.assertEqual(v.SAMPLE_RULE_ID, "first_5_by_card_id_asc_per_shard")
        self.assertEqual(self.receipt()["sample_policy"], {"rule_id": "first_5_by_card_id_asc_per_shard", "shards": 20, "per_shard": 5, "total": 100})
        manifest = self.read_json(MANIFEST)
        self.assertEqual((manifest["policy"], manifest["shards"], manifest["per_shard"], manifest["total"]),
                         ("first_5_by_card_id_asc_per_shard", 20, 5, 100))

    def test_manifest_cannot_restate_a_fixed_number(self):
        cases = {
            "per_shard": (4, "per_shard is 4"),
            "total": (80, "total is 80"),
            "shards": (19, "shards is 19"),
            "policy": ("first_4_by_card_id_asc_per_shard", "policy is 'first_4_by_card_id_asc_per_shard'"),
            "protocol_version": ("simul-stage1-v2", "protocol_version is 'simul-stage1-v2'"),
        }
        for key, (value, fragment) in cases.items():
            with self.subTest(key=key):
                self.reset()
                self.edit_json(MANIFEST, lambda manifest: manifest.__setitem__(key, value))
                self.assert_fails("archive.sample_policy", fragment)

    def test_a_consistently_smaller_sample_still_fails(self):
        """Manifest, per-shard count, total and sample all trimmed to 4 per shard: self-consistent, and refused."""
        def trim(manifest):
            manifest["per_shard"], manifest["total"] = 4, 80
            manifest["policy"] = "first_4_by_card_id_asc_per_shard"
            manifest["cards"] = {edition: ids[:4] for edition, ids in manifest["cards"].items()}
        self.edit_json(MANIFEST, trim)
        lines = self.lines()
        self.write_lines([line for k, line in enumerate(lines) if k % 5 != 4])
        receipt = self.assert_fails("archive.sample_policy", "per_shard is 4")
        joined = "\n".join(self.errors(receipt, "archive.sample_policy"))
        self.assertIn("80 cards, the policy fixes 100", joined)
        self.assertIn("does not list 5 distinct card ids", joined)

    def test_dropping_a_shard_from_manifest_and_sample_fails(self):
        self.edit_json(MANIFEST, lambda manifest: manifest["cards"].pop("21"))
        self.write_lines(self.lines()[:95])
        self.assert_fails("archive.sample_policy", "lists 19 shard(s), the policy fixes 20")

    def test_manifest_ids_must_be_ascending_and_distinct(self):
        self.edit_json(MANIFEST, lambda manifest: manifest["cards"]["01"].reverse())
        self.assert_fails("archive.sample_policy", "edition 01 does not list 5 distinct card ids in ascending order")

    def test_swapping_a_card_breaks_the_manifest_binding(self):
        self.edit_card(0, lambda card: card.__setitem__("card_id", "ffffffffffff"))
        self.assert_fails("archive.sample_policy", "not the ones the manifest lists")

    def test_editing_the_card_index_breaks_the_manifest_binding(self):
        self.edit_json(INDEX, lambda index: index.__setitem__("updated", "2099-01-01"))
        self.assert_fails("archive.sample_policy", "source_index_sha256")


class FileBoundary(PackCase):
    """Files are opened once, inside the root, without following links, and must be regular files."""

    def outside_copy(self, rel):
        target = self.base / "outside" / Path(rel).name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(self.root / rel, target)
        return target

    def test_symlinked_file_is_refused_even_when_its_target_is_valid(self):
        for rel in (STATUS, SAMPLE, SCHEMA):
            with self.subTest(rel=rel):
                self.reset()
                target = self.outside_copy(rel)
                (self.root / rel).unlink()
                os.symlink(target, self.root / rel)
                self.assert_fails("archive.files_readable", f"{rel}: is a symbolic link")

    def test_symlinked_directory_is_refused(self):
        real = self.base / "real-data"
        shutil.move(str(self.root / "data"), str(real))
        os.symlink(real, self.root / "data")
        self.assert_fails("archive.files_readable", "directory 'data' is missing or is a symbolic link")

    def test_symlinked_root_is_refused(self):
        link = self.base / "link-to-pack"
        os.symlink(self.root, link)
        receipt = v.verify(link)
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertIn("pack root: not a real directory", "\n".join(self.errors(receipt, "archive.files_readable")))
        self.assertEqual(receipt["artifacts"], [])

    def test_directory_in_place_of_a_file_is_refused(self):
        (self.root / STATUS).unlink()
        (self.root / STATUS).mkdir()
        self.assert_fails("archive.files_readable", f"{STATUS}: not a regular file")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_named_pipe_is_refused_without_waiting_on_it(self):
        (self.root / ORG).unlink()
        os.mkfifo(self.root / ORG)
        self.assert_fails("archive.files_readable", f"{ORG}: not a regular file")

    def test_missing_file_fails(self):
        (self.root / STATUS).unlink()
        self.assert_fails("archive.files_readable", "data/simul-status.json: cannot be read")

    def test_paths_that_leave_the_root_are_refused(self):
        (self.base / "secret.json").write_text("{}", encoding="utf-8")
        for rel in ("../secret.json", "data/../../secret.json", str(self.base / "secret.json"), "data//x", "./data/x", "", "data\\x", "a\x00b"):
            with self.subTest(rel=rel), self.assertRaises(v.PackError):
                v.read_regular_file_bytes(self.root, rel)
        for rel in (None, 7, ["data"]):
            with self.subTest(rel=rel), self.assertRaises(v.PackError):
                v.read_regular_file_bytes(self.root, rel)

    def test_helper_returns_the_bytes_of_a_regular_file(self):
        self.assertEqual(v.read_regular_file_bytes(self.root, STATUS), (self.root / STATUS).read_bytes())

    def test_each_file_is_read_once_and_the_same_bytes_are_hashed_and_parsed(self):
        """Every read returns different bytes for the status file: one valid, then broken. Only the first is used."""
        real, calls = v.read_regular_file_bytes, []

        def counting(root, rel, *args, **kwargs):
            calls.append(rel)
            data = real(root, rel, *args, **kwargs)
            return data if calls.count(rel) == 1 else b"{broken"
        v.read_regular_file_bytes = counting
        try:
            receipt = self.receipt()
        finally:
            v.read_regular_file_bytes = real
        self.assertEqual(sorted(calls), sorted(v.FILES.values()), "one open per file")
        self.assertEqual(receipt["overall_status"], "pass")
        digest = next(a["sha256"] for a in receipt["artifacts"] if a["path"] == STATUS)
        self.assertEqual(digest, hashlib.sha256((self.root / STATUS).read_bytes()).hexdigest())


class MalformedInput(PackCase):
    """Wrong types anywhere in a file give a structured failing receipt and exit 1, never a traceback."""

    REPLACEMENTS = (None, True, 0, "(", [], [[1]], {"a": {"b": []}})
    RECEIPT_KEYS = {"schema_version", "overall_status", "checks", "errors", "archive_integrity_status", "record_consistency"}

    def assert_structured(self, receipt, context):
        self.assertTrue(self.RECEIPT_KEYS <= set(receipt), context)
        self.assertIn(receipt["overall_status"], ("pass", "fail"), context)
        self.assertNotIn("verifier.internal_error", self.failed(receipt), context)
        self.assertEqual(len(receipt["errors"]), sum(len(c["errors"]) for c in receipt["checks"]), context)
        json.dumps(receipt)

    def paths(self, node, prefix=()):
        """Paths to every nested value, keeping two members of long lists and maps so the walk stays short."""
        yield prefix
        if isinstance(node, dict):
            keys = list(node)
            keys = keys if len(keys) <= 16 else keys[:2]
            for key in keys:
                yield from self.paths(node[key], prefix + (key,))
        elif isinstance(node, list):
            for k in (range(len(node)) if len(node) <= 3 else (0, len(node) - 1)):
                yield from self.paths(node[k], prefix + (k,))

    @staticmethod
    def replaced(data, path, value):
        if not path:
            return copy.deepcopy(value)
        out = copy.deepcopy(data)
        node = out
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = copy.deepcopy(value)
        return out

    @staticmethod
    def removed(data, path):
        out = copy.deepcopy(data)
        node = out
        for step in path[:-1]:
            node = node[step]
        del node[path[-1]]
        return out

    def test_every_nested_value_of_every_json_file_can_be_wrong(self):
        self.write_lines(self.lines()[:5])  # a short sample keeps each run fast; the walk targets the JSON files
        tried = 0
        for rel in JSON_FILES:
            original = self.read_json(rel)
            for path in self.paths(original):
                variants = [self.replaced(original, path, value) for value in self.REPLACEMENTS]
                if path:
                    variants.append(self.removed(original, path))
                for variant in variants:
                    self.write_json(rel, variant)
                    try:
                        receipt = self.receipt()
                    except Exception as ex:  # the failure this test exists to catch
                        self.fail(f"{rel} at {path!r}: verify raised {type(ex).__name__}: {ex}")
                    self.assert_structured(receipt, f"{rel} at {path!r}")
                    tried += 1
            self.write_json(rel, original)
        self.assertGreater(tried, 2000)

    def test_named_malformed_cases_fail_on_the_owning_check(self):
        cases = [
            (SCHEMA, lambda s: s.__setitem__("properties", ["card_id"]), "archive.schema_contract", "schema.properties: expected object, found array"),
            (SCHEMA, lambda s: s.__setitem__("x-simul-visibility", ["agent"]), "archive.schema_contract", "x-simul-visibility: expected object, found array"),
            (SCHEMA, lambda s: s["x-simul-visibility"].__setitem__("agent", "situation"), "archive.schema_contract", "must be a list of field names, found string"),
            (SCHEMA, lambda s: s["x-simul-visibility"].__setitem__("agent", [["situation"]]), "archive.schema_contract", "must be a list of field names"),
            (SCHEMA, lambda s: s.__setitem__("required", 8), "archive.schema_contract", "schema: required: must be a list of field names, found integer"),
            (SCHEMA, lambda s: s["properties"].__setitem__("situation", True), "archive.schema_contract", "schema.properties.situation: expected object, found boolean"),
            (SCHEMA, lambda s: s["properties"]["card_id"].__setitem__("pattern", "("), "archive.schema_contract", "schema.properties.card_id.pattern"),
            (SCHEMA, lambda s: s["properties"]["card_id"].__setitem__("pattern", "(a+)+$"), "archive.schema_contract", "schema.properties.card_id.pattern"),
            (SCHEMA, lambda s: s["properties"]["edition"].__setitem__("pattern", ["^0$"]), "archive.schema_contract", "schema.properties.edition.pattern: expected string, found array"),
            (SCHEMA, lambda s: s["properties"]["options"].__setitem__("type", {"array": True}), "archive.schema_contract", "schema.properties.options.type"),
            (SCHEMA, lambda s: s.__setitem__("description", {"text": "x"}), "archive.schema_contract", "schema.description: expected string, found object"),
            (INDEX, lambda i: i.__setitem__("editions", {"01": {}}), "archive.index_consistent", "card index.editions: expected array, found object"),
            (INDEX, lambda i: i["editions"].__setitem__(0, "01"), "archive.index_consistent", "card index.editions[0]: expected object, found string"),
            (INDEX, lambda i: i["editions"][0].__setitem__("file", ["/data/cards/s1/01.jsonl"]), "archive.index_consistent", "editions[0].file: expected string, found array"),
            (INDEX, lambda i: i["editions"][0].__setitem__("cards", True), "archive.index_consistent", "editions[0].cards: expected integer, found boolean"),
            (INDEX, lambda i: i["editions"][0].__setitem__("sha256", 12), "archive.index_consistent", "editions[0].sha256: expected string, found integer"),
            (INDEX, lambda i: i.__setitem__("fields", ["card_id"]), "archive.schema_contract", "card index: fields must name exactly"),
            (MANIFEST, lambda m: m.__setitem__("cards", [["a"]]), "archive.sample_policy", "sample manifest.cards: expected object, found array"),
            (MANIFEST, lambda m: m["cards"].__setitem__("01", "004fe71b9355"), "archive.sample_policy", "sample manifest.cards.01: expected array, found string"),
            (MANIFEST, lambda m: m["cards"].__setitem__("01", [1, 2, 3, 4, 5]), "archive.sample_policy", "cards.01[0]: expected string, found integer"),
            (MANIFEST, lambda m: m.__setitem__("per_shard", "5"), "archive.sample_policy", "per_shard: expected integer, found string"),
            (MANIFEST, lambda m: m.__setitem__("per_shard", True), "archive.sample_policy", "per_shard: expected integer, found boolean"),
            (STATUS, lambda s: s.__setitem__("runs", {"s1": {}}), "record_consistency.status_counts", "status.runs: expected array, found object"),
            (STATUS, lambda s: s["runs"].__setitem__(0, ["s1-test-001"]), "record_consistency.status_counts", "status.runs[0]: expected object, found array"),
            (STATUS, lambda s: s["runs"][0].__setitem__("edition", ["01"]), "record_consistency.status_counts", "runs[0].edition: expected string, found array"),
            (STATUS, lambda s: s["runs"][0].__setitem__("observed", "none"), "record_consistency.status_counts", "runs[0].observed: expected object, found string"),
            (STATUS, lambda s: s["runs"][0]["observed"].__setitem__("judge_model_id", {"id": "x"}), "record_consistency.status_counts", "judge_model_id: expected string or null, found object"),
            (STATUS, lambda s: s["runs"][0].__setitem__("judge_family_check", "not_verified"), "record_consistency.status_counts", "judge_family_check: expected object, found string"),
            (STATUS, lambda s: s["editions"][0].__setitem__("branch", {"id": "S1"}), "record_consistency.status_counts", "editions[0].branch: expected string, found object"),
            (STATUS, lambda s: s["branches"][0].__setitem__("size", True), "record_consistency.status_counts", "branches[0].size: expected integer, found boolean"),
            (STATUS, lambda s: s.__setitem__("model_families", ["mimo"]), "record_consistency.status_counts", "model_families: expected object, found array"),
            (STATUS, lambda s: s.__setitem__("judge_models", [{"family": "gpt"}]), "record_consistency.status_counts", "judge_models: expected object or null, found array"),
            (STATUS, lambda s: s["roster_snapshots"][0].__setitem__("routes", {"default": "mimo"}), "record_consistency.status_counts", "routes: expected array, found object"),
            (STATUS, lambda s: s["routing_policies"][0].__setitem__("used_by", "s1-test-001"), "record_consistency.status_counts", "used_by: expected array, found string"),
            (STATUS, lambda s: s["research"].__setitem__("downloads", [7]), "record_consistency.status_counts", "downloads[0]: expected object, found integer"),
            (STATUS, lambda s: s["research"].__setitem__("generated_limits", [["x"]]), "record_consistency.status_counts", "generated_limits[0]: expected string, found array"),
            (ORG, lambda o: o.__setitem__("people", {"ceo": {}}), "record_consistency.roster", "s1-org.people: expected array, found object"),
            (ORG, lambda o: o["people"].__setitem__(0, "ceo"), "record_consistency.roster", "people[0]: expected object, found string"),
            (ORG, lambda o: o["people"][0].__setitem__("id", ["ceo"]), "record_consistency.roster", "people[0].id: expected string, found array"),
            (ORG, lambda o: o["people"][1].__setitem__("reports_to", {"id": "ceo"}), "record_consistency.roster", "reports_to: expected string or null, found object"),
            (ORG, lambda o: o["channels"][1].__setitem__("members", [["cto"]]), "record_consistency.roster", "members[0]: expected string, found array"),
            (ORG, lambda o: o["channels"][1].__setitem__("members", {"cto": True}), "record_consistency.roster", "members: expected string or array, found object"),
            (ORG, lambda o: o["channels"][1].__setitem__("members", "everyone"), "record_consistency.roster", "only the word 'all' or a list is allowed"),
            (ORG, lambda o: o.__setitem__("headcount", 15.0), "record_consistency.roster", "headcount: expected integer, found number"),
        ]
        for rel, change, check_id, fragment in cases:
            with self.subTest(file=rel, fragment=fragment):
                self.reset()
                self.edit_json(rel, change)
                self.assert_fails(check_id, fragment)

    def test_a_malformed_roster_fails_the_run_references_too(self):
        self.edit_json(ORG, lambda o: o.__setitem__("channels", "all"))
        self.assert_fails("record_consistency.run_references", "data/s1-org.json is not usable")

    def test_a_malformed_status_marks_dependent_rules_not_evaluated(self):
        self.edit_json(STATUS, lambda s: s.__setitem__("roster_snapshots", None))
        receipt = self.assert_fails("record_consistency.judge_family", "status: not usable")
        self.assertEqual(receipt["record_consistency"], "fail")
        self.assertEqual(receipt["empirical_judge_separation"]["status"], "not_evaluated")
        self.assertEqual(receipt["archive_integrity_status"], "pass")

    def test_files_that_are_not_json_objects_fail_as_unreadable(self):
        texts = {
            "list": "[1, 2, 3]", "number": "7", "boolean": "true", "null": "null", "string": '"text"',
            "empty": "", "truncated": '{"runs": [', "nan": '{"total": NaN}', "duplicate key": '{"runs": [], "runs": []}',
            "byte order mark": "﻿{}", "deep": "[" * 5000 + "]" * 5000, "deep object": '{"a":' * 60 + "1" + "}" * 60,
            "huge number": '{"total": ' + "9" * 6000 + "}",
        }
        for rel in JSON_FILES:
            for name, text in texts.items():
                with self.subTest(file=rel, case=name):
                    self.reset()
                    (self.root / rel).write_text(text, encoding="utf-8")
                    receipt = self.receipt()
                    self.assert_structured(receipt, f"{rel} {name}")
                    if name != "huge number":
                        self.assertIn("archive.files_readable", self.failed(receipt))
                    self.assertEqual(receipt["overall_status"], "fail")
            self.reset()
            (self.root / rel).write_bytes(b'{"name": "\xff"}')
            self.assert_fails("archive.files_readable", f"{rel}: not UTF-8")

    def test_command_line_prints_a_failing_receipt_and_no_traceback(self):
        breaks = [
            (SCHEMA, lambda s: s["properties"]["card_id"].__setitem__("pattern", "(")),
            (MANIFEST, lambda m: m.__setitem__("cards", 5)),
            (STATUS, lambda s: s.__setitem__("runs", [None])),
            (ORG, lambda o: o.__setitem__("people", [[]])),
            (INDEX, lambda i: i.__setitem__("editions", [True])),
        ]
        for rel, change in breaks:
            with self.subTest(file=rel):
                self.reset()
                self.edit_json(rel, change)
                done = self.run_cli("--root", str(self.root))
                self.assertEqual(done.returncode, 1)
                self.assertNotIn("Traceback", done.stderr + done.stdout)
                self.assertEqual(done.stderr, "")
                receipt = json.loads(done.stdout)
                self.assertEqual(receipt["overall_status"], "fail")
                self.assertEqual(receipt["schema_version"], v.RECEIPT_SCHEMA_VERSION)
                self.assertTrue(receipt["errors"])
                self.assertTrue(all(set(e) == {"check", "message"} for e in receipt["errors"]))

    def test_a_root_that_does_not_exist_gives_a_failing_receipt(self):
        done = self.run_cli("--root", str(self.base / "nowhere"))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertNotIn(str(self.base), done.stdout)

    def test_an_unexpected_error_inside_the_verifier_is_still_a_structured_failure(self):
        real = v.check_index

        def broken(index):
            raise RuntimeError("simulated defect")
        v.check_index = broken
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = v.main(["--root", str(self.root)])
        finally:
            v.check_index = real
        receipt = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertEqual(receipt["checks"][0]["id"], "verifier.internal_error")
        self.assertIn("RuntimeError", receipt["errors"][0]["message"])


class RunRecords(PackCase):
    def run0(self, data):
        return data["runs"][0]

    def name_a_judge(self, family, recorded, model_id="judge-x"):
        def change(status):
            status["judge_models"] = {model_id: {"model": "Judge X", "family": family}}
            run = self.run0(status)
            run["observed"]["judge_model_id"] = model_id
            run["judge_family_check"]["status"] = recorded
        self.edit_json(STATUS, change)

    def outside_family(self):
        speakers = set(self.receipt()["empirical_judge_separation"]["runs"][0]["speaker_families"])
        return sorted(set(self.read_json(STATUS)["model_families"]) - speakers)[0]

    # ---- run references are checked whether or not a judge is named
    def test_broken_run_references_fail_without_any_judge_model(self):
        self.assertIsNone(self.run0(self.read_json(STATUS))["observed"].get("judge_model_id"))
        snapshot = lambda s: s["roster_snapshots"][0]
        cases = {
            "impossible date": (STATUS, lambda s: self.run0(s).__setitem__("date", "2026-13-45"), "is not a calendar date"),
            "date format": (STATUS, lambda s: self.run0(s).__setitem__("date", "23 Oct 2026"), "is not a calendar date"),
            "run date differs from snapshot": (STATUS, lambda s: self.run0(s).__setitem__("date", "2026-10-24"), "date is '2026-10-24', its roster snapshot"),
            "snapshot date": (STATUS, lambda s: snapshot(s).__setitem__("date", "2026-10-22"), "its roster snapshot S1-01-2026-10-23 says '2026-10-22'"),
            "snapshot edition": (STATUS, lambda s: snapshot(s).__setitem__("edition", "03"), "edition is '01', its roster snapshot"),
            "snapshot branch": (STATUS, lambda s: snapshot(s).__setitem__("branch", "S2"), "branch is 'S1', its roster snapshot"),
            "unknown snapshot": (STATUS, lambda s: self.run0(s).__setitem__("roster_snapshot", "S1-01-2026-01-01"), "is not in the status file"),
            "unknown edition": (STATUS, lambda s: self.run0(s).__setitem__("edition", "77"), "names unknown edition '77'"),
            "unknown branch": (STATUS, lambda s: self.run0(s).__setitem__("branch", "S9"), "names unknown branch 'S9'"),
            "edition on another branch": (STATUS, lambda s: self.run0(s).__setitem__("edition", "03"), "its roster snapshot"),
            "unknown channel": (STATUS, lambda s: self.run0(s).__setitem__("channel_id", "board"), "channel 'board' is not in the roster"),
            "channel name": (STATUS, lambda s: self.run0(s).__setitem__("channel", "Engineering"), "is not in the roster under the name 'Engineering'"),
            "unknown routing policy": (STATUS, lambda s: self.run0(s).__setitem__("routing_policy", "none"), "names unknown routing policy 'none'"),
            "not listed by its policy": (STATUS, lambda s: s["routing_policies"][0].__setitem__("used_by", []), "must be listed in used_by"),
            "unknown routing rule": (STATUS, lambda s: s["routing_policies"][0].__setitem__("rule", "random"), "routing rule 'random' is not known"),
            "route missing from snapshot": (STATUS, lambda s: snapshot(s)["routes"].pop(0), "a member route is not in the snapshot's route table"),
            "snapshot blob": (STATUS, lambda s: snapshot(s).__setitem__("git_blob", "0" * 40), "git blob differs"),
            "edited roster": (ORG, lambda o: o.__setitem__("note", o["note"] + " (edited)"), "is not the roster snapshot S1-01-2026-10-23 (git blob differs)"),
            "unknown channel member": (ORG, lambda o: o["channels"][2]["members"].append("ghost"), "has an unknown member"),
            "observed speaker outside the channel": (STATUS, lambda s: self.run0(s)["observed"].__setitem__("speaker_model_ids", ["astra"]), "observed speaker 'astra' is not a route open"),
        }
        for name, (rel, change, fragment) in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.edit_json(rel, change)
                receipt = self.assert_fails("record_consistency.run_references", fragment)
                self.assertEqual(receipt["record_consistency"], "fail")
                row = receipt["empirical_judge_separation"]["runs"][0]
                self.assertIsNone(row["judge_model_id"])
                self.assertIsNone(row["speaker_families"], "speakers of a run with a broken reference are not reported")

    def test_run_references_are_checked_before_the_judge(self):
        """A judge from a separate family does not rescue a run whose snapshot is for another date."""
        self.name_a_judge(self.outside_family(), "verified")
        self.edit_json(STATUS, lambda s: s["roster_snapshots"][0].__setitem__("date", "2026-10-22"))
        receipt = self.assert_fails("record_consistency.run_references", "its roster snapshot")
        self.assertIn("speaker families cannot be established", "\n".join(self.errors(receipt, "record_consistency.judge_family")))
        self.assertEqual(receipt["empirical_judge_separation"]["runs"][0]["record_state"], "invalid")

    # ---- one model id, one family
    def test_conflicting_family_for_one_model_id_fails(self):
        cases = {
            "model_routes against snapshot": lambda s: s["model_routes"][0].__setitem__("family", "muse"),
            "snapshot against model_routes": lambda s: s["roster_snapshots"][0]["routes"][3].__setitem__("family", "claude"),
            "judge_models against model_routes": lambda s: s.__setitem__("judge_models", {"astra": {"family": "claude"}}),
            "two snapshots": lambda s: s["roster_snapshots"].append(dict(copy.deepcopy(s["roster_snapshots"][0]), id="S1-01-later",
                                                                         routes=[{"route": "default", "model": "MiMo Flash", "model_id": "mimo-v2.6-flash", "family": "gpt"}])),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.edit_json(STATUS, change)
                self.assert_fails("record_consistency.model_families", "is declared in family")

    def test_family_outside_model_families_fails(self):
        self.edit_json(STATUS, lambda s: s["model_routes"][0].__setitem__("family", "unlisted"))
        self.assert_fails("record_consistency.model_families", "which is not in model_families")

    def test_a_speaker_model_renamed_into_another_family_cannot_pass_as_a_separate_judge(self):
        """The judge is a speaker model listed under another family in judge_models: the conflict fails the pack."""
        def change(status):
            status["judge_models"] = {"muse-spark-1.3": {"family": "claude"}}
            run = self.run0(status)
            run["observed"]["judge_model_id"] = "muse-spark-1.3"
            run["judge_family_check"]["status"] = "verified"
        self.edit_json(STATUS, change)
        receipt = self.assert_fails("record_consistency.model_families", "model 'muse-spark-1.3' is declared in family 'muse'")
        self.assertIn("record_consistency.judge_family", self.failed(receipt), "the first declaration (muse) is a speaker family")

    # ---- judge record
    def test_verified_without_a_judge_model_fails(self):
        self.edit_json(STATUS, lambda s: self.run0(s)["judge_family_check"].__setitem__("status", "verified"))
        self.assert_fails("record_consistency.judge_family", "says verified, the record supports not_verified")

    def test_judge_from_a_speaker_family_fails(self):
        speaker = self.receipt()["empirical_judge_separation"]["runs"][0]["speaker_families"][0]
        self.name_a_judge(speaker, "verified")
        receipt = self.assert_fails("record_consistency.judge_family", f"judge family {speaker} is also a speaker family")
        self.assertEqual(receipt["empirical_judge_separation"]["runs"][0]["record_state"], "judge_shares_speaker_family")

    def test_a_separate_judge_in_the_record_is_consistent_but_still_not_empirically_verified(self):
        self.name_a_judge(self.outside_family(), "verified")
        receipt = self.receipt()
        self.assertEqual(receipt["overall_status"], "pass", receipt["errors"])
        self.assertEqual(receipt["record_consistency"], "pass")
        judges = receipt["empirical_judge_separation"]
        self.assertEqual(judges["runs"][0]["record_state"], "judge_family_distinct_in_record")
        self.assertEqual(judges["record_declares_distinct_judge_family_runs"], 1)
        self.assertEqual((judges["status"], judges["verified_runs"], judges["not_verified_runs"]),
                         ("not_verified_from_public_record", 0, 1))
        self.assertEqual(receipt["empirical_blinding"]["status"], "not_verified_from_public_record")

    def test_separated_judge_recorded_as_not_verified_fails(self):
        self.name_a_judge(self.outside_family(), "not_verified")
        self.assert_fails("record_consistency.judge_family", "says not_verified, the record supports verified")

    def test_judge_with_no_known_family_fails(self):
        self.name_a_judge("unlisted-family", "verified")
        receipt = self.assert_fails("record_consistency.judge_family", "has no known family")
        self.assertEqual(receipt["empirical_judge_separation"]["runs"][0]["record_state"], "invalid")

    def test_judge_named_but_not_declared_anywhere_fails(self):
        self.edit_json(STATUS, lambda s: self.run0(s)["observed"].__setitem__("judge_model_id", "mystery-model"))
        self.assert_fails("record_consistency.judge_family", "judge model 'mystery-model' has no known family")

    def test_unknown_judge_status_fails(self):
        self.edit_json(STATUS, lambda s: self.run0(s)["judge_family_check"].__setitem__("status", "passed"))
        self.assert_fails("record_consistency.judge_family", "judge_family_check.status is 'passed'")

    def test_limits_must_say_when_a_run_is_not_verified(self):
        self.edit_json(STATUS, lambda s: s["research"].__setitem__("generated_limits", ["One end-to-end test run so far."]))
        self.assert_fails("record_consistency.judge_family", "generated_limits does not say so")

    # ---- status and roster
    def test_duplicate_edition_fails(self):
        self.edit_json(STATUS, lambda s: s["editions"].insert(1, dict(s["editions"][0])))
        self.assert_fails("record_consistency.status_counts", "edition 01 is repeated")

    def test_edition_on_an_unknown_branch_fails(self):
        self.edit_json(STATUS, lambda s: s["editions"][0].__setitem__("branch", "S9"))
        self.assert_fails("record_consistency.status_counts", "names unknown branch 'S9'")

    def test_edition_size_must_match_its_branch(self):
        self.edit_json(STATUS, lambda s: s["editions"][0].__setitem__("size", 16))
        self.assert_fails("record_consistency.status_counts", "has size 16")

    def test_duplicate_run_id_fails(self):
        self.edit_json(STATUS, lambda s: s["runs"].append(dict(s["runs"][0])))
        self.assert_fails("record_consistency.status_counts", "a run id is repeated")

    def test_card_total_in_the_status_must_match_the_index(self):
        def change(status):
            link = next(d for d in status["research"]["downloads"] if d["id"] == "cards-s1")
            link["label"] = link["label"].replace("19,414", "20,000")
        self.edit_json(STATUS, change)
        self.assert_fails("record_consistency.status_counts", "cards download label does not say")

    def test_index_total_must_equal_the_shard_counts(self):
        self.edit_json(INDEX, lambda index: index.__setitem__("total", index["total"] + 1))
        self.assert_fails("archive.index_consistent", "the shard counts add up to")

    def test_roster_headcount_must_match(self):
        self.edit_json(ORG, lambda org: org["people"].pop())
        self.assert_fails("record_consistency.roster", "headcount says")

    def test_status_with_a_duplicate_key_fails(self):
        path = self.root / STATUS
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('{', '{"runs": [], ', 1), encoding="utf-8")
        self.assert_fails("archive.files_readable", "duplicate key 'runs'")


class FullShards(PackCase):
    """--shards: a miniature set of shards built from the sample, with an index and manifest to match."""

    def setUp(self):
        super().setUp()
        self.shards = self.root / "shards"
        self.shards.mkdir()
        by_edition = {}
        for line in self.lines():
            by_edition.setdefault(json.loads(line)["edition"], []).append(line)
        index_path = self.root / INDEX
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index["editions"]:
            data = ("\n".join(reversed(by_edition[entry["edition"]])) + "\n").encode("utf-8")  # unsorted on purpose
            (self.shards / f"{entry['edition']}.jsonl").write_bytes(data)
            entry["cards"], entry["sha256"] = len(by_edition[entry["edition"]]), hashlib.sha256(data).hexdigest()
        index["total"] = 100
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        self.edit_json(MANIFEST, lambda m: m.__setitem__("source_index_sha256", hashlib.sha256(index_path.read_bytes()).hexdigest()))

        def relabel(status):
            link = next(d for d in status["research"]["downloads"] if d["id"] == "cards-s1")
            link["label"] = link["label"].replace("19,414", "100")
        self.edit_json(STATUS, relabel)

    def test_matching_shards_pass_and_the_sample_re_derives(self):
        receipt = self.receipt(self.shards)
        self.assertEqual(receipt["overall_status"], "pass", receipt["errors"])
        self.assertIn("archive.shards_full", {c["id"] for c in receipt["checks"]})
        self.assertEqual(sum(1 for a in receipt["artifacts"] if a["path"].startswith("shards/")), 20)
        self.assertNotIn(str(self.base), v.render(receipt))

    def test_every_shard_card_is_audited_for_blind_readiness(self):
        blind = self.receipt(self.shards)["blind_readiness"]
        self.assertEqual(blind["shards"]["cards_audited"], 100)
        self.assertEqual(blind["shards"]["flagged_situation_card_ids"], blind["flagged_situation_card_ids"])
        self.assertEqual(blind["shards"]["evaluator_resolution_cue_count"], blind["evaluator_resolution_cue_count"])
        self.assertNotIn("flagged_agent_visible_matches", blind["shards"])
        self.assert_fails("blind_readiness.require_clean", "shards:", self.shards, require_blind_clean=True)

    def test_changed_shard_fails_its_digest(self):
        path = next(iter(sorted(self.shards.iterdir())))
        path.write_bytes(path.read_bytes().replace(b"client", b"klient", 1))
        self.assert_fails("archive.shards_full", "has SHA-256", self.shards)

    def test_card_id_repeated_across_shards_fails(self):
        first, second = sorted(self.shards.iterdir())[:2]
        card = json.loads(second.read_text(encoding="utf-8").splitlines()[0])
        card["card_id"] = json.loads(first.read_text(encoding="utf-8").splitlines()[0])["card_id"]
        second.write_text(json.dumps(card, ensure_ascii=False) + "\n" + "\n".join(second.read_text(encoding="utf-8").splitlines()[1:]) + "\n", encoding="utf-8")
        self.assert_fails("archive.shards_full", "repeats 01.jsonl:1", self.shards)

    def test_missing_shard_fails(self):
        sorted(self.shards.iterdir())[0].unlink()
        self.assert_fails("archive.shards_full", "shards/01.jsonl: cannot be read", self.shards)

    def test_symlinked_shard_file_is_refused(self):
        path = self.shards / "01.jsonl"
        target = self.base / "01-copy.jsonl"
        shutil.copy(path, target)
        path.unlink()
        os.symlink(target, path)
        self.assert_fails("archive.shards_full", "shards/01.jsonl: is a symbolic link", self.shards)

    def test_symlinked_shards_directory_is_refused(self):
        link = self.base / "shards-link"
        os.symlink(self.shards, link)
        receipt = self.assert_fails("archive.shards_full", "shards directory: not a real directory", link)
        self.assertEqual([a for a in receipt["artifacts"] if a["path"].startswith("shards/")], [])

    def test_missing_shards_directory_fails_from_the_command_line(self):
        done = self.run_cli("--root", str(self.root), "--shards", str(self.base / "no-such-folder"))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("archive.shards_full", {c["id"] for c in json.loads(done.stdout)["checks"] if c["result"] == "fail"})

    def test_shards_cannot_be_checked_against_a_malformed_index(self):
        self.edit_json(INDEX, lambda index: index["editions"].__setitem__(0, None))
        self.assert_fails("archive.shards_full", "not evaluated", self.shards)

    def test_sample_builder_gives_the_committed_sample_and_manifest(self):
        sample, manifest = make_sample.build(self.root, self.shards)
        self.assertEqual(sample, (self.root / SAMPLE).read_bytes())
        self.assertEqual(manifest, self.read_json(MANIFEST))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(make_sample.main(["--root", str(self.root), "--shards", str(self.shards), "--check"]), 0)
            (self.shards / "01.jsonl").write_bytes(b"{}\n")
            self.assertEqual(make_sample.main(["--root", str(self.root), "--shards", str(self.shards), "--check"]), 1)


class Readme(unittest.TestCase):
    """The English and Korean summaries carry the same figures, and those figures are the receipt's."""

    def setUp(self):
        self.text = (REPO / "README.md").read_text(encoding="utf-8")
        self.english = self.text.split("## Summary", 1)[1].split("## 한국어 요약", 1)[0]
        self.korean = self.text.split("## 한국어 요약", 1)[1].split("## Verify", 1)[0]

    def test_english_and_korean_summaries_state_the_same_numbers(self):
        def numbers(part):
            return sorted(re.findall(r"\d+(?:,\d{3})*", part))
        self.assertEqual(numbers(self.english), numbers(self.korean))
        self.assertEqual(self.english.count("\n- "), self.korean.count("\n- "), "same number of list items")
        self.assertEqual(self.english.count("**"), self.korean.count("**"), "same number of headed paragraphs")

    def test_summary_figures_are_the_ones_in_the_committed_receipt(self):
        receipt = json.loads((REPO / "expected-receipt.json").read_text(encoding="utf-8"))
        blind, counts = receipt["blind_readiness"], receipt["summary"]
        evaluator, situation = blind["evaluator_resolution_cue_count"], blind["agent_visible_situation_cue_count"]
        self.assertIn(f"flags {evaluator} cards in `invariants` and {situation} cards in `situation`", self.english)
        self.assertIn(f"`invariants`에서 {evaluator}장, `situation`에서 {situation}장", self.korean)
        self.assertEqual(blind["agent_visible_option_cue_count"], 0)
        lines = sorted(int(m["where"].rsplit(" ", 1)[1]) for m in blind["flagged_agent_visible_matches"])
        self.assertEqual(lines, [54, 77, 80])
        total, orgs = counts["card_index"]["cards_total"], counts["card_index"]["organizations"]
        for part in (self.english, self.korean):
            self.assertIn(f"{total:,}", part)
            self.assertIn(str(orgs), part)
            self.assertIn(str(counts["status"]["editions"]), part)
            for status in ("requires_curation_before_judged_run", "not_verified_from_public_record"):
                self.assertIn(status, part)
        self.assertEqual((counts["status"]["runs"], counts["status"]["branches"], counts["status"]["branches_active"],
                          counts["status"]["editions_active"]), (1, 5, 1, 1))


class Discovery(unittest.TestCase):
    """`python3 -B -m unittest` and `python3 -B -m unittest discover -s tests` must find the same tests."""

    def ran(self, *args):
        done = subprocess.run([sys.executable, "-B", "-m", "unittest", *args], cwd=REPO, capture_output=True, text=True,
                              encoding="utf-8", env=dict(os.environ, **{NESTED: "1"}))
        found = re.search(r"^Ran (\d+) tests? in", done.stderr, re.MULTILINE)
        self.assertIsNotNone(found, done.stderr[-2000:])
        return done.returncode, int(found.group(1)), done.stderr

    def test_both_discovery_commands_find_the_same_tests_and_pass(self):
        """The nested runs collect and count every test; tests that need a pack copy are skipped there, not here."""
        if os.environ.get(NESTED):
            self.skipTest("already inside the nested run this test starts")
        default_code, default_count, default_log = self.ran()
        explicit_code, explicit_count, explicit_log = self.ran("discover", "-s", "tests")
        self.assertEqual(default_code, 0, default_log[-2000:])
        self.assertEqual(explicit_code, 0, explicit_log[-2000:])
        self.assertGreater(default_count, 0, "default discovery found no tests")
        self.assertEqual(default_count, explicit_count)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        english, korean = re.findall(r"(\d+) tests\b", readme), re.findall(r"시험 (\d+)개", readme)
        self.assertTrue(english and korean, "README must state the test count in both languages")
        self.assertEqual({int(n) for n in english + korean}, {default_count}, "README states another test count")

    def test_tests_directory_is_a_package(self):
        self.assertTrue((REPO / "tests" / "__init__.py").is_file())


class AstraR2Regressions(PackCase):
    """Regressions for Astra Round 2 findings (projection + blind gate, edition-org binding, full status types, sample digest)."""

    def test_emit_agent_projection_with_require_blind_clean_fails_and_emits_zero_cards(self):
        done = self.run_cli("--root", str(self.root), "--require-blind-clean", "--emit-agent-projection", "all")
        self.assertEqual(done.returncode, 1)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["overall_status"], "fail")
        self.assertIn("blind_readiness.require_clean", {c["id"] for c in receipt["checks"] if c["result"] == "fail"})
        self.assertNotIn("Leila has already said", done.stdout)

    def test_edition_and_snapshot_swap_to_another_org_is_refused(self):
        def swap(status):
            status["runs"][0]["edition"] = "04"
            status["roster_snapshots"][0]["edition"] = "04"
        self.edit_json(STATUS, swap)
        self.assert_fails("record_consistency.run_references", "sevenply-works")

    def test_wrong_types_in_run_agent_inputs_and_research_method_ko_return_fail_receipt(self):
        for label, mutate in (
            ("agent_inputs", lambda s: s["runs"][0].__setitem__("agent_inputs", 42)),
            ("method_ko", lambda s: s["research"].__setitem__("method_ko", 42)),
        ):
            with self.subTest(field=label):
                self.tearDown(); self.setUp()
                self.edit_json(STATUS, mutate)
                done = self.run_cli("--root", str(self.root))
                self.assertEqual(done.returncode, 1)
                self.assertNotIn("Traceback", done.stderr)
                receipt = json.loads(done.stdout)
                self.assertEqual(receipt["overall_status"], "fail")
                self.assertIn("record_consistency.status_counts", {c["id"] for c in receipt["checks"] if c["result"] == "fail"})

    def test_one_byte_edit_in_sample_fails_default_archive_policy(self):
        lines = self.lines()
        card = json.loads(lines[0])
        card["situation"] += "."
        lines[0] = json.dumps(card, ensure_ascii=False)
        self.write_lines(lines, sync_manifest=False)
        self.assert_fails("archive.sample_policy", "sample_sha256")

    def test_wrong_type_in_route_model_display_name_fails_status_counts(self):
        for label, mutate in (
            ("model_routes", lambda s: s["model_routes"][0].__setitem__("model", 42)),
            ("snapshot_routes", lambda s: s["roster_snapshots"][0]["routes"][0].__setitem__("model", 42)),
        ):
            with self.subTest(where=label):
                self.tearDown(); self.setUp()
                self.edit_json(STATUS, mutate)
                done = self.run_cli("--root", str(self.root))
                self.assertEqual(done.returncode, 1)
                self.assertNotIn("Traceback", done.stderr)
                receipt = json.loads(done.stdout)
                self.assertEqual(receipt["overall_status"], "fail")
                self.assertIn("record_consistency.status_counts", {c["id"] for c in receipt["checks"] if c["result"] == "fail"})


if __name__ == "__main__":
    unittest.main()
