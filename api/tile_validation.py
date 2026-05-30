class TileCoordinateValidationError(ValueError):
    """Raised when tile coordinate constraints are violated (z < 0, z > max_zoom, x < 0, y < 0, x >= 2^z, y >= 2^z)."""
    pass


def validate_tile_coordinates(z: int, x: int, y: int, max_zoom: int = 18) -> None:
    if z < 0 or z > max_zoom:
        raise TileCoordinateValidationError(f"Zoom level must be between 0 and {max_zoom}")
    limit = 2 ** z
    if x < 0 or y < 0 or x >= limit or y >= limit:
        raise TileCoordinateValidationError(
            f"Tile coordinate x={x}, y={y} is outside the valid range for zoom {z} (0 <= x,y < {limit})"
        )
