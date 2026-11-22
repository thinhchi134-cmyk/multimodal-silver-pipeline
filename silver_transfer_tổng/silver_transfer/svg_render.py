"""Render a self-contained SVG into a bounded PNG preview."""

from defusedxml import ElementTree
import re
from xml.parsers.expat import errors

SVG_LIMIT = 30 * 1024 * 1024


def load_svg(source):
    with source.open("rb") as stream:
        data = stream.read(SVG_LIMIT + 1)
    if len(data) > SVG_LIMIT:
        raise ValueError("SVG exceeds 30 MiB preview limit")
    try:
        return ElementTree.fromstring(data), False
    except ElementTree.ParseError as error:
        if error.code != errors.codes[errors.XML_ERROR_NO_ELEMENTS] or not data.rstrip().endswith(b">"):
            raise
        try:
            root = ElementTree.fromstring(data + b"\n</svg>\n")
        except ElementTree.ParseError:
            raise error from None
        if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            raise
        return root, True


def render_png(root, destination, max_side):
    from resvg_py import svg_to_bytes

    for node in root.iter():
        for name, value in node.attrib.items():
            value = value.strip()
            if name.rsplit("}", 1)[-1] == "href" and value and not value.startswith(("#", "data:")):
                raise ValueError("SVG has an external resource reference")
    markup = ElementTree.tostring(root, encoding="unicode")
    urls = re.findall(r"url\(\s*['\"]?([^)'\"]+)", markup, re.IGNORECASE)
    if "@import" in markup.lower() or any(not url.strip().startswith(("#", "data:")) for url in urls):
        raise ValueError("SVG has an external stylesheet or resource reference")
    destination.write_bytes(svg_to_bytes(svg_string=markup, width=max_side, height=max_side))
