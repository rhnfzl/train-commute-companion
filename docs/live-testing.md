# Live NS checks

Run the current companion against real NS journeys without sending phone notifications. These tests use a temporary profile and store, so they do not change your installed commute.

Supply `NS_API_KEY` securely through the environment, then run:

```bash
.venv/bin/python -m pytest tests_live -q
```

The tests use Utrecht Centraal to Rotterdam Centraal and Amersfoort Centraal to Utrecht Centraal. Each runs with the accessible-trip filter enabled and disabled. It applies a weekday departure override and explicitly selects a catchable near-future direct train. It checks refreshed journey health, crowd fields, optional vehicle context and saved state. Alerts stay in a local capture object. Background vehicle requests finish or are cancelled before the HTTP client closes.

The tests make real authenticated NS requests and consume API quota. They require an eligible direct train within forty minutes. A failure can mean current service conditions, network access or credentials need investigation; it does not automatically identify a code defect.

The default offline suites exclude `tests_live`. Running that directory without a key skips its tests.

These checks verify live data through the core runtime. They do not verify phone display, sound, GPS accuracy or behaviour over a complete physical journey.

## Dependency-floor checks

The documented Python 3.12 floor command disables the package cache. Local source edits can otherwise reuse an older built wheel. When checking a prebuilt wheel, compare its Python files with the checkout before accepting the result.
