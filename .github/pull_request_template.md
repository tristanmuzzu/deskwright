## What this changes

<!-- One paragraph. What was wrong or missing, and what it does now. -->

## How it was proven

<!--
CONTRIBUTING.md asks for evidence, not assurance. Delete what does not apply.
-->

- [ ] `ruff check .` clean
- [ ] `python3 -m pytest -q` — headless suite green
- [ ] Live suites run on a real session (say which, and paste the tally):
- [ ] `DESKWRIGHT_SESSION=headless deskwright --self-test` — N/N
- [ ] Extension changed: syntax checked with
      `deskwright/extension/deskwright@zeticle.com/check-syntax.sh`, and I logged
      out and back in before testing

## Anything a reviewer should be suspicious of

<!-- Measurements that came from one machine, a threshold you guessed at, a
     path you could not exercise. Say it here rather than let it be found. -->
