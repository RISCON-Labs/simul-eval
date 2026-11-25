# simul-eval

Verification pack for **SIMUL**, a synthetic benchmark of organizational decisions from RISCON Labs.

The English summary and the Korean summary (한국어 요약) below state the same claims, numbers and caveats. The reference sections after them (commands, file formats, check rules) are written in English only.

- Site: <https://simul.risconlabs.com>
- Vericull, the lab's evidence gate: <https://vericull.risconlabs.com>
- RISCON Labs: <https://risconlabs.com>
- Contact: <hello@risconlabs.com>

## Summary

**The design.** SIMUL takes one decision point, a moment where an organization could have gone another way, and places it in simulated companies. The protocol has AI agents play the members of a company and talk the decision through. It requires that an agent be given only the situation before the decision and the channel history, that the reference outcome be withheld for evaluation, and that the judge come from a different model family than the speakers. These are requirements of the protocol. This repository does not show that any run met them.

**What is built so far.**

- Stage-1 case cards for 20 simulated companies: 19,414 cards in 20 files.
- 1 of 5 company sizes: the 15-person company. The other four sizes are planned.
- 1 recorded end-to-end test run (2026-10-23, 9 turns in 60 seconds). The agents deferred the decision; the result is recorded as undecided and is not yet comparable with the reference. The run's turn log is not published, so neither blinding nor judge-family separation is verified for it from the public record.

**What this repository holds.** The card schema, a 100-card sample drawn by a fixed rule, a pinned snapshot of the site's status file (status, editions, runs, method and limits), the roster of the 15-person company, and one standard-library script that checks them and prints a receipt. There are no model scores, no model transcripts and no reference outcomes here, so the pack cannot rank models.

**Before any judged blind run, two things are required.** The published stage-1 cards are not blind-ready as they stand.

1. Strip `invariants` from every agent input. It is an evaluator-only field, and many entries tell how the case was handled, for example "the missing-enclosure condition was waived rather than enforced" (sample line 1).
2. Rewrite to the moment before the decision, or exclude, the cards whose `situation` or `options` tell how the case ended. Sample line 54 says a decision-maker "has already said" the promotion would wait until next year; other published cards end with wording such as "The exchange ends without a decision."

The verifier screens for both by wording. In the 100-card sample it flags 86 cards in `invariants` and 3 cards in `situation` (lines 54, 77 and 80), and none in `options`. Across all 19,414 cards (run with `--shards`) it flags 17,878 cards in `invariants`, 499 in `situation` and 6 in `options`. The screen is lexical: a flag is a candidate for a curator to read, not a proven leak, and a card without a flag is not cleared.

**No card has a `reference_outcome` or `refutation_condition` field.** That is a statement about fields, not about wording: as the paragraph above says, the text of some cards does tell how the case ended. Refutation conditions are not written for stage-1 cards yet.

**Known exceptions in the stage-1 archive.** Cards normally offer two or more options. Among the 19,414 published cards, 3 hold a single option, 3 hold an empty option text, and 23 have `invariants` set to null; on 2 the opener is not among the participants, and 1 lists a participant twice. None of these is in the sample. The published bytes are left as they are.

**Synthetic scope.** The simulated companies, roles and cases are synthetic research constructs. A few stage-1 texts mention real-world public tools or charities in passing.

**What a passing receipt means.** In default mode, the 6 tracked pack inputs (schema, index, sample, manifest, status, roster) are well-formed and internally consistent, and the sample matches its manifest digest. To confirm byte-for-byte integrity against the committed release and all 20 shards, compare the receipt against `expected-receipt.json` and run with `--shards`. A passing receipt does not mean the cards are ready for a blind run, that the recorded run was blind, or that its judge was separate from the speakers. The receipt reports those separately: `blind_readiness` reads `requires_curation_before_judged_run`, and `empirical_judge_separation` and `empirical_blinding` read `not_verified_from_public_record`.

**Other limits.**

- The run so far was a test of the pipeline end to end. No run with a deadline or a budget cap has been made.
- 1 of 28 listed virtual offices is built. The status file states a goal of 100 episodes.
- The screen for post-decision wording cannot tell whether a sentence gives the outcome away in words its lexicon does not list, and some of its flags are background facts rather than outcomes.
- Runs are checked against their own record only. Nothing in this pack is evidence of what a model was shown or which model judged.
- The status file describes the protocol. Where a sentence there says what agents see, read it as a requirement on a runner, not as a measurement of the recorded run.
- The simulation runner is not in this repository.

- Run: `python3 -B -I scripts/verify_simul_pack.py` (prints a JSON receipt; exit code 0 means every check passed)
- Tests: `python3 -B -m unittest` or `python3 -B -m unittest discover -s tests`. Both find the same 117 tests.
- Contact: <hello@risconlabs.com>

## 한국어 요약

**설계.** SIMUL은 조직이 다른 길로 갈 수도 있었던 갈림길 하나를 모의 회사에 옮겨 놓습니다. 프로토콜에서는 AI 에이전트가 회사 구성원을 맡아 그 결정을 논의합니다. 프로토콜은 에이전트에게 결정 이전의 상황과 채널 기록만 줄 것, 기준 결과는 평가용으로 빼 둘 것, 판정 모델은 발화 모델과 다른 계열일 것을 요구합니다. 이것은 프로토콜의 요구 사항입니다. 이 저장소는 어떤 운행이 이 요구를 지켰는지 보여 주지 않습니다.

**지금까지 지은 것.**

- 모의 회사 20곳의 1단계 사례 카드: 파일 20개에 19,414장.
- 회사 규모 5가지 가운데 1가지: 15인 회사. 나머지 네 규모는 계획 단계입니다.
- 기록된 전 과정 시험 운행 1건(2026-10-23, 60초 동안 9턴). 에이전트는 결정을 미뤘고, 결과는 미결정으로 기록됐으며 아직 기준 결과와 비교할 수 없습니다. 이 운행의 턴별 기록은 공개하지 않았으므로, 공개 기록으로는 블라인드 여부도 판정 모델 계열 분리도 확인되지 않습니다.

**이 저장소에 있는 것.** 카드 스키마, 정해진 규칙으로 뽑은 카드 100장 표본, 사이트 상태 파일의 고정 스냅숏(상태, 판본, 운행, 방법, 한계), 15인 회사의 구성원 명단, 그리고 이 파일들을 검사해 영수증을 출력하는 표준 라이브러리 스크립트 하나입니다. 모델 점수, 모델 대화 기록, 기준 결과는 없으므로 이 자료로 모델 순위를 매길 수 없습니다.

**판정하는 블라인드 운행 전에 두 가지가 필요합니다.** 공개된 1단계 카드는 지금 그대로는 블라인드 운행에 쓸 수 없습니다.

1. 모든 에이전트 입력에서 `invariants`를 뺍니다. 평가자 전용 필드이며, 사례가 어떻게 처리됐는지 적은 항목이 많습니다. 표본 1행의 "the missing-enclosure condition was waived rather than enforced"가 그 예입니다.
2. `situation`이나 `options`에 사례의 결말이 드러나는 카드는 결정 직전 시점으로 다시 쓰거나 제외합니다. 표본 54행에는 결정권자가 홍보를 내년으로 미루겠다고 "has already said"(이미 말했다)라는 문장이 있고, 다른 공개 카드에는 "The exchange ends without a decision."처럼 끝나는 문장이 있습니다.

검증기는 두 경우를 문구로 선별합니다. 표본 100장에서는 `invariants`에서 86장, `situation`에서 3장(54·77·80행)을 표시하고 `options`에서는 표시한 카드가 없습니다. 전체 19,414장(`--shards`로 실행)에서는 `invariants`에서 17,878장, `situation`에서 499장, `options`에서 6장을 표시합니다. 이 선별은 어휘 검사입니다. 표시된 카드는 큐레이터가 읽어야 할 후보이며 누출이 입증된 것은 아니고, 표시되지 않은 카드도 통과로 인정된 것이 아닙니다.

**어느 카드에도 `reference_outcome`과 `refutation_condition` 필드는 없습니다.** 이는 필드에 관한 말이고 문구에 관한 말이 아닙니다. 위 문단대로 일부 카드의 문장에는 사례의 결말이 드러납니다. 1단계 카드의 반증 조건은 아직 쓰지 않았습니다.

**1단계 공개본의 알려진 예외.** 카드는 보통 선택지를 두 개 이상 둡니다. 공개된 19,414장 가운데 3장은 선택지가 하나뿐이고, 3장에는 빈 선택지 문구가 있으며, 23장은 `invariants`가 null입니다. 2장은 opener가 participants에 없고, 1장은 같은 참가자를 두 번 적었습니다. 이 카드들은 표본에 없습니다. 공개된 바이트는 고치지 않고 그대로 둡니다.

**합성 범위.** 모의 회사·역할·사례는 연구용으로 만든 합성 구성물입니다. 일부 1단계 카드 문장에는 실제 공개 도구나 자선단체 이름이 지나가듯 언급됩니다.

**영수증 통과의 뜻.** 기본 모드에서 추적 대상 6개 입력(스키마, 색인, 표본, manifest, 상태, 명단)의 형식과 기록 정합성이 맞고 표본이 manifest 해시와 일치한다는 뜻입니다. 커밋된 릴리스 바이트와 20개 샤드 전체의 무결성을 확인하려면 영수증을 `expected-receipt.json`과 대조하고 `--shards`로 실행해야 합니다. 영수증 통과가 카드의 블라인드 운행 준비, 기록된 운행의 블라인드 여부, 판정 모델 계열 분리를 뜻하지는 않습니다. 영수증은 이를 따로 적습니다. `blind_readiness`는 `requires_curation_before_judged_run`, `empirical_judge_separation`과 `empirical_blinding`은 `not_verified_from_public_record`입니다.

**그 밖의 한계.**

- 지금까지의 운행은 전 과정이 돌아가는지 본 시험이었습니다. 기한이나 예산 상한을 넣은 운행은 아직 없습니다.
- 목록에 있는 가상 사무실 28곳 가운데 1곳을 지었습니다. 상태 파일에는 에피소드 100개라는 목표가 적혀 있습니다.
- 결말 문구 선별은 어휘 목록에 없는 말로 결말을 드러내는 문장을 가려내지 못하며, 표시한 것 가운데 일부는 결말이 아니라 배경 사실입니다.
- 운행은 그 운행의 기록끼리만 대조합니다. 이 자료에는 모델이 무엇을 보았는지, 어느 모델이 판정했는지를 보여 주는 증거가 없습니다.
- 상태 파일은 프로토콜을 설명합니다. 거기서 에이전트가 무엇을 보는지 말하는 문장은 실행기가 지켜야 할 요구 사항으로 읽어야 하며, 기록된 운행을 측정한 결과가 아닙니다.
- 시뮬레이션 실행기는 이 저장소에 없습니다.

- 실행: `python3 -B -I scripts/verify_simul_pack.py` (JSON 영수증을 출력하며, 종료 코드 0이면 모든 검사 통과)
- 시험: `python3 -B -m unittest` 또는 `python3 -B -m unittest discover -s tests`. 두 명령은 같은 시험 117개를 찾습니다.
- 문의: <hello@risconlabs.com>

## Verify

Python 3.11 or later on Linux or macOS. No packages to install. `-B` keeps Python from writing compiled files into the clone.

```bash
git clone https://github.com/RISCON-Labs/simul-eval
cd simul-eval
python3 -B -I scripts/verify_simul_pack.py      # prints a JSON receipt; exit 0 = every check passed
python3 -B -m unittest                          # or: python3 -B -m unittest discover -s tests
```

Both find the same 117 tests. The suite takes about twenty seconds on a laptop.

The receipt has no clock time and no machine path, so the same files always give the same bytes. For the files in this repository the output equals `expected-receipt.json`:

```bash
python3 -B -I scripts/verify_simul_pack.py | diff - expected-receipt.json && echo identical
```

To check all 19,414 published cards, download the 20 shards named in `data/cards-s1-index.json` (each `file` is a path on simul.risconlabs.com) into one folder and pass it in:

```bash
python3 -B -I scripts/verify_simul_pack.py --shards path/to/shards
```

This compares every shard with its SHA-256 in the index, validates every card, checks `card_id` across all shards, re-derives the 100-card sample from the shards by the fixed rule, and runs the blind-readiness screen over every card.

Two more switches:

```bash
python3 -B -I scripts/verify_simul_pack.py --require-blind-clean          # exit 1 while any agent-visible cue remains
python3 -B -I scripts/verify_simul_pack.py --emit-agent-projection all    # the agent-side view of the sample, as JSONL
```

`--require-blind-clean` is the gate for a card set that someone intends to run blind. The published pack fails it, on purpose. `--emit-agent-projection all` or `unflagged` prints what an agent would be given; its SHA-256 is the one in the receipt under `blind_readiness.agent_projection`, so a run can record exactly which projection it used.

## What is in the pack

| Path | What it is |
|---|---|
| `data/simul-status.json` | Pinned release snapshot of the status file (built to match `simul.risconlabs.com/data/simul-status.json` upon site release): status, editions, runs, method and limits. Its byte length and SHA-256 are in `expected-receipt.json`; compare them against the live endpoint before treating the two revisions as identical. |
| `data/s1-org.json` | The 15-person simulated company of the test run, with channels and model routes. |
| `data/cards-s1-index.json` | Index of the stage-1 card shards: organization, card count and SHA-256 per shard. |
| `data/sample-cards-s1.jsonl` | 100 cards: the first 5 by `card_id` from each of the 20 shards, copied byte for byte. |
| `data/sample-manifest.json` | The sample policy, the card ids it gives, and the digest of the index it was drawn from. |
| `schema/stage1-card.schema.json` | JSON Schema (2020-12) for one card, with the field contract and the blind-run requirements. |
| `scripts/verify_simul_pack.py` | The verifier. |
| `scripts/make_sample.py` | Rebuilds the sample from the full shards, or checks it with `--check`. |
| `tests/` | 117 tests: the shipped pack passes, and each way of breaking it is caught by the check that owns the rule. |
| `expected-receipt.json` | The receipt for the files as committed. |

## Field contract (protocol `simul-stage1-v1`)

One JSON object per line. All eight fields are required; no other field is allowed. The contract is fixed inside the verifier. The schema file and the card index must state exactly this partition, and a schema file that says otherwise fails; it does not change which fields are inspected.

| Field | Type | Group | Meaning |
|---|---|---|---|
| `card_id` | string, 12 hex digits | identity | Stable identifier, unique across every shard. |
| `edition` | string, `01` to `100` | identity | Roster slot of the organization when the card was built. A display slot; the organization is the `org` in the index. |
| `channel` | string | agent | Office channel id. |
| `opener` | string | agent | Roster id of the member who opens the conversation. |
| `participants` | array of strings | agent | Roster ids of the members involved. |
| `situation` | string | agent | Written as the situation before the decision. Some published texts also tell how the conversation ended; see the summary. |
| `options` | array of strings | agent | The choices on the table. |
| `invariants` | array of strings, or null | evaluator | Organizational properties the case keeps when it moves to another company. Never part of an agent input. |

Withheld, and refused if found in a card at any depth: `reference_outcome`, `refutation_condition`.

`project_for_agent(card)` in the verifier returns the five agent fields and nothing else. It refuses a card that carries a withheld key, a field outside the protocol, or lacks an agent field. It does not edit text: a situation that tells how the case ended is still in the projection, which is why the second requirement in the summary exists.

## What the verifier checks

Checks are grouped by the question they answer. `overall_status` is `pass` when every check below passes.

| Check | Rule |
|---|---|
| `archive.files_readable` | Each file is opened once, inside the pack, without following a symbolic link at any path component, and must be a regular file. The bytes read are the bytes hashed and parsed. Strict JSON: duplicate keys, NaN and runaway nesting are refused. |
| `archive.schema_contract` | The schema file states exactly the field partition and constraints fixed in the verifier, and the card index names the same fields. |
| `archive.index_consistent` | Shards, organizations and digests are unique and well formed; the total equals the sum of the shard counts. |
| `archive.sample_jsonl` | Every line is one JSON object. Blank lines, malformed lines and a missing final newline fail. |
| `archive.sample_schema` | Every card has exactly the protocol's fields with the right types. |
| `archive.sample_card_id_unique` | No `card_id` appears twice. |
| `archive.sample_withheld_absent` | No withheld field, under its own name or a common alias, at any depth; no withheld-field name or labelled outcome line in agent-visible text. |
| `archive.sample_policy` | The manifest states the fixed policy `first_5_by_card_id_asc_per_shard`: 20 shards, 5 cards each, 100 in all. The sample is exactly the cards it lists, bound to this index by digest. A manifest that restates any of these numbers fails. |
| `archive.shards_full` | Only with `--shards`: digests, counts, schema, withheld fields and `card_id` across all shards, plus the sample re-derivation. |
| `blind_readiness.projection_contract` | Every card projects to exactly the five agent fields, with `invariants` stripped. |
| `blind_readiness.require_clean` | Only with `--require-blind-clean`: no card carries a post-decision cue in `situation` or `options`. |
| `record_consistency.status_counts` | Editions, sizes and routing policies are unique and refer to each other; the card figures in the status file match the index. |
| `record_consistency.roster` | Headcount, reporting lines, channels and model routes resolve. |
| `record_consistency.model_families` | One model id has one family across `model_routes`, `roster_snapshots` and `judge_models`. |
| `record_consistency.run_references` | Each run's date, edition, branch, roster snapshot (same edition, branch and date), channel and members resolve. Checked whether or not the run names a judge. |
| `record_consistency.judge_family` | Each run's recorded judge-family status says what its record supports. |

A value of the wrong type in any checked file field is reported as an error in a failing receipt (`schema_version`, `overall_status`, `checks`, `errors`) with exit code 1. `--emit-agent-projection` runs full verification first and refuses to emit any cards (exiting 1 with the failing receipt) if any check fails, including `--require-blind-clean`.

### Receipt fields that are not checks

| Field | Value for this pack | Meaning |
|---|---|---|
| `archive_integrity_status` | `pass` | Every `archive.*` check passed. |
| `record_consistency` | `pass` | Every `record_consistency.*` check passed. This is agreement inside a self-declared record. |
| `blind_readiness.status` | `requires_curation_before_judged_run` | The lexical screen found post-decision wording. It never reads "ready": with no hit it reads `no_lexical_cue_found_review_still_required`. |
| `empirical_judge_separation.status` | `not_verified_from_public_record` (`verified_runs` 0, `not_verified_runs` 1) | The pack holds no turn log and no judge transcript. The first run's record names no judge model. |
| `empirical_blinding.status` | `not_verified_from_public_record` | The pack holds no per-turn model input. |

## License

MIT. Copyright (c) 2026 RISCON Co., Ltd. See `LICENSE`.
