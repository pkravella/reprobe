# Security policy

Reprobe is a security *testing* tool. That makes two different kinds of report possible, and they go to different places.

## 1. A vulnerability in Reprobe itself

Report it privately through GitHub's [private vulnerability reporting](https://github.com/pkravella/reprobe/security/advisories/new). Please do not open a public issue for these.

Expect an acknowledgement within 7 days and an assessment within 14. If a fix is warranted, we will agree a disclosure date with you and credit you in the advisory unless you prefer otherwise.

**In scope** — things that would let Reprobe hurt the person running it:

- **Sandbox escape.** Anything that lets a trial reach the host filesystem, the host network, or another trial.
- **Egress containment failure.** Any path by which a trial reaches a real external service rather than the mock gateway.
- **Credential leakage.** Any way a host environment variable, API key, or local file reaches a trial container without the agent adapter's `env_allowlist` declaring it — or reaches a run store, report, or exported test.
- **Code execution in the operator's context.** Adversarial payload text or agent output that executes on the host, for example through report rendering, export templates, or trace parsing.
- **Unescaped payload rendering.** Adversarial text rendered into an HTML report or a generated test file without escaping.

**Out of scope:**

- An agent being susceptible to prompt injection. That is the finding Reprobe exists to produce, not a vulnerability in Reprobe. See section 2.
- Missing hardening in a scenario *fixture*. Fixtures are deliberately plausible-looking small projects; they are not meant to be secure.
- A check producing a false positive or false negative. Please open a normal issue with the trace — that is a correctness bug, handled in public.

## 2. A flaw you found in a third-party agent, using Reprobe

That belongs to the agent's vendor, not to this project. Reprobe findings are a reproduction rate against a specific agent build — useful, but not by themselves a CVE.

A coordinated-disclosure guide ships with v0.1 (`docs/disclosure.md`), covering what to include, who to contact for each supported agent, and a suggested 90-day timeline. Until then, the short version:

1. Report to the agent vendor through their published security contact.
2. Include the shrunk payload, the reproduction rate with its interval, the pinned agent version, model id and container digest, and the scenario.
3. Canaries are synthetic, so a Reprobe payload contains no secret of yours. You may still want to withhold the payload publicly until the vendor has responded.

## Authorised use

Run Reprobe against agents you own or have permission to test. Testing a third-party hosted agent may breach its terms of service regardless of your intent — check before you start, and note that the cost of a search run falls on whoever owns the API key.

Reprobe is built so that a finding cannot be turned into an attack on a real system without deliberate effort: canaries are synthetic, the collector endpoint is a local mock, and nothing in the corpus targets a real service. Please keep it that way in anything you contribute.

## Supported versions

Pre-release. Until v0.1 there is no supported version and no security backport; `main` is the only branch that receives fixes.
