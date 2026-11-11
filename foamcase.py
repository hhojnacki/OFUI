"""
foamcase - edit an existing OpenFOAM case in place (works on the case files, no geometry needed).

A Case keeps the files you touch in memory as FoamDict objects; nothing is written until
save(), which first copies every file it is about to change into
<case>/.stl2foam_backup/<timestamp>/. Edits replace only the text they concern, so comments,
layout and entries this module does not know about are preserved.

The functions below are what the editor GUI (case_editor_wx.py) calls; they have no GUI
dependencies and can be scripted.
"""
import math
import re
from pathlib import Path
from typing import NamedTuple

from foamdict import FoamDict
from stl2foam_core import FIELD_DEFS, Settings, backup_files, bcs, fmt, guess_role, hdr

NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"
CONSTRAINT_TYPES = {"empty", "wedge", "cyclic", "cyclicAMI", "cyclicSlip", "symmetry",
                    "symmetryPlane", "processor", "processorCyclic", "nonConformalCyclic"}


def floats(text):
    return [float(x) for x in re.findall(NUM, text or "")]


def one_line(text):
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
#  Case: file cache with backup-on-save
# --------------------------------------------------------------------------- #
class Case:
    def __init__(self, path):
        self.path = Path(path)
        self._docs, self._orig = {}, {}

    def is_case(self):
        return (self.path / "system").is_dir()

    def exists(self, rel):
        return rel in self._docs or (self.path / rel).is_file()

    def doc(self, rel):
        if rel not in self._docs:
            p = self.path / rel
            if not p.is_file():
                return None
            text = p.read_text(errors="replace")
            self._docs[rel], self._orig[rel] = FoamDict(text), text
        return self._docs[rel]

    def get(self, rel, path):
        d = self.doc(rel)
        return d.get(path) if d else None

    def has(self, rel, path):
        d = self.doc(rel)
        return bool(d and d.has(path))

    def set(self, rel, path, value):
        d = self.doc(rel)
        if d is None:
            raise FileNotFoundError(rel)
        d.set(path, value)

    def set_body(self, rel, path, body):
        d = self.doc(rel)
        if d is None:
            raise FileNotFoundError(rel)
        d.set_body(path, body)

    def delete(self, rel, path):
        d = self.doc(rel)
        return bool(d and d.delete(path))

    def create(self, rel, text):
        self._docs[rel], self._orig[rel] = FoamDict(text), None

    def changed(self):
        return sorted(r for r, d in self._docs.items() if d.text != self._orig.get(r))

    def externally_modified(self):
        """Changed files whose on-disk content is no longer what was loaded."""
        out = []
        for r in self.changed():
            p, orig = self.path / r, self._orig.get(r)
            if orig is not None and p.is_file() and p.read_text(errors="replace") != orig:
                out.append(r)
        return out

    def save(self):
        """Write changed files (backing up the originals). Returns (files, backup_dir)."""
        ch = self.changed()
        backup = backup_files(self.path, ch)
        for rel in ch:
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(self._docs[rel].text)
            tmp.replace(p)
            self._orig[rel] = self._docs[rel].text
        return ch, backup

    def reload(self):
        self._docs.clear()
        self._orig.clear()


# --------------------------------------------------------------------------- #
#  Generic: flatten a dictionary into editable leaf paths
# --------------------------------------------------------------------------- #
def flatten(case, rel, skip=("FoamFile",), maxdepth=6):
    """Paths of all leaf entries of a file (nested dictionaries are walked)."""
    d = case.doc(rel)
    out = []
    if d is None:
        return out

    def walk(path, depth):
        for n in d.nodes(path):
            if not path and n.key in skip:
                continue
            p = path + [n.key]
            if n.kind == "dict":
                if depth < maxdepth:
                    walk(p, depth + 1)
            else:
                out.append(p)

    walk([], 0)
    return out


CHOICES = {
    "startFrom": ["startTime", "latestTime", "firstTime"],
    "stopAt": ["endTime", "writeNow", "noWriteNow", "nextWrite"],
    "writeControl": ["timeStep", "runTime", "adjustableRunTime", "clockTime", "cpuTime"],
    "writeFormat": ["ascii", "binary"], "timeFormat": ["fixed", "scientific", "general"],
    "runTimeModifiable": ["yes", "no"], "adjustTimeStep": ["yes", "no"],
    "writeCompression": ["off", "on"], "consistent": ["yes", "no"],
    "application": ["simpleFoam", "pimpleFoam", "pisoFoam", "icoFoam", "potentialFoam",
                    "interFoam", "rhoSimpleFoam", "rhoPimpleFoam", "buoyantFoam"],
    "solver": ["PCG", "PBiCGStab", "GAMG", "smoothSolver", "PBiCG", "diagonal"],
    "smoother": ["GaussSeidel", "symGaussSeidel", "DIC", "DILU", "DICGaussSeidel", "FDIC"],
    "preconditioner": ["DIC", "DILU", "FDIC", "GAMG", "none"],
    "transportModel": ["Newtonian", "CrossPowerLaw", "BirdCarreau", "HerschelBulkley", "Casson"],
    "simulationType": ["laminar", "RAS", "LES"], "turbulence": ["on", "off"],
    "printCoeffs": ["on", "off"],
    "method": ["simple", "hierarchical", "scotch", "metis", "manual", "multiLevel"],
    "castellatedMesh": ["true", "false"], "snap": ["true", "false"], "addLayers": ["true", "false"],
    "implicitFeatureSnap": ["true", "false"], "explicitFeatureSnap": ["true", "false"],
    "relativeSizes": ["true", "false"], "allowFreeStandingZoneFaces": ["true", "false"],
    "order": ["xyz", "xzy", "yxz", "yzx", "zxy", "zyx"],
}

def choices_for(rel, path):
    """Suggested values for an entry (None if there is no list for it)."""
    key = path[-1]
    if key == "method" and rel != "system/decomposeParDict":
        return None                       # e.g. wallDist/method has different values
    if key == "solver" and rel != "system/fvSolution":
        return None
    return CHOICES.get(key)


TURB_MODELS = {
    "kEpsilon": ["k", "epsilon", "nut"], "realizableKE": ["k", "epsilon", "nut"],
    "RNGkEpsilon": ["k", "epsilon", "nut"], "kOmega": ["k", "omega", "nut"],
    "kOmegaSST": ["k", "omega", "nut"],
}
for _m in TURB_MODELS:
    CHOICES.setdefault("model", []).append(_m)

# --------------------------------------------------------------------------- #
#  Mesh: background block + snappyHexMesh
# --------------------------------------------------------------------------- #
BLOCKMESH = "system/blockMeshDict"
SNAPPY = "system/snappyHexMeshDict"
_RS = ["castellatedMeshControls", "refinementSurfaces"]


class Background(NamedTuple):
    counts: tuple     # (nx, ny, nz)
    lo: tuple
    hi: tuple
    ext: tuple        # domain extents in metres
    editable: bool    # False if the file has several blocks (edit by hand)


def get_background(case):
    d = case.doc(BLOCKMESH)
    if d is None:
        return None
    vt, bl = d.get(["vertices"]), d.get(["blocks"])
    if vt is None or bl is None:
        return None
    pts = [tuple(float(x) for x in m) for m in
           re.findall(rf"\(\s*({NUM})\s+({NUM})\s+({NUM})\s*\)", vt)]
    hexes = list(re.finditer(rf"hex\s*\([^)]*\)\s*(?:\w+\s*)?\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)", bl))
    if not pts or not hexes:
        return None
    sc = (floats(d.get(["convertToMeters"])) or floats(d.get(["scale"])) or [1.0])[0]
    lo = tuple(min(p[i] for p in pts) * sc for i in range(3))
    hi = tuple(max(p[i] for p in pts) * sc for i in range(3))
    counts = tuple(int(x) for x in hexes[0].groups())
    return Background(counts, lo, hi, tuple(h - l for l, h in zip(lo, hi)), len(hexes) == 1)


def set_background_counts(case, counts):
    d = case.doc(BLOCKMESH)
    bl = d.get(["blocks"])
    m = re.search(r"hex\s*\([^)]*\)\s*(?:\w+\s*)?\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)", bl)
    new, last = "", 0
    for g, n in zip((1, 2, 3), counts):
        new += bl[last:m.start(g)] + str(int(n))
        last = m.end(g)
    case.set(BLOCKMESH, ["blocks"], new + bl[last:])


def counts_for_size(ext, size):
    return tuple(max(1, math.ceil(e / size - 1e-9)) for e in ext)


class SurfaceRow(NamedTuple):
    name: str
    lmin: object      # int or None
    lmax: object
    layers: object    # int or None


def surfaces(case):
    d = case.doc(SNAPPY)
    if d is None:
        return []
    rows = []
    for n in d.nodes(_RS):
        if n.kind != "dict":
            continue
        lv = re.findall(r"\d+", d.get(_RS + [n.key, "level"]) or "")
        ly = d.get(["addLayersControls", "layers", n.key, "nSurfaceLayers"])
        rows.append(SurfaceRow(n.key, int(lv[0]) if len(lv) >= 2 else None,
                               int(lv[1]) if len(lv) >= 2 else None,
                               int(ly) if ly and ly.strip().isdigit() else None))
    return rows


def set_surface_levels(case, name, lmin, lmax):
    lmax = max(lmin, lmax)
    case.set(SNAPPY, _RS + [name, "level"], f"({lmin} {lmax})")


def set_surface_layers(case, name, n):
    if n and n > 0:
        case.set(SNAPPY, ["addLayersControls", "layers", name, "nSurfaceLayers"], str(int(n)))
    else:
        case.delete(SNAPPY, ["addLayersControls", "layers", name])


class RegionRow(NamedTuple):
    surface: str       # refinementSurfaces entry (geometry name)
    region: str        # region name as it appears in the surface (STL solid name)
    patch: str         # resulting patch name: geometry alias, else <surface>_<region>
    alias: bool        # True if the patch name comes from geometry/<surface>/regions/<region>/name
    lmin: object       # explicit level override (None = inherits the surface level)
    lmax: object
    source: str        # where the region was found: geometry / refinementSurfaces / STL, joined by '+'


def _unq(v):
    return (v or "").strip().strip('"')


def stl_region_names(case, fname, limit=50_000_000):
    """Solid names in an ASCII STL under constant/triSurface (empty for binary, missing or huge files)."""
    p = case.path / "constant" / "triSurface" / fname
    try:
        if not p.is_file() or p.stat().st_size > limit:
            return []
        with open(p, "rb") as fh:
            head = fh.read(1024)
            if not head.lstrip().startswith(b"solid") or b"facet" not in head:
                return []
            fh.seek(0)
            names = []
            for line in fh:
                m = re.match(rb"\s*solid\s+(\S+)", line)
                if m:
                    names.append(m.group(1).decode("ascii", "replace"))
        return list(dict.fromkeys(names))
    except OSError:
        return []


def regions(case):
    """Regions of every refined surface: the keys of geometry/<surface>/regions and of
    refinementSurfaces/<surface>/regions, plus the solid names of a multi-solid STL."""
    d = case.doc(SNAPPY)
    if d is None:
        return []
    geom = {}
    for n in d.nodes(["geometry"]):
        if n.kind == "dict":
            geom[_unq(d.get(["geometry", n.key, "name"])) or n.key] = (n.key, _unq(d.get(["geometry", n.key, "file"])) or n.key)
    out = []
    for surf in surfaces(case):
        gkey, fname = geom.get(surf.name, (surf.name, None))
        found = {}
        for n in d.nodes(["geometry", gkey, "regions"]):
            if n.kind == "dict":
                found.setdefault(n.key, []).append("geometry")
        for n in d.nodes(_RS + [surf.name, "regions"]):
            if n.kind == "dict":
                found.setdefault(n.key, []).append("refinementSurfaces")
        solids = stl_region_names(case, fname) if fname else []
        if len(solids) > 1:                                  # one solid = the surface itself
            for r in solids:
                found.setdefault(r, []).append("STL")
        for r, tags in found.items():
            alias = _unq(d.get(["geometry", gkey, "regions", r, "name"]))
            lv = re.findall(r"\d+", d.get(_RS + [surf.name, "regions", r, "level"]) or "")
            out.append(RegionRow(surf.name, r, alias or f"{surf.name}_{r}", bool(alias),
                                 int(lv[0]) if len(lv) >= 2 else None, int(lv[1]) if len(lv) >= 2 else None,
                                 "+".join(tags)))
    return out


def set_region_levels(case, surface, region, lmin, lmax):
    """Give a region its own refinement level (overrides the surface level)."""
    case.set(SNAPPY, _RS + [surface, "regions", region, "level"], f"({lmin} {max(lmin, lmax)})")


def clear_region_levels(case, surface, region):
    """Remove a region's level override (it inherits the surface level again). Empty
    dictionaries left behind are removed; other entries (patchInfo ...) are kept."""
    base = _RS + [surface, "regions"]
    d = case.doc(SNAPPY)
    case.delete(SNAPPY, base + [region, "level"])
    if d.kind(base + [region]) == "dict" and not d.nodes(base + [region]):
        case.delete(SNAPPY, base + [region])
    if d.kind(base) == "dict" and not d.nodes(base):
        case.delete(SNAPPY, base)


# --------------------------------------------------------------------------- #
#  Geometry of any type (triSurfaceMesh, searchableBox, searchableCylinder, ...) and
#  volume refinement (castellatedMeshControls/refinementRegions)
# --------------------------------------------------------------------------- #
_RR = ["castellatedMeshControls", "refinementRegions"]
_PAIR = rf"\(\s*({NUM})\s+(\d+)\s*\)"
DEFAULT_DISTANCE = "1E15"          # distance token OpenFOAM tutorials use for inside/outside


class VolumeRow(NamedTuple):
    name: str          # geometry name
    type: str          # type of that geometry ("" if it is not in the geometry dictionary)
    mode: str          # inside / outside / distance
    pairs: list        # [(distance text, level), ...]
    raw: str           # the levels entry as written
    parsed: bool       # False if `raw` is not a list of (distance level) pairs


class GeomRow(NamedTuple):
    name: str          # name used in refinementSurfaces / refinementRegions
    key: str           # key of the entry in the geometry dictionary (may be e.g. "wing.stl")
    type: str
    params: str        # one-line summary of the geometry's own parameters
    surface: object    # SurfaceRow if it has a refinementSurfaces entry, else None
    volume: object     # VolumeRow if it has a refinementRegions entry, else None


def _geom_entries(case):
    d = case.doc(SNAPPY)
    out = []
    for n in (d.nodes(["geometry"]) if d else []):
        if n.kind == "dict":
            out.append((n.key, _unq(d.get(["geometry", n.key, "name"])) or n.key,
                        _unq(d.get(["geometry", n.key, "type"]))))
    return out


def geometry_params(case, key):
    d = case.doc(SNAPPY)
    return "; ".join(one_line(f"{n.key} {d.text[n.vs:n.ve]}") for n in d.nodes(["geometry", key])
                     if n.kind == "entry" and n.key not in ("type", "name"))


def volumes(case):
    """Volume refinements (refinementRegions) of any geometry type."""
    d = case.doc(SNAPPY)
    if d is None:
        return []
    types = {name: typ for _, name, typ in _geom_entries(case)}
    out = []
    for n in d.nodes(_RR):
        if n.kind != "dict":
            continue
        raw = one_line(d.get(_RR + [n.key, "levels"]) or "")
        pairs = [(a, int(b)) for a, b in re.findall(_PAIR, raw)]
        parsed = bool(re.fullmatch(rf"\(\s*(?:{_PAIR}\s*)+\)", raw))
        out.append(VolumeRow(n.key, types.get(n.key, ""), _unq(d.get(_RR + [n.key, "mode"])), pairs, raw, parsed))
    return out


def geometry(case):
    surfs = {s.name: s for s in surfaces(case)}
    vols = {v.name: v for v in volumes(case)}
    return [GeomRow(name, key, typ, geometry_params(case, key), surfs.get(name), vols.get(name))
            for key, name, typ in _geom_entries(case)]


def parse_pairs(text):
    """'0.1 4, 0.5 3' / '(0.1 4) (0.5 3)' / '0.1:4' -> [('0.1', 4), ('0.5', 3)]."""
    toks = [t for t in re.split(r"[\s,:;()]+", text.strip()) if t]
    if not toks or len(toks) % 2:
        raise ValueError("Enter pairs of 'distance level', e.g.  0.1 4, 0.5 3")
    out = []
    for dist, lev in zip(toks[0::2], toks[1::2]):
        try:
            float(dist)
        except ValueError:
            raise ValueError(f"'{dist}' is not a distance") from None
        if not lev.isdigit():
            raise ValueError(f"Level '{lev}' must be a whole number")
        out.append((dist, int(lev)))
    return out


def format_pairs(pairs):
    return "(" + " ".join(f"({d} {l})" for d, l in pairs) + ")"


def set_volume_mode(case, name, mode):
    case.set(SNAPPY, _RR + [name, "mode"], mode)


def set_volume_pairs(case, name, pairs):
    case.set(SNAPPY, _RR + [name, "levels"], format_pairs(pairs))


def set_volume_level(case, name, level):
    """Single refinement level for an inside/outside region (keeps the distance token in use)."""
    cur = next((v for v in volumes(case) if v.name == name), None)
    dist = cur.pairs[0][0] if cur and cur.pairs else DEFAULT_DISTANCE
    set_volume_pairs(case, name, [(dist, int(level))])


def add_volume(case, name, level, mode="inside"):
    case.set(SNAPPY, _RR + [name, "mode"], mode)
    set_volume_pairs(case, name, [(DEFAULT_DISTANCE, int(level))])


def remove_volume(case, name):
    return case.delete(SNAPPY, _RR + [name])


def remove_surface_refinement(case, name):
    return case.delete(SNAPPY, _RS + [name])


def set_geometry_params(case, key, text):
    """Apply 'param value; param value' to geometry/<key>. type and name are not editable here."""
    for e in split_entries(text):
        parts = e.split(None, 1)
        if len(parts) < 2:
            raise ValueError(f"'{e}' needs a value:  parameter value")
        if parts[0] in ("type", "name"):
            raise ValueError("type and name cannot be changed here (other entries refer to them).")
        case.set(SNAPPY, ["geometry", key, parts[0]], parts[1].strip())


MESH_SPECS = [  # (label, file, path)
    ("Run castellated mesh step", SNAPPY, ["castellatedMesh"]),
    ("Run snap step", SNAPPY, ["snap"]),
    ("Run add-layers step", SNAPPY, ["addLayers"]),
    ("locationInMesh (x y z)", SNAPPY, ["castellatedMeshControls", "locationInMesh"]),
    ("maxLocalCells", SNAPPY, ["castellatedMeshControls", "maxLocalCells"]),
    ("maxGlobalCells", SNAPPY, ["castellatedMeshControls", "maxGlobalCells"]),
    ("minRefinementCells", SNAPPY, ["castellatedMeshControls", "minRefinementCells"]),
    ("nCellsBetweenLevels", SNAPPY, ["castellatedMeshControls", "nCellsBetweenLevels"]),
    ("resolveFeatureAngle", SNAPPY, ["castellatedMeshControls", "resolveFeatureAngle"]),
    ("snap: nSmoothPatch", SNAPPY, ["snapControls", "nSmoothPatch"]),
    ("snap: tolerance", SNAPPY, ["snapControls", "tolerance"]),
    ("snap: nSolveIter", SNAPPY, ["snapControls", "nSolveIter"]),
    ("snap: nRelaxIter", SNAPPY, ["snapControls", "nRelaxIter"]),
    ("snap: nFeatureSnapIter", SNAPPY, ["snapControls", "nFeatureSnapIter"]),
    ("layers: relativeSizes", SNAPPY, ["addLayersControls", "relativeSizes"]),
    ("layers: expansionRatio", SNAPPY, ["addLayersControls", "expansionRatio"]),
    ("layers: finalLayerThickness", SNAPPY, ["addLayersControls", "finalLayerThickness"]),
    ("layers: minThickness", SNAPPY, ["addLayersControls", "minThickness"]),
    ("layers: featureAngle", SNAPPY, ["addLayersControls", "featureAngle"]),
]

# --------------------------------------------------------------------------- #
#  Material
# --------------------------------------------------------------------------- #
def material_file(case):
    for rel in ("constant/transportProperties", "constant/physicalProperties",
                "constant/thermophysicalProperties"):
        if case.exists(rel):
            return rel
    return None


# --------------------------------------------------------------------------- #
#  Patches, fields, boundary conditions
# --------------------------------------------------------------------------- #
def mesh_patches(case):
    """[(name, type)] from constant/polyMesh/boundary; falls back to the 0/ files."""
    out = []
    d = case.doc("constant/polyMesh/boundary")
    if d:
        for n in d.nodes():
            if n.key == "FoamFile":
                continue
            if n.kind == "dict":
                out.append((n.key, d.get([n.key, "type"]) or "patch"))
            elif n.kind == "entry" and n.key.isdigit():
                v = (d.get([n.key]) or "").strip()
                if v.startswith("("):
                    sub = FoamDict(v[1:-1] if v.endswith(")") else v[1:])
                    out += [(m.key, sub.get([m.key, "type"]) or "patch")
                            for m in sub.nodes() if m.kind == "dict"]
    if out:
        return out
    for f in zero_fields(case):
        names = [n.key for n in case.doc(f"0/{f}").nodes(["boundaryField"])
                 if n.kind == "dict" and not re.search(r"[|*\[\]()^$\\]", n.key)]
        if names:
            return [(n, "patch") for n in names]
    return []


def zero_fields(case):
    names = set()
    z = case.path / "0"
    if z.is_dir():
        names |= {p.name for p in z.iterdir() if p.is_file() and "." not in p.name}
    names |= {r[2:] for r in case._docs if r.startswith("0/") and "/" not in r[2:]}
    out = [n for n in names if case.doc(f"0/{n}") and case.doc(f"0/{n}").kind(["boundaryField"]) == "dict"]
    return sorted(out, key=lambda n: (n not in ("U", "p"), ["U", "p"].index(n) if n in ("U", "p") else 0, n))


BC_TYPES = {
    "U": ["fixedValue", "zeroGradient", "noSlip", "slip", "inletOutlet", "symmetry", "symmetryPlane",
          "flowRateInletVelocity", "pressureInletOutletVelocity", "freestream", "empty", "cyclic"],
    "p": ["fixedValue", "zeroGradient", "totalPressure", "freestreamPressure", "fixedFluxPressure",
          "symmetry", "symmetryPlane", "empty", "cyclic"],
    "k": ["fixedValue", "zeroGradient", "inletOutlet", "kqRWallFunction", "freestream",
          "symmetry", "symmetryPlane", "empty", "cyclic"],
    "epsilon": ["fixedValue", "zeroGradient", "inletOutlet", "epsilonWallFunction", "freestream",
                "symmetry", "symmetryPlane", "empty", "cyclic"],
    "omega": ["fixedValue", "zeroGradient", "inletOutlet", "omegaWallFunction", "freestream",
              "symmetry", "symmetryPlane", "empty", "cyclic"],
    "nut": ["calculated", "nutkWallFunction", "nutUSpaldingWallFunction", "nutkRoughWallFunction",
            "nutLowReWallFunction", "zeroGradient", "symmetry", "symmetryPlane", "empty", "cyclic"],
}
_GENERIC_TYPES = ["fixedValue", "zeroGradient", "calculated", "inletOutlet", "symmetry",
                  "symmetryPlane", "empty", "cyclic"]
NEEDS_VALUE = {"fixedValue", "calculated", "kqRWallFunction", "epsilonWallFunction",
               "omegaWallFunction", "nutkWallFunction", "nutUSpaldingWallFunction",
               "nutkRoughWallFunction", "nutLowReWallFunction", "inletOutlet", "outletInlet"}
NEEDS_OTHER = {"inletOutlet": "inletValue", "outletInlet": "outletValue"}


def bc_types(field):
    return BC_TYPES.get(field, _GENERIC_TYPES)


def split_entries(text):
    """Split 'a 1; b (1 2)' on top-level semicolons."""
    out, depth, cur, quoted = [], 0, [], False
    for ch in text or "":
        if ch == '"':
            quoted = not quoted
        if not quoted:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == ";" and depth == 0:
                out.append("".join(cur).strip())
                cur = []
                continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return [e for e in out if e]


class BcRow(NamedTuple):
    patch: str
    patch_type: str
    bc_type: str
    params: str
    editable: bool
    complex: bool     # BC has sub-dictionaries: parameters cannot be edited as one line
    note: str


def bc_rows(case, field):
    rel = f"0/{field}"
    d = case.doc(rel)
    rows = []
    for patch, ptype in mesh_patches(case):
        path = ["boundaryField", patch]
        if d is not None and d.kind(path) == "dict":
            parts, cplx = [], False
            for n in d.nodes(path):
                if n.key == "type":
                    continue
                if n.kind == "dict":
                    cplx = True
                    parts.append(f"{n.key} {{...}}")
                else:
                    parts.append(one_line(f"{n.key} {d.text[n.vs:n.ve]}"))
            rows.append(BcRow(patch, ptype, (d.get(path + ["type"]) or "").strip(), "; ".join(parts),
                              True, cplx, ""))
            continue
        via = None
        for n in (d.nodes(["boundaryField"]) if d else []):
            try:
                if n.kind == "dict" and re.fullmatch(n.key, patch):
                    via = n.key
                    break
            except re.error:
                pass
        if via:
            rows.append(BcRow(patch, ptype, (d.get(["boundaryField", via, "type"]) or "").strip(), "",
                              False, False, f'set by pattern "{via}"'))
        else:
            rows.append(BcRow(patch, ptype, "", "", True, False, "not defined"))
    return rows


def _default_value(case, field):
    d = case.doc(f"0/{field}")
    t = one_line(d.get(["internalField"])) if d else ""
    if t.startswith("uniform "):
        return t
    cls = (d.get(["FoamFile", "class"]) if d else "") or ""
    return "uniform (0 0 0)" if "Vector" in cls else "uniform 0"


def _ensure_required(case, field, patch):
    rel, path = f"0/{field}", ["boundaryField", patch]
    t = (case.get(rel, path + ["type"]) or "").strip()
    if t in NEEDS_VALUE and not case.has(rel, path + ["value"]):
        case.set(rel, path + ["value"], _default_value(case, field))
    other = NEEDS_OTHER.get(t)
    if other and not case.has(rel, path + [other]):
        case.set(rel, path + [other], _default_value(case, field))


def set_bc_type(case, field, patch, new_type):
    rel, path = f"0/{field}", ["boundaryField", patch]
    if case.doc(rel).kind(path) == "dict":
        case.set(rel, path + ["type"], new_type)
    else:
        case.set_body(rel, path, f"\n        type {new_type};\n    ")
    _ensure_required(case, field, patch)


def set_bc_params(case, field, patch, params):
    rel, path = f"0/{field}", ["boundaryField", patch]
    t = (case.get(rel, path + ["type"]) or "").strip()
    if not t:
        raise ValueError("Choose a BC type first.")
    ents = [e for e in split_entries(params) if e.split()[0] != "type"]
    case.set_body(rel, path, f"\n        type {t};\n" + "".join(f"        {e};\n" for e in ents) + "    ")
    _ensure_required(case, field, patch)


# ---- role presets -----------------------------------------------------------
def infer_role(case, patch, ptype=""):
    ub = ["boundaryField", patch]
    ut = (case.get("0/U", ub + ["type"]) or "").strip()
    pt = (case.get("0/p", ub + ["type"]) or "").strip()
    uv = floats(case.get("0/U", ub + ["value"]))
    if ptype in ("symmetry", "symmetryPlane") or ut in ("symmetry", "symmetryPlane"):
        return "symmetry"
    if ut == "noSlip":
        return "wall"
    if ut == "slip":
        return "farfield"
    if ut == "fixedValue":
        return "inlet" if any(abs(x) > 0 for x in uv) else "wall"
    if pt == "fixedValue" or ut in ("inletOutlet", "zeroGradient", "pressureInletOutletVelocity"):
        return "outlet"
    return "wall" if ptype == "wall" else guess_role(patch)


def flow_settings(case):
    """Best estimate of U / nu / turbulence inputs from the case files."""
    s = Settings()
    ud = case.doc("0/U")
    if ud:
        found = None
        for n in ud.nodes(["boundaryField"]):
            if n.kind == "dict" and (ud.get(["boundaryField", n.key, "type"]) or "").strip() == "fixedValue":
                v = floats(ud.get(["boundaryField", n.key, "value"]))
                if len(v) == 3 and any(abs(x) > 0 for x in v):
                    found = tuple(v)
                    break
        if found is None:
            v = floats((ud.get(["internalField"]) or "").replace("uniform", ""))
            if len(v) == 3 and any(abs(x) > 0 for x in v):
                found = tuple(v)
        if found:
            s.U = found
    rel = material_file(case)
    if rel:
        v = floats(re.sub(r"\[[^\]]*\]", "", case.get(rel, ["nu"]) or ""))
        if v and v[0] > 0:
            s.nu = v[0]
    try:
        k = floats((case.get("0/k", ["internalField"]) or "").replace("uniform", ""))[0]
        sp = math.sqrt(sum(c * c for c in s.U))
        e = floats((case.get("0/epsilon", ["internalField"]) or "").replace("uniform", ""))
        w = floats((case.get("0/omega", ["internalField"]) or "").replace("uniform", ""))
        if e:
            length = 0.09 ** 0.75 * k ** 1.5 / e[0]
        elif w:
            length = math.sqrt(k) / (0.09 ** 0.25 * w[0])
        else:
            raise IndexError
        if k > 0 and length > 0 and sp > 0:
            s.intensity, s.length_scale = math.sqrt(k / 1.5) / sp, length
    except (IndexError, ValueError, ZeroDivisionError):
        pass
    return s


def _write_bc_text(case, field, patch, text):
    ents = [e for e in split_entries(text)]
    case.set_body(f"0/{field}", ["boundaryField", patch],
                  "\n" + "".join(f"        {e};\n" for e in ents) + "    ")


def apply_role(case, patch, role):
    """Rewrite one patch's BC in every known 0/ field from a role preset. Returns notes."""
    ptype = dict(mesh_patches(case)).get(patch, "patch")
    table = bcs(role, flow_settings(case))
    notes = []
    for f in zero_fields(case):
        if f in table:
            _write_bc_text(case, f, patch, f"type {ptype};" if ptype in CONSTRAINT_TYPES else table[f])
    if ptype not in CONSTRAINT_TYPES:
        if role == "wall" and ptype != "wall":
            notes.append(f"Patch '{patch}' has type '{ptype}' in constant/polyMesh/boundary; wall functions "
                         "need type 'wall' (use patchInfo in snappyHexMeshDict or edit the boundary file).")
        if role == "symmetry" and ptype not in ("symmetry", "symmetryPlane"):
            notes.append(f"Patch '{patch}' has type '{ptype}'; a symmetry BC needs patch type "
                         "'symmetry' or 'symmetryPlane'.")
    return notes


# --------------------------------------------------------------------------- #
#  Turbulence
# --------------------------------------------------------------------------- #
def turbulence_file(case):
    for rel in ("constant/momentumTransport", "constant/turbulenceProperties", "constant/RASProperties"):
        if case.exists(rel):
            return rel
    return None


def current_model(case):
    rel = turbulence_file(case)
    if not rel:
        return None
    d = case.doc(rel)
    for path in (["RAS", "model"], ["RAS", "RASModel"], ["RASModel"], ["model"]):
        v = d.get(path)
        if v:
            return v.strip()
    return None


def switch_model(case, model):
    """Switch the RAS model and create whatever it newly needs. Returns a list of notes."""
    if model not in TURB_MODELS:
        raise ValueError(f"Unsupported model {model}")
    notes = []
    rel = turbulence_file(case) or "constant/momentumTransport"
    if not case.exists(rel):
        case.create(rel, hdr("dictionary", "momentumTransport", "constant")
                    + f"simulationType RAS;\nRAS\n{{\n    model           {model};\n"
                      "    turbulence      on;\n    printCoeffs     on;\n}\n")
        notes.append(f"created {rel}")
    elif rel.endswith("RASProperties"):
        case.set(rel, ["RASModel"], model)
        notes.append(f"{rel}: RASModel {model}")
    else:
        case.set(rel, ["simulationType"], "RAS")
        case.set(rel, ["RAS", "model"], model)
        if not case.has(rel, ["RAS", "turbulence"]):
            case.set(rel, ["RAS", "turbulence"], "on")
        notes.append(f"{rel}: model {model}")

    fs = flow_settings(case)
    patches = mesh_patches(case)
    roles = {p: infer_role(case, p, t) for p, t in patches}
    init = {"k": fs.k, "epsilon": fs.epsilon, "omega": fs.omega, "nut": 0.0, "nuTilda": 5 * fs.nu}
    for f in TURB_MODELS[model]:
        if case.exists(f"0/{f}"):
            continue
        cls, dims = FIELD_DEFS[f]
        body = ""
        for p, t in patches:
            txt = f"type {t};" if t in CONSTRAINT_TYPES else bcs(roles[p], fs)[f]
            body += f"    {p}\n    {{\n" + "".join(f"        {e};\n" for e in split_entries(txt)) + "    }\n"
        case.create(f"0/{f}", hdr(cls, f) + f"dimensions      {dims};\n\ninternalField   uniform "
                    f"{fmt(init[f])};\n\nboundaryField\n{{\n{body}}}\n")
        notes.append(f"created 0/{f} (BCs from the patch roles inferred from U and p)")

    for f in TURB_MODELS[model]:
        if f == "nut":
            continue
        notes += _ensure_scheme(case, f) + _ensure_solver(case, f)
    sch = "system/fvSchemes"
    if model == "kOmegaSST" and case.exists(sch) and not case.has(sch, ["wallDist"]):
        case.set(sch, ["wallDist", "method"], "meshWave")
        notes.append("fvSchemes: added wallDist (needed by kOmegaSST)")
    return notes


def _key_matches(d, path, name):
    for n in d.nodes(path):
        try:
            if n.key == name or re.fullmatch(n.key, name):
                return n.key
        except re.error:
            pass
    return None


def _ensure_scheme(case, f):
    rel = "system/fvSchemes"
    d = case.doc(rel)
    if d is None:
        return []
    key = f"div(phi,{f})"
    default = (d.get(["divSchemes", "default"]) or "").strip()
    if _key_matches(d, ["divSchemes"], key) or (default and default != "none"):
        return []
    steady = (d.get(["ddtSchemes", "default"]) or "").strip() == "steadyState"
    case.set(rel, ["divSchemes", key], ("bounded " if steady else "") + "Gauss limitedLinear 1")
    return [f"fvSchemes: added {key}"]


def _ensure_solver(case, f):
    rel = "system/fvSolution"
    d = case.doc(rel)
    if d is None:
        return []
    notes = []
    if not _key_matches(d, ["solvers"], f):
        src = _key_matches(d, ["solvers"], "k") or _key_matches(d, ["solvers"], "U")
        body = d.body(["solvers", src]) if src else (
            "\n        solver smoothSolver;\n        smoother symGaussSeidel;\n"
            "        tolerance 1e-8;\n        relTol 0.1;\n    ")
        case.set_body(rel, ["solvers", f], body)
        notes.append(f"fvSolution: added solver for {f}")
    if d.kind(["relaxationFactors", "equations"]) == "dict" and not _key_matches(
            d, ["relaxationFactors", "equations"], f):
        case.set(rel, ["relaxationFactors", "equations", f], "0.7")
        notes.append(f"fvSolution: added relaxation factor for {f}")
    return notes


def apply_turbulence_values(case, intensity, length, uref):
    """Set k / epsilon / omega everywhere (internalField and every uniform BC value)."""
    s = Settings(U=(uref, 0.0, 0.0), intensity=intensity, length_scale=length)
    vals = {"k": s.k, "epsilon": s.epsilon, "omega": s.omega}
    touched = []
    for f, v in vals.items():
        rel = f"0/{f}"
        d = case.doc(rel)
        if d is None:
            continue
        text = f"uniform {fmt(v)}"
        if d.has(["internalField"]):
            case.set(rel, ["internalField"], text)
        for n in d.nodes(["boundaryField"]):
            if n.kind != "dict":
                continue
            for key in ("value", "inletValue", "outletValue"):
                path = ["boundaryField", n.key, key]
                if (case.get(rel, path) or "").strip().startswith("uniform"):
                    case.set(rel, path, text)
        touched.append(f)
    return touched


# --------------------------------------------------------------------------- #
#  Solver / control / decomposition
# --------------------------------------------------------------------------- #
CONTROL = "system/controlDict"
DECOMPOSE = "system/decomposeParDict"

DECOMPOSE_SPECS = [
    ("numberOfSubdomains", DECOMPOSE, ["numberOfSubdomains"]),
    ("method", DECOMPOSE, ["method"]),
    ("simpleCoeffs: n (x y z)", DECOMPOSE, ["simpleCoeffs", "n"]),
    ("simpleCoeffs: delta", DECOMPOSE, ["simpleCoeffs", "delta"]),
    ("hierarchicalCoeffs: n (x y z)", DECOMPOSE, ["hierarchicalCoeffs", "n"]),
    ("hierarchicalCoeffs: delta", DECOMPOSE, ["hierarchicalCoeffs", "delta"]),
    ("hierarchicalCoeffs: order", DECOMPOSE, ["hierarchicalCoeffs", "order"]),
    ("scotchCoeffs: processorWeights", DECOMPOSE, ["scotchCoeffs", "processorWeights"]),
]


def suggest_split(n, ext=(1.0, 1.0, 1.0)):
    """(nx, ny, nz) with nx*ny*nz == n that minimises the area of the cut planes."""
    best, best_cost = (n, 1, 1), None
    for a in range(1, n + 1):
        if n % a:
            continue
        for b in range(1, n // a + 1):
            if (n // a) % b:
                continue
            c = n // a // b
            cost = (a - 1) * ext[1] * ext[2] + (b - 1) * ext[0] * ext[2] + (c - 1) * ext[0] * ext[1]
            if best_cost is None or cost < best_cost - 1e-12:
                best, best_cost = (a, b, c), cost
    return best


def create_decompose(case, n=4, method="scotch"):
    nx, ny, nz = suggest_split(n)
    case.create(DECOMPOSE, hdr("dictionary", "decomposeParDict", "system") + f"""numberOfSubdomains {n};
method          {method};

simpleCoeffs
{{
    n               ({nx} {ny} {nz});
    delta           0.001;
}}

hierarchicalCoeffs
{{
    n               ({nx} {ny} {nz});
    delta           0.001;
    order           xyz;
}}
""")


def decompose_problems(case):
    """Human-readable inconsistencies in decomposeParDict."""
    d = case.doc(DECOMPOSE)
    if d is None:
        return []
    out = []
    n = floats(d.get(["numberOfSubdomains"]))
    method = (d.get(["method"]) or "").strip()
    if method in ("simple", "hierarchical") and n:
        c = floats(d.get([f"{method}Coeffs", "n"]))
        if len(c) == 3 and int(round(c[0] * c[1] * c[2])) != int(n[0]):
            out.append(f"{method}Coeffs n multiplies to {int(round(c[0] * c[1] * c[2]))}, "
                       f"but numberOfSubdomains is {int(n[0])}.")
        if len(c) != 3:
            out.append(f"{method}Coeffs n is missing or not a vector.")
    return out
