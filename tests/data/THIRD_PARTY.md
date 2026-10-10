# Third-party material in tests/data

Small, trimmed samples of two public attack corpora, kept so the seed importers
(R14) are tested against what those projects really emit rather than against a
guess. Each sample is unmodified apart from trimming: fewer tests or files, and
AgentDojo's `messages` transcripts emptied. Both projects are MIT-licensed; their
notices follow.

| File | Upstream | Commit |
| --- | --- | --- |
| `promptfoo-sample.yaml` | [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) `examples/redteam-minimal/redteam.yaml`, `politics` tests only | `6117b52b092882bde2258a9b91ff4a41e09ae778` |
| `promptfoo-mcp-sample.yaml` | [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) `examples/redteam-mcp-agent/redteam.yaml`, first three tests | `6f88eb4c0f560246a28c31786c325ad75c943581` |
| `agentdojo-sample/*.json` | [ethz-spylab/agentdojo](https://github.com/ethz-spylab/agentdojo) `runs/Meta-SecAlign-70B-repeat_user_prompt/banking/...` | `9272bf1e0c1c441600264324f481b97fb54db60b` |

The AgentDojo samples name an account number from AgentDojo's own simulated bank;
nothing here names a real collection endpoint (CONTRIBUTING.md, "The one hard rule").

## Promptfoo

```
Copyright (c) Promptfoo 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## AgentDojo

```
MIT License

Copyright (c) 2024 Edoardo Debenedetti, Jie Zhang, Mislav Balunovic, Luca Beurer-Kellner, Marc Fischer, and Florian Tramèr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
