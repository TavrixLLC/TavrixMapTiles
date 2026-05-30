from __future__ import annotations

import gzip
import math
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from tile_validation import validate_tile_coordinates, TileCoordinateValidationError


TILE_SIZE = 256
MAX_RASTER_ZOOM = 18
RASTER_FORMATS = {
    "png": ("PNG", "image/png"),
    "webp": ("WEBP", "image/webp"),
    "jpg": ("JPEG", "image/jpeg"),
    "jpeg": ("JPEG", "image/jpeg"),
}


class RasterTileError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


THEMES = {
    "light": {
        "background": (247, 245, 239, 255),
        "landuse": (207, 222, 190, 255),
        "park": (185, 216, 168, 255),
        "water": (132, 191, 218, 255),
        "building": (184, 170, 161, 210),
        "boundary": (104, 115, 122, 180),
        "road_casing": (255, 253, 247, 255),
        "road_motorway": (224, 95, 63, 255),
        "road_trunk": (233, 137, 50, 255),
        "road_primary": (236, 189, 66, 255),
        "road_secondary": (213, 196, 83, 255),
        "road_local": (218, 198, 89, 210),
        "text": (38, 50, 58, 255),
        "text_halo": (247, 245, 239, 230),
        "poi": (93, 127, 191, 230),
    },
    "dark": {
        "background": (16, 20, 24, 255),
        "landuse": (35, 61, 52, 255),
        "park": (35, 61, 52, 255),
        "water": (22, 52, 68, 255),
        "building": (51, 64, 74, 230),
        "boundary": (135, 145, 154, 150),
        "road_casing": (12, 15, 18, 255),
        "road_motorway": (225, 125, 87, 255),
        "road_trunk": (216, 148, 79, 255),
        "road_primary": (208, 177, 91, 255),
        "road_secondary": (157, 158, 120, 255),
        "road_local": (88, 96, 104, 210),
        "text": (216, 222, 228, 255),
        "text_halo": (16, 20, 24, 235),
        "poi": (143, 179, 255, 230),
    },
}


def tile_center_lonlat(z: int, x: int, y: int) -> tuple[float, float]:
    scale = 2**z
    lon = ((x + 0.5) / scale) * 360.0 - 180.0
    n = math.pi * (1 - 2 * ((y + 0.5) / scale))
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def tile_bounds_lonlat(z: int, x: int, y: int) -> list[float]:
    scale = 2**z
    west = (x / scale) * 360.0 - 180.0
    east = ((x + 1) / scale) * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y / scale)))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ((y + 1) / scale)))))
    return [west, south, east, north]


def validate_tile(z: int, x: int, y: int) -> None:
    validate_tile_coordinates(z, x, y, MAX_RASTER_ZOOM)


def normalize_raster_format(value: str | None) -> str:
    raster_format = (value or "png").lower().strip().lstrip(".")
    if raster_format not in RASTER_FORMATS:
        supported = ", ".join(sorted(RASTER_FORMATS))
        raise RasterTileError(f"Unsupported raster format '{value}'. Supported formats: {supported}")
    return raster_format


def raster_content_type(image_format: str) -> str:
    return RASTER_FORMATS[normalize_raster_format(image_format)][1]


def raster_tilejson(
    region: str,
    style_id: str,
    tile_url: str,
    manifest: dict[str, Any],
    image_format: str = "png",
) -> dict[str, Any]:
    image_format = normalize_raster_format(image_format)
    tilesets = manifest.get("tilesets", {})
    bounds = None
    for tileset in tilesets.values():
        if isinstance(tileset.get("bounds"), list):
            bounds = tileset["bounds"]
            break
    if not bounds:
        bounds = manifest.get("bounds") or [-180.0, -85.05112878, 180.0, 85.05112878]
    return {
        "tilejson": "3.0.0",
        "name": f"Tavrix raster {region}",
        "scheme": "xyz",
        "tiles": [tile_url],
        "minzoom": 0,
        "maxzoom": MAX_RASTER_ZOOM,
        "bounds": bounds,
        "center": manifest.get("center"),
        "attribution": "Rendered locally from Tavrix PMTiles",
        "format": image_format,
        "region": region,
        "style": style_id,
    }


def render_raster_tile(
    manifest: dict[str, Any],
    output_dir: Path,
    z: int,
    x: int,
    y: int,
    style_id: str = "light",
    image_format: str = "png",
) -> bytes:
    validate_tile(z, x, y)
    image_format = normalize_raster_format(image_format)
    theme = _theme_for(style_id)

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RasterTileError("Raster rendering requires Pillow in the map-api image", 501) from exc

    image = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), theme["background"])
    draw = ImageDraw.Draw(image, "RGBA")

    tilesets = manifest.get("tilesets", {})
    if z <= 5 and "global" in tilesets:
        _render_tileset(draw, tilesets["global"], output_dir, z, x, y, z, x, y, theme, "global")
    elif "basemap" in tilesets:
        maxzoom = int(tilesets["basemap"].get("maxzoom", 14))
        source_z = min(z, maxzoom)
        source_x = x >> max(0, z - source_z)
        source_y = y >> max(0, z - source_z)
        _render_tileset(draw, tilesets["basemap"], output_dir, source_z, source_x, source_y, z, x, y, theme, "basemap")
    elif "global" in tilesets:
        maxzoom = int(tilesets["global"].get("maxzoom", 5))
        source_z = min(z, maxzoom)
        source_x = x >> max(0, z - source_z)
        source_y = y >> max(0, z - source_z)
        _render_tileset(draw, tilesets["global"], output_dir, source_z, source_x, source_y, z, x, y, theme, "global")
    else:
        raise RasterTileError("Manifest does not contain a renderable tileset", 404)

    if "pois" in tilesets and z >= int(tilesets["pois"].get("minzoom", 10)):
        maxzoom = int(tilesets["pois"].get("maxzoom", 16))
        source_z = min(z, maxzoom)
        source_x = x >> max(0, z - source_z)
        source_y = y >> max(0, z - source_z)
        _render_tileset(draw, tilesets["pois"], output_dir, source_z, source_x, source_y, z, x, y, theme, "pois")

    return _image_bytes(image, image_format)


def _theme_for(style_id: str) -> dict[str, tuple[int, int, int, int]]:
    return THEMES["dark"] if "dark" in style_id else THEMES["light"]


def _tileset_path(tileset: dict[str, Any], output_dir: Path) -> Path:
    key = tileset.get("key")
    if key:
        return output_dir / key
    local_path = tileset.get("local_path") or tileset.get("path")
    if local_path:
        return Path(local_path)
    raise RasterTileError("Tileset has no local key/path", 404)


def _render_tileset(
    draw,
    tileset: dict[str, Any],
    output_dir: Path,
    source_z: int,
    source_x: int,
    source_y: int,
    target_z: int,
    target_x: int,
    target_y: int,
    theme: dict[str, tuple[int, int, int, int]],
    kind: str,
) -> None:
    tile = _read_vector_tile(_tileset_path(tileset, output_dir), source_z, source_x, source_y)
    if not tile:
        return
    transformer = _Transformer(source_z, source_x, source_y, target_z, target_x, target_y)

    if kind == "global":
        _draw_polygons(draw, tile.get("countries", []), transformer, (238, 240, 232, 255))
        _draw_polygons(draw, tile.get("water", []), transformer, theme["water"])
        _draw_roads(draw, tile.get("major_roads", []), transformer, theme, target_z)
        _draw_lines(draw, tile.get("country_boundaries", []), transformer, theme["boundary"], 1.0)
        _draw_place_labels(draw, tile.get("major_cities", []), transformer, theme, target_z)
        return

    if kind == "pois":
        _draw_points(draw, tile.get("pois", []), transformer, theme["poi"], 3.5)
        return

    _draw_landuse(draw, tile.get("landuse", []), transformer, theme)
    _draw_polygons(draw, tile.get("water", []), transformer, theme["water"])
    _draw_lines(draw, tile.get("boundaries", []), transformer, theme["boundary"], 1.0)
    _draw_roads(draw, tile.get("roads", []), transformer, theme, target_z)
    if target_z >= 14:
        _draw_polygons(draw, tile.get("buildings", []), transformer, theme["building"])
    _draw_place_labels(draw, tile.get("places", []), transformer, theme, target_z)


def _draw_landuse(draw, features: list[dict], transformer: "_Transformer", theme: dict) -> None:
    for feature in features:
        cls = feature["properties"].get("landuse_class")
        color = theme["park"] if cls in {"park", "garden", "grassland", "wood", "scrub"} else theme["landuse"]
        _draw_feature_polygons(draw, feature, transformer, color)


def _draw_polygons(draw, features: list[dict], transformer: "_Transformer", color: tuple[int, int, int, int]) -> None:
    for feature in features:
        _draw_feature_polygons(draw, feature, transformer, color)


def _draw_feature_polygons(draw, feature: dict, transformer: "_Transformer", color: tuple[int, int, int, int]) -> None:
    for path in feature["geometry"]:
        points = transformer.path(path)
        if len(points) >= 3 and _visible(points):
            draw.polygon(points, fill=color)


def _draw_lines(
    draw,
    features: list[dict],
    transformer: "_Transformer",
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    for feature in features:
        for path in feature["geometry"]:
            points = transformer.path(path)
            if len(points) >= 2 and _visible(points):
                draw.line(points, fill=color, width=max(1, int(round(width))), joint="curve")


def _draw_roads(draw, features: list[dict], transformer: "_Transformer", theme: dict, z: int) -> None:
    order = [
        "service",
        "residential",
        "unclassified",
        "living_street",
        "tertiary",
        "secondary",
        "primary",
        "trunk",
        "motorway",
    ]
    for road_class in order:
        selected = [f for f in features if f["properties"].get("road_class") == road_class]
        width = _road_width(road_class, z)
        _draw_lines(draw, selected, transformer, theme["road_casing"], width + 2.0)
        _draw_lines(draw, selected, transformer, _road_color(theme, road_class), width)


def _draw_points(
    draw,
    features: list[dict],
    transformer: "_Transformer",
    color: tuple[int, int, int, int],
    radius: float,
) -> None:
    for feature in features:
        for path in feature["geometry"]:
            for coord in path:
                px, py = transformer.point(coord)
                if -8 <= px <= TILE_SIZE + 8 and -8 <= py <= TILE_SIZE + 8:
                    r = radius
                    draw.ellipse((px - r, py - r, px + r, py + r), fill=color)


def _draw_place_labels(draw, features: list[dict], transformer: "_Transformer", theme: dict, z: int) -> None:
    if z < 7:
        return
    font = _font(15 if z >= 12 else 12)
    for feature in sorted(features, key=lambda item: int(item["properties"].get("rank") or 9)):
        rank = int(feature["properties"].get("rank") or 9)
        if rank > (2 if z < 10 else 4 if z < 13 else 6):
            continue
        name = feature["properties"].get("name")
        if not name:
            continue
        for path in feature["geometry"]:
            if not path:
                continue
            px, py = transformer.point(path[0])
            if 0 <= px <= TILE_SIZE and 0 <= py <= TILE_SIZE:
                _halo_text(draw, (px, py), str(name), font, theme["text"], theme["text_halo"])


def _halo_text(draw, xy, text: str, font, fill, halo) -> None:
    x, y = xy
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((x + dx, y + dy), text, font=font, fill=halo, anchor="mm")
    draw.text((x, y), text, font=font, fill=fill, anchor="mm")


@lru_cache(maxsize=8)
def _font(size: int):
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _road_width(road_class: str, z: int) -> float:
    base = {
        "motorway": 1.4,
        "trunk": 1.3,
        "primary": 1.1,
        "secondary": 0.9,
        "tertiary": 0.75,
        "residential": 0.45,
        "unclassified": 0.45,
        "living_street": 0.42,
        "service": 0.35,
    }.get(road_class, 0.4)
    return max(1.0, base * (1.0 + max(0, z - 6) * 0.75))


def _road_color(theme: dict, road_class: str) -> tuple[int, int, int, int]:
    if road_class == "motorway":
        return theme["road_motorway"]
    if road_class == "trunk":
        return theme["road_trunk"]
    if road_class == "primary":
        return theme["road_primary"]
    if road_class == "secondary":
        return theme["road_secondary"]
    return theme["road_local"]


def _visible(points: list[tuple[float, float]]) -> bool:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(xs) >= -8 and min(xs) <= TILE_SIZE + 8 and max(ys) >= -8 and min(ys) <= TILE_SIZE + 8


def _image_bytes(image, image_format: str) -> bytes:
    import io

    pil_format, _content_type = RASTER_FORMATS[normalize_raster_format(image_format)]
    output = io.BytesIO()
    if pil_format == "JPEG":
        image = image.convert("RGB")
        image.save(output, format=pil_format, quality=88, optimize=True)
    elif pil_format == "WEBP":
        image.save(output, format=pil_format, quality=84, method=4)
    else:
        image.save(output, format=pil_format, optimize=True)
    return output.getvalue()


class _Transformer:
    def __init__(
        self,
        source_z: int,
        source_x: int,
        source_y: int,
        target_z: int,
        target_x: int,
        target_y: int,
    ) -> None:
        self.scale = 2 ** max(0, target_z - source_z)
        self.offset_x = target_x - (source_x * self.scale)
        self.offset_y = target_y - (source_y * self.scale)

    def point(self, coord: tuple[int, int], extent: int = 4096) -> tuple[float, float]:
        x, y = coord
        px = ((x / extent) * self.scale - self.offset_x) * TILE_SIZE
        py = ((y / extent) * self.scale - self.offset_y) * TILE_SIZE
        return px, py

    def path(self, path: list[tuple[int, int]], extent: int = 4096) -> list[tuple[float, float]]:
        return [self.point(coord, extent) for coord in path]


@lru_cache(maxsize=512)
def _read_vector_tile(pmtiles_path: Path, z: int, x: int, y: int) -> dict[str, list[dict]]:
    if not pmtiles_path.exists():
        raise RasterTileError(f"PMTiles file not found: {pmtiles_path}", 404)
    result = subprocess.run(
        ["pmtiles", "tile", str(pmtiles_path), str(z), str(x), str(y)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0 or not result.stdout:
        return {}
    payload = gzip.decompress(result.stdout) if result.stdout[:2] == b"\x1f\x8b" else result.stdout
    return _decode_mvt(payload)


def _decode_mvt(data: bytes) -> dict[str, list[dict]]:
    layers = {}
    for field, wire, value in _fields(data):
        if field == 3 and wire == 2:
            layer = _decode_layer(value)
            if layer:
                layers[layer["name"]] = layer["features"]
    return layers


def _decode_layer(data: bytes) -> dict[str, Any] | None:
    name = None
    keys: list[str] = []
    values: list[Any] = []
    features_raw: list[bytes] = []
    extent = 4096
    for field, wire, value in _fields(data):
        if field == 1 and wire == 2:
            name = value.decode("utf-8", errors="replace")
        elif field == 2 and wire == 2:
            features_raw.append(value)
        elif field == 3 and wire == 2:
            keys.append(value.decode("utf-8", errors="replace"))
        elif field == 4 and wire == 2:
            values.append(_decode_value(value))
        elif field == 5:
            extent = int(value)
    if not name:
        return None
    features = [_decode_feature(raw, keys, values, extent) for raw in features_raw]
    return {"name": name, "features": [feature for feature in features if feature is not None]}


def _decode_value(data: bytes) -> Any:
    for field, wire, value in _fields(data):
        if field == 1 and wire == 2:
            return value.decode("utf-8", errors="replace")
        if field in (4, 5) and wire == 0:
            return int(value)
        if field == 6 and wire == 0:
            return _zigzag(value)
        if field == 7 and wire == 0:
            return bool(value)
        if field == 2 and wire == 5:
            return value
        if field == 3 and wire == 1:
            return value
    return None


def _decode_feature(data: bytes, keys: list[str], values: list[Any], extent: int) -> dict | None:
    tags: list[int] = []
    geom_type = 0
    geometry_cmds: list[int] = []
    for field, wire, value in _fields(data):
        if field == 2 and wire == 2:
            tags = _packed_varints(value)
        elif field == 3:
            geom_type = int(value)
        elif field == 4 and wire == 2:
            geometry_cmds = _packed_varints(value)
    if not geometry_cmds:
        return None
    props = {}
    for i in range(0, len(tags) - 1, 2):
        key_index, value_index = tags[i], tags[i + 1]
        if key_index < len(keys) and value_index < len(values):
            props[keys[key_index]] = values[value_index]
    return {
        "type": geom_type,
        "properties": props,
        "extent": extent,
        "geometry": _decode_geometry(geometry_cmds),
    }


def _decode_geometry(cmds: list[int]) -> list[list[tuple[int, int]]]:
    paths: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    while i < len(cmds):
        command_integer = cmds[i]
        i += 1
        command = command_integer & 0x7
        count = command_integer >> 3
        if command == 1:
            if current:
                paths.append(current)
            current = []
            for _ in range(count):
                if i + 1 >= len(cmds):
                    break
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                current.append((x, y))
        elif command == 2:
            for _ in range(count):
                if i + 1 >= len(cmds):
                    break
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                current.append((x, y))
        elif command == 7:
            if current and current[0] != current[-1]:
                current.append(current[0])
        else:
            break
    if current:
        paths.append(current)
    return paths


def _fields(data: bytes):
    i = 0
    length = len(data)
    while i < length:
        key, i = _read_varint(data, i)
        field = key >> 3
        wire = key & 0x7
        if wire == 0:
            value, i = _read_varint(data, i)
            yield field, wire, value
        elif wire == 1:
            yield field, wire, data[i : i + 8]
            i += 8
        elif wire == 2:
            size, i = _read_varint(data, i)
            yield field, wire, data[i : i + size]
            i += size
        elif wire == 5:
            yield field, wire, data[i : i + 4]
            i += 4
        else:
            raise RasterTileError(f"Unsupported protobuf wire type: {wire}", 500)


def _packed_varints(data: bytes) -> list[int]:
    values = []
    i = 0
    while i < len(data):
        value, i = _read_varint(data, i)
        values.append(value)
    return values


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if index >= len(data):
            raise RasterTileError("Unexpected end of protobuf varint", 500)
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, index
        shift += 7


def _zigzag(value: int) -> int:
    return (value >> 1) ^ (-(value & 1))
