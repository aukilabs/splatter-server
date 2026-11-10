CARGO ?= cargo
PACKAGE := splatter-bin
# Same default as reconstruction's RECONSTRUCTION_VERSION / bin build.rs.
SPLATTER_VERSION ?= 0.0.0-local
export SPLATTER_VERSION

.PHONY: run check fmt clippy test ci

run:
	$(CARGO) run -p $(PACKAGE)

check:
	$(CARGO) check -p $(PACKAGE)

fmt:
	$(CARGO) fmt --all

clippy:
	$(CARGO) clippy -p $(PACKAGE) --all-targets -- -D warnings

test:
	$(CARGO) test -p $(PACKAGE)

ci: fmt clippy test
	@echo "CI checks passed."
