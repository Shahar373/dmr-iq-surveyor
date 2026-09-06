"""Export geolocation results to formats other tools open.

KML so the credible regions can be laid over aerial imagery and terrain in
Google Earth -- which is the cheapest way to sanity-check a region against
the ground, since the estimator models neither. GPX so the suggested next
stops load into a phone navigator instead of being copied by hand.

Both are written with the standard library's XML writer: the values include
site notes from an operator-supplied CSV, and escaping them is not optional.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import Any
from xml.dom import minidom

# Semi-transparent blue fills, KML's aabbggrr byte order.
_STYLE_50 = ("region50", "ff9c6f1f", "599c6f1f")
_STYLE_90 = ("region90", "cc9c6f1f", "1e9c6f1f")


def _pretty(root: ElementTree.Element) -> str:
    raw = ElementTree.tostring(root, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ")


def _text(parent: ElementTree.Element, tag: str, value: Any) -> ElementTree.Element:
    node = ElementTree.SubElement(parent, tag)
    node.text = "" if value is None else str(value)
    return node


def _coordinates(ring: list[list[float]]) -> str:
    return " ".join(f"{point[0]:.7f},{point[1]:.7f},0" for point in ring)


def to_kml(collection: dict[str, Any], *, name: str = "P25 site geolocation") -> str:
    """A GeoJSON FeatureCollection from `build_map_geojson` as KML."""
    kml = ElementTree.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ElementTree.SubElement(kml, "Document")
    _text(document, "name", name)
    _text(
        document,
        "description",
        "Credible regions are search areas, not transmitter coordinates. A region marked "
        "unbounded extends past the analysed area; a site marked weak geometry was heard from "
        "one direction only.",
    )

    for style_id, line, fill in (_STYLE_50, _STYLE_90):
        style = ElementTree.SubElement(document, "Style", id=style_id)
        line_style = ElementTree.SubElement(style, "LineStyle")
        _text(line_style, "color", line)
        _text(line_style, "width", 2)
        polygon_style = ElementTree.SubElement(style, "PolyStyle")
        _text(polygon_style, "color", fill)

    folders: dict[str, ElementTree.Element] = {}

    def folder(key: str, label: str) -> ElementTree.Element:
        if key not in folders:
            node = ElementTree.SubElement(document, "Folder")
            _text(node, "name", label)
            folders[key] = node
        return folders[key]

    for feature in collection.get("features", []):
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry")
        kind = properties.get("kind")
        if geometry is None:
            continue

        if kind == "credible_region":
            level = properties.get("credible_level")
            placemark = ElementTree.SubElement(
                folder("regions", "Credible regions"), "Placemark"
            )
            _text(
                placemark,
                "name",
                f"{properties.get('site_key', '?')} — {round((level or 0) * 100)}%",
            )
            _text(
                placemark,
                "description",
                f"status: {properties.get('status', '?')}; "
                f"area: {properties.get('area_km2', 0):.2f} km2; "
                f"reaches the edge of the analysed area: "
                f"{properties.get('touches_analysed_edge')}",
            )
            _text(placemark, "styleUrl", f"#{'region50' if level == 0.5 else 'region90'}")
            multi = ElementTree.SubElement(placemark, "MultiGeometry")
            for polygon in geometry.get("coordinates", []):
                node = ElementTree.SubElement(multi, "Polygon")
                for index, ring in enumerate(polygon):
                    boundary = ElementTree.SubElement(
                        node, "outerBoundaryIs" if index == 0 else "innerBoundaryIs"
                    )
                    linear = ElementTree.SubElement(boundary, "LinearRing")
                    _text(linear, "coordinates", _coordinates(ring))
            continue

        if geometry.get("type") != "Point":
            continue
        longitude, latitude = geometry["coordinates"][0], geometry["coordinates"][1]
        if kind == "estimate":
            placemark = ElementTree.SubElement(folder("estimates", "Best estimates"), "Placemark")
            _text(placemark, "name", str(properties.get("site_key", "?")))
            _text(
                placemark,
                "description",
                f"status: {properties.get('status')}; "
                f"{properties.get('detection_count')} detection(s), "
                f"{properties.get('non_detection_count')} non-detection(s); "
                f"90% area: {properties.get('area_km2_90')} km2. "
                "This is the most probable cell, not a confirmed position.",
            )
        elif kind == "measurement":
            placemark = ElementTree.SubElement(folder("stops", "Measurements"), "Placemark")
            _text(
                placemark,
                "name",
                f"{properties.get('site_key', '?')} "
                f"{'heard' if properties.get('detected') else 'not heard'}",
            )
            _text(
                placemark,
                "description",
                f"run: {properties.get('survey_run_id')}; "
                f"level: {properties.get('level_db')}; "
                f"usability: {properties.get('usability')}",
            )
        elif kind == "plan_stop":
            placemark = ElementTree.SubElement(folder("plan", "Suggested next stops"), "Placemark")
            _text(placemark, "name", f"Suggested stop {properties.get('rank')}")
            _text(
                placemark,
                "description",
                "value: "
                f"{properties.get('value')}; helps most: "
                + ", ".join(
                    item.get("site_key", "?") for item in properties.get("helps_most") or []
                ),
            )
        else:
            continue
        point = ElementTree.SubElement(placemark, "Point")
        _text(point, "coordinates", f"{longitude:.7f},{latitude:.7f},0")

    return _pretty(kml)


def to_gpx(
    plan: dict[str, Any], *, visited: list[dict[str, Any]] | None = None, name: str = "P25 survey"
) -> str:
    """Suggested next stops (and, optionally, stops already made) as GPX waypoints."""
    gpx = ElementTree.Element(
        "gpx",
        version="1.1",
        creator="dmr-iq-surveyor",
        xmlns="http://www.topografix.com/GPX/1/1",
    )
    metadata = ElementTree.SubElement(gpx, "metadata")
    _text(metadata, "name", name)

    for rank, stop in enumerate(plan.get("top_stops", []), start=1):
        waypoint = ElementTree.SubElement(
            gpx,
            "wpt",
            lat=f"{stop['latitude']:.6f}",
            lon=f"{stop['longitude']:.6f}",
        )
        _text(waypoint, "name", f"Next stop {rank}")
        _text(
            waypoint,
            "desc",
            f"value {stop['value']:.2f}; helps most: "
            + ", ".join(item.get("site_key", "?") for item in stop.get("helps_most") or []),
        )
        _text(waypoint, "sym", "Flag, Blue")

    for entry in visited or []:
        if entry.get("latitude") is None:
            continue
        waypoint = ElementTree.SubElement(
            gpx,
            "wpt",
            lat=f"{float(entry['latitude']):.6f}",
            lon=f"{float(entry['longitude']):.6f}",
        )
        _text(waypoint, "name", str(entry.get("survey_run_id", "stop")))
        _text(waypoint, "desc", "stop already recorded")
        _text(waypoint, "sym", "Waypoint")

    return _pretty(gpx)


__all__ = ["to_gpx", "to_kml"]
