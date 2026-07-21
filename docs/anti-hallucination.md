# Reducing hallucination with multiple AIs (the honest version)

> The uncomfortable starting point, from a reader named **lasxt1995**:
> *"One AI hallucinates; five AIs also hallucinate."*

Running the same question past several models and going with the majority feels
rigorous. It usually isn't. Models share training data and failure modes, so
they can be **confidently wrong together**. A debate between them often just
surfaces which model lost the thread first — not what is true.

This document is the protocol `ai-orchestra` is built around. It came out of a
[public thread](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj) where
dozens of practitioners pushed back on "just make them debate 5 rounds." Their
combined answer: hallucination drops from **grounding, proof, a cheap
adversary, and replay** — not from more rounds of agreement.

---

## The one rule

**A claim with no source, no `file:line`, and no runnable proof is unverified —
no matter how many models agree with it, and no matter how plausible it sounds.**

Everything below is machinery for enforcing that rule cheaply.

---

## The four levers that actually work

### 1. Ground every claim in a real source
Pin the model to specific inputs and demand a citation for each assertion.
- *"Require it to cite the source for every sentence."* — **paul.chen.pwc**
- *"Give it definite data sources; don't let it answer from training data."* — **lin081626**

A common, invisible hallucination: the model **never actually ran the web
search or opened the file**, and answered from memory instead. Check that the
retrieval step really happened before you trust what depends on it.

### 2. Demand proof-of-work, not assertions
- *"Zero-Trust governance: anything a model claims about itself must produce
  proof-of-work, and pass tests plus QC by another agent."* — **quant_david** (the
  most-upvoted reply in the thread)

"I updated the config" is a claim. The diff, the passing test, the command
output is proof. Ask for the proof; if it can't be produced, the work didn't
happen.

### 3. One good adversary beats five agreeable rounds
- *"One writes, one specifically finds fault. Writer → adversarial reviewer,
  one round is enough — far fewer tokens than five rounds of debate. Every
  conclusion must attach a source or actually run a command; no evidence →
  redo. Cuts hallucination in half."* — **kanisleo328**
- *"I keep only two rounds for decision-changing questions: one to find
  counterexamples, one to verify. Otherwise lock it with tests and sources, so
  I don't spend 5× the tokens for the same conclusion."* — **ovveai_api**

The point of a second model is **disagreement**, not a second vote. Give it the
opposing brief: *find the flaw, refute this, name the evidence that would make
it false.* That is what `verify.py` does.

### 4. Verify completion by replay, not by claim
- *"How do you confirm the agent actually finished, instead of just saying it
  did? A CLI-level replay gate — if the replay doesn't pass, the goal doesn't
  pass."* — **pukpuklouis** (referencing a replay-gate approach)

For anything with side effects, re-run / re-read the result and confirm the
end state. "Done" is a claim (lever 2); the re-read is the proof.

### Bonus lever: first-principles re-derivation
- *"Have the agent re-think from first principles, or periodically re-check its
  own reasoning."* — **solitude6060**

When a conclusion matters, make one pass that ignores the prior reasoning and
rebuilds it from the ground facts. If the two derivations disagree, you found a
hallucination.

---

## What does *not* reduce hallucination

- **Adding more models that agree.** Redundant voices with the same blind spot
  inflate confidence without adding evidence. `verify.py` explicitly refuses to
  treat unanimous-but-unsourced agreement as verification — even all-support
  returns a non-zero "UNVERIFIED — consensus is not proof" exit code, never a
  pass, so a script can't mistake agreement for a green light.
- **More debate rounds.** Past ~1–2 focused rounds you are usually paying tokens
  to relitigate the same context, not to find new counterevidence.
- **Over-orchestration.** As **harry58892** put it: fix the prompt where the
  model actually fails; a clear spec and one capable model often beats a
  hierarchy of agents. Reach for multiple AIs when a task genuinely benefits
  from an independent adversary — not by default.

---

## The protocol, step by step

1. **Write the spec, not just the ask.** Ambiguity is the cheapest source of
   "hallucination". State the inputs, the definition of done, and the acceptance
   check up front.
2. **Do the work with your primary model** (the coordinator — Claude Code in the
   default config), and require it to attach evidence inline: `file:line`,
   command output, source URLs.
3. **Cross-examine with one independent adversary.** Pipe the answer through
   `verify.py --critics <a different provider>`. The critic's job is to refute
   and to name the single piece of evidence that would settle it.
4. **Resolve on evidence, not on vote.** If the critic refutes or finds the
   claim unsupported → fix or attach proof, then re-verify. If it "supports" but
   only by agreeing → still unverified; go confirm the named evidence yourself.
5. **Replay side effects.** Re-read files, re-run commands, confirm the end
   state matches the spec.
6. **Meter it.** Every dispatch is logged (`data/ledger.jsonl`) so you can see
   whether the extra rounds are buying accuracy or just burning tokens.

---

## How the tools map to the protocol

| Protocol step | Tool |
|---|---|
| Independent, read-only reviewer that obeys your repo's `AGENTS.md` | `dispatch.py <claude> --claude-profile review` |
| One-adversary cross-check with an evidence demand + honest "consensus ≠ truth" verdict | `verify.py --critics <provider>` |
| Route grunt work to a cheap/bulk provider to save your scarce coordinator budget | `dispatch.py <openai-compatible provider>` |
| See whether extra verification is worth the spend | `usage_report.py` |

The design bias throughout: **honest > complete.** If a number, a source, or a
completion can't be verified, the tools say "unknown" rather than fill in a
confident-looking blank. That same discipline is what keeps hallucinations out.

---

## Credits

This protocol is a synthesis of a generous public discussion. Thanks to
**quant_david, lasxt1995, kanisleo328, ovveai_api, paul.chen.pwc, lin081626
(CloverAI-Family), pukpuklouis, solitude6060, harry58892, mat.vmk3s_,
jackyyyso**, and everyone else who replied. Related work they shared:

- [CloverAI-Family/agent-guardrails](https://github.com/CloverAI-Family/agent-guardrails) — governance framework for multi-agent teams
- [drpwchen/textbook-to-note](https://github.com/drpwchen/textbook-to-note) — fully-cited, source-grounded notes
- [solitude6060/Yao-skills](https://github.com/solitude6060/Yao-skills) — a first-principles skill
