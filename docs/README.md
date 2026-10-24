# Documentation index

This directory holds the product's written authority. Each document has exactly one job, so a
change has an obvious home and reviewers know which file to trust.

| Document | Owns | Changes |
|---|---|---|
| [prd.md](./prd.md) | What the product is and must do: vision, scope, functional requirements, acceptance criteria | Rare, versioned |
| [architecture.md](./architecture.md) | How it is built: components, contracts, schema, failure handling | With design changes |
| `configuration.md` | **Planned for v0.5** (created when the schema ships). The operator's reference for every configuration key | Whenever the schema changes |
| [runbook.md](./runbook.md) | How to operate a running instance: start, stop, status, recovery, retention workflow | With operational changes |
| [decisions.md](./decisions.md) | Append-only log of product and technical decisions (D-nnn) | Append only |
| [roadmap.md](./roadmap.md) | The release view: what shipped, what is next, in one screen | Per release |
| [iterations/](./iterations/) | One file per delivery iteration: scope, increments, acceptance criteria, exit state | Written once, then immutable |
| [sources.md](./sources.md) | Discovery research: what each candidate employer exposes | Archive, non-normative |
| [source-access.md](./source-access.md) | Access/terms review evidence gathered before the blanket posture (D-066) | Archive, non-normative |

## Reading order by question

- *"What is this product meant to do?"* → `prd.md`
- *"How do I configure my own scope?"* → `configuration.md` (from v0.5), then `runbook.md`
- *"How is it built / why this design?"* → `architecture.md`, then `decisions.md`
- *"What are we building next?"* → `roadmap.md`, then the current file in `iterations/`
- *"Is it running, and how do I stop or fix it?"* → `runbook.md`
- *"Why was this decision made?"* → `decisions.md` (search the D-number cited in code comments)

## Conventions

- **Iterations are the unit of delivery.** Each is `iterations/vX.Y-kebab-slug.md` with a status
  header (`Draft`, `Approved`, `Delivered`). Once delivered it is not rewritten; corrections go
  into a new iteration or the decision log. This keeps history honest and the roadmap short.
- **The roadmap stays one screen.** Detail belongs in the iteration file it links to.
- **Decisions are numbered, never deleted.** A reversal is a new row that supersedes the old one.
- **Archives are labelled.** Research files marked non-normative are evidence, not requirements.
- **Every doc names its authority.** No document silently overrides another; the PRD wins on
  product questions, the decision log wins on resolved trade-offs, the runbook wins on operations.

## Adding a new document

Only add one when an existing document would otherwise own two unrelated jobs. A new file costs a
reviewer a click and a maintainer a stale-reference risk, so prefer extending the file that already
owns the topic.
