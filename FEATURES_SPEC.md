# New Tool Spec — snipa/new-features branch

Requested by Pinchy (2026-08-06). Implement in priority order.
All new tools need: handler method in `addon/server/command_handler.py`, entry in the dispatch
dict, schema entry in `src/fusion360_mcp/tools.py`, and tests in `tests/`.

---

## P1: `point_containment(body_name, points: list[[x,y,z]])`

Batch point-in-solid query. Returns inside/outside/on-boundary per point.

**Handler:**
```python
def point_containment(self, body_name: str, points: list) -> dict:
    body = self._body_by_name(body_name)
    results = []
    # containment enum: 0=outside, 1=inside, 2=on_boundary
    label_map = {0: "outside", 1: "inside", 2: "on_boundary"}
    for pt in points:
        p3d = adsk.core.Point3D.create(pt[0], pt[1], pt[2])
        val = body.pointContainment(p3d)
        results.append({
            "point": pt,
            "containment": label_map.get(int(val), f"unknown({int(val)})"),
            "raw": int(val),
        })
    return {"body": body_name, "results": results, "count": len(results)}
```

**Schema:** `body_name: string (required)`, `points: array of [x,y,z] arrays (required)`

---

## P2: `check_solid(body_name)`

Composite solid validity check: isValid, isSolid, shells, lumps, volume, face/edge/vertex counts.

**Handler:**
```python
def check_solid(self, body_name: str) -> dict:
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
        "euler_characteristic": body.vertices.count - body.edges.count + body.faces.count,
        "health_state": int(body.healthState) if hasattr(body, "healthState") else None,
    }
```

**Schema:** `body_name: string (required)`

---

## P3: `save_document(description="")` / `save_as(name, project_name="Pinchy", description="")`

Wrap the save/saveAs flow including the "never-saved doc must use saveAs first" gate.

**Handler sketch:**
```python
def save_document(self, description: str = "") -> dict:
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if not doc.isSaved:
        raise RuntimeError(
            "Document has never been saved — use save_as(name, project_name) first."
        )
    doc.save(description)
    return {"saved": True, "name": doc.name, "description": description}

def save_as(self, name: str, project_name: str = "Pinchy", description: str = "") -> dict:
    app = adsk.core.Application.get()
    doc = app.activeDocument
    hub = app.data.activeHub
    # Find project by name (case-sensitive)
    project = None
    for i in range(hub.dataProjects.count):
        p = hub.dataProjects.item(i)
        if p.name == project_name:
            project = p
            break
    if project is None:
        available = [hub.dataProjects.item(i).name for i in range(hub.dataProjects.count)]
        raise RuntimeError(
            f"Project '{project_name}' not found. Available: {available}"
        )
    folder = project.rootFolder
    doc.saveAs(name, folder, description, "")
    return {"saved": True, "name": name, "project": project_name}
```

**Schema:** `save_document`: `description: string (optional)`. `save_as`: `name: string (required)`, `project_name: string (default "Pinchy")`, `description: string (optional)`

---

## P4: `list_documents()` / `set_active_document(name)`

Enumerate open documents and switch active one.

**Handler sketch:**
```python
def list_documents(self) -> dict:
    app = adsk.core.Application.get()
    docs = []
    for i in range(app.documents.count):
        d = app.documents.item(i)
        docs.append({
            "index": i,
            "name": d.name,
            "is_active": d == app.activeDocument,
            "is_saved": d.isSaved,
        })
    return {"documents": docs, "count": len(docs)}

def set_active_document(self, name: str) -> dict:
    app = adsk.core.Application.get()
    for i in range(app.documents.count):
        d = app.documents.item(i)
        if d.name == name:
            d.activate()
            return {"activated": True, "name": name}
    available = [app.documents.item(i).name for i in range(app.documents.count)]
    raise RuntimeError(f"Document '{name}' not found. Open documents: {available}")
```

**Schema:** `list_documents`: no params. `set_active_document`: `name: string (required)`

---

## P2b: `set_sketch_visibility(sketch_name, visible)` / `hide_all_sketches()`

```python
def set_sketch_visibility(self, sketch_name: str, visible: bool) -> dict:
    root = self._root()
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
```

---

## P2c: `get_cylindrical_faces(body_name)`

Ground-truth hole/bore radius verification.

```python
def get_cylindrical_faces(self, body_name: str) -> dict:
    body = self._body_by_name(body_name)
    faces = []
    for i in range(body.faces.count):
        face = body.faces.item(i)
        geom = face.geometry
        if hasattr(geom, 'radius'):
            center = geom.origin if hasattr(geom, 'origin') else None
            faces.append({
                "face_index": i,
                "type": type(geom).__name__,
                "radius_cm": geom.radius,
                "radius_mm": geom.radius * 10,
                "center": [center.x, center.y, center.z] if center else None,
                "area_cm2": face.area,
            })
    return {"body": body_name, "cylindrical_faces": faces, "count": len(faces)}
```

---

## P3b: `new_document()`

```python
def new_document(self) -> dict:
    app = adsk.core.Application.get()
    doc = app.documents.add(adsk.fusion.FusionDocumentTypes.ParametricSolidDesignType)
    doc.activate()
    return {"created": True, "name": doc.name}
```

---

## Implementation notes

- All new tools go in the dispatch dict under the correct category comment block
- Tools that mutate state go in `_MUTATION_COMMANDS` set so deltas are captured
- `save_document` / `save_as` / `set_active_document` / `new_document` are mutations
- `point_containment`, `check_solid`, `list_documents`, `get_cylindrical_faces` are read-only
- Tests: use the existing mock pattern in `tests/` — no live Fusion needed
- Push to `origin snipa/new-features` (NOT main) when done — these go through a separate review before merging to the bug-fix line
