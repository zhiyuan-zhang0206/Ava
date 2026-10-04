# Feynman, "Cargo Cult Science" — Reference Material

**Source**: Richard P. Feynman, commencement address, Caltech, June 1974. Collected in *Surely You're Joking, Mr. Feynman!* (1985) and *The Pleasure of Finding Things Out* (1999). Not a book — a ~15-minute speech; included here because it is the sharpest statement of research honesty in the canon.
**Role in this skill**: the canonical source for `principles/honesty` — the anti-self-deception attitude that underlies every anti-cherry-picking and anti-overfitting check.

## Core content

- **The first principle is that you must not fool yourself — and you are the easiest person to fool.** The entire speech is an unpacking of this line.
- **Cargo-cult science**: research that has the *form* of science (experiments, statistics, publication) but none of the integrity — like the Melanesian cargo cults that built runways and control towers in the hope of summoning airplanes, without understanding what makes airplanes land. Feynman's examples: advertising-inspired psychology studies with rigged methodology, and the rat-running experiment where the experimenter's expectations contaminated the results (a classic experimenter-bias case).
- **Utter honesty in reporting**: report everything that might invalidate your result — not just what supports it. The scientist of integrity "states the evidence" for the opposite conclusion with the same care. Details that could invalidate the result must be disclosed, not buried.
- **A field without this integrity is a cargo cult**: if you cannot state how a result could be wrong, you do not have science.
- **Breadth vs depth of honesty**: "It's a kind of scientific integrity, a principle of scientific thought that corresponds to a kind of utter honesty — a kind of leaning over backwards." Leaning over backwards = actively trying to prove yourself wrong, not merely not cheating.

## Why it matters for ML research

- All of the skill's anti-gaming checks (sealing the test set, pre-registration, reporting dropped runs, adversarial self-review) are operationalizations of "do not fool yourself."
- The experimenter-bias example maps directly to the automation-p-hacking failure mode in `ai-era/ai-failure-modes`: when the agent chooses what to report, the reward structure tempts it to fool itself — the fix is structural (presenter ≠ decider), exactly as Feynman's "leaning over backwards" demands.
- Negative results and invalidating details must be reported with the same care as successes (`practices/present`: show the process, not just the result).

## Key quotes (verified)

- "The first principle is that you must not fool yourself — and you are the easiest person to fool."
- "Science is the belief in the ignorance of experts." (from a 1981 BBC interview; often misattributed to this speech — kept out of the skill body, noted here to prevent misquotation)

## Checklist candidates

- [ ] MUST the report discloses details that could invalidate the result, with the same care as supporting evidence
- [ ] MUST the researcher/agent actively attempts to prove the result wrong (leaning over backwards) before believing it
- [ ] SHOULD experimenter-expectation contamination is considered as a threat to any measurement involving judgment
