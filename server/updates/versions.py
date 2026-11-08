# SPDX-License-Identifier: GPL-3.0-or-later
"""Version order, as the app's and the PC library's: "1.2.3", "1.2", "1.2.3-beta.1" (before its release)."""


def _key(text):
    text = text.strip().removeprefix("v")
    main, _sep, pre = text.partition("-")
    try:
        numbers = [int(x) for x in main.split(".")]
    except ValueError:
        return [-1, -1, -1], None
    return (numbers + [0, 0, 0])[:3], pre or None


def _compare_pre(a, b):
    for p, q in zip(a.split("."), b.split(".")):
        c = (int(p) > int(q)) - (int(p) < int(q)) if p.isdigit() and q.isdigit() else (p > q) - (p < q)
        if c:
            return c
    return (len(a.split(".")) > len(b.split("."))) - (len(a.split(".")) < len(b.split(".")))


def compare(a, b):
    (xn, xp), (yn, yp) = _key(a), _key(b)
    if xn != yn:
        return (xn > yn) - (xn < yn)
    if xp == yp:
        return 0
    if xp is None:
        return 1
    if yp is None:
        return -1
    return _compare_pre(xp, yp)
