# SkillScope system contract

SkillScope supports review of a bounded repair to a reusable natural-language
agent skill when executions can be replayed or sandboxed. It does not certify an
open-world agent, infer the owner's policy, or treat a model explanation as an
execution trace.

## System thesis and unit of generality

The system reconciles three deltas that must not be collapsed:

- the **intended delta** is the owner's versioned behavioral scope;
- the **artifact delta** is the exact skill revision and its provenance;
- the **enacted delta** is the observed difference between complete original
  and candidate executions on matched worlds.

The reusable unit is not a travel or expense prompt. It is a domain adapter
that supplies typed tools, resettable worlds, observable fact extraction, and
case fixtures to a domain-independent review lifecycle. A deployment may bind
that adapter to replay logs, a service sandbox, a transaction simulator, or the
packaged deterministic tool worlds used in the study.

## Architecture

```text
workspace or chat UI
        |
review API + versioned task record store
        |--------------------|
candidate/model adapter      execution harness
                             |-- reset world clone
                             |-- dispatch typed tool calls
                             |-- record args/results/order
                             |-- derive observable facts
                             `-- apply hidden or owner criteria
```

The two interfaces call the same review and execution functions. The model can
request a tool but cannot fabricate its return, mutate the frozen fixture, read
an oracle, modify the owner's scope, or authorize a release. State-changing
tools execute against isolated sandbox state with production-equivalent results.

## The closed loop

0. **Assignment envelope.** Before either interface can start an execution, the
   owner receives the same product-like review work order: role, neutral incident
   intake, review objective, terminal decision, timebox, and sandbox boundary.
   The public object is produced by an allowlist and versioned with a task hash;
   it never contains cases, reference instructions, or oracles. Accepting it
   freezes participant, condition, period, scenario, and start time in the skill
   record.
1. **Incident and baseline.** Import a concrete breakdown and clone its task,
   tool schemas, initial state, fixed clock, model configuration, and skill hash.
   Only after the baseline batch completes, a domain adapter may derive a
   participant-facing conflict from the modal run's observable facts and frozen
   world. The snapshot cannot carry a pre-authored incident answer.
2. **Behavioral scope.** Before candidate behavior is visible, the owner records
   a motivating Change in their own words. An intent planner separates its
   trigger, required action, forbidden action, and open ambiguities, then ranks
   only prevalidated executable case ids. Every selected case states which part
   of the rule it probes; the owner records case-grounded commitments as Change,
   Preserve, or Unresolved. A fourth “excluded from this repair” choice is
   non-normative triage: it removes the case from drafting but retains it as a
   monitored release check. The incident cannot be excluded. The plan and every
   edit are versioned and hashed.
   The model-produced interpretation is non-normative planning metadata: the
   candidate manifest links only its plan id/hash and receives the owner's exact
   commitments as the sole source of behavioral intent.
3. **Source cue.** After scope completion, the service automatically selects
   one existing instruction and one executed case. It runs deletion and minimal
   inversion three times each against the original skill. The default product
   card presents the exact source text, a plain-language reason for suggesting
   it, and the limitation that the complete candidate still requires validation.
   A disclosure labeled “查看系统如何判断” contains the two internal checks,
   changed fields, stability, hashes, and run IDs. This is a non-blocking cue:
   it is saved into candidate provenance and remains available to revisit, but
   the owner is never asked to approve a diagnostic location as business policy.
   Candidate generation is authorized by the already committed behavioral scope.
4. **Artifact delta.** A candidate is committed as exact text. Its input manifest
   records the scope version, visible cases, generator-withheld cases, selected
   source evidence, model configuration, author, and hashes. If the manifest
   contains a Change commitment, a byte-equivalent candidate is invalid; the
   generator receives a validation error and may retry. If repeated full-list
   attempts remain no-op, a narrow model call must replace one existing,
   source-located instruction using only the same owner-confirmed manifest; it
   cannot read the reference skill or hidden cases. Only a changed,
   structurally valid full instruction list can enter review, and the recovery
   path is recorded in `generation_validation`.
5. **Enacted delta.** Original and candidate skills execute with identical tool
   schemas and isolated clones of the same world. Tool calls are dispatched by
   the harness; observable facts come from the trace and world end state rather
   than model self-report.
6. **Release review.** Fixed criteria and owner judgments classify each
   commitment. Change and Preserve can align or mismatch. An Unresolved or
   Excluded case that changes becomes Needs judgment and is neither success nor
   failure.
   The interface separates this judgment from execution insufficiency and
   technical failure: each warning is explicitly acknowledged with an owner
   rationale, while additional runs are offered only for insufficient evidence.
7. **Decision and reuse.** Release, revise, gather evidence, or defer creates a
   reconstruction record. A successful release returns a visible versioned
   receipt. Candidate-only revision archives the previous artifact and comparison
   while retaining the locked scope. A post-reveal scope change archives the old
   round, explicitly marks the new one post-reveal, and requires a saved scope
   edit before another preview. Released Change and Preserve cases become
   regression assets for the next version.
8. **Post-decision measurement.** The system freezes the exact released or
   deferred artifact before presenting two new prediction cases. The owner makes
   three observable-fact predictions per case and reports confidence before
   either case runs. After submission, both prediction cases and the fully blind
   research holdout execute out of view;
   answers and oracle results are stored only in the researcher export.

The interface presents enacted evidence in progressive disclosure: a compact,
domain-adapted summary of observable facts is primary; the model's longer
narrative is secondary and collapsed by default. During matched review, original
and candidate facts are aligned by semantic field and changed fields are marked
without implying that every change is good or bad. Markdown-like emphasis is
rendered with a deliberately small DOM-based formatter, so model text is never
inserted as executable HTML.

The visible workspace follows the same evidence hierarchy as the review model:
one repair round is the top-level activity, while incident and neighbouring cases
are evidence within that round rather than peer task tabs. Opening a case reveals
its read-only representative execution without navigating away from scope or
matched review. The internal snapshots and runs remain independent and fully
auditable despite this presentation-level grouping.

## Evidence types

The implementation keeps these records separate:

- `M0`: source-skill interventions that locate behaviorally sensitive regions;
- `A`: the confirmed candidate-input manifest and exposure boundary;
- `Sp`: the exact candidate artifact and diff rationale;
- `Ep`: matched whole-candidate executions on reviewed cases;
- `Mp`: optional candidate-block interventions requested after a mismatch;
- `R`: the owner's release decision, rationale, waivers, and evidence hashes.

Cross-highlighting may connect these records for navigation but does not turn an
author rationale or source mask into causal validation of a candidate block.

## Domain-adapter contract

An adapter must provide the following before its results can be treated as
matched execution evidence:

1. JSON-schema tool definitions with stable semantic names;
2. a dispatcher that validates arguments and returns typed observations;
3. an independently cloned initial world and fixed clock for every run;
4. observable fact extraction from tool trace plus terminal state;
5. an optional issue adapter that derives conflict evidence from a completed run
   without reading hidden assertions;
6. content hashes for case, world, tool schema, and pack;
7. case-level criteria or an explicit no-oracle designation;
8. a side-effect policy that executes writes in an isolated sandbox rather than
   against a production account.

Unsupported open-world services remain outside the evaluated scope instead of
being silently mocked during a release comparison.

## Executable scenario packs

A study scenario is a domain adapter, not an answer embedded in the interface.
Each pack contains:

- a plausible faulty skill and a reference skill used only for calibration;
- typed tool schemas and deterministic, stateful sandbox handlers;
- a fixed clock and resettable initial world for every case;
- an incident, an owner-visible preservation case, a generator-withheld
  preservation case, and an owner-visible unresolved boundary;
- two post-decision prediction holdouts and a fully blind research holdout, none
  of which enters drafting or in-product release evidence;
- deterministic assertions over tool traces and world end states;
- provenance and content hashes.

The product displays the incident as history and neighboring cases as release
checks. Its problem card is produced only after execution from the selected
action, timestamps, policy facts, and other observable fields. Suggested repair
principles are optional owner-editable choices, never prefilled commitments.
The intent planner may select and explain only case ids from this frozen bank;
it cannot synthesize a tool return, inspect an oracle, or decide an open policy.
For formal tasks it retains all three calibrated product cases in both
conditions while recording intent-specific relevance explanations. Product-like
demo tasks may omit a weakly related case, and the selected plan is included in
the candidate manifest and release evidence.
Reference skills, oracle definitions, and research holdouts remain on the
researcher side.

## Information and authority boundaries

- The owner can change scope; the candidate generator cannot.
- The generator can draft text; it cannot release a version.
- A generator-withheld case can test transfer relative to the generator, not the
  owner's ability to generalize to an unseen case.
- A research holdout is separate and never participates in drafting or in-product
  release evidence during the measured task.
- A prediction holdout stays hidden until the owner has made a terminal decision;
  only its input is then shown, and its execution is delayed until after answers
  are committed.
- Any post-reveal scope change creates a new auditable review round. Inherited
  judgments are marked post-reveal and no new candidate can be produced until
  the owner saves a scope edit and a fresh source cue is generated and recorded.

## Review state machine

`Incident -> Scoped -> Source cue recorded -> Candidate committed ->
Matched review -> Release | Revise candidate | Reopen
scope | Gather evidence | Defer`

- A scope edit invalidates any stale candidate-input manifest and source cue.
- Once a candidate exists, criteria are locked for that review round.
- Direct editing cannot bypass scope. The source cue is generated and recorded
  automatically, without becoming a user-confirmation gate. An owner
  edit changes candidate authorship, clears AI rationale, and updates
  case exposure rather than pretending that the original generator stayed
  blind.
- A release with mismatch, insufficient evidence, or Needs judgment requires a
  recorded rationale. Released Change and Preserve rows become regression
  assets; Unresolved and Excluded rows never become automatic assertions.

## Mapping to the formative-study design goals

| Design goal | Implemented mechanism | Observable evidence |
|---|---|---|
| DG1: connect skill text to execution | raw typed tool trace, fixed repeated runs, one automatic source delete/invert preview | exact intervention, artifact/world hashes, behavior distribution |
| DG2: make repair scope explicit and revisable | case-grounded Change/Preserve/Unresolved records, Excluded triage, and immutable review rounds | judgment/reveal timing, criteria, visible/withheld/excluded manifest |
| DG3: support evidence-grounded review while preserving authority | exact candidate diff, full-candidate matched runs, four release exits | run IDs, mismatch/Needs judgment, owner reason, release hash |

The mapping is many-to-many in the product: a panel supports a coherent repair
activity rather than presenting one experimental control per design goal.

## Formal-study invariants

Both UI conditions share the exact model, candidate-drafting and tool-execution
prompts, scenario pack, tool runtime, case exposure, criteria, run budget, and
oracle; condition-specific navigation and routing prompts implement the UI
manipulation. In chat, commitments are still
confirmed before candidate reveal, but remain in the conversation instead of a
persistent structured workspace. Primary outcomes come from traces, world
states, scope records, regressions, and release decisions. Questionnaires are
used for a pre-execution behavior prediction plus secondary measures of
understanding, workload, evidence sufficiency, and perceived control. Review
generation and tool-using execution temperatures are separately recorded and
default to zero for the frozen study build.

Formal scenario packs also freeze the source-preview instruction, case,
question, and minimal inversion. This prevents participant-to-participant
planner variation from changing M0 while preserving the same automatic product
flow and the same participant-facing location summary in both interfaces. The
model-bounded selector remains available for demo
or imported skills, where it can choose only existing instructions and executed
case IDs.

Source access is held constant without copying the treatment UI: chat renders
the complete released and candidate skills as expandable transcript items and
offers the released version in an on-demand read-only drawer. The normal chat
path is state driven: one owner statement records the incident target and
creates a shared intent-to-case scope plan before automatically running
neighbouring cases; one consolidated reply confirms or
   overrides their proposed scope; the service then runs and presents the same
   one-instruction source cue used by the workspace. Once the owner has committed
   all scope decisions, the service freezes the manifest, drafts, and runs
   matched comparisons without requiring an extra approval of the cue. Tool progress is narrated, but internal
capability names are not presented as commands. Chat does not add a persistent
skill/scope sidebar, clickable case switcher, structured diff, or side-by-side
behavioral outcome. Demo reset exists in both interfaces; formal mode suppresses
it so an assigned task cannot erase its own record.

The participant-facing work order is also invariant. `study=1` URLs preassign a
scenario, derive condition from the interface path, and remove mode switching
and reset controls. The intended manipulation begins after assignment: the
workspace may persist scope, diffs, evidence, and release state, while chat may
only narrate and invoke the same operations conversationally. Both conditions
   automatically run the same four product cases, automatic source-cue budget (two
   interventions × three runs), and matched execution budget (three runs per
   artifact and case; five only after explicit Gather Evidence). Automatic chat
orchestration changes input mechanics, not the evidence set or release
authority: only owner-authored utterances become commitments, and publishing
still requires an explicit owner decision. An initial assistant greeting is not
treated as the participant's task.
Formal recovery requires an exact participant × condition × period × scenario ×
task-hash match. Both clients lock on a mismatch, and the server rejects a
second formal load into an occupied process.

The official study launcher binds each participant × period task to one process
and one stable archive filename. Every completed state mutation writes an atomic
`skillscope/2` checkpoint; questionnaire submission writes the same record with
`archive.stage=completed` before acknowledging completion. Relaunching the exact
assignment restores that checkpoint before serving either interface. Demo tasks
remain in memory and do not write participant records.
