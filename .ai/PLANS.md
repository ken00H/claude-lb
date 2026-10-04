<!-- AI ENGINEERING PACK: MANAGED FILE -->
# Execution Plans

Create an execution plan for complex features, multi-module refactors, migrations, new integrations,
consequential security work, or tasks requiring several implementation and verification stages. Do
not create one for a trivial edit.

Store active plans in the `plans/` directory beside this file. Use a descriptive filename such as
`plans/add-payment-idempotency.md`.

## Plan requirements

Write enough repository-specific detail for another contributor to continue from the plan. Update
it whenever evidence changes the design, scope, progress, or risk.

Every plan must contain:

```markdown
# [Outcome]

## Goal
[What observable result will exist when this plan is complete?]

## Context
[Current behavior, relevant architecture, and why the change is needed]

## Acceptance outcomes
- [Success behavior]
- [Boundary or rejection behavior]
- [Failure, retry, or recovery behavior]

## Non-goals
- [Explicitly excluded work]

## Affected surfaces
- [Modules and files]
- [Contracts and consumers]
- [Data and migrations]
- [Security, privacy, accessibility, and operations]

## Design
[Smallest complete solution, ownership, data flow, and important decisions]

## Implementation stages
1. [Stage with concrete deliverable]
2. [Stage with concrete deliverable]
3. [Stage with concrete deliverable]

## Verification
- [Focused tests]
- [Broader quality gates]
- [Integration or environment checks]
- [Negative controls and failure cases]

## Rollout and recovery
[Compatibility, migration order, monitoring, rollback or roll-forward]

## Progress
- [ ] [Pending stage]

## Decisions
- [Date] — [Decision and reason]

## Surprises and discoveries
- [Unexpected finding and its effect on design, scope, or risk]

## Evidence
- PENDING: [outcome] — [planned evidence]

## Remaining risk
- [Risk, consequence, and owner]

## Outcomes and retrospective
- [Verified outcome and its evidence]
- [What worked, what to change on the next plan]
```

## Execution rules

- Update progress and decisions as the work changes; do not preserve a plan that is known to be
  false.
- Record unexpected findings under Surprises and discoveries as they occur, including their effect
  on design, scope, or risk.
- Complete Outcomes and retrospective when the work finishes so later plans start from evidence.
- Keep implementation, tests, contracts, and documentation in the same stages where possible.
- Run focused verification during each stage and final verification after the integrated change.
- Do not mark an outcome verified without fresh evidence.
- Delete or archive obsolete plans according to project policy after the work is complete.
