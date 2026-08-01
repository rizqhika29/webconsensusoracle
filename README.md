# WebConsensusOracle

A reusable GenLayer Intelligent Contract primitive: it resolves a short
factual claim ("Is X true?", "What is Y right now?") by having **every
validator independently** read the same set of web sources, draft an
answer, and reach consensus only if their independent answers agree in
substance. It ships with a dispute lifecycle so a bad resolution can be
challenged instead of being final forever.

Other contracts (prediction markets, parametric insurance, DAOs gating a
payout on a real-world fact) can hold this contract's address and read
`get_claim(claim_id)` once `status == RESOLVED`, instead of re-implementing
web-consensus plumbing themselves.

## Why this is a primitive, not a demo

| Concern | How it's handled |
|---|---|
| Non-determinism | Web fetches + the LLM call live inside one closure (`independent_resolution`), which is the only place `gl.get_webpage` / `gl.nondet.exec_prompt` are allowed to run. |
| Consensus mechanism | `gl.eq_principle.prompt_comparative` — every validator re-executes the closure from scratch and independent answers must agree in substance. This is deliberately stronger than `prompt_non_comparative`, where only the leader runs the block and others merely judge it; a "did independent readers agree" oracle should make every reader actually read. |
| Multi-source input | A claim carries 1–5 source URLs; the model is asked to reconcile them and to say so explicitly when they disagree. |
| Deterministic validation | `_normalize_resolution` parses/validates/truncates the agreed JSON string in plain Python, after consensus, with zero I/O — so it's independently unit-testable and can't become a side channel for extra non-determinism. |
| Bad resolutions | `dispute_claim` reopens a `RESOLVED` claim for a fresh round. After `CHALLENGE_THRESHOLD` (default 2) disputes, the claim is `FROZEN` rather than looping forever, so disputing can't be used to grief the oracle indefinitely. |

## State design

```
Claim
  creator:        Address       # who submitted the claim
  question:       str
  sources:        DynArray[str] # 1..MAX_SOURCES URLs
  status:         u8            # PENDING=0, RESOLVED=1, DISPUTED=2, FROZEN=3
  answer:         str           # set once RESOLVED
  confidence:      str          # "low" | "medium" | "high"
  rationale:      str
  dispute_count:  u32

WebConsensusOracle
  claims:       TreeMap[str, Claim]  # keyed by "claim-<n>"
  claim_count:  u256
```

## Claim lifecycle

```
submit_claim
     │
     ▼
  PENDING ──resolve_claim──► RESOLVED ──dispute_claim──► DISPUTED
                                 ▲                            │
                                 │                     resolve_claim
                                 └────────────────────────────┘
                                            (dispute_count < THRESHOLD)

  RESOLVED ──dispute_claim (dispute_count reaches THRESHOLD)──► FROZEN
```

A `FROZEN` claim is intentionally a dead end for this primitive — the
composing contract decides what "frozen" means for its use case (e.g.
fall back to a different resolution path, refund users, or require a new
claim with different sources).

## Public interface

- `submit_claim(question: str, sources: list[str]) -> str` — returns the new `claim_id`.
- `resolve_claim(claim_id: str) -> None` — runs one consensus round.
- `dispute_claim(claim_id: str) -> None` — reopens or freezes a resolved claim.
- `get_claim(claim_id: str) -> Claim` — view.
- `get_claim_count() -> u256` — view.

## Example usage

```
claim_id = oracle.submit_claim(
    "Did Team A win their match on 2026-07-28?",
    ["https://example-sports-results.com/2026-07-28"],
)
oracle.resolve_claim(claim_id)
claim = oracle.get_claim(claim_id)
# claim.status == RESOLVED, claim.answer / claim.confidence / claim.rationale populated
```

## Testing

- `test/test_normalize_resolution.py` — pure-Python unit tests of the
  deterministic parsing step. No Studio, no network, no LLM required:
  `pytest test/test_normalize_resolution.py`
- `test/test_web_consensus_oracle_integration.py` — full lifecycle tests
  (submit → resolve → dispute → re-resolve → freeze) against a running
  GenLayer Studio / local validator set, using the `gltest` pattern:
  `gltest test/test_web_consensus_oracle_integration.py`

## Known limitations (by design)

- No staking/slashing on disputes — anyone can call `dispute_claim`. If
  your use case needs Sybil resistance on disputes, add a stake
  requirement in the composing contract.
- No expiry/timeout on `PENDING` claims — resolution must be triggered
  explicitly by a `resolve_claim` call.
- `FROZEN` is a terminal state for this primitive; escalation policy
  (governance vote, refund, etc.) is left to the caller.
- Source pages are truncated to `MAX_EXCERPT_CHARS` (2000) characters
  each before being sent to the model, to keep prompts bounded.

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
in its `Depends` header, matching current GenLayer documentation examples.
Update this hash if you're targeting a different SDK version.

## Deployed instance (GenLayer Studio)

- Contract address: `0x75c93AE8c511Bad2e0A4FCfBCE387ec1F0dE9E4b`
- Explorer: https://explorer-studio.genlayer.com/tx/0xa137a08072546aee310158f15f8a2d65280f370d4e034c53a73d4405057aa266

Manually exercised end-to-end on this deployment: `submit_claim` →
`resolve_claim` (reached RESOLVED via `prompt_comparative` consensus) →
`dispute_claim` (RESOLVED → DISPUTED) → `resolve_claim` again (back to
RESOLVED) → `dispute_claim` again (DISPUTED → FROZEN at
`dispute_count == CHALLENGE_THRESHOLD`), plus rejection checks for
malformed source URLs, too many sources, and re-resolving/disputing a
claim in the wrong status.
