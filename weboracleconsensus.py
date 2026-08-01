# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
WebConsensusOracle
===================

A reusable Intelligent Contract *primitive* that resolves short factual
claims ("Is X true?", "What is the current value of Y?") by having every
GenLayer validator independently read the same set of web sources, draft
an answer, and reach consensus only if their independent answers agree
in substance.

Why this is more than a thin LLM wrapper
-----------------------------------------
1. Multi-source aggregation. A claim carries up to MAX_SOURCES URLs; the
   non-deterministic block fetches all of them and asks the model to
   reconcile them, not just parrot a single page.
2. Comparative consensus, not leader-trust. `gl.eq_principle.prompt_comparative`
   re-executes the non-deterministic block on *every* validator and only
   accepts a result the validators substantively agree on. This is
   stronger than asking validators to merely judge the leader's answer
   (`prompt_non_comparative`), which is the right tradeoff for a primitive
   whose whole job is "did independent readers of the same web pages
   reach the same conclusion?".
3. Deterministic post-processing. Once validators agree on a raw JSON
   string, parsing / validation / truncation happens in plain,
   side-effect-free Python (`_normalize_resolution`) *outside* the
   non-deterministic block, so it is unit-testable without ever calling
   an LLM or the network, and it cannot be used to smuggle non-determinism
   into contract state.
4. A dispute lifecycle instead of a single-shot answer. Any account can
   dispute a RESOLVED claim, which reopens it for a fresh consensus
   round from scratch. After CHALLENGE_THRESHOLD disputes the claim is
   FROZEN rather than being re-resolved forever, so a bad-faith actor
   cannot grief the oracle with unlimited disputes.

Reuse pattern
-------------
Other contracts (prediction markets, parametric insurance, DAOs that need
a real-world fact before releasing funds) hold this contract's address,
call `submit_claim` / `resolve_claim`, and read `get_claim(claim_id)`
once `status == RESOLVED`, instead of re-implementing web-consensus
plumbing themselves.

Explicitly out of scope for this primitive (see README "Limitations"):
staking/slashing on disputes, claim expiry timers, and cross-claim
aggregation. Those are policy decisions better left to the contract that
composes this oracle.
"""

from genlayer import *
from dataclasses import dataclass
import json

# --- Tunable constants -----------------------------------------------------

MAX_SOURCES = 5
MAX_EXCERPT_CHARS = 2000
CHALLENGE_THRESHOLD = 2  # disputes allowed before a claim is FROZEN

# --- Claim status enum (plain u8 so it stays cheap + calldata-friendly) ----

STATUS_PENDING = u8(0)   # submitted, never resolved
STATUS_RESOLVED = u8(1)  # validators reached consensus on an answer
STATUS_DISPUTED = u8(2)  # resolved once, challenged, awaiting re-resolution
STATUS_FROZEN = u8(3)    # disputed too many times; needs a new claim instead


@allow_storage
@dataclass
class Claim:
    creator: Address
    question: str
    sources: DynArray[str]
    status: u8
    answer: str
    confidence: str
    rationale: str
    dispute_count: u32


def _normalize_resolution(raw: str) -> dict:
    """Deterministically parse and sanity-check the JSON string that
    validators already reached consensus on.

    This function performs NO I/O and calls NO LLM — it only runs after
    `gl.eq_principle.prompt_comparative` has already finalized `raw`, so
    it is safe (and required) to keep it outside the non-deterministic
    block, and it can be exercised directly in unit tests.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise Exception("oracle: agreed response was not valid JSON")

    if not isinstance(data, dict):
        raise Exception("oracle: agreed response must be a JSON object")

    answer = str(data.get("answer", "")).strip()
    confidence = str(data.get("confidence", "")).strip().lower()
    rationale = str(data.get("rationale", "")).strip()

    if not answer:
        raise Exception("oracle: response is missing a non-empty 'answer'")
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"

    return {
        "answer": answer[:280],
        "confidence": confidence,
        "rationale": rationale[:600],
    }


class WebConsensusOracle(gl.Contract):
    claims: TreeMap[str, Claim]
    claim_count: u256

    def __init__(self):
        self.claims = TreeMap()
        self.claim_count = u256(0)

    # -- Write methods -------------------------------------------------

    @gl.public.write
    def submit_claim(self, question: str, sources: list[str]) -> str:
        """Register a new factual claim and the sources it should be
        resolved from. Returns the new claim_id."""
        question = question.strip()
        if not question:
            raise Exception("question must not be empty")
        if not (1 <= len(sources) <= MAX_SOURCES):
            raise Exception(f"provide between 1 and {MAX_SOURCES} source URLs")

        # DynArray is a storage-only type -- it can't be instantiated
        # directly with DynArray[str](); build a plain list here and let
        # the framework coerce it into storage when the Claim is assigned
        # into `self.claims` below.
        validated_sources = []
        for url in sources:
            url = url.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                raise Exception(f"invalid source URL: {url!r}")
            validated_sources.append(url)

        claim_id = f"claim-{self.claim_count}"
        self.claim_count = self.claim_count + u256(1)

        self.claims[claim_id] = Claim(
            creator=gl.message.sender_address,
            question=question,
            sources=validated_sources,
            status=STATUS_PENDING,
            answer="",
            confidence="",
            rationale="",
            dispute_count=u32(0),
        )
        return claim_id

    @gl.public.write
    def resolve_claim(self, claim_id: str) -> None:
        """Run one consensus round: every validator independently reads
        the claim's sources, drafts an answer, and the network only
        accepts a result the validators substantively agree on."""
        claim = self.claims.get(claim_id)
        if claim is None:
            raise Exception("unknown claim_id")
        if claim.status != STATUS_PENDING and claim.status != STATUS_DISPUTED:
            raise Exception("claim is not awaiting resolution")

        question = claim.question
        sources = [s for s in claim.sources]

        def independent_resolution() -> str:
            # Runs independently, in a sandboxed VM, on every validator.
            # Storage is inaccessible here by design -- only the closed-over
            # `question` / `sources` locals and the network are available.
            excerpts = []
            for url in sources:
                try:
                    page = gl.get_webpage(url, mode="text")
                except Exception:
                    page = "(source unreachable)"
                excerpts.append(f"SOURCE: {url}\n{page[:MAX_EXCERPT_CHARS]}")
            combined = "\n\n---\n\n".join(excerpts)

            prompt = f"""You are a neutral fact-checker. Answer the question
using ONLY the source excerpts below. If the sources disagree or the
answer cannot be determined from them, say so explicitly in the answer
field and set confidence to "low".

QUESTION: {question}

{combined}

Respond with ONLY a JSON object, no other text:
{{"answer": "<short factual answer>", "confidence": "low|medium|high", "rationale": "<1-2 sentences citing which source(s) you relied on>"}}"""

            return gl.nondet.exec_prompt(prompt)

        raw = gl.eq_principle.prompt_comparative(
            independent_resolution,
            principle=(
                "The 'answer' fields must agree in substance (not "
                "necessarily wording), the 'confidence' tiers must match, "
                "and the rationale must point to the same underlying "
                "evidence. Disagreement on the substantive answer or on "
                "the confidence tier means the results are NOT equivalent."
            ),
        )

        parsed = _normalize_resolution(raw)

        claim.answer = parsed["answer"]
        claim.confidence = parsed["confidence"]
        claim.rationale = parsed["rationale"]
        claim.status = STATUS_RESOLVED
        self.claims[claim_id] = claim

    @gl.public.write
    def dispute_claim(self, claim_id: str) -> None:
        """Reopen a RESOLVED claim for another independent consensus
        round. After CHALLENGE_THRESHOLD disputes the claim is frozen
        instead of being re-resolved forever."""
        claim = self.claims.get(claim_id)
        if claim is None:
            raise Exception("unknown claim_id")
        if claim.status != STATUS_RESOLVED:
            raise Exception("only a resolved claim can be disputed")

        claim.dispute_count = claim.dispute_count + u32(1)
        if claim.dispute_count >= u32(CHALLENGE_THRESHOLD):
            claim.status = STATUS_FROZEN
        else:
            claim.status = STATUS_DISPUTED
        self.claims[claim_id] = claim

    # -- Read methods ----------------------------------------------------

    @gl.public.view
    def get_claim(self, claim_id: str) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise Exception("unknown claim_id")
        return claim

    @gl.public.view
    def get_claim_count(self) -> u256:
        return self.claim_count
