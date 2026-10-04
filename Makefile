# Sandbox images. `make images` builds all four.
#
# Versions default to `latest` so a fresh build picks up the current agent CLI.
# Pin them for a reproducible build: `make images CLAUDE_CODE_VERSION=2.1.289`.
IMAGES := base claude-code codex-cli mockgw fakeagent
CLAUDE_CODE_VERSION ?= latest
CODEX_VERSION ?= latest

.PHONY: images $(IMAGES) digests
images: $(IMAGES)

base:
	docker build -t reprobe/base:dev images/base

claude-code: base
	docker build --build-arg CLAUDE_CODE_VERSION=$(CLAUDE_CODE_VERSION) \
		-t reprobe/claude-code:dev images/claude-code

codex-cli: base
	docker build --build-arg CODEX_VERSION=$(CODEX_VERSION) \
		-t reprobe/codex-cli:dev images/codex-cli

mockgw:
	docker build -t reprobe/mockgw:dev images/mockgw

fakeagent: base
	docker build -t reprobe/fakeagent:dev images/fakeagent

# The image id every exported finding is pinned to (R11).
#
# `.Id` is the content-addressable digest of the local image and is always
# present. `.RepoDigests` is NOT: it is populated only for images that have been
# pushed to or pulled from a registry, so indexing into it blindly aborts with
# "index out of range" on exactly the locally built images this target is for.
digests:
	@for i in $(IMAGES); do \
		printf '%-24s %s\n' "reprobe/$$i:dev" \
			"$$(docker image inspect reprobe/$$i:dev --format '{{.Id}}' 2>/dev/null \
				|| echo 'not built')"; \
	done
