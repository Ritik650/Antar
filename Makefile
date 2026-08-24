# Antar - see tasks.py for the implementation of every target.
#
# There is deliberately no logic here. `make` is unavailable on some of the
# machines this repo has to build on, so the real runner is tasks.py and this
# file is a thin alias layer for CI and Linux. ADR-0003.

PY ?= python

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test coverage check gate simulate \
        calibration-report bakeoff evaluate figures register console api \
        seed-test-mode demo clean freeze

help:
	@$(PY) tasks.py help

install:
	@$(PY) tasks.py install

lint:
	@$(PY) tasks.py lint

format:
	@$(PY) tasks.py format

typecheck:
	@$(PY) tasks.py typecheck

test:
	@$(PY) tasks.py test $(if $(K),K=$(K),) $(if $(M),M=$(M),)

coverage:
	@$(PY) tasks.py coverage

check:
	@$(PY) tasks.py check

# The single pre-commit / pre-push gate. CI runs this exact target; see
# .github/workflows/ci.yml. A local pass is a convenience, CI is the verdict.
gate:
	@$(PY) tasks.py gate

simulate:
	@$(PY) tasks.py simulate $(if $(SCENARIO),SCENARIO=$(SCENARIO),) $(if $(SEED),SEED=$(SEED),) $(if $(POLICY),POLICY=$(POLICY),)

calibration-report:
	@$(PY) tasks.py calibration-report $(if $(SCENARIO),SCENARIO=$(SCENARIO),)

bakeoff:
	@$(PY) tasks.py bakeoff $(if $(SCENARIO),SCENARIO=$(SCENARIO),)

evaluate:
	@$(PY) tasks.py evaluate $(if $(QUICK),QUICK=$(QUICK),) $(if $(SCENARIOS),SCENARIOS=$(SCENARIOS),)

figures:
	@$(PY) tasks.py figures

register:
	@$(PY) tasks.py register

console:
	@$(PY) tasks.py console

api:
	@$(PY) tasks.py api

seed-test-mode:
	@$(PY) tasks.py seed-test-mode

demo:
	@$(PY) tasks.py demo

clean:
	@$(PY) tasks.py clean

freeze:
	@$(PY) tasks.py freeze
