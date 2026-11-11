#!/usr/bin/env python3
"""
stl2foam_wx - wxPython front end.

  * Create a case from STL files: add STL files (button or drag and drop), set each patch's role
    and refinement levels, choose the case settings, then Generate (stl2foam_core).
  * Edit an existing case: "Open existing case..." opens the case editor (case_editor_wx), which
    works on the case files themselves.

Requires: pip install wxPython
Files: stl2foam_wx.py (this), stl2foam_core.py, case_editor_wx.py, foamcase.py, foamdict.py
"""
from pathlib import Path

from stl2foam_core import (ROLES, Settings, existing_case_files, grow, load_stl, rename_patch,
                           sanitize, vec, write_case)


def main():
    import wx
    import wx.grid as gridlib

    COLS = ["Patch name", "Source file", "Triangles", "Role", "Ref min", "Ref max"]
    C_NAME, C_FILE, C_TRIS, C_ROLE, C_LMIN, C_LMAX = range(6)

    class StlDrop(wx.FileDropTarget):
        def __init__(self, callback):
            super().__init__()
            self.callback = callback

        def OnDropFiles(self, x, y, filenames):
            self.callback([f for f in filenames if f.lower().endswith(".stl")])
            return True

    class MainFrame(wx.Frame):
        def __init__(self):
            super().__init__(None, title="STL -> OpenFOAM case", size=(900, 680))
            self.patches, self.bbox = [], None
            self.ctrl = {}

            panel = wx.Panel(self)
            root = wx.BoxSizer(wx.VERTICAL)

            # toolbar row
            bar = wx.BoxSizer(wx.HORIZONTAL)
            b_open = wx.Button(panel, label="Open existing case...")
            b_add = wx.Button(panel, label="Add STL files...")
            b_del = wx.Button(panel, label="Remove selected")
            self.info = wx.StaticText(panel, label="No geometry loaded (drag STL files here)")
            bar.Add(b_open, 0, wx.RIGHT, 14)
            bar.Add(b_add, 0, wx.RIGHT, 6)
            bar.Add(b_del, 0, wx.RIGHT, 12)
            bar.Add(self.info, 1, wx.ALIGN_CENTER_VERTICAL)
            root.Add(bar, 0, wx.EXPAND | wx.ALL, 8)

            # patch table
            self.grid = gridlib.Grid(panel)
            self.grid.CreateGrid(0, len(COLS))
            for i, c in enumerate(COLS):
                self.grid.SetColLabelValue(i, c)
            for i, w in enumerate((200, 170, 80, 110, 70, 70)):
                self.grid.SetColSize(i, w)
            self.grid.SetRowLabelSize(30)
            self.grid.SetDefaultRowSize(28, True)
            # constant was renamed in wxPython 4.2 (GridSelectRows -> SelectRows)
            select_rows = getattr(gridlib.Grid, "SelectRows",
                                  getattr(gridlib.Grid, "GridSelectRows", 1))
            self.grid.SetSelectionMode(select_rows)
            self.grid.EnableDragGridSize(False)
            self.grid.SetMinSize((300, 120))
            root.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            root.Add(wx.StaticText(panel, label="Edit name, role and refinement levels directly in the table."),
                     0, wx.LEFT | wx.TOP, 8)

            # settings
            box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Case settings")
            fg = wx.FlexGridSizer(cols=2, vgap=4, hgap=10)
            fg.AddGrowableCol(1)

            def row(label, widgets):
                fg.Add(wx.StaticText(box.GetStaticBox(), label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                h = wx.BoxSizer(wx.HORIZONTAL)
                for w in widgets:
                    h.Add(w, 0, wx.RIGHT, 4)
                fg.Add(h, 1)

            def entry(key, value, width=80):
                t = wx.TextCtrl(box.GetStaticBox(), value=value, size=(width, -1))
                self.ctrl[key] = t
                return t

            self.ctrl["mode"] = wx.Choice(box.GetStaticBox(), choices=["external", "internal"])
            self.ctrl["mode"].SetSelection(0)
            row("Domain", [self.ctrl["mode"]])
            row("Inlet U (m/s)", [entry("ux", "10"), entry("uy", "0"), entry("uz", "0")])
            row("nu (m2/s)", [entry("nu", "1.5e-5")])
            row("Turbulence intensity / length scale (m)", [entry("ti", "0.05"), entry("ls", "0.01")])
            row("Background cell size (m)", [entry("cell", "0.1")])
            row("locationInMesh x y z (blank = auto)", [entry("lx", ""), entry("ly", ""), entry("lz", "")])
            row("End iteration", [entry("end", "1000")])
            box.Add(fg, 0, wx.EXPAND | wx.ALL, 8)
            root.Add(box, 0, wx.EXPAND | wx.ALL, 8)

            b_gen = wx.Button(panel, label="Generate case...")
            root.Add(b_gen, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
            panel.SetSizer(root)

            self.CreateStatusBar()
            self.SetStatusText("Ready")

            b_open.Bind(wx.EVT_BUTTON, self.on_open)
            b_add.Bind(wx.EVT_BUTTON, self.on_add)
            b_del.Bind(wx.EVT_BUTTON, self.on_remove)
            b_gen.Bind(wx.EVT_BUTTON, self.on_generate)
            self.grid.Bind(gridlib.EVT_GRID_CELL_CHANGED, self.on_cell_changed)
            self.SetDropTarget(StlDrop(self.load_files))
            self.Centre()

        # -- geometry ------------------------------------------------------
        def refresh(self):
            g = self.grid
            g.BeginBatch()
            if g.GetNumberRows():
                g.DeleteRows(0, g.GetNumberRows())
            g.AppendRows(len(self.patches))
            for r, p in enumerate(self.patches):
                vals = (p.name, p.source, str(p.ntri), p.role, str(p.lmin), str(p.lmax))
                for c, v in enumerate(vals):
                    g.SetCellValue(r, c, v)
                for c in (C_FILE, C_TRIS):
                    g.SetReadOnly(r, c, True)
                for c in (C_TRIS, C_LMIN, C_LMAX):
                    g.SetCellAlignment(r, c, wx.ALIGN_RIGHT, wx.ALIGN_CENTRE)
                g.SetCellEditor(r, C_ROLE, gridlib.GridCellChoiceEditor(ROLES, False))
                # dropdown instead of GridCellNumberEditor: avoids GTK spin-button warnings
                for c in (C_LMIN, C_LMAX):
                    g.SetCellEditor(r, c, gridlib.GridCellChoiceEditor([str(i) for i in range(9)], False))
            g.EndBatch()
            if self.bbox:
                lo, hi = self.bbox
                self.info.SetLabel("bbox  " + vec(lo) + " -> " + vec(hi))
            else:
                self.info.SetLabel("No geometry loaded (drag STL files here)")
            self.Layout()

        def load_files(self, paths):
            for path in paths:
                try:
                    ps, bb = load_stl(path, [p.name for p in self.patches])
                except Exception as e:
                    wx.MessageBox(str(e), "STL error", wx.OK | wx.ICON_ERROR)
                    continue
                self.patches += ps
                if self.bbox is None:
                    self.bbox = bb
                else:
                    self.bbox = grow([list(self.bbox[0]), list(self.bbox[1])], bb)
            self.refresh()
            self.SetStatusText(f"{len(self.patches)} patches loaded")

        def on_add(self, _):
            with wx.FileDialog(self, "Select STL files", wildcard="STL files (*.stl)|*.stl;*.STL|All files|*.*",
                               style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    self.load_files(dlg.GetPaths())

        def on_remove(self, _):
            rows = set(self.grid.GetSelectedRows())
            if not rows and self.grid.GetGridCursorRow() >= 0:
                rows = {self.grid.GetGridCursorRow()}
            for r in sorted(rows, reverse=True):
                if r < len(self.patches):
                    del self.patches[r]
            if not self.patches:
                self.bbox = None
            self.refresh()

        def on_cell_changed(self, ev):
            r, c = ev.GetRow(), ev.GetCol()
            p = self.patches[r]
            val = self.grid.GetCellValue(r, c).strip()
            if c == C_NAME:
                n = sanitize(val)
                if any(q is not p and q.name == n for q in self.patches):
                    wx.MessageBox("That patch name is already used.", "Name", wx.OK | wx.ICON_WARNING)
                else:
                    rename_patch(p, n)
            elif c == C_ROLE and val in ROLES:
                p.role = val
            elif c in (C_LMIN, C_LMAX):
                try:
                    setattr(p, "lmin" if c == C_LMIN else "lmax", max(0, min(8, int(val))))
                except ValueError:
                    pass
                if c == C_LMIN:
                    p.lmax = max(p.lmax, p.lmin)
                else:
                    p.lmin = min(p.lmin, p.lmax)
            self.refresh()
            self.grid.GoToCell(r, c)

        # -- settings ------------------------------------------------------
        def collect_settings(self):
            """Read the settings panel; returns Settings, or None after showing an error."""
            roles = [p.role for p in self.patches]
            mode = self.ctrl["mode"].GetStringSelection()
            if mode == "internal" and not ("inlet" in roles and "outlet" in roles):
                if wx.MessageBox("Internal flow without both an inlet and an outlet patch. Continue?",
                                 "Check boundaries", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                    return None
            try:
                val = lambda k: self.ctrl[k].GetValue().strip()
                g = lambda k: float(val(k))
                loc = None
                if all(val(k) for k in ("lx", "ly", "lz")):
                    loc = (g("lx"), g("ly"), g("lz"))
                s = Settings(mode=mode, nu=g("nu"), U=(g("ux"), g("uy"), g("uz")),
                             intensity=g("ti"), length_scale=g("ls"), cell_size=g("cell"),
                             loc=loc, end_time=int(g("end")))
                if s.cell_size <= 0 or s.length_scale <= 0:
                    raise ValueError
            except ValueError:
                wx.MessageBox("Check the numeric fields (cell size and length scale must be > 0).",
                              "Settings", wx.OK | wx.ICON_ERROR)
                return None
            return s

        # -- open existing case --------------------------------------------
        def on_open(self, _):
            with wx.DirDialog(self, "Open existing OpenFOAM case",
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                d = dlg.GetPath()
            self.open_editor(d)

        def open_editor(self, d):
            from case_editor_wx import CaseEditorFrame
            if not (Path(d) / "system").is_dir():
                wx.MessageBox(f"{d}\n\ndoes not look like an OpenFOAM case (no system/ directory).",
                              "Cannot open case", wx.OK | wx.ICON_ERROR)
                return
            CaseEditorFrame(self, d).Show()
            self.SetStatusText(f"Opened case editor for {d}")

        # -- generate --------------------------------------------------------
        def on_generate(self, _):
            if not self.patches:
                wx.MessageBox("Load at least one STL file first.", "Nothing to do", wx.OK | wx.ICON_INFORMATION)
                return
            s = self.collect_settings()
            if s is None:
                return
            with wx.DirDialog(self, "Case directory (created / overwritten)",
                              style=wx.DD_DEFAULT_STYLE) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                d = dlg.GetPath()
            clashes = existing_case_files(d, self.patches)
            if clashes:
                shown = "\n".join(clashes[:8])
                if len(clashes) > 8:
                    shown += f"\n... and {len(clashes) - 8} more"
                with wx.MessageDialog(self, f"A case already exists in:\n{d}\n\nThese files would be "
                                            f"overwritten:\n{shown}\n\nThey are backed up first. Mesh and "
                                            "result directories are not touched.", "Case exists",
                                      wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) as q:
                    q.SetYesNoLabels("Overwrite", "Cancel")
                    if q.ShowModal() != wx.ID_YES:
                        self.SetStatusText("Cancelled - existing case left untouched")
                        return
            try:
                res = write_case(d, self.patches, self.bbox, s, overwrite=True)
            except ValueError as e:
                wx.MessageBox(str(e), "Cannot generate", wx.OK | wx.ICON_ERROR)
                return
            except OSError as e:
                wx.MessageBox(str(e), "Write error", wx.OK | wx.ICON_ERROR)
                return
            self.SetStatusText(f"Case written to {d}")
            wx.MessageBox(f"{d}\n\nBackground mesh: {res.n[0]} x {res.n[1]} x {res.n[2]} cells\n"
                          f"locationInMesh: {vec(res.loc)}\n\n"
                          + (f"Previous versions saved in:\n{res.backup}\n\n" if res.backup else "")
                          + "Check that point lies in the fluid region, then run ./Allrun",
                          "Case written", wx.OK | wx.ICON_INFORMATION)
            if wx.MessageBox("Open this case in the case editor?", "Case editor",
                             wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
                self.open_editor(d)

    app = wx.App()
    MainFrame().Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
