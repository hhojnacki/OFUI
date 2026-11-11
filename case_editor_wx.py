"""
case_editor_wx - wxPython editor for an existing OpenFOAM case.

Works directly on the case files (see foamcase.py): edits change only the entries you touch,
nothing is written until Save, and Save backs up every file it replaces into
<case>/.stl2foam_backup/<timestamp>/.

The tables are read-only; values are edited through small dialogs (double-click a value, or use
the Edit buttons). wxGrid cell-editor widgets are deliberately avoided: once used they stay behind
as hidden child widgets, and GTK3 prints Gtk-CRITICAL warnings when the grid scrolls or resizes.

Tabs: Mesh, Material, Turbulence, Solver, Boundary conditions, Control, Decompose.
"""
import wx
import wx.grid as gridlib

import foamcase as fc
from foamdict import FoamParseError
from stl2foam_core import ROLES


def say(parent, text, title="Case editor", icon=wx.ICON_INFORMATION):
    return wx.MessageBox(text, title, wx.OK | icon, parent)


def make_grid(parent, headers, widths):
    g = gridlib.Grid(parent)
    g.CreateGrid(0, len(headers))
    for i, (h, w) in enumerate(zip(headers, widths)):
        g.SetColLabelValue(i, h)
        g.SetColSize(i, w)
    g.SetRowLabelSize(30)
    g.SetDefaultRowSize(28, True)
    g.EnableDragGridSize(False)
    g.SetMinSize((300, 120))     # a tiny allocation makes GTK print scrollbar 'size >= 0' warnings
    # constant was renamed in wxPython 4.2 (GridSelectRows -> SelectRows)
    g.SetSelectionMode(getattr(gridlib.Grid, "SelectRows", getattr(gridlib.Grid, "GridSelectRows", 1)))
    return g


OTHER = "Other (type a value)..."
INHERIT = "(inherit from surface)"


def clear_rows(g, n):
    """Give the grid n blank, read-only rows. Rows are only added/removed at the end and the rest
    are reset in place (no cell editors are ever created, see the module docstring)."""
    g.BeginBatch()
    cur = g.GetNumberRows()
    if cur > n:
        g.DeleteRows(n, cur - n)
    elif cur < n:
        g.AppendRows(n - cur)
    bg = g.GetDefaultCellBackgroundColour()
    for r in range(n):
        for c in range(g.GetNumberCols()):
            if r < cur:
                g.SetCellValue(r, c, "")
            g.SetReadOnly(r, c, True)
            g.SetCellBackgroundColour(r, c, bg)


def selected_row(g):
    rows = g.GetSelectedRows()
    return rows[0] if rows else g.GetGridCursorRow()


def choose_one(parent, label, items, current="", title="Choose"):
    """Pick one of several items from a drop-down; returns the item, or None if cancelled.
    (wx.SingleChoiceDialog is avoided on purpose: its list box is a scrolled window, and with few
    items GTK3 prints Gtk-CRITICAL scrollbar warnings.)"""
    items = list(items)
    with wx.Dialog(parent, title=title) as dlg:
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(wx.StaticText(dlg, label=label), 0, wx.ALL, 12)
        choice = wx.Choice(dlg, choices=items)
        choice.SetMinSize((280, -1))
        choice.SetSelection(items.index(current) if current in items else 0)
        box.Add(choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        box.Add(dlg.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        dlg.SetSizerAndFit(box)
        if dlg.ShowModal() != wx.ID_OK:
            return None
        return choice.GetStringSelection()


def prompt_value(parent, label, current, choices=None, title="Edit value"):
    """Ask for a value. With `choices`: pick from a list (or 'Other...' to type one).
    Returns the new text, or None if cancelled."""
    if choices:
        picked = choose_one(parent, label, list(choices) + [OTHER], current, title)
        if picked is None:
            return None
        if picked != OTHER:
            return picked
    with wx.TextEntryDialog(parent, label, title, current) as dlg:
        if dlg.ShowModal() != wx.ID_OK:
            return None
        return dlg.GetValue().strip()


GREY = wx.Colour(235, 235, 235)


# --------------------------------------------------------------------------- #
#  Generic grid of "setting | value | file" rows
# --------------------------------------------------------------------------- #
class EntryGrid(wx.Panel):
    """Entries of case files as "setting | value | file" rows. `provider()` returns
    [(label, file, path), ...]. Double-click a row (or press Edit value...) to change it;
    an empty value removes the entry from the file."""

    def __init__(self, parent, frame, provider, add_files=None, hint=""):
        super().__init__(parent)
        self.frame, self.provider, self.add_files = frame, provider, add_files or []
        self.rows, self.extra = [], []
        self.grid = make_grid(self, ["Setting", "Value", "File"], [330, 380, 200])
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(wx.StaticText(self, label=(hint + "   " if hint else "") + "Double-click a row to edit."),
                0, wx.ALL, 6)
        box.Add(self.grid, 1, wx.EXPAND | wx.ALL, 6)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        b_edit = wx.Button(self, label="Edit value...")
        b_edit.Bind(wx.EVT_BUTTON, self.on_edit)
        btns.Add(b_edit, 0, wx.RIGHT, 6)
        if self.add_files:
            b = wx.Button(self, label="Add entry...")
            b.Bind(wx.EVT_BUTTON, self.on_add)
            btns.Add(b, 0)
        box.Add(btns, 0, wx.LEFT | wx.BOTTOM, 6)
        self.SetSizer(box)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_dclick)

    def reload(self):
        g, case = self.grid, self.frame.case
        rows = list(self.provider())
        have = {(rel, tuple(p)) for _, rel, p in rows}
        rows += [e for e in self.extra if (e[1], tuple(e[2])) not in have]
        self.rows = rows
        clear_rows(g, len(rows))
        for r, (label, rel, path) in enumerate(rows):
            g.SetCellValue(r, 0, label)
            g.SetCellValue(r, 2, rel)
            if not case.exists(rel):
                g.SetCellValue(r, 1, "(file not found)")
                continue
            v = case.get(rel, path)
            if v is None:
                g.SetCellBackgroundColour(r, 1, GREY)           # not set in the file
            g.SetCellValue(r, 1, fc.one_line(v))
        g.EndBatch()

    def on_dclick(self, ev):
        self.edit_row(ev.GetRow())

    def on_edit(self, _):
        self.edit_row(selected_row(self.grid))

    def edit_row(self, r):
        if r < 0 or r >= len(self.rows):
            say(self, "Select a row first.", icon=wx.ICON_WARNING)
            return
        label, rel, path = self.rows[r]
        case = self.frame.case
        if not case.exists(rel):
            return
        current = fc.one_line(case.get(rel, path))
        value = prompt_value(self, f"{label}\n(leave empty to remove the entry)", current,
                             fc.choices_for(rel, path))
        if value is None or value == current:
            return
        try:
            if value:
                case.set(rel, path, value)
            elif not case.delete(rel, path):
                return
        except (FoamParseError, FileNotFoundError) as e:
            say(self, str(e), "Cannot edit", wx.ICON_ERROR)
            return
        g = self.grid
        g.SetCellValue(r, 1, value)
        g.SetCellBackgroundColour(r, 1, g.GetDefaultCellBackgroundColour() if value else GREY)
        g.ForceRefresh()
        self.frame.touched()

    def on_add(self, _):
        case = self.frame.case
        files = [f for f in self.add_files if case.exists(f)]
        if not files:
            say(self, "None of the relevant files exist in this case.", icon=wx.ICON_WARNING)
            return
        rel = files[0]
        if len(files) > 1:
            rel = choose_one(self, "Add to which file?", files, files[0], "Add entry")
            if rel is None:
                return
        with wx.TextEntryDialog(self, "Entry name; use / for nested keys\n(e.g. writeInterval  or  SIMPLE/consistent):",
                                "Add entry") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = [p for p in dlg.GetValue().strip().split("/") if p]
        if not path:
            return
        with wx.TextEntryDialog(self, f"Value for {'/'.join(path)}:", "Add entry") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            value = dlg.GetValue().strip()
        try:
            case.set(rel, path, value)
        except (FoamParseError, FileNotFoundError) as e:
            say(self, str(e), "Cannot add", wx.ICON_ERROR)
            return
        self.extra.append((f"{rel.split('/')[-1]}: {'/'.join(path)}", rel, path))
        self.frame.touched()
        self.reload()


# --------------------------------------------------------------------------- #
#  Mesh
# --------------------------------------------------------------------------- #
LEVELS = [str(i) for i in range(0, 11)]


class SurfacePanel(wx.Panel):
    """Every refined surface (any geometry type) followed by its regions."""

    def __init__(self, parent, frame, refresh):
        super().__init__(parent)
        self.frame, self.refresh, self.surf, self.items = frame, refresh, [], []
        v = wx.BoxSizer(wx.VERTICAL)
        v.Add(wx.StaticText(self, label="Surface refinement (castellatedMeshControls/refinementSurfaces) of any "
                                        "geometry type, with regions below their surface. Double-click a value "
                                        "to edit; grey = inherited from the surface."), 0, wx.ALL, 6)
        self.grid = make_grid(self, ["Surface / region", "Min level", "Max level", "Layers", "Info"],
                              [250, 80, 80, 70, 440])
        v.Add(self.grid, 1, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(v)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_dclick)

    def reload(self):
        case = self.frame.case
        self.surf = fc.surfaces(case)
        regs = fc.regions(case)
        geoms = {x.name: x for x in fc.geometry(case)}
        self.items = []
        for s in self.surf:
            self.items.append(("surface", s))
            self.items += [("region", x) for x in regs if x.surface == s.name]
        g = self.grid
        clear_rows(g, len(self.items))
        by_name = {s.name: s for s in self.surf}
        txt = lambda v: "" if v is None else str(v)
        for r, (kind, it) in enumerate(self.items):
            if kind == "surface":
                n = sum(1 for k, x in self.items if k == "region" and x.surface == it.name)
                gm = geoms.get(it.name)
                bits = []
                if gm and gm.type:
                    bits.append(gm.type)
                if gm and gm.type != "triSurfaceMesh" and gm.params:
                    bits.append(gm.params)
                if n:
                    bits.append(f"{n} region(s)")
                for c, val in enumerate((it.name, txt(it.lmin), txt(it.lmax), txt(it.layers), "; ".join(bits))):
                    g.SetCellValue(r, c, val)
                continue
            s = by_name[it.surface]
            inherited = it.lmin is None
            lmin, lmax = (s.lmin, s.lmax) if inherited else (it.lmin, it.lmax)
            g.SetCellValue(r, 0, f"      region: {it.region}")
            g.SetCellValue(r, 1, txt(lmin))
            g.SetCellValue(r, 2, txt(lmax))
            g.SetCellValue(r, 4, f"patch {it.patch}{'' if it.alias else ' (default name)'};  "
                                 f"{'inherits surface level' if inherited else 'own level'};  found in {it.source}")
            if inherited:
                g.SetCellBackgroundColour(r, 1, GREY)
                g.SetCellBackgroundColour(r, 2, GREY)
        g.EndBatch()

    def on_dclick(self, ev):
        r, c = ev.GetRow(), ev.GetCol()
        if r >= len(self.items) or c not in (1, 2, 3):
            return
        kind, it = self.items[r]
        if kind == "surface":
            what = {1: "minimum refinement level", 2: "maximum refinement level",
                    3: "number of boundary layers (0 = none)"}[c]
            txt = prompt_value(self, f"{it.name}: {what}", self.grid.GetCellValue(r, c), LEVELS, "Edit surface")
            if txt is not None:
                self.apply_surface(it, c, txt)
            return
        if c == 3:
            say(self, "Boundary layers are set per patch (addLayersControls/layers), not per region. "
                      "Set them with 'Add entry...' in 'Other settings' if you need them.",
                icon=wx.ICON_INFORMATION)
            return
        what = {1: "minimum refinement level", 2: "maximum refinement level"}[c]
        txt = prompt_value(self, f"{it.surface} / region {it.region}: {what}\n"
                                 f"'{INHERIT}' removes the region's own level",
                           self.grid.GetCellValue(r, c), [INHERIT] + LEVELS, "Edit region")
        if txt is not None:
            self.apply_region(it, c, txt)

    def apply_surface(self, s, c, txt):
        case = self.frame.case
        try:
            if c in (1, 2):
                lmin = int(txt) if c == 1 else (s.lmin if s.lmin is not None else int(txt))
                lmax = int(txt) if c == 2 else (s.lmax if s.lmax is not None else int(txt))
                fc.set_surface_levels(case, s.name, lmin, lmax)
            elif c == 3:
                fc.set_surface_layers(case, s.name, int(txt) if txt else 0)
        except (ValueError, FoamParseError) as e:
            say(self, f"Could not apply: {e}", icon=wx.ICON_ERROR)
        self.refresh()
        self.frame.touched()

    def apply_region(self, reg, c, txt):
        case = self.frame.case
        try:
            if txt == INHERIT:
                fc.clear_region_levels(case, reg.surface, reg.region)
            else:
                v = int(txt)
                s = next(x for x in self.surf if x.name == reg.surface)
                cur_min = reg.lmin if reg.lmin is not None else s.lmin     # what the region has now
                cur_max = reg.lmax if reg.lmax is not None else s.lmax
                lmin = v if c == 1 else (cur_min if cur_min is not None else v)
                lmax = v if c == 2 else (cur_max if cur_max is not None else v)
                fc.set_region_levels(case, reg.surface, reg.region, lmin, lmax)
        except (ValueError, FoamParseError) as e:
            say(self, f"Could not apply: {e}", icon=wx.ICON_ERROR)
        self.refresh()
        self.frame.touched()


def levels_text(v):
    """Short text for a VolumeRow's levels."""
    if not v.parsed:
        return v.raw
    if v.mode in ("inside", "outside") and len(v.pairs) == 1:
        return str(v.pairs[0][1])
    return ", ".join(f"{d}:{l}" for d, l in v.pairs)


class VolumePanel(wx.Panel):
    """Volume refinement (castellatedMeshControls/refinementRegions) of any geometry type."""

    def __init__(self, parent, frame, refresh):
        super().__init__(parent)
        self.frame, self.refresh, self.rows = frame, refresh, []
        v = wx.BoxSizer(wx.VERTICAL)
        v.Add(wx.StaticText(self, label="Volume refinement (castellatedMeshControls/refinementRegions). "
                                        "inside/outside: one level for the whole volume; distance: "
                                        "refinement level by distance from the surface. Double-click Mode "
                                        "or Level(s) to edit."), 0, wx.ALL, 6)
        self.grid = make_grid(self, ["Geometry", "Type", "Mode", "Level(s)", "Info"], [170, 150, 90, 200, 380])
        v.Add(self.grid, 1, wx.EXPAND | wx.ALL, 6)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        b_add = wx.Button(self, label="Add volume refinement...")
        b_del = wx.Button(self, label="Remove selected")
        btns.Add(b_add, 0, wx.RIGHT, 6)
        btns.Add(b_del, 0)
        v.Add(btns, 0, wx.LEFT | wx.BOTTOM, 6)
        self.SetSizer(v)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_dclick)
        b_add.Bind(wx.EVT_BUTTON, self.on_add)
        b_del.Bind(wx.EVT_BUTTON, self.on_remove)

    def reload(self):
        case = self.frame.case
        self.rows = fc.volumes(case)
        geoms = {x.name: x for x in fc.geometry(case)}
        g = self.grid
        clear_rows(g, len(self.rows))
        for r, v in enumerate(self.rows):
            gm = geoms.get(v.name)
            info = gm.params if gm and gm.type != "triSurfaceMesh" else ""
            if gm is None:
                info = "not in the geometry dictionary"
            elif gm.type == "triSurfaceMesh":
                info = "STL surface (inside/outside need a closed surface)"
            if v.parsed and v.mode in ("inside", "outside") and v.pairs and v.pairs[0][0].upper() != fc.DEFAULT_DISTANCE:
                info += f";  distance {v.pairs[0][0]} is not used in this mode"
            for c, val in enumerate((v.name, v.type, v.mode, levels_text(v), info)):
                g.SetCellValue(r, c, val)
        g.EndBatch()

    def on_dclick(self, ev):
        r, c = ev.GetRow(), ev.GetCol()
        if r >= len(self.rows) or c not in (2, 3):
            return
        v, case = self.rows[r], self.frame.case
        try:
            if c == 2:
                mode = choose_one(self, f"Mode for {v.name}:\ninside / outside: one level for the volume\n"
                                        "distance: levels by distance from the surface",
                                  ["inside", "outside", "distance"], v.mode, "Edit volume refinement")
                if mode is None:
                    return
                fc.set_volume_mode(case, v.name, mode)
            elif v.parsed and v.mode in ("inside", "outside") and len(v.pairs) <= 1:
                txt = prompt_value(self, f"{v.name}: refinement level {v.mode} the shape",
                                   str(v.pairs[0][1]) if v.pairs else "", LEVELS, "Edit volume refinement")
                if txt is None:
                    return
                fc.set_volume_level(case, v.name, int(txt))
            else:
                cur = ", ".join(f"{d} {l}" for d, l in v.pairs) if v.parsed else v.raw
                txt = prompt_value(self, f"{v.name}: pairs of 'distance level'\n(e.g.  0.1 4, 0.5 3)", cur, None,
                                   "Edit volume refinement")
                if txt is None:
                    return
                fc.set_volume_pairs(case, v.name, fc.parse_pairs(txt))
        except (ValueError, FoamParseError) as e:
            say(self, f"Could not apply: {e}", icon=wx.ICON_ERROR)
        self.refresh()
        self.frame.touched()

    def on_add(self, _):
        case = self.frame.case
        free = [g.name for g in fc.geometry(case) if g.volume is None]
        if not free:
            say(self, "Every geometry already has a volume refinement (or there is no geometry).",
                icon=wx.ICON_WARNING)
            return
        name = choose_one(self, "Add a volume refinement for which geometry?", free, free[0], "Add volume refinement")
        if name is None:
            return
        txt = prompt_value(self, f"Refinement level for {name} (mode inside; change the mode afterwards)", "2",
                           LEVELS, "Add volume refinement")
        if txt is None:
            return
        try:
            fc.add_volume(case, name, int(txt))
        except (ValueError, FoamParseError) as e:
            say(self, f"Could not add: {e}", icon=wx.ICON_ERROR)
        self.refresh()
        self.frame.touched()

    def on_remove(self, _):
        r = selected_row(self.grid)
        if r < 0 or r >= len(self.rows):
            say(self, "Select a row first.", icon=wx.ICON_WARNING)
            return
        name = self.rows[r].name
        if wx.MessageBox(f"Remove the volume refinement of {name}?", "Remove", wx.YES_NO | wx.NO_DEFAULT |
                         wx.ICON_QUESTION, self) != wx.YES:
            return
        fc.remove_volume(self.frame.case, name)
        self.refresh()
        self.frame.touched()


class GeometryPanel(wx.Panel):
    """Everything in the geometry dictionary, whatever its type, and how it is refined."""

    def __init__(self, parent, frame, refresh):
        super().__init__(parent)
        self.frame, self.refresh, self.rows = frame, refresh, []
        v = wx.BoxSizer(wx.VERTICAL)
        v.Add(wx.StaticText(self, label="Geometry dictionary. Double-click '(none)' to add a refinement, or an "
                                        "existing one to remove it (levels are edited in the other tabs). "
                                        "Double-click Parameters to edit the shape itself."), 0, wx.ALL, 6)
        self.grid = make_grid(self, ["Geometry", "Type", "Parameters", "Surface refinement", "Volume refinement"],
                              [150, 150, 340, 140, 190])
        v.Add(self.grid, 1, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(v)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_dclick)

    def reload(self):
        self.rows = fc.geometry(self.frame.case)
        g = self.grid
        clear_rows(g, len(self.rows))
        for r, x in enumerate(self.rows):
            surf = f"({x.surface.lmin} {x.surface.lmax})" if x.surface and x.surface.lmin is not None else (
                "yes" if x.surface else "(none)")
            vol = f"{x.volume.mode}: {levels_text(x.volume)}" if x.volume else "(none)"
            for c, val in enumerate((x.name, x.type, x.params, surf, vol)):
                g.SetCellValue(r, c, val)
        g.EndBatch()

    def on_dclick(self, ev):
        r, c = ev.GetRow(), ev.GetCol()
        if r >= len(self.rows) or c not in (2, 3, 4):
            return
        x, case = self.rows[r], self.frame.case
        try:
            if c == 2:
                txt = prompt_value(self, f"{x.name} ({x.type}): 'parameter value' pairs separated by ';'\n"
                                         "(type and name cannot be changed here)", x.params, None, "Edit geometry")
                if txt is None or txt == x.params:
                    return
                fc.set_geometry_params(case, x.key, txt)
            elif c == 3 and x.surface is None:
                txt = prompt_value(self, f"Surface refinement level for {x.name}", "2", LEVELS, "Add surface refinement")
                if txt is None:
                    return
                fc.set_surface_levels(case, x.name, int(txt), int(txt))
            elif c == 4 and x.volume is None:
                txt = prompt_value(self, f"Volume refinement level for {x.name} (mode inside)", "2", LEVELS,
                                   "Add volume refinement")
                if txt is None:
                    return
                fc.add_volume(case, x.name, int(txt))
            else:
                what = "surface" if c == 3 else "volume"
                if wx.MessageBox(f"Remove the {what} refinement of {x.name}?\n(Its levels are edited in the "
                                 f"'{'Surfaces & regions' if c == 3 else 'Volume refinement'}' tab.)", "Remove",
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                (fc.remove_surface_refinement if c == 3 else fc.remove_volume)(case, x.name)
        except (ValueError, FoamParseError) as e:
            say(self, f"Could not apply: {e}", icon=wx.ICON_ERROR)
        self.refresh()
        self.frame.touched()


class MeshPage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame, self.bg = frame, None
        v = wx.BoxSizer(wx.VERTICAL)

        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Background mesh (blockMeshDict)")
        sb = box.GetStaticBox()
        self.info = wx.StaticText(sb, label="")
        box.Add(self.info, 0, wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.n = [wx.TextCtrl(sb, size=(60, -1)) for _ in range(3)]
        row.Add(wx.StaticText(sb, label="Cells x y z"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        for t in self.n:
            row.Add(t, 0, wx.RIGHT, 4)
        b_apply = wx.Button(sb, label="Apply cell counts")
        row.Add(b_apply, 0, wx.LEFT | wx.RIGHT, 8)
        row.Add(wx.StaticText(sb, label="or target cell size (m)"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.size = wx.TextCtrl(sb, size=(80, -1))
        row.Add(self.size, 0, wx.RIGHT, 4)
        b_size = wx.Button(sb, label="Set counts from size")
        row.Add(b_size, 0)
        box.Add(row, 0, wx.ALL, 6)
        v.Add(box, 0, wx.EXPAND | wx.ALL, 6)

        # A notebook created without a size is first allocated ~1 px wide, which makes GTK print
        # 'size >= 0' / 'negative content width' warnings for its tab-scroll arrows.
        self.sub = wx.Notebook(self, size=(860, 300))
        self.sub.SetMinSize((500, 220))
        self.surfaces = SurfacePanel(self.sub, frame, self.reload_tables)
        self.volumes = VolumePanel(self.sub, frame, self.reload_tables)
        self.geometry = GeometryPanel(self.sub, frame, self.reload_tables)
        self.entries = EntryGrid(self.sub, frame, self.entry_rows, [fc.SNAPPY, fc.BLOCKMESH],
                                 "snappyHexMesh settings (clear a value to remove the entry)")
        for title, page in (("Surfaces & regions", self.surfaces), ("Volume refinement", self.volumes),
                            ("Geometry", self.geometry), ("Other settings", self.entries)):
            self.sub.AddPage(page, title)
        v.Add(self.sub, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(v)

        b_apply.Bind(wx.EVT_BUTTON, self.on_apply)
        b_size.Bind(wx.EVT_BUTTON, self.on_size)

    def entry_rows(self):
        return [(a, b, c) for a, b, c in fc.MESH_SPECS]

    def reload_tables(self):
        for p in (self.surfaces, self.volumes, self.geometry):
            p.reload()

    def reload(self):
        case = self.frame.case
        self.bg = fc.get_background(case)
        if self.bg is None:
            self.info.SetLabel("blockMeshDict not found or not understood (edit it by hand).")
            for t in self.n:
                t.SetValue("")
        else:
            b = self.bg
            size = ", ".join(f"{e / n:.4g}" for e, n in zip(b.ext, b.counts))
            self.info.SetLabel(f"Domain {b.ext[0]:.4g} x {b.ext[1]:.4g} x {b.ext[2]:.4g} m;  "
                               f"{b.counts[0] * b.counts[1] * b.counts[2]:,} cells;  cell size {size} m"
                               + ("" if b.editable else "   (several blocks: not editable here)"))
            for t, n in zip(self.n, b.counts):
                t.SetValue(str(n))
        self.reload_tables()
        self.entries.reload()

    def on_apply(self, _):
        if not self.bg or not self.bg.editable:
            return
        try:
            counts = tuple(int(t.GetValue()) for t in self.n)
            if min(counts) < 1:
                raise ValueError
        except ValueError:
            say(self, "Cell counts must be positive whole numbers.", icon=wx.ICON_ERROR)
            return
        fc.set_background_counts(self.frame.case, counts)
        self.frame.touched()
        self.reload()

    def on_size(self, _):
        if not self.bg:
            return
        try:
            size = float(self.size.GetValue())
            if size <= 0:
                raise ValueError
        except ValueError:
            say(self, "Enter a positive cell size in metres.", icon=wx.ICON_ERROR)
            return
        for t, n in zip(self.n, fc.counts_for_size(self.bg.ext, size)):
            t.SetValue(str(n))
        self.on_apply(None)


# --------------------------------------------------------------------------- #
#  Boundary conditions
# --------------------------------------------------------------------------- #
class BcPage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame, self.rows = frame, []
        v = wx.BoxSizer(wx.VERTICAL)
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(self, label="Field"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.field = wx.Choice(self)
        top.Add(self.field, 0, wx.RIGHT, 10)
        b_type = wx.Button(self, label="Edit BC type...")
        top.Add(b_type, 0, wx.RIGHT, 20)
        top.Add(wx.StaticText(self, label="Role preset for the selected patch (all fields)"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.role = wx.Choice(self, choices=ROLES + ["farfield"])
        self.role.SetSelection(0)
        top.Add(self.role, 0, wx.RIGHT, 6)
        b = wx.Button(self, label="Apply preset")
        top.Add(b, 0)
        v.Add(top, 0, wx.ALL, 8)
        self.grid = make_grid(self, ["Patch", "Patch type", "BC type", "Parameters", "Note"],
                              [150, 90, 190, 330, 190])
        v.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        v.Add(wx.StaticText(self, label="Double-click a BC type or its parameters to edit (the type list "
                                        "also accepts any other name). Parameters are 'key value' pairs "
                                        "separated by ';'. Wall functions need patch type 'wall'."),
              0, wx.ALL, 8)
        self.SetSizer(v)
        self.field.Bind(wx.EVT_CHOICE, lambda e: self.reload_grid())
        b.Bind(wx.EVT_BUTTON, self.on_preset)
        b_type.Bind(wx.EVT_BUTTON, lambda e: self.edit_type(selected_row(self.grid)))
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self.on_dclick)

    def reload(self):
        fields = fc.zero_fields(self.frame.case)
        keep = self.field.GetStringSelection()
        self.field.Set(fields)
        if keep in fields:
            self.field.SetStringSelection(keep)
        elif fields:
            self.field.SetSelection(0)
        self.reload_grid()

    def fill_row(self, r):
        g, row = self.grid, self.rows[r]
        for c, val in enumerate((row.patch, row.patch_type, row.bc_type, row.params, row.note)):
            g.SetCellValue(r, c, val)
        g.SetCellBackgroundColour(r, 2, GREY if not row.bc_type else g.GetDefaultCellBackgroundColour())

    def reload_grid(self):
        field = self.field.GetStringSelection()
        self.rows = fc.bc_rows(self.frame.case, field) if field else []
        clear_rows(self.grid, len(self.rows))
        for r in range(len(self.rows)):
            self.fill_row(r)
        self.grid.EndBatch()

    def on_dclick(self, ev):
        r, c = ev.GetRow(), ev.GetCol()
        if c == 2:
            self.edit_type(r)
        elif c == 3:
            self.edit_params(r)

    def edit_type(self, r):
        field = self.field.GetStringSelection()
        if r < 0 or r >= len(self.rows) or not self.rows[r].editable:
            say(self, "Select an editable patch row first.", icon=wx.ICON_WARNING)
            return
        row = self.rows[r]
        txt = prompt_value(self, f"BC type for patch {row.patch} ({field})", row.bc_type,
                           fc.bc_types(field), "Edit BC type")
        if txt:
            self.apply_edit(r, 2, txt)

    def edit_params(self, r):
        if r < 0 or r >= len(self.rows):
            return
        row = self.rows[r]
        if not row.editable or not row.bc_type:
            say(self, "Set a BC type for this patch first." if row.editable else
                "This patch is covered by a pattern entry; use the role preset to give it its own.",
                icon=wx.ICON_WARNING)
            return
        if row.complex:
            say(self, "This BC contains sub-dictionaries; edit those in the file by hand.",
                icon=wx.ICON_WARNING)
            return
        txt = prompt_value(self, f"Parameters for patch {row.patch} ({self.field.GetStringSelection()}):\n"
                                 "'key value' pairs separated by ';'", row.params, None, "Edit parameters")
        if txt is not None:
            self.apply_edit(r, 3, txt)

    def apply_edit(self, r, c, txt):
        """Write one edit (c=2: BC type, c=3: parameters) and refresh that row in place."""
        field, row, case = self.field.GetStringSelection(), self.rows[r], self.frame.case
        try:
            if c == 2 and txt:
                fc.set_bc_type(case, field, row.patch, txt)
            elif c == 3:
                fc.set_bc_params(case, field, row.patch, txt)
        except (ValueError, FoamParseError) as e:
            say(self, str(e), "Cannot edit", wx.ICON_ERROR)
        self.rows = fc.bc_rows(case, field)
        self.fill_row(r)
        self.grid.ForceRefresh()
        self.frame.touched()

    def on_preset(self, _):
        r = selected_row(self.grid)
        if r < 0 or r >= len(self.rows):
            say(self, "Select a patch row first.", icon=wx.ICON_WARNING)
            return
        patch, role = self.rows[r].patch, self.role.GetStringSelection()
        notes = fc.apply_role(self.frame.case, patch, role)
        self.frame.touched()
        self.reload_grid()
        if notes:
            say(self, "\n\n".join(notes), "Check this", wx.ICON_WARNING)


# --------------------------------------------------------------------------- #
#  Turbulence
# --------------------------------------------------------------------------- #
class TurbulencePage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        v = wx.BoxSizer(wx.VERTICAL)

        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Model")
        sb = box.GetStaticBox()
        self.current = wx.StaticText(sb, label="")
        box.Add(self.current, 0, wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.model = wx.Choice(sb, choices=list(fc.TURB_MODELS))
        self.model.SetSelection(0)
        row.Add(self.model, 0, wx.RIGHT, 8)
        b = wx.Button(sb, label="Switch model")
        row.Add(b, 0, wx.RIGHT, 8)
        row.Add(wx.StaticText(sb, label="Creates any missing 0/ fields, schemes and solver entries."),
                0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.ALL, 6)
        v.Add(box, 0, wx.EXPAND | wx.ALL, 6)

        box2 = wx.StaticBoxSizer(wx.VERTICAL, self, "Initial / inlet turbulence values")
        sb2 = box2.GetStaticBox()
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.ti, self.ls, self.uref = (wx.TextCtrl(sb2, size=(80, -1)) for _ in range(3))
        for label, ctrl in (("Intensity", self.ti), ("Length scale (m)", self.ls), ("Reference speed (m/s)", self.uref)):
            row2.Add(wx.StaticText(sb2, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
            row2.Add(ctrl, 0, wx.RIGHT, 14)
        b2 = wx.Button(sb2, label="Apply to k / epsilon / omega")
        row2.Add(b2, 0)
        box2.Add(row2, 0, wx.ALL, 6)
        v.Add(box2, 0, wx.EXPAND | wx.ALL, 6)

        self.entries = EntryGrid(self, frame, self.entry_rows, ["constant/momentumTransport"],
                                 "Turbulence file (clear a value to remove the entry)")
        v.Add(self.entries, 1, wx.EXPAND)
        self.SetSizer(v)
        b.Bind(wx.EVT_BUTTON, self.on_switch)
        b2.Bind(wx.EVT_BUTTON, self.on_apply)

    def entry_rows(self):
        rel = fc.turbulence_file(self.frame.case)
        return [("/".join(p), rel, p) for p in fc.flatten(self.frame.case, rel)] if rel else []

    def reload(self):
        case = self.frame.case
        m = fc.current_model(case)
        self.current.SetLabel(f"Current model: {m or 'none found (laminar?)'}   "
                              f"({fc.turbulence_file(case) or 'no momentumTransport file'})")
        if m in fc.TURB_MODELS:
            self.model.SetStringSelection(m)
        s = fc.flow_settings(case)
        self.ti.SetValue(f"{s.intensity:.4g}")
        self.ls.SetValue(f"{s.length_scale:.4g}")
        self.uref.SetValue(f"{(s.U[0] ** 2 + s.U[1] ** 2 + s.U[2] ** 2) ** 0.5:.4g}")
        self.entries.reload()

    def on_switch(self, _):
        model = self.model.GetStringSelection()
        try:
            notes = fc.switch_model(self.frame.case, model)
        except (ValueError, FoamParseError) as e:
            say(self, str(e), "Cannot switch", wx.ICON_ERROR)
            return
        self.frame.touched()
        self.frame.reload_all()
        say(self, "Changes (not saved yet):\n\n" + "\n".join("  " + n for n in notes), "Model switched")

    def on_apply(self, _):
        try:
            ti, ls, uref = float(self.ti.GetValue()), float(self.ls.GetValue()), float(self.uref.GetValue())
            if ti <= 0 or ls <= 0 or uref <= 0:
                raise ValueError
        except ValueError:
            say(self, "Intensity, length scale and speed must be positive numbers.", icon=wx.ICON_ERROR)
            return
        touched = fc.apply_turbulence_values(self.frame.case, ti, ls, uref)
        self.frame.touched()
        self.frame.reload_all()
        say(self, "Updated: " + (", ".join("0/" + f for f in touched) or "nothing (no k/epsilon/omega files)"))


# --------------------------------------------------------------------------- #
#  Decompose
# --------------------------------------------------------------------------- #
class DecomposePage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        v = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.create = wx.Button(self, label="Create decomposeParDict...")
        self.suggest = wx.Button(self, label="Suggest n from domain shape")
        row.Add(self.create, 0, wx.RIGHT, 8)
        row.Add(self.suggest, 0, wx.RIGHT, 16)
        self.status = wx.StaticText(self, label="")
        row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL)
        v.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        self.entries = EntryGrid(self, frame, self.entry_rows, [fc.DECOMPOSE],
                                 "Only the coefficients of the chosen method are used. "
                                 "simple/hierarchical: n must multiply to numberOfSubdomains.")
        v.Add(self.entries, 1, wx.EXPAND)
        self.SetSizer(v)
        self.create.Bind(wx.EVT_BUTTON, self.on_create)
        self.suggest.Bind(wx.EVT_BUTTON, self.on_suggest)

    def entry_rows(self):
        return list(fc.DECOMPOSE_SPECS) if self.frame.case.exists(fc.DECOMPOSE) else []

    def reload(self):
        have = self.frame.case.exists(fc.DECOMPOSE)
        self.create.Enable(not have)
        self.suggest.Enable(have)
        problems = fc.decompose_problems(self.frame.case)
        self.status.SetLabel("system/decomposeParDict not found" if not have else
                             ("Problem: " + " ".join(problems) if problems else "Settings are consistent"))
        self.entries.reload()

    def on_create(self, _):
        with wx.TextEntryDialog(self, "Number of subdomains (processors):", "Create decomposeParDict", "4") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            try:
                n = int(dlg.GetValue())
                if n < 2:
                    raise ValueError
            except ValueError:
                say(self, "Enter a whole number of at least 2.", icon=wx.ICON_ERROR)
                return
        fc.create_decompose(self.frame.case, n)
        self.frame.touched()
        self.frame.reload_all()

    def on_suggest(self, _):
        case = self.frame.case
        n = fc.floats(case.get(fc.DECOMPOSE, ["numberOfSubdomains"]))
        if not n:
            say(self, "numberOfSubdomains is not set.", icon=wx.ICON_WARNING)
            return
        bg = fc.get_background(case)
        nx, ny, nz = fc.suggest_split(int(n[0]), bg.ext if bg else (1.0, 1.0, 1.0))
        for method in ("simple", "hierarchical"):
            case.set(fc.DECOMPOSE, [f"{method}Coeffs", "n"], f"({nx} {ny} {nz})")
        self.frame.touched()
        self.reload()
        say(self, f"Set n = ({nx} {ny} {nz}) for the simple and hierarchical methods.")


# --------------------------------------------------------------------------- #
#  Frame
# --------------------------------------------------------------------------- #
class CaseEditorFrame(wx.Frame):
    def __init__(self, parent, case_dir):
        super().__init__(parent, title="Case editor", size=(1040, 760))
        self.case = fc.Case(case_dir)
        self._shown = None          # last (title, status, can_save) pushed to the window
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.b_save = wx.Button(panel, label="Save changes")
        b_reload = wx.Button(panel, label="Reload from disk")
        bar.Add(self.b_save, 0, wx.RIGHT, 6)
        bar.Add(b_reload, 0, wx.RIGHT, 14)
        bar.Add(wx.StaticText(panel, label=str(self.case.path)), 1, wx.ALIGN_CENTER_VERTICAL)
        root.Add(bar, 0, wx.EXPAND | wx.ALL, 8)

        nb = wx.Notebook(panel, size=(960, 540))
        nb.SetMinSize((700, 360))
        self.nb = nb
        self.pages = []
        for title, page in (
                ("Mesh", MeshPage(nb, self)),
                ("Material", EntryGrid(nb, self, self.material_rows,
                                       ["constant/transportProperties", "constant/physicalProperties"],
                                       "Material / transport properties (clear a value to remove the entry)")),
                ("Turbulence", TurbulencePage(nb, self)),
                ("Solver", EntryGrid(nb, self, self.solver_rows, ["system/controlDict", "system/fvSolution",
                                                                  "system/fvSchemes"],
                                     "Application, fvSolution (solvers, SIMPLE/PIMPLE, relaxation) and fvSchemes")),
                ("Boundary conditions", BcPage(nb, self)),
                ("Control", EntryGrid(nb, self, self.control_rows, ["system/controlDict"],
                                      "controlDict: time control, write control and other settings")),
                ("Decompose", DecomposePage(nb, self))):
            nb.AddPage(page, title)
            self.pages.append(page)
        root.Add(nb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(root)
        self.CreateStatusBar()
        self.SetMinSize((820, 600))

        self.b_save.Bind(wx.EVT_BUTTON, self.on_save)
        b_reload.Bind(wx.EVT_BUTTON, self.on_reload)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Layout()
        self.Centre()
        # fill the pages once the window has its real size (not while it is still being built)
        wx.CallAfter(lambda: self.reload_all() if self else None)

    # -- row providers ----------------------------------------------------------
    def material_rows(self):
        rel = fc.material_file(self.case)
        return [("/".join(p), rel, p) for p in fc.flatten(self.case, rel)] if rel else []

    def control_rows(self):
        return [("/".join(p), fc.CONTROL, p) for p in fc.flatten(self.case, fc.CONTROL)]

    def solver_rows(self):
        rows = [("controlDict: application", fc.CONTROL, ["application"])]
        if self.case.has(fc.CONTROL, ["solver"]):
            rows.append(("controlDict: solver", fc.CONTROL, ["solver"]))
        for rel in ("system/fvSolution", "system/fvSchemes"):
            rows += [(f"{rel.split('/')[-1]}: {'/'.join(p)}", rel, p) for p in fc.flatten(self.case, rel)]
        return rows

    # -- state ----------------------------------------------------------------------
    def reload_all(self):
        for p in self.pages:
            p.reload()
        self.touched()

    def touched(self):
        ch = self.case.changed()
        title = f"{'* ' if ch else ''}Case editor - {self.case.path.name}"
        status = (f"{len(ch)} file(s) modified, not saved: " + ", ".join(ch[:5])) if ch else "No unsaved changes"
        state = (title, status, bool(ch))
        if state == self._shown:        # nothing visible changed: avoid needless relayouts per edit
            return
        self._shown = state
        self.SetTitle(title)
        self.SetStatusText(status)
        self.b_save.Enable(bool(ch))

    def on_save(self, _):
        ch = self.case.changed()
        if not ch:
            return
        ext = self.case.externally_modified()
        if ext and wx.MessageBox("These files were changed on disk since you opened the case:\n\n"
                                 + "\n".join(ext) + "\n\nSaving will overwrite those changes "
                                 "(the on-disk versions are backed up). Continue?", "Files changed on disk",
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        try:
            files, backup = self.case.save()
        except OSError as e:
            say(self, str(e), "Write error", wx.ICON_ERROR)
            return
        self.reload_all()
        self.SetStatusText(f"Saved {len(files)} file(s)")
        say(self, f"Saved {len(files)} file(s).\n\n"
                  + (f"Previous versions backed up in:\n{backup}\n\n" if backup else "")
                  + "If you changed the mesh settings or the geometry-related files, re-run blockMesh / "
                    "snappyHexMesh; existing meshes and results are not modified.", "Saved")

    def on_reload(self, _):
        if self.case.changed() and wx.MessageBox("Discard unsaved changes and reload from disk?", "Reload",
                                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            return
        self.case.reload()
        self.reload_all()

    def on_close(self, ev):
        if self.case.changed() and ev.CanVeto():
            with wx.MessageDialog(self, "You have unsaved changes.", "Close case editor",
                                  wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION) as dlg:
                dlg.SetYesNoCancelLabels("Save", "Discard", "Keep editing")
                answer = dlg.ShowModal()
            if answer == wx.ID_CANCEL:
                ev.Veto()
                return
            if answer == wx.ID_YES:
                try:
                    self.case.save()
                except OSError as e:
                    say(self, str(e), "Write error", wx.ICON_ERROR)
                    ev.Veto()
                    return
        self.Destroy()
