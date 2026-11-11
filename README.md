# OFUI
A basic graphical interface for creating and editing OpenFOAM case files.  This is a vibe-coding project
created using the Claude AI.

To run the gui: python3 stl2foam_wx.py

stl2foam_wx - wxPython front end.

  * Create a case from STL files: add STL files (button or drag and drop), set each patch's role
    and refinement levels, choose the case settings, then Generate (stl2foam_core).
  * Edit an existing case: "Open existing case..." opens the case editor (case_editor_wx), which
    works on the case files themselves.

Requires: pip install wxPython
Files: stl2foam_wx.py (this), stl2foam_core.py, case_editor_wx.py, foamcase.py, foamdict.py

case_editor_wx - wxPython editor for an existing OpenFOAM case.

Works directly on the case files (see foamcase.py): edits change only the entries you touch,
nothing is written until Save, and Save backs up every file it replaces into
<case>/.stl2foam_backup/<timestamp>/.

The tables are read-only; values are edited through small dialogs (double-click a value, or use
the Edit buttons). wxGrid cell-editor widgets are deliberately avoided: once used they stay behind
as hidden child widgets, and GTK3 prints Gtk-CRITICAL warnings when the grid scrolls or resizes.

Tabs: Mesh, Material, Turbulence, Solver, Boundary conditions, Control, Decompose.
