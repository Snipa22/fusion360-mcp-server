"""
Fusion360 Command Handler

Executes commands using the Fusion 360 API.  Every method in this class
is called on the **main thread** (via EventBridge), so Fusion API access
is safe.
"""

import ast
import base64
import datetime
import io
import math
import os
import tempfile
import time
import traceback
from contextlib import redirect_stdout

import adsk.cam
import adsk.core
import adsk.fusion

from . import get_logger, hub_cache
from . import hints as _hints

log = get_logger("handler")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


# ----------------------------------------------------------------------
# Hub crawl (adsk-dependent — cannot be unit tested; see hub_cache.py for
# the pure I/O/filtering logic that backs list_hub_files and *is* tested).
# ----------------------------------------------------------------------


def _crawl_project_main_thread(app, project_name: str, cache: dict) -> int:
    """Crawl one project's ``rootFolder.dataFiles`` on the MAIN THREAD.

    The Fusion adsk API is not thread-safe — calling ``dataFiles`` from a
    daemon/background thread silently fails or times out.  This function must
    only be called from the main thread (i.e. inside a tool handler that was
    dispatched through the CustomEvent bridge, such as ``list_hub_files``).
    """
    try:
        hub = app.data.activeHub
        project = None
        for i in range(hub.dataProjects.count):
            p = hub.dataProjects.item(i)
            if p.name == project_name:
                project = p
                break
        if project is None:
            log.warning("list_hub_files: project '%s' not found", project_name)
            return 0
        data_files = project.rootFolder.dataFiles  # cloud call — ~1-3s on main thread
        count = 0
        for i in range(data_files.count):
            df = data_files.item(i)
            file_info = {
                "name": df.name,
                "id": df.id,
                "last_modified": df.dateModified.isoformat(),
                "description": df.description or "",
            }
            try:
                file_info["version"] = df.versionNumber
            except AttributeError:
                pass
            hub_cache._upsert_file_entry(cache, project_name, file_info)
            count += 1
        cache.setdefault("projects", {}).setdefault(project_name, {})[
            "last_crawled"
        ] = _now_iso()
        log.info(
            "list_hub_files: crawled project '%s' — %d files", project_name, count
        )
        return count
    except Exception:
        log.error(
            "list_hub_files: crawl failed for project '%s':\n%s",
            project_name,
            traceback.format_exc(),
        )
        return 0


class CommandHandler:
    """Runs Fusion API operations.  Instantiated once; reused across requests."""

    def __init__(self):
        self.app = adsk.core.Application.get()
        self.ui = self.app.userInterface
        # No background thread — adsk API is main-thread-only.
        # list_hub_files does a lazy on-demand crawl when the cache is stale.

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    _COMMANDS = None  # populated lazily

    # Canonical camera presets, (eye_dir, up_vec) in Fusion's Z-up world.
    # Shared by render_view and export_view_sheet.
    _VIEW_DIRS = {
        "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_ne": ((1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_nw": ((-1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_sw": ((-1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
        "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        "back": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        "bottom": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
        "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        "left": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    }

    # Commands that can change body mass / bbox / body count.  Only these
    # get before/after snapshots so agents can sanity-check without a render.
    _MUTATION_COMMANDS = frozenset(
        {
            # feature ops
            "extrude",
            "revolve",
            "sweep",
            "loft",
            "fillet",
            "chamfer",
            "shell",
            "mirror",
            "create_hole",
            "rectangular_pattern",
            "circular_pattern",
            "create_thread",
            "draft_faces",
            "split_body",
            "split_face",
            "offset_faces",
            "scale_body",
            "suppress_feature",
            "unsuppress_feature",
            # body ops
            "move_body",
            "boolean_operation",
            # primitives
            "create_box",
            "create_cylinder",
            "create_sphere",
            "create_torus",
            # surface / sheet metal (can produce / thicken bodies)
            "thicken_surface",
            "patch_surface",
            "stitch_surfaces",
            "ruled_surface",
            "trim_surface",
            "create_flange",
            "create_bend",
            "flat_pattern",
            "unfold",
            # scene-wide
            "delete_all",
            "undo",
            # parametric & agent-authored changes
            "set_parameter",
            "execute_code",
            # document management
            "save_document",
            "save_as",
            "set_active_document",
            "new_document",
        }
    )

    def execute_command(self, command: dict) -> dict:
        """Route *command* to the correct handler; return a response dict."""
        if self._COMMANDS is None:
            self.__class__._COMMANDS = {
                # scene / query
                "get_scene_info": self.get_scene_info,
                "get_object_info": self.get_object_info,
                "get_bounding_box": self.get_bounding_box,
                "list_components": self.list_components,
                # sketch
                "create_sketch": self.create_sketch,
                "draw_rectangle": self.draw_rectangle,
                "draw_circle": self.draw_circle,
                "draw_line": self.draw_line,
                "draw_arc": self.draw_arc,
                "draw_spline": self.draw_spline,
                "create_polygon": self.create_polygon,
                "add_constraint": self.add_constraint,
                "add_dimension": self.add_dimension,
                "offset_curve": self.offset_curve,
                "trim_curve": self.trim_curve,
                "extend_curve": self.extend_curve,
                "project_geometry": self.project_geometry,
                "set_sketch_visibility": self.set_sketch_visibility,
                "hide_all_sketches": self.hide_all_sketches,
                # features
                "extrude": self.extrude,
                "revolve": self.revolve,
                "sweep": self.sweep,
                "loft": self.loft,
                "fillet": self.fillet,
                "chamfer": self.chamfer,
                "shell": self.shell,
                "mirror": self.mirror,
                "create_hole": self.create_hole,
                "rectangular_pattern": self.rectangular_pattern,
                "circular_pattern": self.circular_pattern,
                "create_thread": self.create_thread,
                "draft_faces": self.draft_faces,
                "split_body": self.split_body,
                "split_face": self.split_face,
                "offset_faces": self.offset_faces,
                "scale_body": self.scale_body,
                "suppress_feature": self.suppress_feature,
                "unsuppress_feature": self.unsuppress_feature,
                # body operations
                "move_body": self.move_body,
                "rename_body": self.rename_body,
                "export_stl": self.export_stl,
                "export_step": self.export_step,
                "export_f3d": self.export_f3d,
                "export_view_sheet": self.export_view_sheet,
                "export": self.export,
                "import_mesh": self.import_mesh,
                "create_box_parametric": self.create_box_parametric,
                "boolean_operation": self.boolean_operation,
                "delete_all": self.delete_all,
                "undo": self.undo,
                # direct primitives
                "create_box": self.create_box,
                "create_cylinder": self.create_cylinder,
                "create_sphere": self.create_sphere,
                "create_torus": self.create_torus,
                # construction geometry
                "create_construction_plane": self.create_construction_plane,
                "create_construction_axis": self.create_construction_axis,
                # assembly
                "create_component": self.create_component,
                "add_joint": self.add_joint,
                "create_as_built_joint": self.create_as_built_joint,
                "create_rigid_group": self.create_rigid_group,
                # inspection / analysis
                "measure_distance": self.measure_distance,
                "measure_angle": self.measure_angle,
                "get_physical_properties": self.get_physical_properties,
                "create_section_analysis": self.create_section_analysis,
                "check_interference": self.check_interference,
                "point_containment": self.point_containment,
                "check_solid": self.check_solid,
                "get_cylindrical_faces": self.get_cylindrical_faces,
                # appearance
                "set_appearance": self.set_appearance,
                # parameters
                "get_parameters": self.get_parameters,
                "create_parameter": self.create_parameter,
                "set_parameter": self.set_parameter,
                "delete_parameter": self.delete_parameter,
                # surface operations
                "patch_surface": self.patch_surface,
                "stitch_surfaces": self.stitch_surfaces,
                "thicken_surface": self.thicken_surface,
                "ruled_surface": self.ruled_surface,
                "trim_surface": self.trim_surface,
                # sheet metal
                "create_flange": self.create_flange,
                "create_bend": self.create_bend,
                "flat_pattern": self.flat_pattern,
                "unfold": self.unfold,
                # code execution
                "execute_code": self.execute_code,
                # CAM
                "cam_list_setups": self.cam_list_setups,
                "cam_list_operations": self.cam_list_operations,
                "cam_get_operation_info": self.cam_get_operation_info,
                "cam_create_setup": self.cam_create_setup,
                "cam_create_operation": self.cam_create_operation,
                "cam_generate_toolpath": self.cam_generate_toolpath,
                "cam_post_process": self.cam_post_process,
                # health
                "ping": self.ping,
                # design type safety
                "get_design_type": self.get_design_type,
                "set_design_type": self.set_design_type,
                # perception
                "render_view": self.render_view,
                # document management
                "save_document": self.save_document,
                "save_as": self.save_as,
                "list_documents": self.list_documents,
                "set_active_document": self.set_active_document,
                "new_document": self.new_document,
                "list_hub_files": self.list_hub_files,
            }

        cmd_type = command.get("type")
        params = command.get("params", {})

        handler = self._COMMANDS.get(cmd_type)
        if handler is None:
            # Infrastructure-level failure (not an application error) —
            # keep the legacy error envelope so the client raises.
            return {"status": "error", "message": f"Unknown command: {cmd_type}"}

        is_mutation = cmd_type in self._MUTATION_COMMANDS
        snap_before = self._snapshot() if is_mutation else None

        try:
            t0 = time.monotonic()
            result = handler(**params)
            elapsed = time.monotonic() - t0
            log.debug("%s completed in %.3fs", cmd_type, elapsed)
        except Exception as exc:
            log.error("%s raised: %s", cmd_type, exc)
            error_kind, hint_list = _hints.classify(exc)
            return {
                "status": "success",
                "result": {
                    "ok": False,
                    "error_kind": error_kind,
                    "error_message": str(exc) or exc.__class__.__name__,
                    "hints": hint_list,
                    "traceback": traceback.format_exc(),
                },
            }

        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("ok", True)

        if is_mutation and snap_before is not None:
            snap_after = self._snapshot()
            if snap_after is not None:
                result["deltas"] = self._compute_deltas(snap_before, snap_after)

        return {"status": "success", "result": result}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _design(self):
        # Always get the Design product by type — activeProduct changes when
        # switching to Manufacture workspace and returns the CAM product instead.
        doc = self.app.activeDocument
        d = doc.products.itemByProductType("DesignProductType")
        if d is None:
            raise RuntimeError("No active design")
        return d

    def _root(self):
        return self._design().rootComponent

    def _last_sketch(self):
        root = self._root()
        if root.sketches.count == 0:
            raise RuntimeError("No sketch available — create one first")
        return root.sketches.item(root.sketches.count - 1)

    def _sketch_by_name(self, name: str):
        root = self._root()
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            if s.name == name:
                return s
        raise RuntimeError(f"Sketch '{name}' not found")

    def _body_by_name(self, name: str):
        root = self._root()
        # Search root bodies first
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.name == name:
                return b
        # Search bodies inside components via occurrence proxies (assembly design)
        # Returns proxy body in root coordinate space for correct boolean ops
        for occ in root.allOccurrences:
            for i in range(occ.bRepBodies.count):
                b = occ.bRepBodies.item(i)
                if b.name == name:
                    return b
        raise RuntimeError(f"Body '{name}' not found")

    def _body_or_plane_by_name(self, name: str):
        """Resolve a name to a BRepBody or ConstructionPlane.
        Checks construction planes first (since that's the gap in split_body),
        then falls back to bRepBodies. Raises RuntimeError if not found in either.
        """
        root = self._root()
        # Check construction planes first
        for i in range(root.constructionPlanes.count):
            plane = root.constructionPlanes.item(i)
            if plane.name == name:
                return plane
        # Fall back to bodies
        for i in range(root.bRepBodies.count):
            body = root.bRepBodies.item(i)
            if body.name == name:
                return body
        raise RuntimeError(
            f"BODY_NOT_FOUND: '{name}' not found as a construction plane or body. "
            f"Available planes: {[root.constructionPlanes.item(i).name for i in range(root.constructionPlanes.count)]}, "
            f"bodies: {[root.bRepBodies.item(i).name for i in range(root.bRepBodies.count)]}"
        )

    def _component_by_name(self, name: str):
        root = self._root()
        if root.name == name:
            return root
        for occ in root.allOccurrences:
            if occ.component.name == name:
                return occ.component
        raise RuntimeError(f"Component '{name}' not found")

    def _construction_plane(self, plane: str):
        root = self._root()
        m = {
            "xy": root.xYConstructionPlane,
            "yz": root.yZConstructionPlane,
            "xz": root.xZConstructionPlane,
        }
        p = m.get(plane)
        if p is None:
            raise RuntimeError(f"Unknown plane '{plane}' — use xy, yz, or xz")
        return p

    def _construction_axis(self, axis: str):
        root = self._root()
        m = {
            "x": root.xConstructionAxis,
            "y": root.yConstructionAxis,
            "z": root.zConstructionAxis,
        }
        a = m.get(axis)
        if a is None:
            raise RuntimeError(f"Unknown axis '{axis}' — use x, y, or z")
        return a

    @staticmethod
    def _operation_type(name: str):
        m = {
            "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
            "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        }
        t = m.get(name)
        if t is None:
            raise RuntimeError(
                f"Unknown operation '{name}' — use new_body/join/cut/intersect"
            )
        return t

    def _select_edges(self, body, selection: str):
        """Return an ObjectCollection of edges based on *selection*."""
        coll = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox

        if selection == "all":
            for edge in body.edges:
                coll.add(edge)
        elif selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for edge in body.edges:
                mid = edge.pointOnEdge
                if mid.z > threshold:
                    coll.add(edge)
        elif selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for edge in body.edges:
                mid = edge.pointOnEdge
                if mid.z < threshold:
                    coll.add(edge)
        elif selection == "vertical":
            for edge in body.edges:
                sp = edge.startVertex.geometry
                ep = edge.endVertex.geometry
                if abs(sp.x - ep.x) < 0.001 and abs(sp.y - ep.y) < 0.001:
                    coll.add(edge)
        else:
            raise RuntimeError(
                f"Unknown edge_selection '{selection}' — use all/top/bottom/vertical"
            )

        if coll.count == 0:
            raise RuntimeError(f"No edges matched selection '{selection}'")
        return coll

    def _select_faces(self, body, selection: str):
        """Return an ObjectCollection of faces based on *selection*."""
        coll = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox

        if selection == "all":
            for face in body.faces:
                coll.add(face)
        elif selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for face in body.faces:
                if face.boundingBox.maxPoint.z > threshold:
                    coll.add(face)
        elif selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for face in body.faces:
                if face.boundingBox.minPoint.z < threshold:
                    coll.add(face)
        elif selection == "vertical":
            for face in body.faces:
                # Check if face normal is roughly horizontal (vertical face)
                try:
                    _, normal_vec = face.evaluator.getNormalAtPoint(face.pointOnFace)
                    if abs(normal_vec.z) < 0.1:
                        coll.add(face)
                except Exception:
                    pass
        else:
            raise RuntimeError(
                f"Unknown face_selection '{selection}' — use all/top/bottom/vertical"
            )

        if coll.count == 0:
            raise RuntimeError(f"No faces matched selection '{selection}'")
        return coll

    # ------------------------------------------------------------------
    # Scene / Query
    # ------------------------------------------------------------------

    def get_scene_info(self):
        design = self._design()
        root = self._root()

        bodies = []
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            bodies.append(
                {
                    "name": b.name,
                    "volume": b.volume,
                    "area": b.area,
                    "material": b.material.name if b.material else None,
                    "is_visible": b.isVisible,
                }
            )

        sketches = []
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            sketches.append(
                {
                    "name": s.name,
                    "profile_count": s.profiles.count,
                    "is_visible": s.isVisible,
                }
            )

        return {
            "design_name": design.parentDocument.name,
            "design_type": design.productType,
            "bodies": bodies,
            "sketches": sketches,
            "bodies_count": root.bRepBodies.count,
            "sketches_count": root.sketches.count,
            "features_count": root.features.count,
            "timeline_count": (
                design.timeline.count if hasattr(design, "timeline") else 0
            ),
            "camera": self._camera_info(),
        }

    def get_object_info(self, name: str):
        root = self._root()

        # bodies
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.name == name:
                return {
                    "found": True,
                    "type": "body",
                    "name": name,
                    "volume": b.volume,
                    "area": b.area,
                    "material": b.material.name if b.material else None,
                    "is_visible": b.isVisible,
                    "faces_count": b.faces.count,
                    "edges_count": b.edges.count,
                    "vertices_count": b.vertices.count,
                    "bounding_box": self._bbox_dict(b.boundingBox),
                }

        # sketches
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            if s.name == name:
                return {
                    "found": True,
                    "type": "sketch",
                    "name": name,
                    "is_visible": s.isVisible,
                    "profile_count": s.profiles.count,
                    "curve_count": s.sketchCurves.count,
                }

        return {"found": False, "name": name}

    def list_components(self):
        root = self._root()
        components = [{"name": root.name, "is_root": True}]
        for occ in root.allOccurrences:
            components.append(
                {
                    "name": occ.component.name,
                    "is_root": False,
                    "is_visible": occ.isVisible,
                }
            )
        return {"components": components, "count": len(components)}

    def get_bounding_box(self, name: str):
        """Axis-aligned bounding box for a body or component. Values in cm."""
        def _payload(obj_type, mn, mx):
            return {
                "found": True, "type": obj_type, "name": name,
                "min": mn, "max": mx,
                "size": [mx[i] - mn[i] for i in range(3)],
                "center": [(mn[i] + mx[i]) / 2 for i in range(3)],
            }

        # Try body first (covers root bodies + bodies inside components)
        try:
            body = self._body_by_name(name)
            bb = body.boundingBox
            return _payload(
                "body",
                [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z],
                [bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z],
            )
        except RuntimeError:
            pass

        # Fall back to component: union bbox of all contained bodies
        try:
            comp = self._component_by_name(name)
        except RuntimeError:
            return {"found": False, "name": name}

        mn = [float("inf")] * 3
        mx = [float("-inf")] * 3

        def _extend(bodies):
            for i in range(bodies.count):
                bb = bodies.item(i).boundingBox
                lo = [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z]
                hi = [bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z]
                for axis in range(3):
                    if lo[axis] < mn[axis]:
                        mn[axis] = lo[axis]
                    if hi[axis] > mx[axis]:
                        mx[axis] = hi[axis]

        _extend(comp.bRepBodies)
        for occ in comp.allOccurrences:
            _extend(occ.bRepBodies)

        if mn[0] == float("inf"):
            return {"found": True, "type": "component",
                    "name": name, "empty": True}

        return _payload("component", mn, mx)

    def import_mesh(self, file_path: str, units: str = "mm", component_name: str = None):
        """Import a mesh file (STL/OBJ/3MF) as a mesh body. Returns mesh name and bounding box."""
        import adsk.fusion
        unit_map = {
            "mm": adsk.fusion.MeshUnits.MillimeterMeshUnit,
            "cm": adsk.fusion.MeshUnits.CentimeterMeshUnit,
            "m":  adsk.fusion.MeshUnits.MeterMeshUnit,
            "in": adsk.fusion.MeshUnits.InchMeshUnit,
            "ft": adsk.fusion.MeshUnits.FootMeshUnit,
        }
        if units not in unit_map:
            raise ValueError(f"Unknown units: {units!r} — use mm, cm, m, in, ft")

        root = self._root()
        if component_name:
            target = self._component_by_name(component_name)
        else:
            target = root

        mesh = target.meshBodies.addByFile(file_path, unit_map[units])
        if mesh is None:
            raise RuntimeError(f"import_mesh failed for {file_path!r} — check the file path and format")

        bb = mesh.boundingBox
        bbox = {
            "min": [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z],
            "max": [bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z],
            "size": [bb.maxPoint.x - bb.minPoint.x,
                     bb.maxPoint.y - bb.minPoint.y,
                     bb.maxPoint.z - bb.minPoint.z],
        }
        return {
            "imported": True,
            "file_path": file_path,
            "mesh_name": mesh.name,
            "component": target.name,
            "units": units,
            "bounding_box": bbox,
        }


    # ------------------------------------------------------------------

    def create_sketch(self, plane: str = "xy", z_offset: float = None):
        root = self._root()

        if z_offset is not None and z_offset != 0:
            # Create an offset construction plane
            planes = root.constructionPlanes
            plane_input = planes.createInput()
            offset_val = adsk.core.ValueInput.createByReal(z_offset)
            plane_input.setByOffset(self._construction_plane(plane), offset_val)
            cp = planes.add(plane_input)
            sketch = root.sketches.add(cp)
        else:
            sketch = root.sketches.add(self._construction_plane(plane))

        return {"sketch_name": sketch.name, "plane": plane, "z_offset": z_offset}

    def draw_rectangle(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        origin_z: float = 0,
    ):
        sketch = self._last_sketch()
        p1 = adsk.core.Point3D.create(origin_x, origin_y, origin_z)
        p2 = adsk.core.Point3D.create(origin_x + width, origin_y + height, origin_z)
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
        return {"sketch": sketch.name, "width": width, "height": height}

    def draw_circle(
        self,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        sketch = self._last_sketch()
        c = adsk.core.Point3D.create(center_x, center_y, center_z)
        sketch.sketchCurves.sketchCircles.addByCenterRadius(c, radius)
        return {
            "sketch": sketch.name,
            "radius": radius,
            "center": [center_x, center_y, center_z],
        }

    def draw_line(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        start_z: float = 0,
        end_z: float = 0,
    ):
        sketch = self._last_sketch()
        sp = adsk.core.Point3D.create(start_x, start_y, start_z)
        ep = adsk.core.Point3D.create(end_x, end_y, end_z)
        sketch.sketchCurves.sketchLines.addByTwoPoints(sp, ep)
        return {
            "sketch": sketch.name,
            "start": [start_x, start_y, start_z],
            "end": [end_x, end_y, end_z],
        }

    def draw_arc(
        self,
        center_x: float,
        center_y: float,
        start_x: float,
        start_y: float,
        sweep_angle: float,
        center_z: float = 0,
        start_z: float = 0,
    ):
        sketch = self._last_sketch()
        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        start = adsk.core.Point3D.create(start_x, start_y, start_z)
        sweep_rad = math.radians(sweep_angle)
        sketch.sketchCurves.sketchArcs.addByCenterStartSweep(center, start, sweep_rad)
        return {"sketch": sketch.name, "sweep_angle": sweep_angle}

    def draw_spline(self, spline_type: str, points: list, degree: int = 3):
        sketch = self._last_sketch()
        pts = adsk.core.ObjectCollection.create()
        for p in points:
            z = p[2] if len(p) > 2 else 0
            pts.add(adsk.core.Point3D.create(p[0], p[1], z))

        if spline_type == "fit_points":
            sketch.sketchCurves.sketchFittedSplines.add(pts)
        else:  # control_points
            sketch.sketchCurves.sketchControlPointSplines.add(pts, degree)
        return {
            "sketch": sketch.name,
            "spline_type": spline_type,
            "points_count": len(points),
        }

    def create_polygon(
        self,
        sides: int,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        sketch = self._last_sketch()
        # Draw inscribed polygon
        for i in range(sides):
            angle1 = 2 * math.pi * i / sides
            angle2 = 2 * math.pi * (i + 1) / sides
            p1 = adsk.core.Point3D.create(
                center_x + radius * math.cos(angle1),
                center_y + radius * math.sin(angle1),
                center_z,
            )
            p2 = adsk.core.Point3D.create(
                center_x + radius * math.cos(angle2),
                center_y + radius * math.sin(angle2),
                center_z,
            )
            sketch.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
        return {"sketch": sketch.name, "sides": sides, "radius": radius}

    def add_constraint(
        self,
        constraint_type: str,
        entity_one: int = None,
        entity_two: int = None,
        symmetry_line: int = None,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        constraints = sketch.geometricConstraints
        curves = list(sketch.sketchCurves)

        e1 = curves[entity_one] if entity_one is not None else None
        e2 = curves[entity_two] if entity_two is not None else None

        constraint_map = {
            "coincident": lambda: constraints.addCoincident(e1, e2),
            "parallel": lambda: constraints.addParallel(e1, e2),
            "perpendicular": lambda: constraints.addPerpendicular(e1, e2),
            "tangent": lambda: constraints.addTangent(e1, e2),
            "equal": lambda: constraints.addEqual(e1, e2),
            "fix": lambda: constraints.addFix(e1),
            "horizontal": lambda: constraints.addHorizontal(e1),
            "vertical": lambda: constraints.addVertical(e1),
            "concentric": lambda: constraints.addConcentric(e1, e2),
            "collinear": lambda: constraints.addCollinear(e1, e2),
            "smooth": lambda: constraints.addSmooth(e1, e2),
            "midpoint": lambda: constraints.addMidPoint(
                sketch.sketchPoints.item(entity_one), e2
            ),
            "symmetry": lambda: constraints.addSymmetry(e1, e2, curves[symmetry_line]),
        }

        if constraint_type not in constraint_map:
            raise RuntimeError(f"Unknown constraint type: {constraint_type}")

        constraint_map[constraint_type]()
        return {"sketch": sketch.name, "constraint_type": constraint_type}

    def add_dimension(
        self,
        dimension_type: str,
        value: float,
        entity_one: int = None,
        entity_two: int = None,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        dims = sketch.sketchDimensions
        curves = list(sketch.sketchCurves)

        e1 = curves[entity_one] if entity_one is not None else None
        e2 = curves[entity_two] if entity_two is not None else None
        text_pt = adsk.core.Point3D.create(0, 0, 0)

        if dimension_type == "distance":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "horizontal":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "vertical":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "angular":
            dim = dims.addAngularDimension(e1, e2, text_pt)
        elif dimension_type == "radial":
            dim = dims.addRadialDimension(e1, text_pt)
        elif dimension_type == "diameter":
            dim = dims.addDiameterDimension(e1, text_pt)
        else:
            raise RuntimeError(f"Unknown dimension type: {dimension_type}")

        dim.parameter.value = value
        return {"sketch": sketch.name, "dimension_type": dimension_type, "value": value}

    def offset_curve(
        self,
        curve_index: int,
        offset_distance: float,
        direction_x: float = 1,
        direction_y: float = 0,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        direction_pt = adsk.core.Point3D.create(direction_x, direction_y, 0)

        coll = adsk.core.ObjectCollection.create()
        coll.add(curve)
        sketch.offset(coll, direction_pt, offset_distance)
        return {"sketch": sketch.name, "offset_distance": offset_distance}

    def trim_curve(
        self, curve_index: int, point_x: float, point_y: float, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        point = adsk.core.Point3D.create(point_x, point_y, 0)
        curve.trim(point)
        return {"sketch": sketch.name, "trimmed": True}

    def extend_curve(
        self, curve_index: int, point_x: float, point_y: float, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        point = adsk.core.Point3D.create(point_x, point_y, 0)
        curve.extend(point)
        return {"sketch": sketch.name, "extended": True}

    def project_geometry(
        self, source_name: str, is_linked: bool = True, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        body = self._body_by_name(source_name)

        projected = []
        for edge in body.edges:
            proj = sketch.project(edge)
            projected.append(proj.count)

        return {
            "sketch": sketch.name,
            "source": source_name,
            "projected_curves": sum(projected),
        }

    def set_sketch_visibility(self, sketch_name: str, visible: bool) -> dict:
        sketch = self._sketch_by_name(sketch_name)
        sketch.isVisible = visible
        return {"sketch": sketch_name, "visible": visible}

    def hide_all_sketches(self) -> dict:
        root = self._root()
        count = 0
        for i in range(root.sketches.count):
            root.sketches.item(i).isVisible = False
            count += 1
        return {"hidden_count": count}

    # ------------------------------------------------------------------
    # Features
    # ------------------------------------------------------------------

    def extrude(
        self,
        height: float,
        profile_index: int = 0,
        operation: str = "new_body",
        direction: str = "positive",
    ):
        root = self._root()
        sketch = self._last_sketch()
        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        ext_feats = root.features.extrudeFeatures
        ext_input = ext_feats.createInput(profile, self._operation_type(operation))
        dist = adsk.core.ValueInput.createByReal(height)
        if direction == "symmetric":
            ext_input.setSymmetricExtent(dist, True)
        else:
            ext_input.setDistanceExtent(direction == "negative", dist)

        feat = ext_feats.add(ext_input)
        return {
            "feature_name": feat.name,
            "height": height,
            "operation": operation,
            "direction": direction,
        }

    def revolve(
        self,
        angle: float,
        profile_index: int = 0,
        axis_origin_x: float = 0,
        axis_origin_y: float = 0,
        axis_origin_z: float = 0,
        axis_direction_x: float = 1,
        axis_direction_y: float = 0,
        axis_direction_z: float = 0,
        operation: str = "new_body",
    ):
        root = self._root()
        sketch = self._last_sketch()
        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        # Determine axis entity first (required for createInput)
        axis_entity = None
        is_x = abs(axis_direction_x) > 0.99 and abs(axis_direction_y) < 0.01
        is_y = abs(axis_direction_y) > 0.99 and abs(axis_direction_x) < 0.01
        is_z = abs(axis_direction_z) > 0.99 and abs(axis_direction_x) < 0.01
        if is_x and abs(axis_direction_z) < 0.01:
            axis_entity = root.xConstructionAxis
        elif is_y and abs(axis_direction_z) < 0.01:
            axis_entity = root.yConstructionAxis
        elif is_z and abs(axis_direction_y) < 0.01:
            axis_entity = root.zConstructionAxis
        else:
            # Create construction line in sketch
            origin = adsk.core.Point3D.create(
                axis_origin_x, axis_origin_y, axis_origin_z
            )
            end_pt = adsk.core.Point3D.create(
                axis_origin_x + axis_direction_x * 10,
                axis_origin_y + axis_direction_y * 10,
                axis_origin_z + axis_direction_z * 10,
            )
            line = sketch.sketchCurves.sketchLines.addByTwoPoints(origin, end_pt)
            line.isConstruction = True
            axis_entity = line

        rev_feats = root.features.revolveFeatures
        rev_input = rev_feats.createInput(
            profile, axis_entity, self._operation_type(operation)
        )

        angle_val = adsk.core.ValueInput.createByString(f"{angle} deg")
        rev_input.setAngleExtent(False, angle_val)

        feat = rev_feats.add(rev_input)
        return {"feature_name": feat.name, "angle": angle, "operation": operation}

    def sweep(
        self,
        profile_index: int,
        path_sketch_name: str,
        path_curve_index: int = 0,
        operation: str = "new_body",
    ):
        root = self._root()
        sketch = self._last_sketch()
        path_sketch = self._sketch_by_name(path_sketch_name)

        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        path_curves = list(path_sketch.sketchCurves)
        path_curve = path_curves[path_curve_index]

        path = root.features.createPath(path_curve)

        sweep_feats = root.features.sweepFeatures
        sweep_input = sweep_feats.createInput(
            profile, path, self._operation_type(operation)
        )
        feat = sweep_feats.add(sweep_input)
        return {"feature_name": feat.name, "operation": operation}

    def loft(self, profile_sketch_names: list, operation: str = "new_body"):
        root = self._root()
        loft_feats = root.features.loftFeatures
        loft_input = loft_feats.createInput(self._operation_type(operation))

        for sketch_name in profile_sketch_names:
            sketch = self._sketch_by_name(sketch_name)
            if sketch.profiles.count == 0:
                raise RuntimeError(f"No profiles in sketch '{sketch_name}'")
            loft_input.loftSections.add(sketch.profiles.item(0))

        feat = loft_feats.add(loft_input)
        return {
            "feature_name": feat.name,
            "operation": operation,
            "profile_count": len(profile_sketch_names),
        }

    def fillet(
        self,
        radius: float,
        body_name: str = None,
        body_index: int = 0,
        edge_selection: str = "all",
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )
        edges = self._select_edges(body, edge_selection)

        fillets = root.features.filletFeatures
        inp = fillets.createInput()
        if isinstance(radius, str):
            radius_input = adsk.core.ValueInput.createByString(radius)
        else:
            radius_input = adsk.core.ValueInput.createByReal(radius)
        inp.addConstantRadiusEdgeSet(edges, radius_input, True)
        feat = fillets.add(inp)
        return {"feature_name": feat.name, "radius": radius, "edges_count": edges.count}

    def chamfer(
        self,
        distance: float,
        body_name: str = None,
        body_index: int = 0,
        edge_selection: str = "all",
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )
        edges = self._select_edges(body, edge_selection)

        chamfers = root.features.chamferFeatures
        inp = chamfers.createInput(edges, True)
        if isinstance(distance, str):
            distance_input = adsk.core.ValueInput.createByString(distance)
        else:
            distance_input = adsk.core.ValueInput.createByReal(distance)
        inp.setToEqualDistance(distance_input)
        feat = chamfers.add(inp)
        return {
            "feature_name": feat.name,
            "distance": distance,
            "edges_count": edges.count,
        }

    def shell(
        self,
        thickness: float,
        body_name: str = None,
        body_index: int = 0,
        face_selection: str = "top",
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        faces = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox

        if face_selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for face in body.faces:
                if face.boundingBox.maxPoint.z > threshold:
                    faces.add(face)
        elif face_selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for face in body.faces:
                if face.boundingBox.minPoint.z < threshold:
                    faces.add(face)
        else:
            raise RuntimeError(
                f"Unknown face_selection '{face_selection}' — use top/bottom"
            )

        if faces.count == 0:
            raise RuntimeError(f"No faces matched '{face_selection}'")

        shells = root.features.shellFeatures
        body_coll = adsk.core.ObjectCollection.create()
        body_coll.add(body)
        inp = shells.createInput(body_coll)
        inp.facesToRemove = faces
        if isinstance(thickness, str):
            thickness_input = adsk.core.ValueInput.createByString(thickness)
        else:
            thickness_input = adsk.core.ValueInput.createByReal(thickness)
        inp.insideThickness = thickness_input
        feat = shells.add(inp)
        return {
            "feature_name": feat.name,
            "thickness": thickness,
            "faces_removed": faces.count,
        }

    def mirror(self, mirror_plane: str, body_name: str = None, body_index: int = 0):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        entities = adsk.core.ObjectCollection.create()
        entities.add(body)

        mirrors = root.features.mirrorFeatures
        inp = mirrors.createInput(entities, self._construction_plane(mirror_plane))
        feat = mirrors.add(inp)
        return {"feature_name": feat.name, "mirror_plane": mirror_plane}

    def create_hole(
        self,
        diameter: float,
        depth: float,
        body_name: str = None,
        body_index: int = 0,
        face_selection: str = "top",
        center_x: float = 0,
        center_y: float = 0,
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        # Get the face Z height for top/bottom
        bbox = body.boundingBox
        if face_selection == "top":
            plane_offset = bbox.maxPoint.z
        elif face_selection == "bottom":
            plane_offset = bbox.minPoint.z
        else:
            raise RuntimeError(f"Unknown face_selection '{face_selection}' — use top/bottom")

        # Create construction plane at the face Z height
        planes = root.constructionPlanes
        plane_input = planes.createInput()
        plane_input.setByOffset(root.xYConstructionPlane, 
                                adsk.core.ValueInput.createByReal(plane_offset))
        construction_plane = planes.add(plane_input)

        # Sketch on construction plane instead of directly on face
        sketch = root.sketches.add(construction_plane)
        center = adsk.core.Point3D.create(center_x, center_y, 0)
        sketch_pt = sketch.sketchPoints.add(center)

        # Create hole feature
        holes = root.features.holeFeatures
        # createSimpleInput takes a diameter (not radius)
        if isinstance(diameter, str):
            diameter_input = adsk.core.ValueInput.createByString(diameter)
        else:
            diameter_input = adsk.core.ValueInput.createByReal(diameter)
        if isinstance(depth, str):
            depth_input = adsk.core.ValueInput.createByString(depth)
        else:
            depth_input = adsk.core.ValueInput.createByReal(depth)
        hole_input = holes.createSimpleInput(diameter_input)
        hole_input.setPositionBySketchPoint(sketch_pt)
        hole_input.setDistanceExtent(depth_input)
        
        # Set participant body — must be a list, not ObjectCollection
        hole_input.participantBodies = [body]

        feat = holes.add(hole_input)
        return {"feature_name": feat.name, "diameter": diameter, "depth": depth}

    def rectangular_pattern(
        self,
        body_name: str,
        x_count: int = 1,
        x_spacing: float = 1.0,
        y_count: int = 1,
        y_spacing: float = 1.0,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        patterns = root.features.rectangularPatternFeatures
        inp = patterns.createInput(
            bodies,
            root.xConstructionAxis,
            adsk.core.ValueInput.createByReal(x_count),
            adsk.core.ValueInput.createByReal(x_spacing),
            adsk.fusion.PatternDistanceType.SpacingPatternDistanceType,
        )
        inp.setDirectionTwo(
            root.yConstructionAxis,
            adsk.core.ValueInput.createByReal(y_count),
            adsk.core.ValueInput.createByReal(y_spacing),
        )
        feat = patterns.add(inp)
        return {"feature_name": feat.name, "x_count": x_count, "y_count": y_count}

    def circular_pattern(
        self, body_name: str, count: int, axis: str = "z", total_angle: float = 360
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        patterns = root.features.circularPatternFeatures
        inp = patterns.createInput(bodies, self._construction_axis(axis))
        inp.quantity = adsk.core.ValueInput.createByReal(count)
        inp.totalAngle = adsk.core.ValueInput.createByString(f"{total_angle} deg")
        feat = patterns.add(inp)
        return {"feature_name": feat.name, "count": count, "total_angle": total_angle}

    def create_thread(
        self,
        body_name: str,
        face_index: int,
        is_internal: bool = False,
        thread_type: str = "ISO Metric profile",
        thread_designation: str = "M10x1.5",
        thread_class: str = "6g",
        is_modeled: bool = False,
        is_full_length: bool = True,
        thread_length: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        face = body.faces.item(face_index)

        threads = root.features.threadFeatures
        # ThreadInfo.create(isTapered, isInternal, threadType, threadDesignation, threadClass, isRightHanded)
        thread_info = adsk.fusion.ThreadInfo.create(
            False,
            is_internal,
            thread_type,
            thread_designation,
            thread_class,
            True,
        )

        inp = threads.createInput(face, thread_info)
        inp.isInternal = is_internal
        inp.isModeled = is_modeled
        inp.isFullLength = is_full_length
        if not is_full_length and thread_length:
            inp.threadLength = adsk.core.ValueInput.createByReal(thread_length)

        feat = threads.add(inp)
        return {"feature_name": feat.name, "thread_type": thread_type}

    def draft_faces(
        self,
        body_name: str,
        angle: float,
        face_selection: str = "vertical",
        pull_direction_plane: str = "xy",
        is_tangent_chain: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        mass_before = body.physicalProperties.mass

        faces = self._select_faces(body, face_selection)
        if faces.count == 0:
            raise RuntimeError(
                f"draft_faces: no '{face_selection}' faces found on '{body_name}'"
            )

        drafts = root.features.draftFeatures
        faces_list = [faces.item(i) for i in range(faces.count)]
        pull_plane = self._construction_plane(pull_direction_plane)

        inp = drafts.createInput(faces_list, pull_plane, is_tangent_chain)
        # DraftFeatureInput has no .angle property — must use setSingleAngle(angle=ValueInput)
        # (SWIG binding requires keyword arg form, not positional)
        inp.setSingleAngle(
            angle=adsk.core.ValueInput.createByString(f"{angle} deg"),
            isSymmetric=False,
        )

        feat = drafts.add(inp)

        mass_after = body.physicalProperties.mass
        if angle != 0 and abs(mass_after - mass_before) < 1e-8:
            return {
                "ok": False,
                "error_kind": "DRAFT_NO_OP",
                "feature_name": feat.name,
                "angle": angle,
                "mass_before_g": mass_before * 1000,
                "mass_after_g": mass_after * 1000,
                "error_message": (
                    "draft_faces completed but body mass is unchanged — the draft "
                    "had no effect. Try a different pull_direction_plane "
                    "(e.g. 'xz' or 'yz' instead of 'xy')."
                ),
            }

        return {
            "feature_name": feat.name,
            "angle": angle,
            "faces_drafted": faces.count,
            "mass_delta_g": (mass_after - mass_before) * 1000,
        }

    def split_body(
        self,
        body_name: str,
        splitting_plane: str = "xy",
        splitting_body: str = None,
        extend_tool: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        splits = root.features.splitBodyFeatures
        if splitting_body:
            # Try construction planes by name first, then bodies
            tool = self._body_or_plane_by_name(splitting_body)
            inp = splits.createInput(body, tool, extend_tool)
        else:
            inp = splits.createInput(
                body, self._construction_plane(splitting_plane), extend_tool
            )
        feat = splits.add(inp)
        body_count = root.bRepBodies.count
        return {
            "feature_name": feat.name,
            "splitting_plane": splitting_plane,
            "splitting_body": splitting_body,
            "body_count_after": body_count,
        }

    def split_face(
        self,
        body_name: str,
        face_indices: list = None,
        splitting_plane: str = "xy",
        extend_tool: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        faces = adsk.core.ObjectCollection.create()
        if face_indices:
            for idx in face_indices:
                faces.add(body.faces.item(idx))
        else:
            for face in body.faces:
                faces.add(face)

        splits = root.features.splitFaceFeatures
        inp = splits.createInput(
            faces, self._construction_plane(splitting_plane), extend_tool
        )
        feat = splits.add(inp)
        return {"feature_name": feat.name}

    def offset_faces(
        self,
        body_name: str,
        distance: float,
        face_selection: str = "top",
        face_indices: list = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        if face_indices:
            faces = adsk.core.ObjectCollection.create()
            for idx in face_indices:
                faces.add(body.faces.item(idx))
        else:
            faces = self._select_faces(body, face_selection)

        offsets = root.features.offsetFeatures
        inp = offsets.createInput(
            faces,
            adsk.core.ValueInput.createByReal(distance),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        feat = offsets.add(inp)
        return {"feature_name": feat.name, "distance": distance}

    def scale_body(
        self,
        body_name: str,
        scale: float,
        scale_x: float = None,
        scale_y: float = None,
        scale_z: float = None,
        anchor_x: float = 0,
        anchor_y: float = 0,
        anchor_z: float = 0,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        # Use the design's origin point as anchor if anchor is 0,0,0
        # Otherwise create a sketch point at the specified location
        if anchor_x == 0 and anchor_y == 0 and anchor_z == 0:
            anchor = root.originConstructionPoint
        else:
            # Create a temporary sketch to get a proper point reference
            sketches = root.sketches
            tmp_sketch = sketches.add(root.xYConstructionPlane)
            sp = tmp_sketch.sketchPoints.add(
                adsk.core.Point3D.create(anchor_x, anchor_y, anchor_z)
            )
            anchor = sp

        scales = root.features.scaleFeatures
        if scale_x is not None and scale_y is not None and scale_z is not None:
            inp = scales.createInput(
                bodies,
                anchor,
                adsk.core.ValueInput.createByReal(scale_x),
                adsk.core.ValueInput.createByReal(scale_y),
                adsk.core.ValueInput.createByReal(scale_z),
            )
        else:
            inp = scales.createInput(
                bodies, anchor, adsk.core.ValueInput.createByReal(scale)
            )
        feat = scales.add(inp)
        return {"feature_name": feat.name, "scale": scale}

    def suppress_feature(self, feature_name: str):
        design = self._design()
        for i in range(design.timeline.count):
            item = design.timeline.item(i)
            has_entity = hasattr(item, "entity") and item.entity
            if has_entity and item.entity.name == feature_name:
                item.isSuppressed = True
                return {"suppressed": True, "feature": feature_name}
        raise RuntimeError(f"Feature '{feature_name}' not found in timeline")

    def unsuppress_feature(self, feature_name: str):
        design = self._design()
        for i in range(design.timeline.count):
            item = design.timeline.item(i)
            has_entity = hasattr(item, "entity") and item.entity
            if has_entity and item.entity.name == feature_name:
                item.isSuppressed = False
                return {"unsuppressed": True, "feature": feature_name}
        raise RuntimeError(f"Feature '{feature_name}' not found in timeline")

    # ------------------------------------------------------------------
    # Body Operations
    # ------------------------------------------------------------------

    def rename_body(self, body_name: str, new_name: str):
        body = self._body_by_name(body_name)
        old_name = body.name
        body.name = new_name
        return {"renamed": True, "old_name": old_name, "new_name": new_name}

    def move_body(self, body_name: str, x: float = 0, y: float = 0, z: float = 0):
        root = self._root()
        body = self._body_by_name(body_name)

        if x == 0 and y == 0 and z == 0:
            return {"feature_name": None, "body": body_name, "translation": [0, 0, 0], "no_op": True}

        move_feats = root.features.moveFeatures
        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        transform = adsk.core.Matrix3D.create()
        transform.translation = adsk.core.Vector3D.create(x, y, z)

        inp = move_feats.createInput(bodies, transform)
        feat = move_feats.add(inp)
        return {"feature_name": feat.name, "body": body_name, "translation": [x, y, z]}

    def export(self, body_name: str = None, file_path: str = None, format: str = None):
        """Unified export dispatcher — routes to export_stl/export_step/export_f3d
        based on explicit format or file extension."""
        if format is None and file_path:
            ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
            format = {"stl": "stl", "step": "step", "stp": "step", "f3d": "f3d"}.get(ext)
        if format is None:
            raise ValueError(
                "Cannot determine export format — pass format='stl'/'step'/'f3d' "
                "or a file_path with a recognised extension (.stl, .step, .stp, .f3d)"
            )
        format = format.lower()
        if format == "stl":
            return self.export_stl(body_name=body_name, file_path=file_path)
        elif format in ("step", "stp"):
            return self.export_step(body_name=body_name, file_path=file_path)
        elif format == "f3d":
            return self.export_f3d(file_path=file_path)
        else:
            raise ValueError(f"Unknown export format: {format!r} — use 'stl', 'step', or 'f3d'")

    def export_stl(self, body_name: str, file_path: str = None):
        body = self._body_by_name(body_name)

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{body_name}.stl")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = self._design().exportManager
        occ = body.assemblyContext  # None if body is at root

        if occ is None:
            stl_opts = export_mgr.createSTLExportOptions(body, file_path)
            stl_opts.meshRefinement = (
                adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
            )
            export_mgr.execute(stl_opts)
            return {"exported": True, "body": body_name, "file_path": file_path}

        # Body lives in a component occurrence: hide siblings so the
        # occurrence export only contains the target body. Identify
        # siblings by entityToken, not name, to handle same-name bodies.
        target_token = body.entityToken
        hidden = []
        for i in range(occ.bRepBodies.count):
            sibling = occ.bRepBodies.item(i)
            if sibling.entityToken != target_token and sibling.isVisible:
                sibling.isVisible = False
                hidden.append(sibling)

        try:
            stl_opts = export_mgr.createSTLExportOptions(occ, file_path)
            stl_opts.meshRefinement = (
                adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
            )
            export_mgr.execute(stl_opts)
        finally:
            for sibling in hidden:
                sibling.isVisible = True

        return {"exported": True, "body": body_name, "file_path": file_path}

    def export_step(self, body_name: str, file_path: str = None):
        body = self._body_by_name(body_name)
        design = self._design()

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{body_name}.step")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = design.exportManager
        # Export the root component — passing a single body object as the
        # scope argument raises "invalid argument geometry" even on valid
        # watertight solids. Component-scope export is reliable.
        step_opts = export_mgr.createSTEPExportOptions(file_path)
        export_mgr.execute(step_opts)
        return {"exported": True, "body": body_name, "file_path": file_path}

    def export_f3d(self, file_path: str = None):
        design = self._design()
        doc_name = design.parentDocument.name

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{doc_name}.f3d")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = design.exportManager
        f3d_opts = export_mgr.createFusionArchiveExportOptions(file_path)
        export_mgr.execute(f3d_opts)

        return {"exported": True, "file_path": file_path}

    # ── view sheet ─────────────────────────────────────────────────────
    # Render canonical views (iso/front/top/right/...) as PNGs and emit
    # a self-contained HTML page suitable for print-to-PDF. Intended
    # audience: mechanical engineers who want a quick sense of the part.

    def _scene_center_and_radius(self):
        """Return (center, radius) covering all visible root bodies."""
        root = self._root()
        minp = [float("inf")] * 3
        maxp = [float("-inf")] * 3
        found = False

        def _grow(bb):
            nonlocal found
            for j, coord in enumerate(("x", "y", "z")):
                minp[j] = min(minp[j], getattr(bb.minPoint, coord))
                maxp[j] = max(maxp[j], getattr(bb.maxPoint, coord))
            found = True

        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.isVisible:
                _grow(b.boundingBox)
        for i in range(root.occurrences.count):
            occ = root.occurrences.item(i)
            if not occ.isVisible:
                continue
            for j in range(occ.bRepBodies.count):
                b = occ.bRepBodies.item(j)
                if b.isVisible:
                    _grow(b.boundingBox)

        if not found:
            return (0.0, 0.0, 0.0), 10.0
        center = tuple((minp[i] + maxp[i]) / 2 for i in range(3))
        span = max(maxp[i] - minp[i] for i in range(3))
        return center, max(span, 1.0)

    def _apply_view(self, view_name: str, center, radius: float):
        """Point the active viewport camera at *center* from *view_name*."""
        dir_vec, up_vec = self._VIEW_DIRS[view_name]
        length = math.sqrt(sum(c * c for c in dir_vec))
        dist = radius * 3.0  # give fit() headroom
        eye = tuple(center[i] + dir_vec[i] / length * dist for i in range(3))
        vp = self.app.activeViewport
        cam = vp.camera
        cam.isSmoothTransition = False
        cam.cameraType = adsk.core.CameraTypes.OrthographicCameraType
        cam.eye = adsk.core.Point3D.create(*eye)
        cam.target = adsk.core.Point3D.create(*center)
        cam.upVector = adsk.core.Vector3D.create(*up_vec)
        cam.isFitView = True
        vp.camera = cam
        vp.refresh()
        adsk.doEvents()

    def export_view_sheet(
        self,
        title: str = None,
        notes: str = "",
        views: list = None,
        image_size: list = None,
        output_dir: str = None,
    ):
        """Render canonical views as PNGs + a shareable HTML sheet.

        Args:
            title: heading on the sheet (default: document name).
            notes: free-form text rendered below the views (newlines
                preserved; HTML is escaped).
            views: ordered list of view names. Valid: iso, iso_ne,
                iso_nw, iso_sw, front, back, top, bottom, right, left.
                Default: ["iso", "front", "top", "right"].
            image_size: [width, height] in pixels (default [1200, 900]).
            output_dir: destination folder
                (default: ~/Desktop/<doc>_views_<timestamp>).
        """
        import base64
        import html
        import json as _json

        design = self._design()
        doc_name = design.parentDocument.name
        sheet_title = title or doc_name
        views = views or ["iso", "front", "top", "right"]
        image_size = image_size or [1200, 900]
        width, height = int(image_size[0]), int(image_size[1])

        unknown = [v for v in views if v not in self._VIEW_DIRS]
        if unknown:
            raise RuntimeError(
                f"Unknown views: {unknown}. Valid: {sorted(self._VIEW_DIRS)}"
            )

        if output_dir is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(
                os.path.expanduser("~"),
                "Desktop",
                f"{doc_name}_views_{ts}",
            )
        os.makedirs(output_dir, exist_ok=True)

        vp = self.app.activeViewport
        orig = vp.camera
        orig_state = {
            "eye": (orig.eye.x, orig.eye.y, orig.eye.z),
            "target": (orig.target.x, orig.target.y, orig.target.z),
            "up": (orig.upVector.x, orig.upVector.y, orig.upVector.z),
            "type": orig.cameraType,
        }

        center, radius = self._scene_center_and_radius()

        rendered = []
        try:
            for view_name in views:
                self._apply_view(view_name, center, radius)
                png_path = os.path.join(output_dir, f"{view_name}.png")
                vp.saveAsImageFile(png_path, width, height)
                rendered.append({"view": view_name, "path": png_path})
        finally:
            cam = vp.camera
            cam.isSmoothTransition = False
            cam.cameraType = orig_state["type"]
            cam.eye = adsk.core.Point3D.create(*orig_state["eye"])
            cam.target = adsk.core.Point3D.create(*orig_state["target"])
            cam.upVector = adsk.core.Vector3D.create(*orig_state["up"])
            vp.camera = cam
            vp.refresh()

        # Build self-contained HTML with base64-embedded PNGs.
        figures = []
        for r in rendered:
            with open(r["path"], "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            label = r["view"].replace("_", " ").upper()
            figures.append(
                "<figure>"
                f'<img src="data:image/png;base64,{b64}" alt="{label}">'
                f"<figcaption>{label}</figcaption>"
                "</figure>"
            )

        notes_block = ""
        if notes:
            notes_block = (
                '<section class="notes"><h2>Notes</h2>'
                f"<pre>{html.escape(notes)}</pre></section>"
            )

        timestamp = time.strftime("%Y-%m-%d %H:%M")
        html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{html.escape(sheet_title)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
           sans-serif; color: #111; max-width: 1400px;
           margin: 2rem auto; padding: 0 2rem; }}
  header {{ border-bottom: 1px solid #d0d0d0; padding-bottom: .75rem;
            margin-bottom: 2rem; }}
  header h1 {{ margin: 0; font-weight: 500; font-size: 1.6rem; }}
  header .meta {{ color: #666; font-size: .85rem; margin-top: .25rem; }}
  .views {{ display: grid; grid-template-columns: 1fr 1fr;
            gap: 1.25rem; }}
  figure {{ margin: 0; border: 1px solid #e0e0e0; padding: .5rem;
            background: #fafafa; }}
  figure img {{ width: 100%; display: block; background: #fff; }}
  figcaption {{ text-align: center; font-size: .75rem; color: #555;
                margin-top: .35rem; letter-spacing: .15em; }}
  .notes {{ margin-top: 2rem; padding-top: 1rem;
            border-top: 1px solid #e0e0e0; }}
  .notes h2 {{ font-size: 1rem; font-weight: 500; margin: 0 0 .5rem; }}
  .notes pre {{ font-family: inherit; white-space: pre-wrap;
                margin: 0; color: #333; }}
  @media print {{
    body {{ max-width: none; margin: 0; padding: 1cm; }}
    .views {{ gap: .5cm; }}
    figure {{ break-inside: avoid; }}
  }}
</style>
</head><body>
<header>
  <h1>{html.escape(sheet_title)}</h1>
  <div class="meta">{html.escape(doc_name)} * {timestamp}</div>
</header>
<section class="views">{"".join(figures)}</section>
{notes_block}
</body></html>
"""
        html_path = os.path.join(output_dir, "view_sheet.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)

        # Sidecar manifest - machine-readable record of what was emitted.
        manifest = {
            "title": sheet_title,
            "document": doc_name,
            "views": rendered,
            "html_path": html_path,
            "image_size": [width, height],
        }
        with open(os.path.join(output_dir, "manifest.json"), "w") as f:
            _json.dump(manifest, f, indent=2)

        return {
            "html_path": html_path,
            "output_dir": output_dir,
            "views": rendered,
            "title": sheet_title,
            "image_size": [width, height],
        }

    def boolean_operation(
        self, target_body: str, tool_body: str, operation: str = "join"
    ):
        root = self._root()
        target = self._body_by_name(target_body)
        tool = self._body_by_name(tool_body)

        body_count_before = root.bRepBodies.count

        combine_feats = root.features.combineFeatures
        tool_coll = adsk.core.ObjectCollection.create()
        tool_coll.add(tool)

        op_map = {
            "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
            "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        }
        op = op_map.get(operation)
        if op is None:
            raise RuntimeError(
                f"Unknown boolean op '{operation}' — use join/cut/intersect"
            )

        inp = combine_feats.createInput(target, tool_coll)
        inp.operation = op
        feat = combine_feats.add(inp)

        body_count_after = root.bRepBodies.count

        # Fusion's combineFeatures.add() reports "Healthy" even when bodies don't
        # actually overlap (join/intersect of disjoint solids silently no-ops).
        # Detect this: a successful join or intersect MUST reduce body count.
        if operation in ("join", "intersect") and body_count_after >= body_count_before:
            return {
                "ok": False,
                "error_kind": "BOOLEAN_NO_OP",
                "feature_name": feat.name,
                "operation": operation,
                "target": target_body,
                "tool": tool_body,
                "body_count_before": body_count_before,
                "body_count_after": body_count_after,
                "error_message": (
                    f"boolean_operation('{operation}') completed but body count did not "
                    f"decrease ({body_count_before} → {body_count_after}). The bodies "
                    f"likely do not overlap. Use check_interference to verify contact, "
                    f"or move the bodies closer before joining."
                ),
            }

        return {
            "feature_name": feat.name,
            "operation": operation,
            "target": target_body,
            "tool": tool_body,
            "body_count_before": body_count_before,
            "body_count_after": body_count_after,
            "body_count_delta": body_count_after - body_count_before,
        }

    def delete_all(self):
        design = self._design()
        root = self._root()
        if hasattr(design, "timeline") and design.timeline.count > 0:
            tl = design.timeline
            for i in range(tl.count - 1, -1, -1):
                try:
                    tl.item(i).deleteMe()
                except Exception:
                    pass
            adsk.doEvents()

        # Timeline deleteMe() silently fails on sketches — sweep up any that remain
        for i in range(root.sketches.count - 1, -1, -1):
            try:
                root.sketches.item(i).deleteMe()
            except Exception:
                pass

        # Same for any remaining bodies
        for i in range(root.bRepBodies.count - 1, -1, -1):
            try:
                root.bRepBodies.item(i).deleteMe()
            except Exception:
                pass
        adsk.doEvents()

        # Post-condition check
        remaining_bodies = root.bRepBodies.count
        remaining_sketches = root.sketches.count
        if remaining_bodies > 0 or remaining_sketches > 0:
            return {
                "ok": False,
                "error_kind": "DELETE_INCOMPLETE",
                "error_message": f"delete_all completed but {remaining_bodies} bodies and {remaining_sketches} sketches remain",
                "remaining_bodies": remaining_bodies,
                "remaining_sketches": remaining_sketches,
            }

        return {"deleted": True}

    def undo(self):
        design = self._design()
        type_before = design.designType

        cmd_def = self.ui.commandDefinitions.itemById("UndoCommand")
        if cmd_def:
            cmd_def.execute()

        # Check if undo silently switched design type (Parametric → Direct)
        adsk.doEvents()  # let Fusion process the undo
        type_after = design.designType
        if type_before != type_after:
            # Undo the undo — redo to restore original state
            redo_def = self.ui.commandDefinitions.itemById("RedoCommand")
            if redo_def:
                redo_def.execute()
                adsk.doEvents()
            raise RuntimeError(
                f"Undo aborted: would have changed design type from "
                f"{'Parametric' if type_before == 1 else 'Direct'} to "
                f"{'Parametric' if type_after == 1 else 'Direct'}. "
                f"The undo was automatically reversed (redo). "
                f"Delete the failed feature explicitly instead."
            )

        return {"undone": True, "design_type": type_after}

    # ------------------------------------------------------------------
    # Direct Primitives (via TemporaryBRepManager)
    # ------------------------------------------------------------------

    def create_box(
        self,
        length: float,
        width: float,
        height: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        # Box orientation matrix
        orient = adsk.core.OrientedBoundingBox3D.create(
            adsk.core.Point3D.create(center_x, center_y, center_z + height / 2),
            adsk.core.Vector3D.create(1, 0, 0),
            adsk.core.Vector3D.create(0, 1, 0),
            length,
            width,
            height,
        )

        box_body = temp_brep.createBox(orient)
        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(box_body, base_feat)
        base_feat.finishEdit()

        return {"created": True, "length": length, "width": width, "height": height}

    def create_box_parametric(self, length, width, height,
                              origin_x: float = 0.0,
                              origin_y: float = 0.0,
                              origin_z: float = 0.0,
                              plane: str = "xy",
                              component_name: str = None,
                              body_name: str = None):
        """Parametric box: sketch rectangle + dimensions + extrude.

        length/width/height may be numeric (cm) or string expressions
        (e.g. 'boxL', '56 mm'). Expressions are applied via Fusion's
        parameter system so later changes to User Parameters propagate.
        """
        comp = (self._component_by_name(component_name)
                if component_name else self._root())

        base_plane = self._construction_plane(plane)
        if origin_z != 0:
            plane_input = comp.constructionPlanes.createInput()
            offset_val = adsk.core.ValueInput.createByReal(origin_z)
            plane_input.setByOffset(base_plane, offset_val)
            sketch_plane = comp.constructionPlanes.add(plane_input)
        else:
            sketch_plane = base_plane
        sketch = comp.sketches.add(sketch_plane)

        def _initial(val):
            return float(val) if isinstance(val, (int, float)) else 1.0

        p1 = adsk.core.Point3D.create(origin_x, origin_y, 0)
        p2 = adsk.core.Point3D.create(
            origin_x + _initial(length),
            origin_y + _initial(width), 0)
        rect = sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)

        dims = sketch.sketchDimensions
        text_pt = adsk.core.Point3D.create(0, 0, 0)

        def _set_dim(dim, value):
            if isinstance(value, (int, float)):
                dim.parameter.value = float(value)
            else:
                dim.parameter.expression = str(value)

        bottom = rect.item(0)
        length_dim = dims.addDistanceDimension(
            bottom.startSketchPoint, bottom.endSketchPoint,
            adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            text_pt)
        _set_dim(length_dim, length)

        right = rect.item(1)
        width_dim = dims.addDistanceDimension(
            right.startSketchPoint, right.endSketchPoint,
            adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
            text_pt)
        _set_dim(width_dim, width)

        if sketch.profiles.count == 0:
            raise RuntimeError("Rectangle sketch produced no profile")
        profile = sketch.profiles.item(0)

        ext_feats = comp.features.extrudeFeatures
        ext_input = ext_feats.createInput(
            profile,
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        if isinstance(height, (int, float)):
            h_vi = adsk.core.ValueInput.createByReal(float(height))
        else:
            h_vi = adsk.core.ValueInput.createByString(str(height))
        ext_input.setDistanceExtent(False, h_vi)
        feat = ext_feats.add(ext_input)

        body = feat.bodies.item(0)
        if body_name:
            body.name = body_name

        return {
            "created": True,
            "body_name": body.name,
            "feature_name": feat.name,
            "sketch_name": sketch.name,
            "length": length,
            "width": width,
            "height": height,
            "origin": [origin_x, origin_y, origin_z],
            "plane": plane,
            "component": comp.name,
        }

    def create_cylinder(
        self,
        radius: float,
        height: float,
        base_x: float = 0,
        base_y: float = 0,
        base_z: float = 0,
        center_x: float = None,
        center_y: float = None,
        center_z: float = None,
        axis: str = "z",
        body_name: str = None,
        name: str = None,  # alias for body_name
    ):
        # Accept either body_name or name
        if name and not body_name:
            body_name = name
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        # If center coordinates are provided, calculate base position
        if center_x is not None or center_y is not None or center_z is not None:
            # Use provided center values, default to 0 if not provided
            cx = center_x if center_x is not None else 0
            cy = center_y if center_y is not None else 0
            cz = center_z if center_z is not None else 0
            
            # Calculate base from center based on axis
            axis_vec = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
            base_x = cx - axis_vec[0] * height / 2
            base_y = cy - axis_vec[1] * height / 2
            base_z = cz - axis_vec[2] * height / 2

        base_pt = adsk.core.Point3D.create(base_x, base_y, base_z)
        axis_vec = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        top_pt = adsk.core.Point3D.create(
            base_x + axis_vec[0] * height,
            base_y + axis_vec[1] * height,
            base_z + axis_vec[2] * height,
        )

        cyl_body = temp_brep.createCylinderOrCone(base_pt, radius, top_pt, radius)

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        new_body = root.bRepBodies.add(cyl_body, base_feat)
        base_feat.finishEdit()

        if body_name:
            new_body.name = body_name

        # Re-read name after any rename — Fusion may assign a different name
        actual_name = new_body.name
        return {"created": True, "radius": radius, "height": height, "body_name": actual_name}

    def create_sphere(
        self,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        sphere_body = temp_brep.createSphere(center, radius)

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(sphere_body, base_feat)
        base_feat.finishEdit()

        return {"created": True, "radius": radius}

    def create_torus(
        self,
        major_radius: float,
        minor_radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
        axis: str = "z",
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        axis_vec = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        axis_vector = adsk.core.Vector3D.create(*axis_vec)

        torus_body = temp_brep.createTorus(
            center, axis_vector, major_radius, minor_radius
        )

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(torus_body, base_feat)
        base_feat.finishEdit()

        return {
            "created": True,
            "major_radius": major_radius,
            "minor_radius": minor_radius,
        }

    # ------------------------------------------------------------------
    # Construction Geometry
    # ------------------------------------------------------------------

    def create_construction_plane(
        self,
        method: str,
        plane: str = None,
        offset: float = None,
        angle: float = None,
        edge_name: str = None,
        plane_one: str = None,
        plane_two: str = None,
        point_one: list = None,
        point_two: list = None,
        point_three: list = None,
    ):
        root = self._root()
        planes = root.constructionPlanes
        inp = planes.createInput()

        if method == "offset":
            inp.setByOffset(
                self._construction_plane(plane),
                adsk.core.ValueInput.createByReal(offset),
            )
        elif method == "angle":
            inp.setByAngle(
                self._construction_axis(edge_name or "x"),
                adsk.core.ValueInput.createByString(f"{angle} deg"),
                self._construction_plane(plane),
            )
        elif method == "midplane":
            inp.setByTwoPlanes(
                self._construction_plane(plane_one), self._construction_plane(plane_two)
            )
        elif method == "three_points":
            p1 = adsk.core.Point3D.create(*point_one)
            p2 = adsk.core.Point3D.create(*point_two)
            p3 = adsk.core.Point3D.create(*point_three)
            inp.setByThreePoints(p1, p2, p3)
        elif method == "tangent":
            raise RuntimeError("Tangent plane needs face selection—use execute_code")
        else:
            raise RuntimeError(f"Unknown method: {method}")

        plane_obj = planes.add(inp)
        return {"created": True, "name": plane_obj.name, "method": method}

    def create_construction_axis(
        self,
        method: str,
        point_one: list = None,
        point_two: list = None,
        plane_one: str = None,
        plane_two: str = None,
        body_name: str = None,
        edge_index: int = None,
    ):
        root = self._root()
        axes = root.constructionAxes
        inp = axes.createInput()

        if method == "two_points":
            p1 = adsk.core.Point3D.create(*point_one)
            p2 = adsk.core.Point3D.create(*point_two)
            inp.setByTwoPoints(p1, p2)
        elif method == "intersection":
            inp.setByTwoPlanes(
                self._construction_plane(plane_one), self._construction_plane(plane_two)
            )
        elif method == "edge":
            body = self._body_by_name(body_name)
            edge = body.edges.item(edge_index)
            inp.setByEdge(edge)
        elif method == "perpendicular_at_point":
            p1 = adsk.core.Point3D.create(*point_one)
            inp.setByPerpendicularAtPoint(self._construction_plane(plane_one), p1)
        else:
            raise RuntimeError(f"Unknown method: {method}")

        axis_obj = axes.add(inp)
        return {"created": True, "name": axis_obj.name, "method": method}

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def create_component(self, name: str, parent_name: str = None):
        root = self._root()
        parent = self._component_by_name(parent_name) if parent_name else root

        occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occ.component.name = name
        return {"created": True, "name": name}

    def add_joint(
        self, component_one: str, component_two: str, joint_type: str = "rigid"
    ):
        root = self._root()

        occ1 = occ2 = None
        for occ in root.allOccurrences:
            if occ.component.name == component_one:
                occ1 = occ
            if occ.component.name == component_two:
                occ2 = occ

        if not occ1 or not occ2:
            raise RuntimeError("One or both components not found")

        joints = root.joints
        joint_types = {
            "rigid": adsk.fusion.JointTypes.RigidJointType,
            "revolute": adsk.fusion.JointTypes.RevoluteJointType,
            "slider": adsk.fusion.JointTypes.SliderJointType,
            "cylindrical": adsk.fusion.JointTypes.CylindricalJointType,
            "pin_slot": adsk.fusion.JointTypes.PinSlotJointType,
            "planar": adsk.fusion.JointTypes.PlanarJointType,
            "ball": adsk.fusion.JointTypes.BallJointType,
        }

        jt = joint_types.get(joint_type)
        if jt is None:
            raise RuntimeError(f"Unknown joint type: {joint_type}")

        # Create joint geometry from origin points
        origin1 = occ1.component.originConstructionPoint
        origin2 = occ2.component.originConstructionPoint
        geo1 = adsk.fusion.JointGeometry.createByPoint(occ1, origin1)
        geo2 = adsk.fusion.JointGeometry.createByPoint(occ2, origin2)

        inp = joints.createInput(geo1, geo2)
        if joint_type == "rigid":
            inp.setAsRigidJointMotion()
        joints.add(inp)
        return {"created": True, "joint_type": joint_type}

    def create_as_built_joint(
        self, component_one: str, component_two: str, joint_type: str = "rigid"
    ):
        root = self._root()

        occ1 = occ2 = None
        for occ in root.allOccurrences:
            if occ.component.name == component_one:
                occ1 = occ
            if occ.component.name == component_two:
                occ2 = occ

        if not occ1 or not occ2:
            raise RuntimeError("One or both components not found")

        as_built = root.asBuiltJoints
        inp = as_built.createInput(occ1, occ2, None)
        as_built.add(inp)
        return {"created": True, "joint_type": joint_type}

    def create_rigid_group(self, component_names: list, include_children: bool = True):
        root = self._root()
        occs = adsk.core.ObjectCollection.create()

        for name in component_names:
            for occ in root.allOccurrences:
                if occ.component.name == name:
                    occs.add(occ)
                    break

        if occs.count < 2:
            raise RuntimeError("Need at least 2 components for rigid group")

        groups = root.rigidGroups
        groups.add(occs, include_children)
        return {"created": True, "component_count": occs.count}

    # ------------------------------------------------------------------
    # Inspection / Analysis
    # ------------------------------------------------------------------

    def measure_distance(self, entity_one: str, entity_two: str):
        root = self._root()

        def get_entity(name):
            # Try as body
            for i in range(root.bRepBodies.count):
                b = root.bRepBodies.item(i)
                if b.name == name:
                    return b
            # Try as point (x,y,z format)
            if "," in name:
                coords = [float(x.strip()) for x in name.split(",")]
                return adsk.core.Point3D.create(*coords)
            raise RuntimeError(f"Entity '{name}' not found")

        e1 = get_entity(entity_one)
        e2 = get_entity(entity_two)

        measure = self.app.measureManager
        result = measure.measureMinimumDistance(e1, e2)
        return {
            "distance": result.value,
        }

    def measure_angle(self, entity_one: str, entity_two: str):
        root = self._root()

        def get_entity(name):
            for i in range(root.bRepBodies.count):
                b = root.bRepBodies.item(i)
                if b.name == name:
                    return b.faces.item(0)  # First face
            raise RuntimeError(f"Entity '{name}' not found")

        e1 = get_entity(entity_one)
        e2 = get_entity(entity_two)

        measure = self.app.measureManager
        result = measure.measureAngle(e1, e2)
        return {"angle_degrees": math.degrees(result.value)}

    def get_physical_properties(self, body_name: str, accuracy: str = "medium"):
        body = self._body_by_name(body_name)

        accuracy_map = {
            "low": adsk.fusion.CalculationAccuracy.LowCalculationAccuracy,
            "medium": adsk.fusion.CalculationAccuracy.MediumCalculationAccuracy,
            "high": adsk.fusion.CalculationAccuracy.HighCalculationAccuracy,
            "very_high": adsk.fusion.CalculationAccuracy.VeryHighCalculationAccuracy,
        }
        acc = accuracy_map.get(accuracy, accuracy_map["medium"])

        props = body.getPhysicalProperties(acc)
        return {
            "mass": props.mass,
            "volume": props.volume,
            "area": props.area,
            "density": props.density,
            "center_of_mass": [
                props.centerOfMass.x,
                props.centerOfMass.y,
                props.centerOfMass.z,
            ],
        }

    def create_section_analysis(self, plane: str = "yz", offset: float = 0):
        design = self._design()
        # SectionAnalyses is nested under design.analyses
        analyses = design.analyses.sectionAnalyses

        inp = analyses.createInput(
            self._construction_plane(plane),
            float(offset),
        )
        sa = analyses.add(inp)
        return {"created": True, "plane": plane, "offset": offset, "name": sa.name}

    def check_interference(
        self, component_names: list, include_coincident_faces: bool = False
    ):
        root = self._root()
        bodies = adsk.core.ObjectCollection.create()

        for name in component_names:
            for occ in root.allOccurrences:
                if occ.component.name == name:
                    for b in occ.bRepBodies:
                        bodies.add(b)

        if bodies.count < 2:
            raise RuntimeError("Need at least 2 components with bodies")

        interference = root.interfere(bodies, include_coincident_faces)
        results = []
        for i in range(interference.interferenceResultCount):
            result = interference.interferenceResult(i)
            results.append(
                {
                    "body_one": result.entityOne.name,
                    "body_two": result.entityTwo.name,
                    "volume": result.interferenceBody.volume,
                }
            )

        return {"interferences": results, "count": len(results)}

    def point_containment(self, body_name: str, points: list) -> dict:
        """Batch point-in-solid query. Returns inside/outside/on-boundary per point."""
        body = self._body_by_name(body_name)
        results = []
        # Fusion PointContainment enum (confirmed live via adsk.fusion.PointContainment.*):
        # PointInsidePointContainment=0, PointOnPointContainment=1, PointOutsidePointContainment=2
        label_map = {0: "inside", 1: "on_boundary", 2: "outside"}
        for pt in points:
            p3d = adsk.core.Point3D.create(pt[0], pt[1], pt[2])
            val = body.pointContainment(p3d)
            results.append(
                {
                    "point": pt,
                    "containment": label_map.get(int(val), f"unknown({int(val)})"),
                    "raw": int(val),
                }
            )
        return {"body": body_name, "results": results, "count": len(results)}

    def check_solid(self, body_name: str) -> dict:
        """Composite solid validity check: isValid, isSolid, shells, lumps, volume,
        face/edge/vertex counts."""
        body = self._body_by_name(body_name)
        pp = body.physicalProperties
        return {
            "body": body_name,
            "is_valid": body.isValid,
            "is_solid": body.isSolid,
            "shells_count": body.shells.count,
            "lumps_count": body.lumps.count,
            "faces_count": body.faces.count,
            "edges_count": body.edges.count,
            "vertices_count": body.vertices.count,
            "volume_cm3": body.volume,
            "area_cm2": body.area,
            "mass_g": pp.mass * 1000,
            # topology note: V - E + F == 2 only holds for genus-0 solids with no
            # curved edges; through-holes/genus>=1 and curved edges both break it.
            "euler_characteristic": body.vertices.count
            - body.edges.count
            + body.faces.count,
            "health_state": (
                int(body.healthState) if hasattr(body, "healthState") else None
            ),
        }

    def get_cylindrical_faces(self, body_name: str) -> dict:
        """Ground-truth hole/bore radius verification."""
        body = self._body_by_name(body_name)
        faces = []
        for i in range(body.faces.count):
            face = body.faces.item(i)
            geom = face.geometry
            if hasattr(geom, "radius"):
                center = geom.origin if hasattr(geom, "origin") else None
                faces.append(
                    {
                        "face_index": i,
                        "type": type(geom).__name__,
                        "radius_cm": geom.radius,
                        "radius_mm": geom.radius * 10,
                        "center": [center.x, center.y, center.z] if center else None,
                        "area_cm2": face.area,
                    }
                )
        return {"body": body_name, "cylindrical_faces": faces, "count": len(faces)}

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def set_appearance(
        self,
        target_name: str,
        appearance_name: str,
        target_type: str = "body",
        face_index: int = None,
    ):
        # Find appearance in library — try both known library names
        app_lib = self.app.materialLibraries.itemByName("Fusion Appearance Library")
        if app_lib is None:
            app_lib = self.app.materialLibraries.itemByName(
                "Fusion 360 Appearance Library"
            )
        if app_lib is None:
            # Fall back to searching all libraries
            for i in range(self.app.materialLibraries.count):
                lib = self.app.materialLibraries.item(i)
                if lib.appearances.count > 0:
                    app_lib = lib
                    break
        if app_lib is None:
            raise RuntimeError("No appearance library found")

        appearance = None
        for i in range(app_lib.appearances.count):
            app = app_lib.appearances.item(i)
            if app.name == appearance_name:
                appearance = app
                break

        if not appearance:
            raise RuntimeError(f"Appearance '{appearance_name}' not found")

        if target_type == "body":
            body = self._body_by_name(target_name)
            body.appearance = appearance
        elif target_type == "component":
            comp = self._component_by_name(target_name)
            comp.appearance = appearance
        elif target_type == "face":
            body = self._body_by_name(target_name)
            face = body.faces.item(face_index)
            face.appearance = appearance

        return {"applied": True, "target": target_name, "appearance": appearance_name}

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def get_parameters(self):
        design = self._design()
        params = []
        for param in design.userParameters:
            params.append(
                {
                    "name": param.name,
                    "value": param.value,
                    "expression": param.expression,
                    "unit": param.unit,
                    "comment": param.comment,
                }
            )
        return {"parameters": params, "count": len(params)}

    def create_parameter(self, name: str, value: float, unit: str, comment: str = None):
        design = self._design()
        params = design.userParameters
        # Build the ValueInput expression.  When unit is blank (dimensionless)
        # a trailing space in f"{value} {unit}" confuses Fusion's evaluator and
        # silently produces 0.  Strip to keep the expression clean.
        expression = f"{value} {unit}".strip() if unit else str(value)
        params.add(name, adsk.core.ValueInput.createByString(expression), unit, comment or "")
        return {"created": True, "name": name, "value": value, "unit": unit}

    def set_parameter(self, name: str, value: float):
        design = self._design()
        param = design.userParameters.itemByName(name)
        if not param:
            raise RuntimeError(f"Parameter '{name}' not found")
        param.value = value
        return {"updated": True, "name": name, "value": value}

    def delete_parameter(self, name: str):
        design = self._design()
        param = design.userParameters.itemByName(name)
        if not param:
            raise RuntimeError(f"Parameter '{name}' not found")
        param.deleteMe()
        return {"deleted": True, "name": name}

    # ------------------------------------------------------------------
    # Surface Operations
    # ------------------------------------------------------------------

    def patch_surface(
        self, sketch_name: str, profile_index: int = 0, continuity: str = "connected"
    ):
        root = self._root()
        sketch = self._sketch_by_name(sketch_name)

        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        patches = root.features.patchFeatures
        inp = patches.createInput(
            profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation
        )

        sct = adsk.fusion.SurfaceContinuityTypes
        cont_map = {
            "connected": sct.ConnectedSurfaceContinuityType,
            "tangent": sct.TangentSurfaceContinuityType,
            "curvature": sct.CurvatureSurfaceContinuityType,
        }
        inp.boundaryContinuity = cont_map.get(continuity, cont_map["connected"])

        feat = patches.add(inp)
        return {"feature_name": feat.name, "continuity": continuity}

    def stitch_surfaces(self, body_names: list, tolerance: float = 0.01):
        root = self._root()
        bodies = adsk.core.ObjectCollection.create()
        for name in body_names:
            bodies.add(self._body_by_name(name))

        stitches = root.features.stitchFeatures
        inp = stitches.createInput(
            bodies,
            adsk.core.ValueInput.createByReal(tolerance),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        feat = stitches.add(inp)
        return {"feature_name": feat.name, "body_count": len(body_names)}

    def thicken_surface(
        self, body_name: str, thickness: float, direction: str = "symmetric"
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        faces = adsk.core.ObjectCollection.create()
        for face in body.faces:
            faces.add(face)

        thickens = root.features.thickenFeatures
        inp = thickens.createInput(
            faces,
            adsk.core.ValueInput.createByReal(thickness),
            False,
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            direction == "symmetric",
        )
        feat = thickens.add(inp)
        return {"feature_name": feat.name, "thickness": thickness}

    def ruled_surface(
        self,
        body_name: str,
        edge_index: int,
        distance: float = 1.0,
        rule_type: str = "normal",
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        edge = body.edges.item(edge_index)

        ruled = root.features.ruledSurfaceFeatures
        inp = ruled.createInput(edge, adsk.core.ValueInput.createByReal(distance))
        feat = ruled.add(inp)
        return {"feature_name": feat.name, "distance": distance}

    def trim_surface(self, body_name: str, tool_name: str):
        root = self._root()
        body = self._body_by_name(body_name)
        tool = self._body_by_name(tool_name)

        trims = root.features.trimFeatures
        inp = trims.createInput(body, tool)
        feat = trims.add(inp)
        return {"feature_name": feat.name}

    # ------------------------------------------------------------------
    # Sheet Metal
    # ------------------------------------------------------------------

    def create_flange(
        self,
        body_name: str,
        edge_index: int,
        height: float = 1.0,
        angle: float = 90,
        bend_radius: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        edge = body.edges.item(edge_index)

        flanges = root.features.flangeFeatures
        inp = flanges.createInput(edge, True)
        inp.angle = adsk.core.ValueInput.createByString(f"{angle} deg")
        inp.height = adsk.core.ValueInput.createByReal(height)
        if bend_radius:
            inp.bendRadius = adsk.core.ValueInput.createByReal(bend_radius)

        feat = flanges.add(inp)
        return {"feature_name": feat.name, "height": height, "angle": angle}

    def create_bend(
        self,
        body_name: str,
        bend_line_sketch: str = None,
        angle: float = 90,
        bend_radius: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        if bend_line_sketch:
            sketch = self._sketch_by_name(bend_line_sketch)
            bend_line = sketch.sketchCurves.sketchLines.item(0)

            bends = root.features.bendFeatures
            inp = bends.createInput(body, bend_line, True)
            inp.bendAngle = adsk.core.ValueInput.createByString(f"{angle} deg")
            if bend_radius:
                inp.bendRadius = adsk.core.ValueInput.createByReal(bend_radius)

            feat = bends.add(inp)
            return {"feature_name": feat.name, "angle": angle}
        else:
            raise RuntimeError("bend_line_sketch is required")

    def flat_pattern(self, body_name: str):
        root = self._root()
        body = self._body_by_name(body_name)

        flat_patterns = root.features.flatPatternFeatures
        inp = flat_patterns.createInput(body)
        feat = flat_patterns.add(inp)
        return {"feature_name": feat.name}

    def unfold(self, body_name: str, bend_indices: list = None):
        root = self._root()
        body = self._body_by_name(body_name)

        unfolds = root.features.unfoldFeatures

        bends = adsk.core.ObjectCollection.create()
        if bend_indices:
            for idx in bend_indices:
                # Get bend faces from sheet metal body
                bends.add(body.faces.item(idx))
        else:
            # Unfold all bends
            for face in body.faces:
                bends.add(face)

        # Find stationary face (first planar face)
        stationary = None
        for face in body.faces:
            if face.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
                stationary = face
                break

        if not stationary:
            raise RuntimeError("No planar face found for stationary face")

        inp = unfolds.createInput(bends, stationary)
        feat = unfolds.add(inp)
        return {"feature_name": feat.name}

    # ------------------------------------------------------------------
    # CAM
    # ------------------------------------------------------------------

    def _get_cam(self):
        """Get the CAM product from the active document.
        
        The CAM product only exists after the Manufacture workspace has been
        opened at least once in the current session. If it's not found, the
        user must manually switch to the Manufacture workspace in Fusion 360.
        """
        doc = self.app.activeDocument
        # Try both known productType strings
        cam_product = None
        for pt in ("CAMProductType", "adsk::cam::CAM"):
            try:
                cam_product = doc.products.itemByProductType(pt)
                if cam_product:
                    break
            except Exception:
                pass
        if not cam_product:
            raise RuntimeError(
                "CAM product not found. Switch to the Manufacture workspace "
                "in Fusion 360 (Workspace selector → Manufacture) to initialize it, "
                "then retry."
            )
        return cam_product

    def _find_setup(self, cam, name: str):
        for i in range(cam.setups.count):
            s = cam.setups.item(i)
            if s.name == name:
                return s
        raise RuntimeError(f"Setup '{name}' not found")

    def _find_operation(self, setup, name: str):
        for i in range(setup.operations.count):
            op = setup.operations.item(i)
            if op.name == name:
                return op
        raise RuntimeError(f"Operation '{name}' not found in setup '{setup.name}'")

    def cam_list_setups(self):
        cam = self._get_cam()
        result = []
        for i in range(cam.setups.count):
            setup = cam.setups.item(i)
            ops = []
            for j in range(setup.operations.count):
                ops.append(setup.operations.item(j).name)
            result.append(
                {
                    "name": setup.name,
                    "operations": ops,
                    "is_valid": setup.isValid,
                }
            )
        return {"setups": result, "count": len(result)}

    def cam_list_operations(self, setup_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        result = []
        for i in range(setup.operations.count):
            op = setup.operations.item(i)
            result.append(
                {
                    "name": op.name,
                    "has_toolpath": op.hasToolpath,
                    "is_valid": op.isValid,
                }
            )
        return {"setup": setup_name, "operations": result, "count": len(result)}

    def cam_get_operation_info(self, setup_name: str, operation_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation(setup, operation_name)

        info = {
            "name": op.name,
            "is_valid": op.isValid,
            "has_toolpath": op.hasToolpath,
        }

        if hasattr(op, "tool") and op.tool:
            tool = op.tool
            desc = tool.description if hasattr(tool, "description") else str(tool)
            info["tool"] = {"description": desc}

        if hasattr(op, "parameters"):
            params = {}
            for param in op.parameters:
                try:
                    params[param.name] = param.expression
                except Exception:
                    pass
            info["parameters"] = params

        return info

    def cam_create_setup(
        self,
        body_name: str = None,
        name: str = None,
        operation_type: str = "milling",
        stock_mode: str = "relative_box",
        stock_offset_sides: float = 0,
        stock_offset_top: float = 0,
        stock_offset_bottom: float = 0,
    ):
        cam = self._get_cam()

        op_type_map = {
            "milling": adsk.cam.OperationTypes.MillingOperation,
            "turning": adsk.cam.OperationTypes.TurningOperation,
            "cutting": adsk.cam.OperationTypes.JetOperation,
        }
        op_type = op_type_map.get(operation_type)
        if op_type is None:
            raise RuntimeError(
                f"Unknown operation_type '{operation_type}' "
                "— use milling/turning/cutting"
            )

        setup_input = cam.setups.createInput(op_type)
        # Note: CAM setups operate on the full design model, not individual bodies.
        # Fusion defaults to all bodies when models is not set.
        # body_name is accepted for API compatibility but not used here.

        if name:
            setup_input.name = name

        setup = cam.setups.add(setup_input)
        return {"name": setup.name, "body": body_name, "operation_type": operation_type}

    def cam_create_operation(
        self,
        setup_name: str,
        strategy: str,
        name: str = None,
        tool_number: int = None,
        tool_diameter: float = None,
        stepdown: float = None,
        stepover: float = None,
        feed_rate: float = None,
        spindle_speed: float = None,
        coolant: str = "flood",
    ):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)

        op_input = setup.operations.createInput(strategy)
        if name:
            op_input.name = name
        if tool_diameter:
            op_input.toolDiameter = adsk.core.ValueInput.createByReal(tool_diameter)
        if stepdown:
            op_input.maximumStepdown = adsk.core.ValueInput.createByReal(stepdown)
        if stepover:
            op_input.maximumStepover = adsk.core.ValueInput.createByReal(stepover)

        op = setup.operations.add(op_input)
        return {"name": op.name, "setup": setup_name, "strategy": strategy}

    def cam_generate_toolpath(
        self,
        setup_name: str = None,
        operation_name: str = None,
        generate_all: bool = False,
    ):
        cam = self._get_cam()

        if generate_all:
            future = cam.generateAllToolpaths(False)
            import time as _time
            while not future.isGenerationCompleted:
                _time.sleep(0.1)
            return {"generated": True, "scope": "all"}

        if operation_name and setup_name:
            setup = self._find_setup(cam, setup_name)
            op = self._find_operation(setup, operation_name)
            future = cam.generateToolpath(op)
            import time as _time
            while not future.isGenerationCompleted:
                _time.sleep(0.1)
            return {
                "generated": True,
                "scope": "operation",
                "operation": operation_name,
            }

        if setup_name:
            setup = self._find_setup(cam, setup_name)
            ops = adsk.core.ObjectCollection.create()
            for i in range(setup.operations.count):
                ops.add(setup.operations.item(i))
            future = cam.generateToolpath(ops)
            import time as _time
            while not future.isGenerationCompleted:
                _time.sleep(0.1)
            return {"generated": True, "scope": "setup", "setup": setup_name}

        raise RuntimeError("Provide setup_name, operation_name, or generate_all=true")

    def cam_post_process(
        self,
        setup_name: str,
        operation_name: str = None,
        post_processor: str = "fanuc",
        output_folder: str = None,
        output_units: str = "mm",
    ):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)

        if not output_folder:
            output_folder = os.path.join(os.path.expanduser("~"), "Desktop")

        post_config = os.path.join(cam.genericPostFolder, f"{post_processor}.cps")

        units = (
            adsk.cam.PostOutputUnitOptions.MillimetersOutput
            if output_units == "mm"
            else adsk.cam.PostOutputUnitOptions.InchesOutput
        )

        post_input = adsk.cam.PostProcessInput.create(
            setup_name, post_config, output_folder, units
        )
        post_input.isOpenInEditor = False

        if operation_name:
            op = self._find_operation(setup, operation_name)
            cam.postProcess(op, post_input)
        else:
            cam.postProcess(setup, post_input)

        return {
            "setup": setup_name,
            "post_processor": post_processor,
            "output_folder": output_folder,
            "units": output_units,
        }

    # ------------------------------------------------------------------
    # Document management
    # ------------------------------------------------------------------

    def save_document(self, description: str = "") -> dict:
        doc = self.app.activeDocument
        if not doc.isSaved:
            raise RuntimeError(
                "Document has never been saved — use save_as(name, project_name) first."
            )
        doc.save(description)

        cache = hub_cache._load_cache()
        for _pname, pdata in cache.get("projects", {}).items():
            for entry in pdata.get("files", []):
                if entry.get("name") == doc.name:
                    entry["last_modified"] = _now_iso()
                    entry["version"] = int(entry.get("version") or 1) + 1
                    if description:
                        entry["description"] = description
                    break
        cache["last_updated"] = _now_iso()
        hub_cache._save_cache(cache)

        return {"saved": True, "name": doc.name, "description": description}

    def save_as(
        self, name: str, project_name: str = "Pinchy", description: str = ""
    ) -> dict:
        doc = self.app.activeDocument
        hub = self.app.data.activeHub
        # Find project by name (case-sensitive)
        project = None
        for i in range(hub.dataProjects.count):
            p = hub.dataProjects.item(i)
            if p.name == project_name:
                project = p
                break
        if project is None:
            available = [
                hub.dataProjects.item(i).name for i in range(hub.dataProjects.count)
            ]
            raise RuntimeError(
                f"Project '{project_name}' not found. Available: {available}"
            )
        folder = project.rootFolder
        doc.saveAs(name, folder, description, "")

        cache = hub_cache._load_cache()
        hub_cache._upsert_file_entry(
            cache,
            project_name,
            {
                "name": name,
                "description": description,
                "last_modified": _now_iso(),
                "version": 1,
            },
        )
        cache["last_updated"] = _now_iso()
        hub_cache._save_cache(cache)

        return {"saved": True, "name": name, "project": project_name}

    def list_documents(self) -> dict:
        docs = []
        for i in range(self.app.documents.count):
            d = self.app.documents.item(i)
            docs.append(
                {
                    "index": i,
                    "name": d.name,
                    "is_active": d == self.app.activeDocument,
                    "is_saved": d.isSaved,
                }
            )
        return {"documents": docs, "count": len(docs)}

    def set_active_document(self, name: str) -> dict:
        for i in range(self.app.documents.count):
            d = self.app.documents.item(i)
            if d.name == name:
                d.activate()
                return {"activated": True, "name": name}
        available = [
            self.app.documents.item(i).name for i in range(self.app.documents.count)
        ]
        raise RuntimeError(
            f"Document '{name}' not found. Open documents: {available}"
        )

    def new_document(self) -> dict:
        doc = self.app.documents.add(
            adsk.core.DocumentTypes.FusionDesignDocumentType
        )
        doc.activate()
        return {"created": True, "name": doc.name}

    def list_hub_files(
        self, project_name: str = "Pinchy", search: str = None
    ) -> dict:
        """Return saved files for *project_name* from the local cache.

        Lazy crawl: if the requested project has never been indexed (or
        'all' is requested and no projects exist yet), we crawl the
        project(s) right now on the main thread before returning.  The
        result is cached to disk so subsequent calls are instant.

        The Fusion adsk Data API is main-thread-only — background threads
        silently fail.  We're always dispatched through the CustomEvent
        bridge here, so this is safe.
        """
        cache = hub_cache._load_cache()
        projects = cache.get("projects", {})

        # Determine which projects need crawling.
        need_crawl: list[str] = []
        if project_name == "all" or project_name is None:
            if not projects:
                # First ever call — enumerate and crawl everything.
                try:
                    hub = self.app.data.activeHub
                    cache["hub"] = hub.name
                    all_names = [
                        hub.dataProjects.item(i).name
                        for i in range(hub.dataProjects.count)
                    ]
                    # Pinchy first.
                    need_crawl = sorted(
                        all_names, key=lambda n: (n != "Pinchy", n)
                    )
                except Exception:
                    log.error(
                        "list_hub_files: failed to enumerate projects:\n%s",
                        traceback.format_exc(),
                    )
        elif project_name not in projects:
            # Specific project not yet cached.
            try:
                cache["hub"] = self.app.data.activeHub.name
            except Exception:
                pass
            need_crawl = [project_name]

        if need_crawl:
            for name in need_crawl:
                _crawl_project_main_thread(self.app, name, cache)
            cache["last_updated"] = _now_iso()
            hub_cache._save_cache(cache)

        return hub_cache._build_list_response(
            project_name=project_name, search=search
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self):
        return {"pong": True}

    # ------------------------------------------------------------------
    # Design type safety
    # ------------------------------------------------------------------

    def get_design_type(self):
        """Return current design type: 'parametric' or 'direct'."""
        design = self._design()
        dt = design.designType
        is_parametric = dt == adsk.fusion.DesignTypes.ParametricDesignType
        return {
            "design_type": "parametric" if is_parametric else "direct",
            "design_type_id": dt,
        }

    def set_design_type(self, design_type: str):
        """Switch design type. Use 'parametric' to recover from accidental
        direct-mode switches (equivalent to UI 'Capture Design History')."""
        design = self._design()
        current = design.designType

        if design_type == "parametric":
            target = adsk.fusion.DesignTypes.ParametricDesignType
            if current == target:
                return {
                    "changed": False,
                    "design_type": "parametric",
                    "message": "Already in parametric mode",
                }
            design.designType = target
            adsk.doEvents()
            # Verify it actually changed
            if design.designType != target:
                raise RuntimeError(
                    "Failed to switch to parametric mode. "
                    "Try 'Capture Design History' in the Fusion UI."
                )
            return {"changed": True, "design_type": "parametric"}

        elif design_type == "direct":
            target = adsk.fusion.DesignTypes.DirectDesignType
            if current == target:
                return {
                    "changed": False,
                    "design_type": "direct",
                    "message": "Already in direct mode",
                }
            design.designType = target
            adsk.doEvents()
            return {"changed": True, "design_type": "direct"}

        else:
            raise RuntimeError(
                f"Invalid design_type '{design_type}'. Use 'parametric' or 'direct'."
            )

    # ------------------------------------------------------------------
    # Code execution (REPL-style)
    # ------------------------------------------------------------------

    def execute_code(self, code: str):
        design = self._design()
        type_before = design.designType

        ns = {
            "adsk": adsk,
            "app": self.app,
            "ui": self.ui,
            "design": design,
            "component": self._root(),
            "math": math,
        }

        buf = io.StringIO()

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise RuntimeError(f"SyntaxError: {exc}")

        last_expr_value = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_node = tree.body.pop()
            if tree.body:
                with redirect_stdout(buf):
                    exec(
                        compile(
                            ast.Module(body=tree.body, type_ignores=[]), "<mcp>", "exec"
                        ),
                        ns,
                    )
            expr_code = compile(ast.Expression(body=last_node.value), "<mcp>", "eval")
            with redirect_stdout(buf):
                last_expr_value = eval(expr_code, ns)
        else:
            with redirect_stdout(buf):
                exec(compile(tree, "<mcp>", "exec"), ns)

        output = buf.getvalue()
        result = last_expr_value if last_expr_value is not None else output

        # Warn if design type changed during execution
        type_after = design.designType
        design_type_warning = None
        if type_before != type_after:
            design_type_warning = (
                f"WARNING: Design type changed from "
                f"{'parametric' if type_before == 1 else 'direct'} to "
                f"{'parametric' if type_after == 1 else 'direct'} "
                f"during code execution. Use set_design_type to recover."
            )
            log.warning(design_type_warning)
        if result is not None:
            try:
                import json as _json

                _json.dumps(result)
            except (TypeError, ValueError):
                result = str(result)

        response = {"executed": True, "result": result, "output": output}
        if design_type_warning:
            response["design_type_warning"] = design_type_warning
        return response

    # ------------------------------------------------------------------
    # Camera helper
    # ------------------------------------------------------------------

    def _camera_info(self):
        try:
            cam = self.app.activeViewport.camera
            return {
                "eye": [cam.eye.x, cam.eye.y, cam.eye.z],
                "target": [cam.target.x, cam.target.y, cam.target.z],
                "up_vector": [cam.upVector.x, cam.upVector.y, cam.upVector.z],
            }
        except Exception:
            return None

    @staticmethod
    def _bbox_dict(bbox):
        return {
            "min": [bbox.minPoint.x, bbox.minPoint.y, bbox.minPoint.z],
            "max": [bbox.maxPoint.x, bbox.maxPoint.y, bbox.maxPoint.z],
        }

    # ------------------------------------------------------------------
    # Mutation snapshot (before/after deltas for feedback)
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict | None:
        """Capture body_count, overall bbox, and total mass of the design.

        Best-effort — returns None if the design isn't readable yet.  Mass
        is reported in grams; bbox in cm (Fusion's internal unit).
        """
        try:
            design = self.app.activeProduct
            if design is None or not hasattr(design, "rootComponent"):
                return None
            root = design.rootComponent

            # Count bodies recursively (root + occurrences).
            body_count = root.bRepBodies.count
            try:
                for occ in design.rootComponent.allOccurrences:
                    body_count += occ.bRepBodies.count
            except Exception:
                pass  # allOccurrences can fail on empty designs

            bbox_dict = None
            if body_count > 0:
                try:
                    bbox = root.boundingBox
                    if bbox is not None:
                        bbox_dict = self._bbox_dict(bbox)
                except Exception:
                    bbox_dict = None

            mass_g = 0.0
            if body_count > 0:
                try:
                    # physicalProperties.mass is in kg
                    mass_g = float(root.physicalProperties.mass) * 1000.0
                except Exception:
                    mass_g = 0.0

            return {
                "body_count": body_count,
                "bbox": bbox_dict,
                "mass_g": mass_g,
            }
        except Exception as exc:
            log.debug("snapshot failed: %s", exc)
            return None

    @staticmethod
    def _compute_deltas(before: dict, after: dict) -> dict:
        """Return a diff suitable for an agent: counts + masses + bboxes."""
        return {
            "body_count_before": before.get("body_count", 0),
            "body_count_after": after.get("body_count", 0),
            "body_count_delta": after.get("body_count", 0)
            - before.get("body_count", 0),
            "mass_g_before": before.get("mass_g", 0.0),
            "mass_g_after": after.get("mass_g", 0.0),
            "mass_g_delta": after.get("mass_g", 0.0) - before.get("mass_g", 0.0),
            "bbox_before": before.get("bbox"),
            "bbox_after": after.get("bbox"),
        }

    # ------------------------------------------------------------------
    # Viewport render (perception)
    # ------------------------------------------------------------------

    def render_view(
        self,
        view: str = "current",
        width: int = 1024,
        height: int = 768,
        fit: bool = True,
    ):
        """Save the active viewport to a PNG and return base64-encoded bytes.

        * ``view`` — ``"current"`` keeps the existing camera, or one of
          ``_VIEW_DIRS`` keys (iso, front, top, ...) to reposition first.
        * ``width``/``height`` — pixel dimensions.
        * ``fit`` — call viewport.fit() before capture so the model frames.

        If ``view != "current"``, the camera is restored to its prior state
        before returning so the user's view isn't disturbed.
        """
        viewport = self.app.activeViewport
        if viewport is None:
            raise RuntimeError("No active viewport")

        repositioned = view != "current"
        if repositioned:
            spec = self._VIEW_DIRS.get(view)
            if spec is None:
                raise RuntimeError(
                    f"Unknown view '{view}'. "
                    f"Expected: current, {', '.join(self._VIEW_DIRS)}"
                )
            orig = viewport.camera
            orig_state = {
                "eye": (orig.eye.x, orig.eye.y, orig.eye.z),
                "target": (orig.target.x, orig.target.y, orig.target.z),
                "up": (orig.upVector.x, orig.upVector.y, orig.upVector.z),
                "type": orig.cameraType,
            }
            self._orient_camera(viewport, spec)

        try:
            if fit:
                try:
                    viewport.fit()
                except Exception:
                    pass  # fit() can fail on empty designs; keep going

            # saveAsImageFile requires a real path; write to a tempfile.
            fd, path = tempfile.mkstemp(suffix=".png", prefix="fusion_render_")
            os.close(fd)
            try:
                ok = viewport.saveAsImageFile(path, int(width), int(height))
                if not ok or not os.path.exists(path):
                    raise RuntimeError("saveAsImageFile returned false")
                with open(path, "rb") as f:
                    data = f.read()
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
        finally:
            if repositioned:
                cam = viewport.camera
                cam.isSmoothTransition = False
                cam.cameraType = orig_state["type"]
                cam.eye = adsk.core.Point3D.create(*orig_state["eye"])
                cam.target = adsk.core.Point3D.create(*orig_state["target"])
                cam.upVector = adsk.core.Vector3D.create(*orig_state["up"])
                viewport.camera = cam

        return {
            "view": view,
            "width": int(width),
            "height": int(height),
            "image_format": "png",
            "image_base64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
        }

    def _orient_camera(self, viewport, spec):
        """Position the camera at a canonical view relative to the model.

        ``spec`` is ``(eye_dir, up_vec)`` from ``_VIEW_DIRS``.
        """
        eye_dir, up_vec = spec
        design = self.app.activeProduct
        root = design.rootComponent if design is not None else None

        # Target is the model centroid (or origin if no bodies).
        target = adsk.core.Point3D.create(0.0, 0.0, 0.0)
        distance = 20.0
        if root is not None and root.bRepBodies.count > 0:
            try:
                bbox = root.boundingBox
                if bbox is not None:
                    cx = (bbox.minPoint.x + bbox.maxPoint.x) * 0.5
                    cy = (bbox.minPoint.y + bbox.maxPoint.y) * 0.5
                    cz = (bbox.minPoint.z + bbox.maxPoint.z) * 0.5
                    target = adsk.core.Point3D.create(cx, cy, cz)
                    dx = bbox.maxPoint.x - bbox.minPoint.x
                    dy = bbox.maxPoint.y - bbox.minPoint.y
                    dz = bbox.maxPoint.z - bbox.minPoint.z
                    distance = max(dx, dy, dz, 1.0) * 2.5
            except Exception:
                pass

        eye = adsk.core.Point3D.create(
            target.x + eye_dir[0] * distance,
            target.y + eye_dir[1] * distance,
            target.z + eye_dir[2] * distance,
        )
        up = adsk.core.Vector3D.create(up_vec[0], up_vec[1], up_vec[2])

        cam = viewport.camera
        cam.eye = eye
        cam.target = target
        cam.upVector = up
        cam.isSmoothTransition = False
        viewport.camera = cam
