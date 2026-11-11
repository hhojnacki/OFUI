"""
foamdict - tolerant, edit-in-place reader/writer for OpenFOAM dictionary files.

Unlike a full parse-and-reserialise, every edit replaces only the text span it
concerns, so comments, layout, #include lines and entries this module does not
understand are left exactly as they were.

    d = FoamDict(Path("system/controlDict").read_text())
    d.get(["endTime"])                       # '1000'
    d.set(["endTime"], "2000")               # replaces just the value
    d.set(["functions", "probes", "type"], "probes")   # creates missing dictionaries
    d.delete(["writeInterval"])
    Path("system/controlDict").write_text(d.text)

Paths are lists of keys. Quoted keys such as "(U|k|epsilon)" are addressed
without the quotes.
"""
import re
from typing import NamedTuple


class FoamParseError(ValueError):
    pass


class Node(NamedTuple):
    key: str
    kind: str    # "entry" | "dict" | "directive"
    ks: int      # start of the key
    end: int     # end of the whole statement (after ';' or '}')
    vs: int      # entry: start of value text;  dict: start of body (after '{')
    ve: int      # entry: end of value text;    dict: end of body (index of '}')


# --------------------------------------------------------------------------- #
#  scanning
# --------------------------------------------------------------------------- #
def _skip_ws(t, i, b):
    while i < b:
        c = t[i]
        if c.isspace():
            i += 1
        elif t.startswith("//", i):
            j = t.find("\n", i, b)
            i = b if j < 0 else j + 1
        elif t.startswith("/*", i):
            j = t.find("*/", i, b)
            i = b if j < 0 else j + 2
        else:
            break
    return i


def _end_string(t, i):
    """t[i] is an opening quote; return the index just after the closing one."""
    j = i + 1
    while j < len(t):
        if t[j] == "\\":
            j += 2
            continue
        if t[j] == '"':
            return j + 1
        j += 1
    return len(t)


def _match_brace(t, i):
    """t[i] == '{'; return the index of the matching '}' (len(t) if unbalanced)."""
    depth, j, n = 0, i, len(t)
    while j < n:
        c = t[j]
        if c == '"':
            j = _end_string(t, j)
            continue
        if t.startswith("//", j):
            k = t.find("\n", j)
            j = n if k < 0 else k
            continue
        if t.startswith("/*", j):
            k = t.find("*/", j)
            j = n if k < 0 else k + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


def scan(t, a, b):
    """Top-level statements of t[a:b] as a list of Node."""
    nodes, i = [], _skip_ws(t, a, b)
    while i < b:
        c = t[i]
        if c == "#":
            j = t.find("\n", i, b)
            j = b if j < 0 else j
            nodes.append(Node(t[i:j].strip(), "directive", i, j, j, j))
            i = _skip_ws(t, j, b)
            continue
        if c in ";}{)(":
            i = _skip_ws(t, i + 1, b)
            continue
        if c == '"':
            j = _end_string(t, i)
            key = t[i + 1:j - 1]
        else:
            j = i
            # a comment may start right after a word (`name// comment`), so stop there too
            while j < b and not t[j].isspace() and t[j] not in ";{}" and not t.startswith(("//", "/*"), j):
                j += 1
            if j == i:                       # cannot happen after _skip_ws; never loop forever
                i += 1
                continue
            key = t[i:j]
        ks = i
        i = _skip_ws(t, j, b)
        if i < b and t[i] == "{":
            e = min(_match_brace(t, i), b)
            nodes.append(Node(key, "dict", ks, e + 1, i + 1, e))
            i = _skip_ws(t, e + 1, b)
            if i < b and t[i] == ";":
                i = _skip_ws(t, i + 1, b)
            continue
        if i < b and t[i] == ";":
            nodes.append(Node(key, "entry", ks, i + 1, i, i))
            i = _skip_ws(t, i + 1, b)
            continue
        vs, depth, j = i, 0, i
        while j < b:
            ch = t[j]
            if ch == '"':
                j = _end_string(t, j)
                continue
            if t.startswith("//", j):
                k = t.find("\n", j, b)
                j = b if k < 0 else k
                continue
            if t.startswith("/*", j):
                k = t.find("*/", j, b)
                j = b if k < 0 else k + 2
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth < 0:
                    break
            elif ch == ";" and depth == 0:
                break
            j += 1
        ve = j
        while ve > vs and t[ve - 1].isspace():
            ve -= 1
        end = j + 1 if j < b and t[j] == ";" else j
        nodes.append(Node(key, "entry", ks, end, vs, ve))
        i = _skip_ws(t, end, b)
    return nodes


def _quote(key):
    return f'"{key}"' if re.search(r'[|*\[\]^$\\"]', key) else key


def _indent_of(t, pos):
    ls = t.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", t[ls:])
    return m.group(0)


# --------------------------------------------------------------------------- #
#  FoamDict
# --------------------------------------------------------------------------- #
class FoamDict:
    def __init__(self, text=""):
        self.text = text

    # -- reading -----------------------------------------------------------
    def _locate(self, path):
        a, b, node = 0, len(self.text), None
        path = list(path)
        for idx, k in enumerate(path):
            node = next((n for n in scan(self.text, a, b)
                         if n.kind != "directive" and n.key == k), None)
            if node is None:
                return None
            if idx < len(path) - 1:
                if node.kind != "dict":
                    return None
                a, b = node.vs, node.ve
        return node

    def _span(self, path):
        if not list(path):
            return 0, len(self.text)
        n = self._locate(path)
        return (n.vs, n.ve) if n is not None and n.kind == "dict" else None

    def kind(self, path):
        n = self._locate(path)
        return n.kind if n else None

    def has(self, path):
        return self._locate(path) is not None

    def get(self, path):
        """Value text of an entry (None if missing or if path is a dictionary)."""
        n = self._locate(path)
        return self.text[n.vs:n.ve] if n is not None and n.kind == "entry" else None

    def body(self, path):
        """Inner text of a dictionary (None if missing or not a dictionary)."""
        sp = self._span(path)
        return self.text[sp[0]:sp[1]] if sp else None

    def nodes(self, path=()):
        """Child statements of a dictionary (empty list if path is not one)."""
        sp = self._span(path)
        return [n for n in scan(self.text, *sp) if n.kind != "directive"] if sp else []

    def keys(self, path=()):
        return [(n.key, n.kind) for n in self.nodes(path)]

    # -- writing -------------------------------------------------------------
    def set(self, path, value):
        """Set an entry's value, creating the entry (and parent dictionaries) if needed."""
        path = list(path)
        n = self._locate(path)
        if n is not None:
            if n.kind != "entry":
                raise FoamParseError(f"{'/'.join(path)} is a dictionary, not an entry")
            pad = " " if n.vs == n.ve and value and not self.text[n.vs - 1].isspace() else ""
            self.text = self.text[:n.vs] + pad + value + self.text[n.ve:]
            return
        self._insert(path[:-1], lambda ind: self._entry_text(path[-1], value, ind))

    def set_body(self, path, body):
        """Replace (or create) a dictionary's whole body. `body` is the inner text."""
        path = list(path)
        n = self._locate(path)
        if n is not None:
            if n.kind != "dict":
                raise FoamParseError(f"{'/'.join(path)} is an entry, not a dictionary")
            self.text = self.text[:n.vs] + body + self.text[n.ve:]
            return
        self._insert(path[:-1], lambda ind: f"{ind}{_quote(path[-1])}\n{ind}{{{body}{ind}}}\n")

    def delete(self, path):
        n = self._locate(path)
        if n is None:
            return False
        t = self.text
        ls = t.rfind("\n", 0, n.ks) + 1
        le = t.find("\n", n.end)
        le = len(t) if le < 0 else le + 1
        if not t[ls:n.ks].strip() and not t[n.end:le].strip():
            self.text = t[:ls] + t[le:]
        else:
            self.text = t[:n.ks] + t[n.end:]
        return True

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _entry_text(key, value, ind):
        return f"{ind}{_quote(key)} {value};\n" if value else f"{ind}{_quote(key)};\n"

    def _insert(self, parent, make):
        """Insert make(indent) as a new child of `parent`, creating missing ancestors."""
        parent = list(parent)
        # deepest existing ancestor dictionary
        k = len(parent)
        while k > 0:
            n = self._locate(parent[:k])
            if n is not None:
                if n.kind != "dict":
                    raise FoamParseError(f"{'/'.join(parent[:k])} is an entry, not a dictionary")
                break
            k -= 1
        missing = parent[k:]

        def wrap(ind):
            if not missing:
                return make(ind)
            out = ""
            for depth, name in enumerate(missing):
                out += f"{ind}{'    ' * depth}{_quote(name)}\n{ind}{'    ' * depth}{{\n"
            out += make(ind + "    " * len(missing))
            for depth in reversed(range(len(missing))):
                out += f"{ind}{'    ' * depth}}}\n"
            return out

        t = self.text
        if k == 0:                                   # file level
            kids = scan(t, 0, len(t))
            kids = [x for x in kids if x.kind != "directive"]
            ind = _indent_of(t, kids[-1].ks) if kids else ""
            if t and not t.endswith("\n"):
                t += "\n"
            self.text = t + wrap(ind)
            return
        anc = self._locate(parent[:k])
        kids = scan(t, anc.vs, anc.ve)
        pind = _indent_of(t, anc.ks)
        ind = _indent_of(t, kids[0].ks) if kids and "\n" in t[anc.vs:kids[0].ks] else pind + "    "
        block = wrap(ind)
        close = anc.ve                               # index of '}'
        ls = t.rfind("\n", 0, close) + 1
        if not t[ls:close].strip():                  # '}' is alone on its line
            self.text = t[:ls] + block + t[ls:]
        else:                                        # inline dictionary: break it open
            self.text = t[:close] + "\n" + block + pind + t[close:]
