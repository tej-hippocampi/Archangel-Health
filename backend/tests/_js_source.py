"""Reading JS source in assertions, without the two traps.

Nine test files here grep the shipped portal JS, because this codebase explains
its rules in prose beside the code that follows them and those renderers have no
DOM harness. Grepping source is fine; grepping it NAIVELY is not, and it fails in
both directions:

  * a comment that names a control we deleted satisfies a "it is gone" grep, and
    a comment that quotes a call satisfies an "it is called" grep — so the prose
    explaining a change can make the test for that change vacuous;
  * a stripper that treats `//` as a line comment without tracking string
    literals eats the rest of any line containing a URL. `'https://calendly.com/…'`
    becomes `'https:`, which silently defeats every assertion about hrefs — and
    one such assertion is live in test_applicant_home_screen.py.

`strip_js_comments` below handles string literals, so both hold.
"""

from __future__ import annotations


def strip_js_comments(source: str) -> str:
    """JS with comments removed and string literals preserved intact."""
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        elif source[i] in "\"'`":
            # A string literal. Copied whole, so a URL's // survives and a
            # comment marker inside quotes is not mistaken for one.
            quote, j = source[i], i + 1
            while j < n and source[j] != quote:
                j += 2 if source[j] == "\\" else 1
            out.append(source[i:j + 1])
            i = j + 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)
