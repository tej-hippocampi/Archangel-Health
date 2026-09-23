"""JSON embedded in an HTML script must remain one script text node."""
import json
from typing import Any


def json_for_html_script(value: Any) -> str:
    """Preserve decoded values while excluding HTML delimiters from JSON text.

    JSON quoting alone does not stop the HTML parser recognizing a closing
    script tag inside a string. ASCII serialization also escapes JS line
    separators without changing their decoded values.
    """
    return (json.dumps(value, ensure_ascii=True)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))
