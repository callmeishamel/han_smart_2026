#!/usr/bin/env python3
"""Convert a ROS occupancy map into a Gazebo Classic digital-twin world.

The map remains the navigation source of truth.  Occupied cells are extruded
into a static 3-D mesh; free cells are left open and unknown cells are never
invented as walls.  Small isolated occupied components can be discarded to
avoid turning one-scan SLAM speckles into permanent factory structure.

Default source/output layout (relative to this file)::

    maps/factory_map.yaml
      -> models/factory_map_twin/{model.config,model.sdf,meshes/walls.obj}
      -> worlds/factory_digital_twin.world
      -> config/factory_digital_twin.json

Run this again after replacing ``factory_map.pgm/.yaml`` with a new SLAM map.
Only the generated model, world and metadata above are replaced; the SLAM map
itself is never modified.
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
import json
import math
from pathlib import Path
import sys
from typing import Dict, Iterable, List, Sequence, Set, Tuple


HERE = Path(__file__).resolve().parent
PACKAGE_DIR = HERE.parent
DEFAULT_MAP = HERE / "factory_map.yaml"
DEFAULT_MODEL_DIR = PACKAGE_DIR / "models" / "factory_map_twin"
DEFAULT_WORLD = PACKAGE_DIR / "worlds" / "factory_digital_twin.world"
DEFAULT_METADATA = PACKAGE_DIR / "config" / "factory_digital_twin.json"

Cell = Tuple[int, int]  # (row from top, column from left), as stored in PGM


def load_map_yaml(path: Path) -> Dict[str, object]:
    """Read the small ROS map YAML subset without requiring PyYAML."""
    values: Dict[str, object] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split(":", 1))
        if key == "image":
            values[key] = raw_value.strip("'\"")
        elif key == "origin":
            origin = ast.literal_eval(raw_value)
            if not isinstance(origin, (list, tuple)) or len(origin) < 2:
                raise ValueError(f"invalid map origin: {raw_value}")
            values[key] = [float(v) for v in origin]
        elif key in {"resolution", "occupied_thresh", "free_thresh"}:
            values[key] = float(raw_value)
        elif key == "negate":
            values[key] = int(raw_value)
        else:
            values[key] = raw_value

    required = {"image", "resolution", "origin"}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"{path}: missing map keys: {', '.join(missing)}")
    values.setdefault("negate", 0)
    values.setdefault("occupied_thresh", 0.65)
    values.setdefault("free_thresh", 0.196)
    return values


def _next_pgm_token(data: bytes, index: int) -> Tuple[bytes, int]:
    size = len(data)
    while index < size:
        if data[index:index + 1] == b"#":
            newline = data.find(b"\n", index)
            index = size if newline < 0 else newline + 1
        elif data[index] in b" \t\r\n":
            index += 1
        else:
            break
    start = index
    while index < size and data[index] not in b" \t\r\n#":
        index += 1
    if start == index:
        raise ValueError("unexpected end of PGM header")
    return data[start:index], index


def read_pgm(path: Path) -> Tuple[int, int, List[int]]:
    """Read an 8-bit P2/P5 PGM and return top-to-bottom pixels."""
    data = path.read_bytes()
    index = 0
    tokens = []
    for _ in range(4):
        token, index = _next_pgm_token(data, index)
        tokens.append(token)
    magic, width_raw, height_raw, maximum_raw = tokens
    width, height, maximum = int(width_raw), int(height_raw), int(maximum_raw)
    if magic not in {b"P2", b"P5"}:
        raise ValueError(f"{path}: only P2/P5 PGM is supported")
    if width <= 0 or height <= 0 or not 0 < maximum <= 255:
        raise ValueError(f"{path}: invalid PGM dimensions/range")

    if magic == b"P5":
        # P5 requires one whitespace separator after maxval.  Treat CRLF as
        # one separator, but do not skip arbitrary bytes because pixel values
        # are allowed to equal ASCII whitespace.
        if index >= len(data) or data[index] not in b" \t\r\n":
            raise ValueError(f"{path}: missing PGM raster separator")
        if data[index:index + 2] == b"\r\n":
            index += 2
        else:
            index += 1
        pixels = list(data[index:index + width * height])
    else:
        pixels = []
        for _ in range(width * height):
            token, index = _next_pgm_token(data, index)
            pixels.append(int(token))

    if len(pixels) != width * height:
        raise ValueError(
            f"{path}: expected {width * height} pixels, found {len(pixels)}")
    if maximum != 255:
        pixels = [round(value * 255 / maximum) for value in pixels]
    return width, height, pixels


def classify_cells(
    pixels: Sequence[int],
    width: int,
    height: int,
    *,
    negate: int,
    occupied_thresh: float,
    free_thresh: float,
) -> Tuple[Set[Cell], Set[Cell], Set[Cell]]:
    """Apply nav_msgs map_server trinary semantics to PGM pixels."""
    occupied: Set[Cell] = set()
    free: Set[Cell] = set()
    unknown: Set[Cell] = set()
    for row in range(height):
        for col in range(width):
            value = pixels[row * width + col]
            probability = value / 255.0 if negate else (255 - value) / 255.0
            cell = (row, col)
            if probability > occupied_thresh:
                occupied.add(cell)
            elif probability < free_thresh:
                free.add(cell)
            else:
                unknown.add(cell)
    return occupied, free, unknown


def connected_components(cells: Set[Cell]) -> List[Set[Cell]]:
    remaining = set(cells)
    components: List[Set[Cell]] = []
    while remaining:
        first = remaining.pop()
        component = {first}
        queue = deque([first])
        while queue:
            row, col = queue.popleft()
            for neighbor in ((row - 1, col), (row + 1, col),
                             (row, col - 1), (row, col + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def filter_components(cells: Set[Cell], minimum_size: int) -> Set[Cell]:
    if minimum_size <= 1:
        return set(cells)
    return set().union(*(
        component for component in connected_components(cells)
        if len(component) >= minimum_size
    )) if cells else set()


def cells_to_rectangles(cells: Set[Cell]) -> List[Tuple[int, int, int, int]]:
    """Losslessly merge equal horizontal runs on consecutive rows."""
    if not cells:
        return []
    rows: Dict[int, List[int]] = {}
    for row, col in cells:
        rows.setdefault(row, []).append(col)

    rectangles: List[Tuple[int, int, int, int]] = []
    active: Dict[Tuple[int, int], Tuple[int, int]] = {}
    previous_row = None
    for row in sorted(rows):
        runs: List[Tuple[int, int]] = []
        sorted_cols = sorted(rows[row])
        start = end = sorted_cols[0]
        for col in sorted_cols[1:]:
            if col == end + 1:
                end = col
            else:
                runs.append((start, end))
                start = end = col
        runs.append((start, end))

        if previous_row is None or row != previous_row + 1:
            for (col0, col1), (row0, row1) in active.items():
                rectangles.append((row0, row1, col0, col1))
            active = {}

        current: Dict[Tuple[int, int], Tuple[int, int]] = {}
        for run in runs:
            if run in active:
                current[run] = (active[run][0], row)
            else:
                current[run] = (row, row)
        for run, span in active.items():
            if run not in current:
                rectangles.append((span[0], span[1], run[0], run[1]))
        active = current
        previous_row = row

    for (col0, col1), (row0, row1) in active.items():
        rectangles.append((row0, row1, col0, col1))
    return rectangles


def cell_center(
    cell: Cell, width: int, height: int, resolution: float,
    origin_x: float, origin_y: float,
) -> Tuple[float, float]:
    del width  # kept in the signature to make coordinate intent explicit
    row, col = cell
    return (
        origin_x + (col + 0.5) * resolution,
        origin_y + (height - row - 0.5) * resolution,
    )


def clearance_map(width: int, height: int, free: Set[Cell]) -> Dict[Cell, int]:
    """Return clearance in cells, treating unknown/outside as solid."""
    distances: Dict[Cell, int] = {}
    queue = deque()
    for row in range(height):
        for col in range(width):
            cell = (row, col)
            if (cell not in free or row in {0, height - 1}
                    or col in {0, width - 1}):
                distances[cell] = 0
                queue.append(cell)
    while queue:
        row, col = queue.popleft()
        candidate = distances[(row, col)] + 1
        for neighbor in ((row - 1, col), (row + 1, col),
                         (row, col - 1), (row, col + 1)):
            nr, nc = neighbor
            if (0 <= nr < height and 0 <= nc < width
                    and neighbor not in distances):
                distances[neighbor] = candidate
                queue.append(neighbor)
    return {cell: distances[cell] for cell in free}


def choose_layout(
    width: int,
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    free: Set[Cell],
) -> Dict[str, object]:
    """Choose safe demo poses from known free space."""
    if not free:
        raise ValueError("map contains no known free cells")
    clearance = clearance_map(width, height, free)
    spawn_cell = max(
        free,
        key=lambda cell: (clearance[cell], -cell[0], cell[1]),
    )
    spawn = cell_center(
        spawn_cell, width, height, resolution, origin_x, origin_y)

    minimum_clearance_cells = max(2, math.ceil(0.25 / resolution))
    candidates = [
        cell for cell in free
        if clearance[cell] >= minimum_clearance_cells and cell != spawn_cell
    ]
    if not candidates:
        candidates = [cell for cell in free if cell != spawn_cell]

    def xy(cell: Cell) -> Tuple[float, float]:
        return cell_center(cell, width, height, resolution, origin_x, origin_y)

    def distance_from_spawn(cell: Cell) -> float:
        px, py = xy(cell)
        return math.hypot(px - spawn[0], py - spawn[1])

    event_pool = [
        cell for cell in candidates
        if 0.8 <= distance_from_spawn(cell) <= 2.5
    ]
    if not event_pool:
        event_pool = candidates
    fire_cell = max(
        event_pool,
        key=lambda cell: (
            clearance[cell], -abs(distance_from_spawn(cell) - 1.4)),
    ) if event_pool else spawn_cell
    fire = xy(fire_cell)
    fire_angle = math.atan2(fire[1] - spawn[1], fire[0] - spawn[0])

    helmet_pool = []
    for cell in event_pool:
        px, py = xy(cell)
        separation = math.hypot(px - fire[0], py - fire[1])
        angle = math.atan2(py - spawn[1], px - spawn[0])
        delta = abs(math.atan2(
            math.sin(angle - fire_angle), math.cos(angle - fire_angle)))
        if separation >= 0.55 and delta <= math.radians(50):
            helmet_pool.append(cell)
    helmet_cell = max(
        helmet_pool,
        key=lambda cell: (
            clearance[cell], -abs(distance_from_spawn(cell) - 1.8)),
    ) if helmet_pool else fire_cell
    helmet = xy(helmet_cell)

    helmet_angle = math.atan2(helmet[1] - spawn[1], helmet[0] - spawn[0])
    yaw = math.atan2(
        math.sin(fire_angle) + math.sin(helmet_angle),
        math.cos(fire_angle) + math.cos(helmet_angle),
    )

    waypoint_candidates = sorted(
        candidates,
        key=lambda cell: clearance[cell],
        reverse=True,
    )
    chosen = [spawn_cell]
    for _ in range(3):
        remaining = [c for c in waypoint_candidates if c not in chosen]
        if not remaining:
            break
        next_cell = max(
            remaining,
            key=lambda cell: min(
                math.hypot(
                    xy(cell)[0] - xy(old)[0],
                    xy(cell)[1] - xy(old)[1],
                )
                for old in chosen
            ),
        )
        if min(
            math.hypot(
                xy(next_cell)[0] - xy(old)[0],
                xy(next_cell)[1] - xy(old)[1],
            )
            for old in chosen
        ) < 0.6:
            break
        chosen.append(next_cell)

    waypoints = []
    for index, cell in enumerate(chosen):
        point = xy(cell)
        following = (
            xy(chosen[(index + 1) % len(chosen)])
            if len(chosen) > 1 else point
        )
        heading = math.atan2(following[1] - point[1], following[0] - point[0])
        waypoints.extend([
            round(point[0], 4), round(point[1], 4), round(heading, 4)])

    return {
        "spawn": {"x": round(spawn[0], 4), "y": round(spawn[1], 4),
                  "yaw": round(yaw, 4)},
        "events": {
            "fire": {"x": round(fire[0], 4), "y": round(fire[1], 4), "z": 0.5},
            "helmet_violation": {
                "x": round(helmet[0], 4), "y": round(helmet[1], 4), "z": 0.0,
            },
        },
        "waypoints": waypoints,
    }


def _append_box(
    lines: List[str],
    bounds: Tuple[float, float, float, float],
    wall_height: float,
    vertex_offset: int,
) -> int:
    x0, y0, x1, y1 = bounds
    vertices = [
        (x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0),
        (x0, y0, wall_height), (x1, y0, wall_height),
        (x1, y1, wall_height), (x0, y1, wall_height),
    ]
    for x, y, z in vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    faces = [
        (1, 3, 2), (1, 4, 3),       # bottom
        (5, 6, 7), (5, 7, 8),       # top
        (1, 2, 6), (1, 6, 5),       # south
        (2, 3, 7), (2, 7, 6),       # east
        (3, 4, 8), (3, 8, 7),       # north
        (4, 1, 5), (4, 5, 8),       # west
    ]
    for face in faces:
        indices = [vertex_offset + index for index in face]
        lines.append("f " + " ".join(str(index) for index in indices))
    return vertex_offset + 8


def write_obj(
    path: Path,
    rectangles: Iterable[Tuple[int, int, int, int]],
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    wall_height: float,
) -> None:
    lines = ["# Auto-generated from ROS occupancy grid", "o factory_map_walls"]
    vertex_offset = 0
    for row0, row1, col0, col1 in rectangles:
        x0 = origin_x + col0 * resolution
        x1 = origin_x + (col1 + 1) * resolution
        y0 = origin_y + (height - row1 - 1) * resolution
        y1 = origin_y + (height - row0) * resolution
        vertex_offset = _append_box(
            lines, (x0, y0, x1, y1), wall_height, vertex_offset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_model(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.config").write_text("""<?xml version="1.0"?>
<model>
  <name>Factory map twin walls</name>
  <version>1.0</version>
  <sdf version="1.6">model.sdf</sdf>
  <author><name>smart_factory_sim map converter</name></author>
  <description>
    Static walls generated from the real TurtleBot SLAM map.
  </description>
</model>
""", encoding="utf-8")
    (model_dir / "model.sdf").write_text("""<?xml version="1.0"?>
<sdf version="1.6">
  <model name="factory_map_twin">
    <static>true</static>
    <link name="walls">
      <collision name="collision">
        <geometry>
          <mesh><uri>model://factory_map_twin/meshes/walls.obj</uri></mesh>
        </geometry>
      </collision>
      <visual name="visual">
        <geometry>
          <mesh><uri>model://factory_map_twin/meshes/walls.obj</uri></mesh>
        </geometry>
        <material>
          <ambient>0.58 0.62 0.67 1</ambient>
          <diffuse>0.72 0.76 0.82 1</diffuse>
          <specular>0.08 0.08 0.08 1</specular>
        </material>
      </visual>
    </link>
  </model>
</sdf>
""", encoding="utf-8")


def write_world(
    path: Path,
    layout: Dict[str, object],
    map_width_m: float,
    map_height_m: float,
    origin_x: float,
    origin_y: float,
) -> None:
    center_x = origin_x + map_width_m / 2.0
    center_y = origin_y + map_height_m / 2.0
    camera_z = max(5.0, max(map_width_m, map_height_m) * 1.15)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""<?xml version="1.0"?>
<sdf version="1.6">
  <world name="factory_mapped_digital_twin">
    <plugin name="gazebo_ros_state" filename="libgazebo_ros_state.so">
      <ros><namespace>/gazebo</namespace></ros>
      <update_rate>10.0</update_rate>
    </plugin>
    <physics name="factory_physics" type="ode">
      <max_step_size>0.001</max_step_size>
      <real_time_update_rate>1000</real_time_update_rate>
    </physics>
    <scene>
      <ambient>0.45 0.45 0.45 1</ambient>
      <background>0.72 0.75 0.80 1</background>
      <shadows>true</shadows>
    </scene>
    <include><uri>model://sun</uri></include>
    <include><uri>model://ground_plane</uri></include>
    <include>
      <uri>model://factory_map_twin</uri>
      <name>factory_map_twin</name>
    </include>
    <gui fullscreen="0">
      <camera name="user_camera">
        <pose>{center_x:.4f} {center_y - map_height_m * 0.85:.4f}
              {camera_z:.4f} 0 0.85 1.57</pose>
        <view_controller>orbit</view_controller>
      </camera>
    </gui>
  </world>
</sdf>
""", encoding="utf-8")


def build(
    map_yaml: Path,
    model_dir: Path,
    world_path: Path,
    metadata_path: Path,
    *,
    wall_height: float = 2.0,
    minimum_component_size: int = 2,
) -> Dict[str, object]:
    map_yaml = map_yaml.resolve()
    config = load_map_yaml(map_yaml)
    image_path = Path(str(config["image"]))
    if not image_path.is_absolute():
        image_path = map_yaml.parent / image_path
    width, height, pixels = read_pgm(image_path)
    occupied, free, unknown = classify_cells(
        pixels, width, height,
        negate=int(config["negate"]),
        occupied_thresh=float(config["occupied_thresh"]),
        free_thresh=float(config["free_thresh"]),
    )
    filtered_occupied = filter_components(occupied, minimum_component_size)
    rectangles = cells_to_rectangles(filtered_occupied)
    resolution = float(config["resolution"])
    origin = config["origin"]
    origin_x, origin_y = float(origin[0]), float(origin[1])
    layout = choose_layout(
        width, height, resolution, origin_x, origin_y, free)

    write_obj(
        model_dir / "meshes" / "walls.obj",
        rectangles,
        height,
        resolution,
        origin_x,
        origin_y,
        wall_height,
    )
    write_model(model_dir)
    write_world(
        world_path,
        layout,
        width * resolution,
        height * resolution,
        origin_x,
        origin_y,
    )

    metadata: Dict[str, object] = {
        # Nav2 must use precisely the map used to make the wall mesh.  Keeping
        # the resolved path allows a newly saved real map (for example
        # /home/<user>/factory_map_auto.yaml) to drive both sides without
        # copying or overwriting the original map files.
        "map_yaml": str(map_yaml),
        "source_map": map_yaml.name,
        "source_image": image_path.name,
        "resolution": resolution,
        "origin": [
            origin_x,
            origin_y,
            float(origin[2]) if len(origin) > 2 else 0.0,
        ],
        "width_cells": width,
        "height_cells": height,
        "wall_height": wall_height,
        "occupied_cells_source": len(occupied),
        "occupied_cells_used": len(filtered_occupied),
        "filtered_noise_cells": len(occupied) - len(filtered_occupied),
        "free_cells": len(free),
        "unknown_cells": len(unknown),
        "wall_rectangles": len(rectangles),
        **layout,
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP,
                        help="ROS map YAML (default: maps/factory_map.yaml)")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR,
                        help="generated Gazebo model directory")
    parser.add_argument("--world", type=Path, default=DEFAULT_WORLD,
                        help="generated Gazebo world path")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA,
                        help="generated spawn/event/waypoint JSON path")
    parser.add_argument("--wall-height", type=float, default=2.0,
                        help="extruded wall height in metres")
    parser.add_argument("--min-component-cells", type=int, default=2,
                        help=(
                            "discard occupied 4-connected components smaller "
                            "than this"))
    args = parser.parse_args(argv)
    if args.wall_height <= 0:
        parser.error("--wall-height must be positive")
    if args.min_component_cells <= 0:
        parser.error("--min-component-cells must be positive")

    metadata = build(
        args.map,
        args.model_dir,
        args.world,
        args.metadata,
        wall_height=args.wall_height,
        minimum_component_size=args.min_component_cells,
    )
    print(
        f"Generated {args.world} from {args.map}: "
        f"{metadata['occupied_cells_used']} occupied cells -> "
        f"{metadata['wall_rectangles']} mesh boxes; "
        f"filtered {metadata['filtered_noise_cells']} isolated cells."
    )
    print(
        "Spawn: "
        f"({metadata['spawn']['x']}, {metadata['spawn']['y']}, "
        f"yaw={metadata['spawn']['yaw']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
