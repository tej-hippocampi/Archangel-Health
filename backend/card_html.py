"""Safe presentation copies of generated cards; accepted source HTML is immutable.

nh3 uses an HTML5 parser for the security boundary. tinycss2 parses CSS rather
than matching spellings, so escaped URL/function names cannot bypass the filter.
Existing class-based card styles and teach-back anchor IDs remain usable.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import nh3
import tinycss2


def _card_id(value: str) -> str:
    # Arbitrary IDs must not become window/document named properties. The
    # teach-back service already produces namespaced IDs consumed by the UI.
    if value.startswith(("care-card-", "tb-anchor-")):
        return value
    return "care-card-" + value


_VALUE_FUNCTIONS = {
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix",
    "calc", "min", "max", "clamp", "var", "env", "round", "mod", "rem", "abs", "sign",
    "minmax", "repeat", "fit-content", "linear-gradient", "radial-gradient", "conic-gradient",
    "repeating-linear-gradient", "repeating-radial-gradient", "repeating-conic-gradient",
    "translate", "translatex", "translatey", "translatez", "translate3d", "scale", "scalex",
    "scaley", "scalez", "scale3d", "rotate", "rotatex", "rotatey", "rotatez", "rotate3d",
    "skew", "skewx", "skewy", "matrix", "matrix3d", "perspective", "cubic-bezier", "steps",
    "blur", "brightness", "contrast", "drop-shadow", "grayscale", "hue-rotate", "invert",
    "opacity", "saturate", "sepia", "counter", "counters", "attr", "rect", "inset", "circle",
    "ellipse", "polygon", "path",
}
_SELECTOR_FUNCTIONS = {"is", "not", "where", "has", "nth-child", "nth-last-child",
                       "nth-of-type", "nth-last-of-type", "lang", "dir", "selector"}


def _safe_css_tokens(tokens, *, selectors: bool = False) -> bool:
    for token in tokens:
        if token.type in {"url", "bad-url", "error", "at-keyword"}:
            return False
        if token.type == "function":
            if token.lower_name not in _VALUE_FUNCTIONS | (_SELECTOR_FUNCTIONS if selectors else set()):
                return False
            if not _safe_css_tokens(token.arguments, selectors=selectors):
                return False
        if hasattr(token, "content") and not _safe_css_tokens(token.content, selectors=selectors):
            return False
    return True


def _declarations(value) -> str:
    kept = []
    for declaration in tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True):
        if declaration.type != "declaration":
            continue
        # CSS cannot run script in modern browsers, but legacy bindings and
        # network-bearing values are unnecessary for our self-contained cards.
        if declaration.lower_name in {"behavior", "-moz-binding"}:
            continue
        if not _safe_css_tokens(declaration.value):
            continue
        kept.append(declaration)
    return tinycss2.serialize(kept)


def _namespace_selectors(tokens) -> None:
    for token in tokens:
        if token.type == "hash" and token.is_identifier:
            token.value = _card_id(token.value)
        if token.type == "function":
            _namespace_selectors(token.arguments)
        if hasattr(token, "content"):
            _namespace_selectors(token.content)


def _stylesheet(value: str) -> str:
    kept = []
    for rule in tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True):
        if rule.type == "qualified-rule" and _safe_css_tokens(rule.prelude, selectors=True):
            _namespace_selectors(rule.prelude)
            declarations = _declarations(rule.content)
            if declarations:
                kept.append(tinycss2.serialize(rule.prelude) + "{" + declarations + "}")
        elif (rule.type == "at-rule" and rule.lower_at_keyword in {"media", "supports"}
              and rule.content is not None and _safe_css_tokens(rule.prelude, selectors=True)):
            # Responsive layouts are useful; imports, fonts and other rules
            # that fetch content have no place in a generated medical card.
            nested = _stylesheet(tinycss2.serialize(rule.content))
            if nested:
                kept.append("@" + rule.lower_at_keyword + " " + tinycss2.serialize(rule.prelude)
                            + "{" + nested + "}")
    # CSS escapes may decode to an HTML closing tag during serialization.
    # Keep all less-than characters CSS-escaped inside the raw-text style tag.
    return "".join(kept).replace("<", "\\3c ")


def _attribute(tag: str, name: str, value: str) -> str | None:
    if name == "style":
        return _declarations(value) or None
    if name == "id":
        return _card_id(value)
    if name == "href" and value.startswith("#"):
        return "#" + _card_id(value[1:])
    if name == "target" and value not in {"_blank", "_self"}:
        return None
    # Static SVG presentation attributes can also contain CSS url() values.
    if name in {"fill", "stroke"} and not _safe_css_tokens(tinycss2.parse_component_value_list(value)):
        return None
    return value


_SVG = {"svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "title", "desc"}
_ATTRIBUTES = {
    "*": {"class", "id", "style", "title", "lang", "dir", "role", "aria-label", "aria-hidden"},
    "a": {"href", "target"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "time": {"datetime"},
}
for _tag in _SVG:
    _ATTRIBUTES[_tag] = {"viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2",
                         "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "stroke",
                         "stroke-width", "stroke-linecap", "stroke-linejoin", "fill-rule",
                         "clip-rule", "opacity", "fill-opacity", "stroke-opacity", "transform"}

_CLEANER = nh3.Cleaner(
    tags=(nh3.ALLOWED_TAGS - {"img", "map", "area"}) | {"style", "section"} | _SVG,
    clean_content_tags={"script", "iframe", "object", "embed", "template"},
    attributes=_ATTRIBUTES,
    attribute_filter=_attribute,
    url_schemes={"http", "https", "mailto", "tel"},
    link_rel="noopener noreferrer",
)


class _Styles(HTMLParser):
    """Rewrite only style text from nh3's canonical output, then sanitize again."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.style: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        self.output.append(self.get_starttag_text())
        if tag == "style":
            self.style = []

    def handle_startendtag(self, tag, attrs):
        self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == "style" and self.style is not None:
            self.output.append(_stylesheet("".join(self.style)))
            self.style = None
        self.output.append("</" + tag + ">")

    def handle_data(self, data):
        (self.style if self.style is not None else self.output).append(data)

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")


def sanitize_card_html(value: str | None) -> str:
    html = (value or "").strip()
    if html.startswith("```"):
        html = html.split("\n", 1)[1] if "\n" in html else html[3:]
        if html.endswith("```"):
            html = html[:-3].strip()
    parser = _Styles()
    parser.feed(_CLEANER.clean(html))
    parser.close()
    # The final HTML5 parse keeps CSS rewriting from becoming a new markup
    # insertion boundary (including malformed HTML/SVG namespace transitions).
    return _CLEANER.clean("".join(parser.output))


def card_presentation(value: Any) -> Any:
    """Copy JSON payloads while sanitizing only their explicit HTML card fields."""
    if isinstance(value, dict):
        return {key: sanitize_card_html(item) if key in {"battlecard_html", "battlecard"}
                and isinstance(item, (str, type(None))) else card_presentation(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [card_presentation(item) for item in value]
    return value
