# MetaCrucible

MetaCrucible is a workbench for improving portable agent capabilities through repeatable optimization, evaluation, and review loops.

## Language

**MetaCrucible**:
A workbench for improving portable Skills and subagents across agent runtimes through repeatable optimization, evaluation, and review loops.
_Avoid_: Claude Code optimizer, prompt optimizer, agent framework

**Agent Runtime**:
An environment that can load and execute Skills or subagents.
_Avoid_: Claude Code, platform, client

**Skill**:
A portable capability package that teaches an agent runtime how to perform a bounded task or workflow.
_Avoid_: Claude Code command, prompt snippet, plugin

**Subagent**:
A specialized execution unit delegated by a primary agent, with its own role, instructions, and tool boundary.
_Avoid_: worker, bot, assistant, thread

**Capability Artifact**:
A Skill or subagent source definition that can be evaluated, revised, synthesized, and loaded by an agent runtime.
_Avoid_: prompt, file, config

**Revision**:
A change to an existing capability artifact intended to improve its behavior or fit.
_Avoid_: patch, edit, update

**Synthesis**:
The creation of a new capability artifact from a task description or capability need.
_Avoid_: generation, scaffolding, creation

**Static Review**:
An evaluation of a capability artifact without executing it in an agent runtime.
_Avoid_: lint, rubric, checklist

**Execution Evaluation**:
An evaluation of a capability artifact by running it against tasks in an agent runtime.
_Avoid_: test, benchmark, trial

**Rubric**:
A set of criteria used to judge a capability artifact during static review or execution evaluation.
_Avoid_: checklist, scorecard, grading prompt

**Static Review Profile**:
A versioned set of rule checks and rubric guidance used to perform static review under specific risk or policy conditions.
_Avoid_: lint rule, checklist, quality preset

**Baseline**:
The reference artifact version and evaluation result used to judge whether a revision or synthesis improved capability.
_Avoid_: previous version, control, before state

**Evaluation Case**:
A scenario used to evaluate a capability artifact, including the input, expected behavior, and judgment method.
_Avoid_: task, test case, prompt

**Check**:
A deterministic judgment method for an evaluation case.
_Avoid_: assertion, test, validation

**Judgment**:
A non-deterministic judgment method for an evaluation case that must record supporting evidence.
_Avoid_: opinion, rating, review

**Judge**:
An evaluator that makes a non-deterministic judgment from a rubric and recorded evidence.
_Avoid_: grader, critic, reviewer

**Evidence Bundle**:
The recorded inputs, outputs, checks, judgments, and rationale used to support an evaluation result.
_Avoid_: log, report, transcript

**Receipt**:
The stable entrypoint record for an evidence bundle, binding the run result to artifact, benchmark, envelope, harness, adapter, and model identities.
_Avoid_: summary, report, log header

**Trajectory Digest**:
A bounded, redacted summary of a target's execution path used by judges, reviewers, and optimizers.
_Avoid_: raw transcript, chat log, trace dump

**Acceptance Gate**:
The required set of review, evaluation, and evidence conditions that a capability artifact must satisfy before it is accepted.
_Avoid_: review gate, approval, done

**Runtime Adapter**:
A component that prepares a capability artifact for loading or execution in a specific agent runtime.
_Avoid_: integration, connector, plugin

**Canonical Source**:
The runtime-native source file that MetaCrucible treats as the authoritative capability artifact.
_Avoid_: internal format, normalized schema, generated artifact

**Artifact Envelope**:
Metadata that tells MetaCrucible how to evaluate, revise, or synthesize a capability artifact without replacing its source format.
_Avoid_: manifest, wrapper, schema

**Routing Surface**:
The part of a capability artifact that controls how an agent runtime discovers, selects, or invokes it.
_Avoid_: frontmatter, trigger, metadata

**Mutable Range**:
A portion of a canonical source that MetaCrucible is allowed to revise automatically.
_Avoid_: body, editable section, patch target

**Patch Revision**:
A revision that changes a mutable range through bounded add, delete, or replace edits.
_Avoid_: patch, diff, small edit

**Exploratory Rewrite**:
A human-confirmed revision that rewrites a larger mutable range to escape a local optimum.
_Avoid_: full rewrite, regeneration, overhaul

**Optimization Round**:
One pass of proposing a revision, applying it, evaluating the result, and deciding whether to keep or revert.
_Avoid_: epoch, step, iteration, batch

**Acceptance Decision**:
The decision to keep, reject, revert, or abort a revision after evaluating it against the baseline.
_Avoid_: ratchet, validation gate, outcome

**Revision History**:
The recorded sequence of artifact versions, revisions, evaluations, and acceptance decisions for a capability artifact.
_Avoid_: git history, audit log, changelog

**Generated Evaluation Case**:
An evaluation case proposed by MetaCrucible that cannot be used for optimization until it is reviewed by a human.
_Avoid_: synthetic task, bootstrap case, draft test

**Optimizer**:
The role that proposes revisions to a capability artifact from evaluation evidence.
_Avoid_: editor, trainer, improver

**Target**:
The role that executes a capability artifact during execution evaluation.
_Avoid_: runner, agent, model

**Callable Artifact**:
A capability artifact that can be invoked directly by a target during execution evaluation.
_Avoid_: runnable artifact, executable skill, callable subagent

**Benchmark**:
A reviewed collection of evaluation cases used to define and measure capability quality for a capability artifact.
_Avoid_: test suite, dataset, task list

**Eval Split**:
The portion of a benchmark used to score candidate revisions during optimization.
_Avoid_: dev set, validation set, sel split

**Held-Out Split**:
The portion of a benchmark reserved from optimization, used to detect overfit or regression before acceptance.
_Avoid_: test set, holdout, unseen set

**Stopping Condition**:
A condition that ends optimization before or at the maximum round limit.
_Avoid_: early stop, max rounds, budget limit

**Execution Boundary**:
The permissions and isolation constraints applied while a target executes a callable artifact.
_Avoid_: sandbox, permissions, environment

**Model Provider**:
An external service or local system that supplies model inference within an agent runtime.
_Avoid_: LLM provider, model API, runtime

**Adapter Preflight**:
A runtime adapter check that verifies a capability artifact can be discovered by an agent runtime before execution evaluation begins.
_Avoid_: smoke test, execution evaluation, skill-use check
