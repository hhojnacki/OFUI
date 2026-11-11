#!/usr/bin/env python3
"""
stl2foam_core - STL parsing and OpenFOAM case generation (no GUI code).

Used by stl2foam_wx.py; can also be imported from scripts:

    from stl2foam_core import load_stl, write_case, Settings
    patches, bbox = load_stl('body.stl')
    write_case('myCase', patches, bbox, Settings(mode='external'))  # overwrite=True to replace

Turns one or more STL files into an OpenFOAM case. Editing existing cases is in
foamcase.py (it works on the case files, not on geometry).

Workflow
  1. Add STL files. ASCII STLs with several `solid ... endsolid` blocks are split
     so that every solid becomes its own region (= boundary patch). Binary STLs
     cannot hold names, so each binary file becomes one patch named after the file.
  2. Assign a role (wall / inlet / outlet / symmetry) and refinement levels per patch.
  3. Generate: writes constant/triSurface/*.stl, blockMeshDict, snappyHexMeshDict,
     0/{U,p,k,epsilon,nut}, fvSchemes, fvSolution, controlDict, Allrun.

Target: steady incompressible RANS (simpleFoam, k-epsilon with wall functions). Tested against the
file layout used by OpenFOAM.org v8-v9 and ESI (openfoam.com) v2xxx; see NOTES
at the bottom for version differences.
"""
import math
import re
import shutil
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

ROLES = ["wall", "inlet", "outlet", "symmetry"]
TURB_MODEL = "kEpsilon"


# --------------------------------------------------------------------------- #
#  STL reading
# --------------------------------------------------------------------------- #
@dataclass
class Patch:
    name: str
    source: str
    ntri: int
    payload: bytes            # bytes written to constant/triSurface/<name>.stl
    role: str = "wall"
    lmin: int = 2
    lmax: int = 2


def sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name.strip()) or "patch"
    return "p_" + name if name[0].isdigit() else name


def guess_role(name: str) -> str:
    n = name.lower()
    if "inlet" in n or n.startswith("in_") or n == "in":
        return "inlet"
    if "outlet" in n or n.startswith("out_") or n == "out":
        return "outlet"
    if "sym" in n:
        return "symmetry"
    return "wall"


def _is_binary(b: bytes) -> bool:
    if len(b) < 84:
        return False
    n = struct.unpack("<I", b[80:84])[0]
    return 84 + 50 * n == len(b)


def grow(bbox, pts):
    for x, y, z in pts:
        if bbox is None:
            bbox = [[x, y, z], [x, y, z]]
        else:
            for i, v in enumerate((x, y, z)):
                bbox[0][i] = min(bbox[0][i], v)
                bbox[1][i] = max(bbox[1][i], v)
    return bbox


_VERT = re.compile(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)")
_SOLID = re.compile(r"^[ \t]*solid\b(.*?)$(.*?)^[ \t]*endsolid\b.*?$", re.S | re.M)


def load_stl(path, taken=()):
    """Return (patches, bbox) for one STL file. bbox = [[xmin,ymin,zmin],[xmax,ymax,zmax]]."""
    path = Path(path)
    raw = path.read_bytes()
    stem = sanitize(path.stem)
    taken = set(taken)

    def unique(n):
        base, i = n, 2
        while n in taken:
            n, i = f"{base}_{i}", i + 1
        taken.add(n)
        return n

    if _is_binary(raw):
        n = struct.unpack("<I", raw[80:84])[0]
        pts = []
        for rec in struct.iter_unpack("<12fH", raw[84:]):
            pts += [rec[3:6], rec[6:9], rec[9:12]]
        name = unique(stem)
        return [Patch(name, path.name, n, raw, guess_role(name))], grow(None, pts)

    text = raw.decode("ascii", errors="replace")
    solids = _SOLID.findall(text)
    if not solids:
        raise ValueError(f"{path.name}: not a valid STL file")
    patches, bbox = [], None
    for sname, body in solids:
        pts = [tuple(float(v) for v in m) for m in _VERT.findall(body)]
        if not pts:
            continue
        bbox = grow(bbox, pts)
        if len(solids) == 1:
            name = unique(stem)
        else:
            name = unique(sanitize(sname) if sname.strip() else f"{stem}_{len(patches)}")
        payload = f"solid {name}\n{body.strip()}\nendsolid {name}\n".encode()
        patches.append(Patch(name, path.name, len(pts) // 3, payload, guess_role(name)))
    return patches, bbox


def rename_patch(p, new_name):
    """Rename a patch and the solid name inside its ASCII STL payload."""
    p.name = new_name
    if p.payload[:5] == b"solid" and not _is_binary(p.payload):
        p.payload = re.sub(rb"^solid[^\n]*", b"solid " + new_name.encode(), p.payload, count=1)
        p.payload = re.sub(rb"endsolid[^\n]*\s*$", b"endsolid " + new_name.encode() + b"\n", p.payload)


# --------------------------------------------------------------------------- #
#  Case writing
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    mode: str = "external"                 # "external" (wind tunnel box) or "internal"
    nu: float = 1.5e-5
    U: tuple = (10.0, 0.0, 0.0)
    intensity: float = 0.05
    length_scale: float = 0.01
    cell_size: float = 0.1                 # background mesh cell size
    loc: tuple = None                      # locationInMesh (None = auto)
    end_time: int = 1000

    @property
    def k(self):
        sp = math.sqrt(sum(c * c for c in self.U))
        return max(1.5 * (sp * self.intensity) ** 2, 1e-8)

    @property
    def omega(self):
        return math.sqrt(self.k) / (0.09 ** 0.25 * self.length_scale)

    @property
    def epsilon(self):
        # eps = Cmu^0.75 k^1.5 / L, Cmu = 0.09
        return 0.09 ** 0.75 * self.k ** 1.5 / self.length_scale


def fmt(v):
    return f"{v:.6g}"


def vec(v):
    return "(" + " ".join(fmt(x) for x in v) + ")"


def hdr(cls, obj, loc=None):
    l = f'    location    "{loc}";\n' if loc else ""
    return ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
            f"    class       {cls};\n{l}    object      {obj};\n}}\n\n")


def domain(patches, bbox, s):
    lo, hi = bbox
    ext = [h - l for l, h in zip(lo, hi)]
    L = max(ext)
    if s.mode == "external":
        a = [lo[0] - 2 * L, lo[1] - 2 * L, lo[2] - 2 * L]
        b = [hi[0] + 5 * L, hi[1] + 2 * L, hi[2] + 2 * L]
        loc = s.loc or (a[0] + 0.5 * L, a[1] + 0.5 * L, a[2] + 0.5 * L)
    else:
        pad = 0.05 * L
        a = [v - pad for v in lo]
        b = [v + pad for v in hi]
        loc = s.loc or tuple((l + h) / 2 for l, h in zip(lo, hi))
    n = [max(1, math.ceil((bb - aa) / s.cell_size)) for aa, bb in zip(a, b)]
    return a, b, n, loc


def bcs(role, s):
    """Boundary-condition body per role and field."""
    k, w, U = fmt(s.k), fmt(s.epsilon), vec(s.U)   # w = epsilon
    om, nt = fmt(s.omega), fmt(5 * s.nu)
    z = "(0 0 0)"
    return {
        "wall": dict(U="type noSlip;", p="type zeroGradient;",
                     k=f"type kqRWallFunction; value uniform {k};",
                     epsilon=f"type epsilonWallFunction; value uniform {w};",
                     omega=f"type omegaWallFunction; value uniform {om};",
                     nuTilda="type fixedValue; value uniform 0;",
                     nut="type nutkWallFunction; value uniform 0;"),
        "inlet": dict(U=f"type fixedValue; value uniform {U};", p="type zeroGradient;",
                      k=f"type fixedValue; value uniform {k};",
                      epsilon=f"type fixedValue; value uniform {w};",
                      omega=f"type fixedValue; value uniform {om};",
                      nuTilda=f"type fixedValue; value uniform {nt};",
                      nut="type calculated; value uniform 0;"),
        "outlet": dict(U=f"type inletOutlet; inletValue uniform {z}; value uniform {z};",
                       p="type fixedValue; value uniform 0;",
                       k=f"type inletOutlet; inletValue uniform {k}; value uniform {k};",
                       epsilon=f"type inletOutlet; inletValue uniform {w}; value uniform {w};",
                       omega=f"type inletOutlet; inletValue uniform {om}; value uniform {om};",
                       nuTilda=f"type inletOutlet; inletValue uniform {nt}; value uniform {nt};",
                       nut="type calculated; value uniform 0;"),
        "symmetry": {f: "type symmetry;" for f in ("U", "p", "k", "epsilon", "omega", "nuTilda", "nut")},
        "farfield": dict(U="type slip;", p="type zeroGradient;", k="type zeroGradient;",
                         epsilon="type zeroGradient;", omega="type zeroGradient;", nuTilda="type zeroGradient;", nut="type calculated; value uniform 0;"),
    }[role]


FIELDS = {  # name: (class, dimensions)
    "U": ("volVectorField", "[0 1 -1 0 0 0 0]"),
    "p": ("volScalarField", "[0 2 -2 0 0 0 0]"),
    "k": ("volScalarField", "[0 2 -2 0 0 0 0]"),
    "epsilon": ("volScalarField", "[0 2 -3 0 0 0 0]"),
    "nut": ("volScalarField", "[0 2 -1 0 0 0 0]"),
}


FIELD_DEFS = dict(FIELDS)
FIELD_DEFS["omega"] = ("volScalarField", "[0 0 -1 0 0 0 0]")
FIELD_DEFS["nuTilda"] = ("volScalarField", "[0 2 -1 0 0 0 0]")

CASE_FILES = [
    "0/U", "0/p", "0/k", "0/epsilon", "0/nut",
    "constant/transportProperties", "constant/momentumTransport",
    "system/blockMeshDict", "system/snappyHexMeshDict", "system/controlDict",
    "system/fvSchemes", "system/fvSolution", "Allrun", "case.foam",
]


def existing_case_files(case_dir, patches):
    """Relative paths of files write_case would overwrite (empty list = safe)."""
    case = Path(case_dir)
    planned = CASE_FILES + [f"constant/triSurface/{p.name}.stl" for p in patches]
    return [r for r in planned if (case / r).exists()]


def _generate(case_dir, patches, bbox, s):
    """Write every case file unconditionally (callers handle overwrite and backups)."""
    reserved = {"external": {"inlet", "outlet", "farfield"}, "internal": {"background"}}[s.mode]
    clash = sorted(reserved & {p.name for p in patches})
    if clash:
        raise ValueError(
            f"Patch name(s) {', '.join(clash)} clash with the background-box patches that "
            f"blockMesh creates in {s.mode} mode. Rename the patch(es) in the table.")
    case = Path(case_dir)
    for d in ("0", "constant/triSurface", "system"):
        (case / d).mkdir(parents=True, exist_ok=True)
    a, b, n, loc = domain(patches, bbox, s)

    # geometry
    for p in patches:
        (case / "constant/triSurface" / f"{p.name}.stl").write_bytes(p.payload)

    # blockMeshDict
    x0, y0, z0 = a
    x1, y1, z1 = b
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    if s.mode == "external":
        bnd = ("boundary\n(\n    inlet   { type patch; faces ((0 4 7 3)); }\n"
               "    outlet  { type patch; faces ((1 2 6 5)); }\n"
               "    farfield { type patch; faces ((0 1 5 4) (3 7 6 2) (0 3 2 1) (4 5 6 7)); }\n);\n")
        domain_patches = {"inlet": "inlet", "outlet": "outlet", "farfield": "farfield"}
    else:
        bnd = ("defaultPatch { name background; type patch; }\nboundary ();\n")
        domain_patches = {"background": "farfield"}
    (case / "system/blockMeshDict").write_text(
        hdr("dictionary", "blockMeshDict") + "convertToMeters 1;\n\nvertices\n(\n"
        + "".join(f"    {vec(v)}\n" for v in verts) + ");\n\nblocks\n(\n"
        f"    hex (0 1 2 3 4 5 6 7) ({n[0]} {n[1]} {n[2]}) simpleGrading (1 1 1)\n);\n\n"
        "edges ();\n\n" + bnd + "\nmergePatchPairs ();\n")

    # snappyHexMeshDict
    geom = "".join(f'    {p.name}\n    {{\n        type triSurfaceMesh;\n'
                   f'        file "{p.name}.stl";\n    }}\n' for p in patches)
    surfs = ""
    for p in patches:
        info = {"wall": "wall", "symmetry": "symmetry"}.get(p.role, "patch")
        surfs += (f"        {p.name}\n        {{\n            level ({p.lmin} {p.lmax});\n"
                  f"            patchInfo {{ type {info}; }}\n        }}\n")
    (case / "system/snappyHexMeshDict").write_text(
        hdr("dictionary", "snappyHexMeshDict") + f"""castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
{geom}}}

castellatedMeshControls
{{
    maxLocalCells 1000000;
    maxGlobalCells 5000000;
    minRefinementCells 10;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;
    features ();
    refinementSurfaces
    {{
{surfs}    }}
    resolveFeatureAngle 30;
    refinementRegions {{}}
    locationInMesh {vec(loc)};
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 100;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap true;
    explicitFeatureSnap false;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes true;
    layers {{}}
    expansionRatio 1.2;
    finalLayerThickness 0.5;
    minThickness 0.1;
    nGrow 0;
    featureAngle 60;
    nRelaxIter 5;
    nSmoothSurfaceNormals 1;
    nSmoothNormals 3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter 50;
}}

meshQualityControls
{{
    #includeEtc "caseDicts/meshQualityDict"
}}

writeFlags ();
mergeTolerance 1e-6;
""")

    # boundary conditions: patch name -> role
    roles = {p.name: p.role for p in patches}
    roles.update(domain_patches)
    U0 = "(0 0 0)" if s.mode == "internal" else vec(s.U)
    inits = {"U": U0, "p": "0", "k": fmt(s.k), "epsilon": fmt(s.epsilon), "nut": "0"}
    for f, (cls, dims) in FIELDS.items():
        body = "".join(f"    {name}\n    {{\n        {bcs(role, s)[f]}\n    }}\n"
                       for name, role in roles.items())
        (case / "0" / f).write_text(
            hdr(cls, f, "") .replace('    location    "";\n', "")
            + f"dimensions      {dims};\n\ninternalField   uniform {inits[f]};\n\n"
            f"boundaryField\n{{\n{body}}}\n")

    # solver setup
    (case / "constant/transportProperties").write_text(
        hdr("dictionary", "transportProperties", "constant")
        + f"transportModel  Newtonian;\nnu              [0 2 -1 0 0 0 0] {fmt(s.nu)};\n")
    (case / "constant/momentumTransport").write_text(
        hdr("dictionary", "momentumTransport", "constant")
        + "simulationType RAS;\nRAS\n{\n    model           " + TURB_MODEL + ";\n    turbulence      on;\n"
          "    printCoeffs     on;\n}\n")
    (case / "system/controlDict").write_text(
        hdr("dictionary", "controlDict", "system") + f"""application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {s.end_time};
deltaT          1;
writeControl    timeStep;
writeInterval   100;
purgeWrite      2;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;
""")
    (case / "system/fvSchemes").write_text(
        hdr("dictionary", "fvSchemes", "system") + """ddtSchemes      {{ default steadyState; }}
gradSchemes     {{ default Gauss linear; }}
divSchemes
{{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind grad(U);
    div(phi,k)      bounded Gauss limitedLinear 1;
    div(phi,epsilon) bounded Gauss limitedLinear 1;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}}
laplacianSchemes {{ default Gauss linear corrected; }}
interpolationSchemes {{ default linear; }}
snGradSchemes   {{ default corrected; }}
wallDist        {{ method meshWave; }}
""".replace("{{", "{").replace("}}", "}"))
    (case / "system/fvSolution").write_text(
        hdr("dictionary", "fvSolution", "system") + """solvers
{
    p
    {
        solver GAMG; smoother GaussSeidel; tolerance 1e-6; relTol 0.1;
    }
    "(U|k|epsilon)"
    {
        solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1;
    }
}

SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    residualControl
    {
        p 1e-3;
        U 1e-4;
        "(k|epsilon)" 1e-4;
    }
}

relaxationFactors
{
    equations { U 0.9; ".*" 0.9; }
}
""")

    allrun = case / "Allrun"
    allrun.write_text("#!/bin/sh\ncd \"${0%/*}\" || exit 1\nblockMesh > log.blockMesh 2>&1\n"
                      "snappyHexMesh -overwrite > log.snappyHexMesh 2>&1\n"
                      "checkMesh > log.checkMesh 2>&1\n"
                      "echo 'Check constant/polyMesh/boundary: patch names must match 0/* files.'\n"
                      "simpleFoam > log.simpleFoam 2>&1\n")
    allrun.chmod(allrun.stat().st_mode | stat.S_IXUSR)
    (case / "case.foam").touch()
    return a, b, n, loc


# --------------------------------------------------------------------------- #
#  Writing a new case, with overwrite protection and backups
# --------------------------------------------------------------------------- #
BACKUP_DIR = ".stl2foam_backup"


class CaseResult(NamedTuple):
    a: list          # background box, lower corner
    b: list          # background box, upper corner
    n: list          # background cell counts
    loc: tuple       # locationInMesh used
    backup: object   # Path of the backup folder, or None if nothing was replaced


def backup_files(case, rels):
    """Copy existing files into <case>/.stl2foam_backup/<timestamp>/ ; returns that Path or None."""
    case = Path(case)
    rels = [r for r in rels if r != "case.foam" and (case / r).is_file()]
    if not rels:
        return None
    base = case / BACKUP_DIR / time.strftime("%Y%m%d-%H%M%S")
    d, i = base, 1
    while d.exists():
        d, i = Path(f"{base}-{i}"), i + 1
    for r in rels:
        dest = d / r
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case / r, dest)
    return d


def write_case(case_dir, patches, bbox, s, overwrite=False):
    """Write a new case.

    Raises FileExistsError (listing the files) if any target file exists and
    overwrite is False; with overwrite=True the replaced files are backed up first.
    """
    case = Path(case_dir)
    clashes = existing_case_files(case_dir, patches)
    if clashes and not overwrite:
        raise FileExistsError("Case files already exist: " + ", ".join(clashes))
    backup = backup_files(case, clashes)
    return CaseResult(*_generate(case, patches, bbox, s), backup)


# NOTES
#  * OpenFOAM.org v10+ replaced simpleFoam with `foamRun -solver incompressibleFluid`
#    (set `solver incompressibleFluid;` in controlDict and run foamRun).
#  * Old releases (<=v7 / ESI <2012) name the turbulence file RASProperties /
#    turbulenceProperties instead of momentumTransport.
#  * Wall functions: nutkWallFunction + epsilonWallFunction + kqRWallFunction, which want
#    30 < y+ < 300. Tell snappyHexMesh to add layers (or refine walls) accordingly.
#  * The meshQualityDict include path differs on some ESI versions:
#    "caseDicts/mesh/generation/meshQualityDict".
