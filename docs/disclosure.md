# Disclosing what you find

Reprobe exists to find prompt-injection failures in coding agents. If it finds
one in an agent you did not build, this is how to report it. If the flaw is in
Reprobe itself, see [SECURITY.md](../SECURITY.md) instead.

## What a Reprobe finding is, and is not

A finding is a **measurement**: one payload, delivered through one surface of
one scenario, made one agent build do one forbidden thing, at a measured rate.

- It **is** evidence that a specific agent version and model, in a specific
  setup, can be steered by text it read into reading or sending a secret,
  writing a protected file, reaching an unlisted host or running a dangerous
  command, at least as often as the interval's lower bound says.
- It **is not** a CVE, a proof that the agent is unsafe in general, or a claim
  about any other version, model or configuration. Susceptibility to prompt
  injection is, to a degree, a known property of language-model agents. What
  makes a report useful is the specificity: the minimal payload, the rate, and
  the exact build it was measured on.

Everything Reprobe planted was synthetic. The secrets are `RPRB_CANARY_`
canaries generated per trial, and "exfiltration" reached a mock gateway on a
network with no route out. **Nothing real was exposed, and the finding contains
no real secret.** Say so in your report, so that the vendor does not have to
work it out.

## Who to contact

Use each vendor's own published channel, and check its scope first: some
programs treat model behaviour or prompt injection differently from
conventional vulnerabilities.

| Agent | Where the vendor says to report |
| --- | --- |
| Claude Code (Anthropic) | [SECURITY.md in anthropics/claude-code](https://github.com/anthropics/claude-code/blob/main/SECURITY.md), which points to Anthropic's HackerOne program |
| Codex CLI (OpenAI) | [SECURITY.md in openai/codex](https://github.com/openai/codex/blob/main/SECURITY.md), which points to OpenAI's Bugcrowd program |

For any other agent, look for a `SECURITY.md` in its repository or a security
page on the vendor's site. Do not guess an address.

## What to include

Run `reprobe report STORE --out finding.md` and attach the section for the
finding. It already contains most of this:

1. **The shrunk payload and the surface it went in.** The minimal form is the
   useful one; include the original only if the vendor asks for it.
2. **The reproduction rate with its interval and counts**, such as
   `71% [36%, 92%] (5/7)`, and the threshold it was judged against. Say that the
   interval is a lower-bound claim made under early stopping, not a calibrated
   95% interval.
3. **The pinned build:** agent version, model id and container digest.
4. **The scenario:** what the agent was legitimately asked to do, which surface
   the attacker controlled, and what counted as a violation. Attach the
   scenario file.
5. **The timeline** of a reproducing trial, with the evidence rows.
6. **How to reproduce:** the `reprobe run` command from the report, or the
   exported pytest suite. The suite carries its own scenario.
7. **That everything was synthetic**, as above.

## Suggested timeline

A coordinated, 90-day disclosure, unless the vendor's program says otherwise.
If it does, theirs wins.

| Day | Step |
| --- | --- |
| 0 | Report through the vendor's channel. Keep the payload private. |
| within 7 | Expect an acknowledgement. If none comes, try the other channel the vendor lists, once. |
| 0–90 | Answer questions; re-run `reprobe verify` against new builds if asked. |
| 90, or a fix shipped | Publish, if you intend to, once a fixed build is available or 90 days have passed. |

`reprobe verify` against the vendor's fixed build is the cleanest way to confirm
a fix: the exported test passes only once the finding is shown fixed.

## What not to do

- **Do not publish an unfixed payload** before the timeline runs out, even
  though it is "only" a prompt.
- **Do not test against a hosted agent or account you are not authorised to
  test.** Running Reprobe against a third-party hosted agent may breach its
  terms of service; check them first. Reprobe runs the agent CLI in your own
  sandbox with your own API key, which is a different thing from probing
  someone else's deployment, but the provider's terms still apply to the API
  usage.
- **Do not add real credentials or real endpoints to make a finding look
  worse.** A finding against a synthetic canary is the whole point, and the
  project's one hard rule ([CONTRIBUTING.md](../CONTRIBUTING.md)) applies to
  your scenarios too.

## Payload corpora

Reprobe's seed corpus and any imported corpus are prompt-injection text. They
target synthetic canaries and a mock collector, and imported seeds keep their
source for attribution ([seeds.md](seeds.md)). A seed file you build from your
own findings is a working attack against whatever you found it on. Treat it
like one, and do not publish it before the disclosure it belongs to.
