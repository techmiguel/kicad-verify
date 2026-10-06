"""Parser mínimo de s-expressions de KiCad (.kicad_pcb / .kicad_sch)."""


def parse(text):
    """Devuelve la lista anidada raíz. Átomos y cadenas se devuelven como str."""
    stack = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            new = []
            stack[-1].append(new)
            stack.append(new)
            i += 1
        elif c == ")":
            if len(stack) > 1:
                stack.pop()
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            stack[-1].append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    return stack[0][0] if stack[0] else []


def children(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def prop(node, key):
    for c in children(node, "property"):
        if len(c) > 2 and c[1] == key:
            return c[2]
    return None


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default
